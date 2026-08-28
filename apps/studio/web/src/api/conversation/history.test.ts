import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  deleteConversation,
  fetchConversationHistoryDetail,
  fetchConversationHistoryGroupConfig,
  fetchConversationHistoryList,
  followConversationTrace,
  patchConversation,
  type ConversationHistoryDetail,
} from './history'
import { clearAuthSession, saveAuthSession } from '../../auth/session'

function envelope(data: unknown, code = 0, message = 'success', status = 200) {
  return new Response(JSON.stringify({ code, message, data }), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

const detail = (): ConversationHistoryDetail => ({
  id: 1,
  threadId: 'thread-trace',
  title: 'Trace 会话',
  lastModel: 'main',
  runtimeProfile: 'deepagents-v2',
  pinned: false,
  asOfSeq: 4,
  headRunId: 'run-1',
  availableHeads: ['run-1'],
  historyCursor: null,
  messageCount: 1,
  toolCallCount: 0,
  messages: [],
  reasoning: [],
  nodes: [],
  state: { root: {}, subgraphs: {} },
  interactions: [],
  status: { execution: 'succeeded', headRunId: 'run-1' },
  completeness: { missingPrefix: false, missingTail: false, payloadOmitted: false },
  createdAt: '2026-08-28T00:00:00',
  updatedAt: '2026-08-28T00:01:00',
})

const streamResponse = (...values: unknown[]) => {
  const encoder = new TextEncoder()
  return new Response(new ReadableStream<Uint8Array>({
    start(controller) {
      for (const value of values) {
        controller.enqueue(encoder.encode(
          'event: trace\ndata: ' + JSON.stringify(value) + '\n\n',
        ))
      }
      controller.close()
    },
  }), { headers: { 'Content-Type': 'text/event-stream' } })
}

describe('conversation Trace client', () => {
  beforeEach(() => {
    saveAuthSession({
      token: 'history-token',
      tokenType: 'Bearer',
      expiresAt: '2099-01-01T00:00:00.000Z',
      user: {
        user_id: 7,
        username: 'yunsan',
        display_name: '云杉',
        avatar_url: null,
        roles: [],
        disabled: false,
      },
    })
  })

  afterEach(() => {
    clearAuthSession()
    vi.unstubAllGlobals()
  })

  it.each([
    ['list', () => fetchConversationHistoryList()],
    ['config', () => fetchConversationHistoryGroupConfig()],
    ['detail', () => fetchConversationHistoryDetail('thread-auth')],
    ['patch', () => patchConversation('thread-auth', { title: '新标题' })],
    ['delete', () => deleteConversation('thread-auth')],
  ])('sends the session bearer token for %s requests', async (_name, request) => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const sentRequest = input instanceof Request ? input : new Request(input)
      expect(sentRequest.headers.get('Authorization')).toBe('Bearer history-token')
      if (sentRequest.method === 'DELETE') return new Response(null, { status: 204 })
      return envelope({ items: [], nextCursor: null, dayRanges: [7, 30], ...detail() })
    })
    vi.stubGlobal('fetch', fetchMock)

    await request()

    expect(fetchMock).toHaveBeenCalledOnce()
  })

  it('encodes list and fixed Trace history cursors independently', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const request = input instanceof Request ? input : new Request(input)
      const url = new URL(request.url)
      if (url.pathname.endsWith('/history') && url.pathname.includes('thread-trace')) {
        expect(url.searchParams.get('historyCursor')).toBe('opaque-trace-cursor')
        expect(url.searchParams.get('limit')).toBe('40')
        return envelope(detail())
      }
      expect(url.searchParams.get('pageSize')).toBe('5')
      expect(url.searchParams.get('cursor')).toBe('opaque-list-cursor')
      expect(url.searchParams.get('query')).toBe('目标会话')
      return envelope({ items: [], nextCursor: null })
    })
    vi.stubGlobal('fetch', fetchMock)

    await fetchConversationHistoryList({
      pageSize: 5,
      cursor: 'opaque-list-cursor',
      query: '目标会话',
    })
    await fetchConversationHistoryDetail('thread-trace', {
      historyCursor: 'opaque-trace-cursor',
      limit: 40,
    })

    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('parses the mandatory snapshot before semantic Trace updates', async () => {
    const snapshot = { type: 'snapshot' as const, snapshot: detail() }
    const update = {
      type: 'update' as const,
      update: {
        asOfSeq: 5,
        events: [],
        facts: [],
        messages: { upserts: [], removes: [] },
        reasoning: { upserts: [], removes: [] },
        nodes: { upserts: [], removes: [] },
        interactions: { upserts: [], removes: [] },
        state: { root: {}, subgraphs: {} },
        status: { execution: 'succeeded', headRunId: 'run-1' },
        completeness: { missingPrefix: false, missingTail: false, payloadOmitted: false },
        messageCount: 1,
        toolCallCount: 0,
        projections: {},
      },
    }
    vi.stubGlobal('fetch', vi.fn(async () => streamResponse(snapshot, update)))

    const received = []
    for await (const event of followConversationTrace('thread-trace')) received.push(event)

    expect(received).toEqual([snapshot, update])
  })

  it('rejects a non-Trace SSE event instead of coercing it', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => streamResponse({ type: 'RUN_STARTED' })))

    const consume = async () => {
      for await (const event of followConversationTrace('thread-trace')) {
        // 消费完整流以触发边界校验
        void event
      }
    }

    await expect(consume()).rejects.toThrow()
  })

  it('keeps delete conflict and empty 204 behavior', async () => {
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce(envelope(
        null,
        1_001_004_003,
        '会话仍在运行，请先停止并等待运行结束',
        409,
      ))
      .mockResolvedValueOnce(new Response(null, { status: 204 })))

    await expect(deleteConversation('thread-running')).rejects.toThrow(
      '会话仍在运行，请先停止并等待运行结束',
    )
    await expect(deleteConversation('thread-idle')).resolves.toBeUndefined()
  })
})
