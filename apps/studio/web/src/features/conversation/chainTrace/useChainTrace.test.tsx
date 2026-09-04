import { act, renderHook, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type {
  TraceGraphEvent,
  TraceGraphPage,
} from '../../../api/conversation/traceGraph'
import { useChainTrace } from './useChainTrace'

const followTraceGraph = vi.hoisted(() => vi.fn())
vi.mock('../../../api/conversation/traceGraph', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../../api/conversation/traceGraph')>(),
  followTraceGraph,
}))

const page: TraceGraphPage = {
  turns: [{
    id: 'turn-1',
    ordinal: 1,
    rootNodeId: 'human-1',
    startedAt: '2026-08-31T00:00:00Z',
  }],
  nodes: [{
    id: 'human-1',
    turnId: 'turn-1',
    parentId: null,
    structuralParentId: null,
    kind: 'human_message',
    status: 'succeeded',
    name: 'HumanMessage',
    runId: 'run-1',
    namespace: [],
    startedAt: '2026-08-31T00:00:00Z',
    completedAt: '2026-08-31T00:00:00Z',
    startedSeq: 1,
    updatedSeq: 1,
    content: 'Find data',
    contentOmitted: false,
    requestOmitted: false,
    resultOmitted: false,
    hooks: [],
    linkIssues: [],
  }],
  orderedNodeIds: ['human-1'],
  rootNodeIds: ['human-1'],
  matchedNodeIds: ['human-1'],
  nextCursor: null,
  asOfSeq: 1,
  facets: {
    kinds: { human_message: 1 },
    statuses: { succeeded: 1 },
    agents: {},
    middleware: {},
    skills: {},
    providers: {},
    models: {},
  },
  completeness: {
    callTrackingMissing: false,
    relationshipEvidenceMissing: false,
    detailsOmitted: false,
  },
}

const waitForAbort = (signal?: AbortSignal) => new Promise<void>((resolve) => {
  if (signal?.aborted) resolve()
  else signal?.addEventListener('abort', () => resolve(), { once: true })
})

