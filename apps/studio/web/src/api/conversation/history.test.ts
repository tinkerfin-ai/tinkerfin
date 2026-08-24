import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  deleteConversation,
  fetchConversationEvents,
  fetchConversationHistoryDetail,
  fetchConversationHistoryGroupConfig,
  fetchConversationHistoryList,
  patchConversation,
} from './history'
import { subscribeApiErrors } from '../shared/http'
import { clearAuthSession, saveAuthSession } from '../../auth/session'

function envelope(data: unknown, code = 0, message = 'success', status = 200) {
  return new Response(JSON.stringify({ code, message, data }), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('conversation history client', () => {
  beforeEach(() => {
    saveAuthSession({
      token: 'history-token',
      tokenType: 'Bearer',
      expiresAt: '2099-01-01T00:00:00.000Z',
      user: { user_id: 7, username: 'yunsan', display_name: '云杉', avatar_url: null, roles: [], disabled: false },
    })
  })

  afterEach(() => {
    clearAuthSession()
    vi.unstubAllGlobals()
  })

  it.each([
    ['list', () => fetchConversationHistoryList()],
    ['config', () => fetchConversationHistoryGroupConfig()],
    ['events', () => fetchConversationEvents('thread-auth')],
    ['patch', () => patchConversation('thread-auth', { title: '新标题' })],
    ['delete', () => deleteConversation('thread-auth')],
  ])('sends the session bearer token for %s requests', async (_name, request) => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const sentRequest = input instanceof Request ? input : new Request(input)
      expect(sentRequest.headers.get('Authorization')).toBe('Bearer history-token')
      if (sentRequest.method === 'DELETE') return new Response(null, { status: 204 })
      return envelope({
        items: [],
        nextCursor: null,
        threadId: 'thread-auth',
        title: '新标题',
      })
    })
    vi.stubGlobal('fetch', fetchMock)

    await request()

    expect(fetchMock).toHaveBeenCalledOnce()
  })

  it('rejects a business conflict returned with HTTP 409', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => envelope(
      null,
      1_001_004_003,
      '会话仍在运行，请先停止并等待运行结束',
      409,
    )))

    await expect(deleteConversation('thread-running')).rejects.toThrow(
      '会话仍在运行，请先停止并等待运行结束',
    )
  })

  it('accepts the empty 204 response after both stores are deleted', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(null, { status: 204 })))

    await expect(deleteConversation('thread-idle')).resolves.toBeUndefined()
  })

  it('unwraps list responses through the shared client', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => envelope({
      items: [],
      nextCursor: null,
    })))

    await expect(fetchConversationHistoryList()).resolves.toEqual({
      items: [],
      nextCursor: null,
    })
  })

  it('encodes the fuzzy query and opaque cursor in list requests', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const sentRequest = input instanceof Request ? input : new Request(input)
      const url = new URL(sentRequest.url)
      expect(url.searchParams.get('pageSize')).toBe('5')
      expect(url.searchParams.get('cursor')).toBe('opaque-cursor')
      expect(url.searchParams.get('query')).toBe('目标会话')
      return envelope({ items: [], nextCursor: null })
    })
    vi.stubGlobal('fetch', fetchMock)

    await fetchConversationHistoryList({
      pageSize: 5,
      cursor: 'opaque-cursor',
      query: '目标会话',
    })

    expect(fetchMock).toHaveBeenCalledOnce()
  })

  it('unwraps history group configuration through the shared client', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => envelope({ dayRanges: [7, 30] })))

    await expect(fetchConversationHistoryGroupConfig()).resolves.toEqual({
      dayRanges: [7, 30],
    })
  })

  it('unwraps update responses through the shared client', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => envelope({
      id: 1,
      threadId: 'thread-rename',
      title: '新标题',
    })))

    await expect(patchConversation('thread-rename', { title: '新标题' })).resolves.toMatchObject({
      threadId: 'thread-rename',
      title: '新标题',
    })
  })

  it('suppresses the global channel when event recovery has local error UI', async () => {
    const listener = vi.fn()
    const unsubscribe = subscribeApiErrors(listener)
    vi.stubGlobal('fetch', vi.fn(async () => envelope(
      null,
      1_001_004_000,
      '会话不存在',
      404,
    )))

    await expect(fetchConversationEvents('thread-missing', {
      suppressGlobalError: true,
    })).rejects.toThrow('会话不存在')
    expect(listener).not.toHaveBeenCalled()

    unsubscribe()
  })

  it.each([
    ['detail', (signal: AbortSignal) => fetchConversationHistoryDetail('thread-cancel', { signal })],
    ['events', (signal: AbortSignal) => fetchConversationEvents('thread-cancel', { signal })],
  ])('propagates AbortSignal through %s requests', async (_name, request) => {
    let requestSignal: AbortSignal | undefined
    let notifyStarted: (() => void) | undefined
    const started = new Promise<void>((resolve) => {
      notifyStarted = resolve
    })
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const sentRequest = input instanceof Request ? input : new Request(input)
      requestSignal = sentRequest.signal
      notifyStarted?.()
      return await new Promise<Response>((_resolve, reject) => {
        sentRequest.signal.addEventListener('abort', () => {
          reject(new DOMException('请求已取消', 'AbortError'))
        }, { once: true })
      })
    }))
    const controller = new AbortController()
    const pending = request(controller.signal)
    const rejection = expect(pending).rejects.toBeDefined()

    await started
    controller.abort()

    await rejection
    expect(requestSignal?.aborted).toBe(true)
  })
})
