import { describe, expect, it, vi } from 'vitest'
import { DomAbortController } from './setup'

describe('请求与页面事件的取消信号', () => {
  it('请求继承组合信号的取消原因并通知监听器', async () => {
    const controller = new AbortController()
    const signal = AbortSignal.any([controller.signal])
    const request = new Request('data:text/plain,unused', {signal})
    const onAbort = vi.fn()
    request.signal.addEventListener('abort', onAbort, {once: true})
    const reason = new Error('cancel request')
    controller.abort(reason)
    expect(request.signal.aborted).toBe(true)
    expect(request.signal.reason).toBe(reason)
    expect(onAbort).toHaveBeenCalledOnce()
    await expect(fetch(request)).rejects.toBe(reason)
  })

  it('页面自有信号仍可取消 DOM 事件监听', () => {
    const controller = new DomAbortController()
    const button = document.createElement('button')
    const onClick = vi.fn()
    button.addEventListener('click', onClick, {signal: controller.signal})
    button.click()
    expect(onClick).toHaveBeenCalledOnce()
    controller.abort()
    button.click()
    expect(onClick).toHaveBeenCalledOnce()
  })
})
