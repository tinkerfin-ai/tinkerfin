import assert from 'node:assert/strict'
import http from 'node:http'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { chromium } from '@playwright/test'
import { createServer } from 'vite'

const webRoot = fileURLToPath(new URL('../../', import.meta.url))

test('真实 HTTP/1.1 附件上传保留文件、鉴权、进度、错误和取消语义', { timeout: 30_000 }, async () => {
  const cache = await mkdtemp(path.join(tmpdir(), 'studio-upload-'))
  const received = []
  const held = new Map()
  const upstream = http.createServer(async (request, response) => {
    try {
      const url = new URL(request.url, 'http://localhost')
      const chunks = []
      for await (const chunk of request) chunks.push(chunk)
      const name = url.searchParams.get('name')
      const bytes = Buffer.concat(chunks)
      received.push({ name, bytes, protocol: request.httpVersion, authorization: request.headers.authorization, contentType: request.headers['content-type'] })
      if (name === 'cancel.pdf' || name === 'progress.pdf') {
        held.set(name, response)
        return
      }
      if (name === 'disconnect.pdf') {
        response.destroy()
        return
      }
      response.writeHead(name === 'invalid.pdf' ? 422 : 200, { 'content-type': 'application/json' })
      response.end(JSON.stringify(name === 'invalid.pdf'
        ? { code: 422, message: '文件内容或名称不符合要求，请检查后重新上传', data: null }
        : { code: 0, message: 'success', data: { id: 'uploaded', name, mime_type: 'application/pdf', size_bytes: bytes.length } }))
    } catch {
      response.destroy()
    }
  })
  let vite
  let browser
  try {
    await new Promise(resolve => upstream.listen(0, '127.0.0.1', resolve))
    vite = await createServer({
      root: webRoot,
      configFile: path.join(webRoot, 'vite.config.ts'),
      cacheDir: path.join(cache, 'vite'),
      logLevel: 'silent',
      // 测试请求始终经过临时代理，不使用开发者配置的外部 API 地址
      define: { 'import.meta.env.VITE_API_BASE_URL': JSON.stringify('') },
      plugins: [{
        name: 'attachment-transport-test-page',
        configureServer(server) {
          server.middlewares.use((request, _response, next) => {
            if (request.url === '/api/attachments?name=network.pdf') {
              request.socket.destroy()
              return
            }
            next()
          })
          server.middlewares.use('/__upload_test__', (_request, response) => {
            response.setHeader('content-type', 'text/html')
            response.end('<!doctype html><html><head><title>附件传输验证</title></head><body></body></html>')
          })
        },
      }],
      server: { host: '127.0.0.1', port: 0, hmr: false, watch: null, proxy: { '/api': { target: `http://127.0.0.1:${upstream.address().port}` } } },
    })
    await vite.listen()
    browser = await chromium.launch()
    const page = await browser.newPage()
    const networkFailures = []
    page.on('requestfailed', request => networkFailures.push(request.failure()?.errorText))
    await page.goto(`http://127.0.0.1:${vite.httpServer.address().port}/__upload_test__`)
    await page.evaluate(() => {
      localStorage.setItem('tinkerfin.auth.session', JSON.stringify({
        token: 'isolated-upload-token', tokenType: 'Bearer', expiresAt: '2099-01-01T00:00:00.000Z',
        user: { user_id: 1, username: 'upload-test', display_name: '附件测试', avatar_url: null, roles: [], disabled: false },
      }))
    })
    const success = await page.evaluate(async () => {
      const { uploadAttachment } = await import('/src/features/conversation/attachments/client.ts')
      const progress = []
      const bytes = Uint8Array.from([37, 80, 68, 70, 45, 49, 46, 55, 0, 255])
      try {
        const attachment = await uploadAttachment(new File([bytes], '中文 附件.pdf', { type: 'application/pdf' }), new AbortController().signal, percent => progress.push(percent))
        return { attachment, progress }
      } catch (error) {
        return { error: error.message }
      }
    })
    assert.deepEqual(success.attachment, { id: 'uploaded', name: '中文 附件.pdf', mime_type: 'application/pdf', size_bytes: 10 }, JSON.stringify({ success, networkFailures }))
    assert.deepEqual(received[0].bytes, Buffer.from([37, 80, 68, 70, 45, 49, 46, 55, 0, 255]))
    assert.equal(received[0].name, '中文 附件.pdf')
    assert.equal(received[0].protocol, '1.1')
    assert.equal(received[0].authorization, 'Bearer isolated-upload-token')
    assert.equal(received[0].contentType, 'application/octet-stream')
    assert.ok(success.progress.length > 0)
    assert.ok(success.progress.every(percent => percent >= 0 && percent <= 99))
    assert.equal(success.progress.at(-1), 99)
    assert.deepEqual(networkFailures, [])

    // 文件已发送时仍等待服务端确认，不把传输进度误认为附件就绪
    await page.evaluate(async () => {
      const { uploadAttachment } = await import('/src/features/conversation/attachments/client.ts')
      globalThis.uploadTest = { progress: [], settled: false, controller: new AbortController() }
      const state = globalThis.uploadTest
      state.completion = uploadAttachment(new File([new Uint8Array(256 * 1024)], 'progress.pdf'), state.controller.signal, percent => state.progress.push(percent))
        .then(value => { state.settled = true; return value })
    })
    await page.waitForFunction(() => globalThis.uploadTest.progress.at(-1) === 99)
    assert.equal(await page.evaluate(() => globalThis.uploadTest.settled), false)
    assert.ok(held.has('progress.pdf'))
    held.get('progress.pdf').end(JSON.stringify({ code: 0, message: 'success', data: { id: 'confirmed', name: 'progress.pdf', mime_type: 'application/pdf', size_bytes: 256 * 1024 } }))
    assert.equal((await page.evaluate(() => globalThis.uploadTest.completion)).id, 'confirmed')

    for (const [name, status, message] of [
      ['invalid.pdf', 422, '文件内容或名称不符合要求，请检查后重新上传'],
      ['disconnect.pdf', 500, '服务暂不可用，请稍后重试'],
      ['network.pdf', 0, '网络请求失败，请稍后重试'],
    ]) {
      const error = await page.evaluate(async name => {
        const { uploadAttachment } = await import('/src/features/conversation/attachments/client.ts')
        try {
          await uploadAttachment(new File(['pdf'], name), new AbortController().signal, () => {})
          return null
        } catch (error) {
          return { status: error.status, message: error.message }
        }
      }, name)
      assert.deepEqual(error, { status, message })
    }

    const beforeCancel = received.length
    const alreadyCanceled = await page.evaluate(async () => {
      const { uploadAttachment } = await import('/src/features/conversation/attachments/client.ts')
      const controller = new AbortController()
      controller.abort()
      try {
        await uploadAttachment(new File(['pdf'], 'not-sent.pdf'), controller.signal, () => {})
        return null
      } catch (error) {
        return error.code
      }
    })
    assert.equal(alreadyCanceled, 'ERR_CANCELED')
    assert.equal(received.length, beforeCancel)

    await page.evaluate(async () => {
      const { uploadAttachment } = await import('/src/features/conversation/attachments/client.ts')
      globalThis.uploadTest = { progress: [], controller: new AbortController() }
      const state = globalThis.uploadTest
      state.completion = uploadAttachment(new File([new Uint8Array(256 * 1024)], 'cancel.pdf'), state.controller.signal, percent => state.progress.push(percent))
        .then(() => 'unexpected success', error => error.code)
    })
    await page.waitForFunction(() => globalThis.uploadTest.progress.at(-1) === 99)
    await page.evaluate(() => globalThis.uploadTest.controller.abort())
    assert.equal(await page.evaluate(() => globalThis.uploadTest.completion), 'ERR_CANCELED')
    assert.ok(held.has('cancel.pdf'))
  } finally {
    if (browser) await browser.close()
    vite?.httpServer?.closeAllConnections()
    if (vite) await vite.close()
    for (const response of held.values()) response.destroy()
    upstream.closeAllConnections()
    await new Promise(resolve => upstream.close(resolve))
    await rm(cache, { recursive: true, force: true })
  }
})
