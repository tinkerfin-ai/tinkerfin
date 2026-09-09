import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { TraceGraphEvent, TraceGraphPage } from '../../../api/conversation/traceGraph'
import { clearAuthSession, saveAuthSession } from '../../../auth/session'
import { traceGraphNode, traceGraphWithNodes } from '../../../test/traceFixtures'
import { useChainTrace } from './useChainTrace'

const snapshot = (asOfSeq = 1): TraceGraphPage => ({
  ...traceGraphWithNodes([traceGraphNode({
    id: 'human-1',
    kind: 'human_message',
    name: 'HumanMessage',
    content: `结果 ${asOfSeq}`,
    updatedSeq: asOfSeq,
  })], asOfSeq),
  nextCursor: null,
})

const jsonResponse = (data: unknown) => Response.json({ code: 0, message: 'success', data })

function openStream() {
  let controller: ReadableStreamDefaultController<Uint8Array>
  const cancelled = vi.fn()
  const response = new Response(new ReadableStream<Uint8Array>({
    start(value) { controller = value },
    cancel: cancelled,
  }), { headers: { 'Content-Type': 'text/event-stream' } })
  return {
    response,
    cancelled,
    send: (event: TraceGraphEvent) => controller.enqueue(new TextEncoder().encode(
      `event: trace\ndata: ${JSON.stringify(event)}\n\n`,
    )),
  }
}

const requestFrom = (input: RequestInfo | URL, init?: RequestInit) => (
  input instanceof Request ? input : new Request(new URL(String(input), window.location.origin), init)
)