describe('useChainTrace', () => {
  beforeEach(() => {
    followTraceGraph.mockReset()
  })

  it('stays idle without opening a follower until the view is active', () => {
    const { result } = renderHook(() => useChainTrace({
      threadId: 'thread-1',
      active: false,
      filter: {},
      limit: 1000,
    }))

    expect(result.current.state.phase).toBe('idle')
    expect(followTraceGraph).not.toHaveBeenCalled()
  })

  it('starts one follower after StrictMode replay and closes it on unmount', async () => {
    const signals: AbortSignal[] = []
    followTraceGraph.mockImplementation((
      _threadId: string,
      _filter: unknown,
      options: { signal?: AbortSignal } = {},
    ) => {
      if (options.signal) signals.push(options.signal)
      return (async function* (): AsyncGenerator<TraceGraphEvent> {
        yield { type: 'snapshot', snapshot: page }
        await waitForAbort(options.signal)
      })()
    })
    const { result, unmount } = renderHook(() => useChainTrace({
      threadId: 'thread-1',
      active: true,
      filter: { kinds: ['tool'] },
      limit: 1000,
    }), { reactStrictMode: true })

    await waitFor(() => expect(result.current.state.phase).toBe('ready'))
    expect(followTraceGraph).toHaveBeenCalledTimes(1)
    expect(signals).toHaveLength(1)
    expect(signals[0].aborted).toBe(false)
    unmount()
    await waitFor(() => expect(signals[0].aborted).toBe(true))
  })

  it('keeps one follower when only the presentation view rerenders', async () => {
    followTraceGraph.mockImplementation((
      _threadId: string,
      _filter: unknown,
      options: { signal?: AbortSignal } = {},
    ) => (
      async function* (): AsyncGenerator<TraceGraphEvent> {
        yield { type: 'snapshot', snapshot: page }
        await waitForAbort(options.signal)
      }
    )())
    const { result, rerender, unmount } = renderHook(
      ({ view }: { view: 'timeline' | 'tree' }) => {
        void view
        return useChainTrace({
          threadId: 'thread-1',
          active: true,
          filter: {},
          limit: 1000,
        })
      },
      { initialProps: { view: 'timeline' as 'timeline' | 'tree' } },
    )

    await waitFor(() => expect(result.current.state.phase).toBe('ready'))
    rerender({ view: 'tree' })
    await Promise.resolve()
    expect(followTraceGraph).toHaveBeenCalledTimes(1)
    unmount()
  })

  it('applies node changes in the framework-provided order', async () => {
    let releaseUpdate: (() => void) | undefined
    followTraceGraph.mockImplementation((
      _threadId: string,
      _filter: unknown,
      options: { signal?: AbortSignal } = {},
    ) => (
      async function* (): AsyncGenerator<TraceGraphEvent> {
        yield { type: 'snapshot', snapshot: page }
        await new Promise<void>((resolve) => { releaseUpdate = resolve })
        yield {
          type: 'update',
          update: {
            asOfSeq: 2,
            nextCursor: 'fresh-older-page',
            turnUpserts: [],
            turnRemoves: [],
            nodeUpserts: [{
              ...page.nodes[0],
              id: 'assistant-1',
              parentId: 'human-1',
              structuralParentId: 'human-1',
              kind: 'assistant_message',
              name: 'AssistantMessage',
              startedSeq: 2,
              updatedSeq: 2,
            }],
            nodeRemoves: [],
            orderedNodeIds: ['human-1', 'assistant-1'],
            rootNodeIds: ['human-1'],
            matchedNodeIds: ['human-1', 'assistant-1'],
            facets: {
              ...page.facets,
              kinds: { human_message: 1, assistant_message: 1 },
            },
            completeness: page.completeness,
          },
        }
        await waitForAbort(options.signal)
      }
    )())
    const { result, unmount } = renderHook(() => useChainTrace({
      threadId: 'thread-1',
      active: true,
      filter: {},
      limit: 1000,
    }))

    await waitFor(() => expect(result.current.state.phase).toBe('ready'))
    act(() => releaseUpdate?.())
    await waitFor(() => {
      expect(result.current.state.phase).toBe('ready')
      if (result.current.state.phase === 'ready') {
        expect(result.current.state.page.nodes.map((node) => node.id)).toEqual([
          'human-1',
          'assistant-1',
        ])
        expect(result.current.state.page.rootNodeIds).toEqual(['human-1'])
        expect(result.current.state.page.nextCursor).toBe('fresh-older-page')
      }
    })
    unmount()
  })

  it('applies the latest completeness cursor when no matching node changes', async () => {
    followTraceGraph.mockImplementation((
      _threadId: string,
      _filter: unknown,
      options: { signal?: AbortSignal } = {},
    ) => (
      async function* (): AsyncGenerator<TraceGraphEvent> {
        yield {
          type: 'snapshot',
          snapshot: { ...page, nextCursor: 'stale-older-page' },
        }
        yield {
          type: 'update',
          update: {
            asOfSeq: 2,
            nextCursor: 'fresh-older-page',
            turnUpserts: [],
            turnRemoves: [],
            nodeUpserts: [],
            nodeRemoves: [],
            orderedNodeIds: page.orderedNodeIds,
            rootNodeIds: page.rootNodeIds,
            matchedNodeIds: page.matchedNodeIds,
            facets: page.facets,
            completeness: page.completeness,
          },
        }
        await waitForAbort(options.signal)
      }
    )())
    const { result, unmount } = renderHook(() => useChainTrace({
      threadId: 'thread-1',
      active: true,
      filter: { kinds: ['model'] },
      limit: 1000,
    }))

    await waitFor(() => {
      expect(result.current.state.phase).toBe('ready')
      if (result.current.state.phase === 'ready') {
        expect(result.current.state.page.asOfSeq).toBe(2)
        expect(result.current.state.page.nextCursor).toBe('fresh-older-page')
      }
    })
    unmount()
  })

  it('rejects a delta whose authoritative order references missing state', async () => {
    followTraceGraph.mockImplementation(() => (
      async function* (): AsyncGenerator<TraceGraphEvent> {
        yield { type: 'snapshot', snapshot: page }
        yield {
          type: 'update',
          update: {
            asOfSeq: 2,
            nextCursor: null,
            turnUpserts: [],
            turnRemoves: [],
            nodeUpserts: [],
            nodeRemoves: [],
            orderedNodeIds: ['missing'],
            rootNodeIds: ['missing'],
            matchedNodeIds: ['missing'],
            facets: page.facets,
            completeness: page.completeness,
          },
        }
      }
    )())
    const { result } = renderHook(() => useChainTrace({
      threadId: 'thread-1',
      active: true,
      filter: {},
      limit: 1000,
    }))
    await waitFor(() => expect(result.current.state.phase).toBe('error'))
  })

  it('replaces a follower and ignores its late snapshot', async () => {
    let releaseStale: (() => void) | undefined
    const signals: AbortSignal[] = []
    let calls = 0
    followTraceGraph.mockImplementation((
      _threadId: string,
      _filter: unknown,
      options: { signal?: AbortSignal } = {},
    ) => {
      calls += 1
      const call = calls
      if (options.signal) signals.push(options.signal)
      return (async function* (): AsyncGenerator<TraceGraphEvent> {
        if (call === 1) {
          yield { type: 'snapshot', snapshot: page }
          await new Promise<void>((resolve) => { releaseStale = resolve })
          yield { type: 'snapshot', snapshot: { ...page, asOfSeq: 3 } }
          return
        }
        yield { type: 'snapshot', snapshot: { ...page, asOfSeq: 2 } }
        await waitForAbort(options.signal)
      })()
    })
    const { result, rerender, unmount } = renderHook(({
      kinds,
    }: { kinds: Array<'tool' | 'model'> }) => useChainTrace({
      threadId: 'thread-1',
      active: true,
      filter: { kinds },
      limit: 1000,
    }), { initialProps: { kinds: ['tool'] } })

    await waitFor(() => expect(result.current.state.phase).toBe('ready'))
    rerender({ kinds: ['model'] })
    await waitFor(() => {
      expect(followTraceGraph).toHaveBeenCalledTimes(2)
      expect(signals[0].aborted).toBe(true)
      expect(result.current.state.phase).toBe('ready')
      if (result.current.state.phase === 'ready') {
        expect(result.current.state.page.asOfSeq).toBe(2)
      }
    })
    await act(async () => {
      releaseStale?.()
      await Promise.resolve()
    })
    if (result.current.state.phase === 'ready') {
      expect(result.current.state.page.asOfSeq).toBe(2)
    }
    unmount()
  })

  it.each(['event', 'throw', 'eof'] as const)(
    'enters the error phase after a terminal %s outcome',
    async (outcome) => {
      followTraceGraph.mockImplementation(() => (
        async function* (): AsyncGenerator<TraceGraphEvent> {
          yield { type: 'snapshot', snapshot: page }
          if (outcome === 'event') yield { type: 'error', code: 'trace_unavailable' }
          else if (outcome === 'throw') throw new Error('follow failed')
        }
      )())
      const { result } = renderHook(() => useChainTrace({
        threadId: 'thread-1',
        active: true,
        filter: {},
        limit: 1000,
      }))
      await waitFor(() => expect(result.current.state.phase).toBe('error'))
    },
  )
})