describe('链路读取协议生命周期', () => {
  beforeEach(() => {
    saveAuthSession({
      token: 'chain-contract-token',
      tokenType: 'Bearer',
      expiresAt: '2099-01-01T00:00:00.000Z',
      user: {
        user_id: 7,
        username: 'chain-contract',
        display_name: '链路契约',
        avatar_url: null,
        roles: [],
        disabled: false,
      },
    })
  })

  afterEach(() => {
    clearAuthSession()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('历史与部分历史只读取筛选快照，保持空闲且可重新激活', async () => {
    const page = { ...snapshot(), nextCursor: 'older-turns', completeness: {
      callTrackingMissing: true,
      relationshipEvidenceMissing: true,
      detailsOmitted: true,
    } }
    const requests: Request[] = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = requestFrom(input, init)
      requests.push(request)
      return jsonResponse(page)
    }))
    const { result, rerender } = renderHook(({ active }) => useChainTrace({
      threadId: 'thread-history', active, live: false,
      filter: { query: '结果' }, limit: 1000,
    }), { initialProps: { active: true }, reactStrictMode: true })

    await waitFor(() => expect(result.current.state).toEqual({ phase: 'ready', page }))
    expect(requests).toHaveLength(1)
    const url = new URL(requests[0]!.url)
    expect(url.pathname).toBe('/api/conversation/thread-history/trace/graph')
    expect(url.searchParams.get('query')).toBe('结果')
    expect(url.searchParams.get('limit')).toBe('1000')
    rerender({ active: false })
    expect(result.current.state.phase).toBe('idle')
    rerender({ active: true })
    await waitFor(() => expect(requests).toHaveLength(2))
    await waitFor(() => expect(result.current.state.phase).toBe('ready'))
  })

  it('运行中接收增量，结束时取消流并补齐最新权威观测后的最终快照', async () => {
    const stream = openStream()
    const requests: Request[] = []
    let current = snapshot(3)
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = requestFrom(input, init)
      requests.push(request)
      return new URL(request.url).pathname.endsWith('/follow') ? stream.response : jsonResponse(current)
    }))
    const { result, rerender } = renderHook(({ live, observedAt }) => useChainTrace({
      threadId: 'thread-live', active: true, live, observedAt, filter: {}, limit: 1000,
    }), { initialProps: { live: true, observedAt: 'initial' } })
    await waitFor(() => expect(requests).toHaveLength(1))
    act(() => stream.send({ type: 'snapshot', snapshot: snapshot() }))
    await waitFor(() => expect(result.current.state.phase).toBe('ready'))
    expect(requests[0]!.signal.aborted).toBe(false)
    act(() => stream.send({ type: 'update', update: {
      asOfSeq: 2, nextCursor: null, turnUpserts: [], turnRemoves: [],
      nodeUpserts: snapshot(2).nodes, nodeRemoves: [],
      orderedNodeIds: ['human-1'], matchedNodeIds: ['human-1'], completeness: snapshot().completeness,
    } }))
    await waitFor(() => expect(result.current.state).toEqual({ phase: 'ready', page: snapshot(2) }))
    rerender({ live: true, observedAt: 'another-live-observation' })
    expect(requests).toHaveLength(1)

    rerender({ live: false, observedAt: 'another-live-observation' })
    await waitFor(() => expect(result.current.state).toEqual({ phase: 'ready', page: snapshot(3) }))
    expect(requests[0]!.signal.aborted).toBe(true)
    expect(stream.cancelled).toHaveBeenCalledTimes(1)
    current = snapshot(4)
    rerender({ live: false, observedAt: 'final-authoritative-observation' })
    await waitFor(() => expect(result.current.state).toEqual({ phase: 'ready', page: snapshot(4) }))
    expect(requests.map((request) => new URL(request.url).pathname)).toEqual([
      '/api/conversation/thread-live/trace/graph/follow',
      '/api/conversation/thread-live/trace/graph',
      '/api/conversation/thread-live/trace/graph',
    ])
  })

  it('恢复或新 Run 立即开启流，不依赖观测时间变化，并取消被隐藏的流', async () => {
    const stream = openStream()
    const nextStream = openStream()
    let follows = 0
    const requests: Request[] = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = requestFrom(input, init)
      requests.push(request)
      if (!new URL(request.url).pathname.endsWith('/follow')) return jsonResponse(snapshot())
      return follows++ === 0 ? stream.response : nextStream.response
    }))
    const { result, rerender, unmount } = renderHook(({ live }) => useChainTrace({
      threadId: 'thread-resumed', active: true, live, observedAt: 'same-observation', filter: {}, limit: 1000,
    }), { initialProps: { live: false } })
    await waitFor(() => expect(result.current.state.phase).toBe('ready'))
    rerender({ live: true })
    await waitFor(() => expect(follows).toBe(1))
    act(() => stream.send({ type: 'snapshot', snapshot: snapshot(2) }))
    await waitFor(() => expect(result.current.state.phase).toBe('ready'))

    const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden')
    act(() => document.dispatchEvent(new Event('visibilitychange')))
    await waitFor(() => expect(stream.cancelled).toHaveBeenCalledTimes(1))
    expect(result.current.state.phase).toBe('idle')
    visibility.mockReturnValue('visible')
    act(() => document.dispatchEvent(new Event('visibilitychange')))
    await waitFor(() => expect(follows).toBe(2))
    act(() => nextStream.send({ type: 'snapshot', snapshot: snapshot(3) }))
    await waitFor(() => expect(result.current.state).toEqual({ phase: 'ready', page: snapshot(3) }))
    unmount()
    await waitFor(() => expect(nextStream.cancelled).toHaveBeenCalledTimes(1))
    expect(requests.at(-1)!.signal.aborted).toBe(true)
  })

  it('过滤与线程切换取消旧快照，迟到响应不能覆盖当前页面', async () => {
    let resolveStale: (response: Response) => void = () => undefined
    const requests: Request[] = []
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      requests.push(requestFrom(input, init))
      return requests.length === 1
        ? new Promise<Response>((resolve) => { resolveStale = resolve })
        : Promise.resolve(jsonResponse(snapshot(2)))
    }))
    const { result, rerender } = renderHook(({ threadId, query }) => useChainTrace({
      threadId, active: true, live: false, filter: { query }, limit: 1000,
    }), { initialProps: { threadId: 'thread-old', query: 'old' } })
    await waitFor(() => expect(requests).toHaveLength(1))
    rerender({ threadId: 'thread-new', query: 'new' })
    await waitFor(() => expect(result.current.state).toEqual({ phase: 'ready', page: snapshot(2) }))
    expect(requests[0]!.signal.aborted).toBe(true)
    await act(async () => { resolveStale(jsonResponse(snapshot(9))) })
    expect(result.current.state).toEqual({ phase: 'ready', page: snapshot(2) })
    expect(new URL(requests[1]!.url).searchParams.get('query')).toBe('new')
  })

  it('同一查询刷新期间保留结果，切换筛选重新加载，刷新失败允许重试', async () => {
    const responses: Array<(response: Response) => void> = []
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(resolve => responses.push(resolve))))
    const { result, rerender } = renderHook(({ observedAt, query }) => useChainTrace({
      threadId: 'thread-refresh', active: true, live: false, observedAt, filter: { query }, limit: 1000,
    }), { initialProps: { observedAt: 'initial', query: '' } })
    await waitFor(() => expect(responses).toHaveLength(1))
    expect(result.current.state.phase).toBe('loading')
    await act(async () => responses[0]!(jsonResponse(snapshot())))
    await waitFor(() => expect(result.current.state).toEqual({ phase: 'ready', page: snapshot() }))

    rerender({ observedAt: 'updated', query: '' })
    await waitFor(() => expect(responses).toHaveLength(2))
    expect(result.current.state).toEqual({ phase: 'ready', page: snapshot() })
    await act(async () => responses[1]!(jsonResponse(snapshot(2))))
    await waitFor(() => expect(result.current.state).toEqual({ phase: 'ready', page: snapshot(2) }))

    rerender({ observedAt: 'updated', query: 'different' })
    await waitFor(() => expect(responses).toHaveLength(3))
    expect(result.current.state.phase).toBe('loading')
    await act(async () => responses[2]!(jsonResponse(snapshot(3))))
    await waitFor(() => expect(result.current.state.phase).toBe('ready'))

    rerender({ observedAt: 'latest', query: 'different' })
    await waitFor(() => expect(responses).toHaveLength(4))
    expect(result.current.state).toEqual({ phase: 'ready', page: snapshot(3) })
    await act(async () => responses[3]!(jsonResponse({ invalid: true })))
    await waitFor(() => expect(result.current.state.phase).toBe('error'))
    act(() => result.current.retry())
    await waitFor(() => expect(responses).toHaveLength(5))
    expect(result.current.state.phase).toBe('loading')
    await act(async () => responses[4]!(jsonResponse(snapshot(4))))
    await waitFor(() => expect(result.current.state).toEqual({ phase: 'ready', page: snapshot(4) }))
  })

  it('快照失败显式报错，用户重试只创建新的快照请求', async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ invalid: true }))
      .mockResolvedValueOnce(jsonResponse(snapshot()))
    vi.stubGlobal('fetch', fetch)
    const { result } = renderHook(() => useChainTrace({
      threadId: 'thread-retry', active: true, live: false, filter: {}, limit: 1000,
    }))
    await waitFor(() => expect(result.current.state.phase).toBe('error'))
    expect(fetch).toHaveBeenCalledTimes(1)
    act(() => result.current.retry())
    await waitFor(() => expect(result.current.state).toEqual({ phase: 'ready', page: snapshot() }))
    expect(fetch).toHaveBeenCalledTimes(2)
  })
})
