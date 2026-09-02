import { act, renderHook, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { TraceEntryEvent, TraceEntryPage } from '../../../api/conversation/traceEntries'
import { useChainTrace } from './useChainTrace'

const followTraceEntries = vi.hoisted(() => vi.fn())
vi.mock('../../../api/conversation/traceEntries', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../../api/conversation/traceEntries')>(),
  followTraceEntries,
}))

const page: TraceEntryPage = {
  turns: [{
    id: 'turn-1',
    ordinal: 1,
    startedAt: '2026-08-31T00:00:00Z',
    userMessage: {
      id: 'message-1',
      traceSeq: 1,
      namespace: [],
      runId: 'run-1',
      role: 'user',
      content: 'Find data',
      contentOmitted: false,
      status: 'completed',
      createdAt: '2026-08-31T00:00:00Z',
    },
  }],
  items: [{
    id: 'running-tool',
    turnId: 'turn-1',
    kind: 'tool',
    status: 'running',
    name: 'search',
    runId: 'run-1',
    namespace: [],
    startedAt: '2026-08-31T00:00:00Z',
    startedSeq: 1,
    updatedSeq: 1,
    requestOmitted: false,
    resultOmitted: false,
    hooks: [],
  }],
  asOfSeq: 1,
  facets: {
    kinds: { tool: 1 },
    statuses: { running: 1 },
    agents: {},
    middleware: {},
    skills: {},
    providers: {},
    models: {},
  },
  completeness: { callTrackingMissing: false, executionTreeMissing: false },
}

describe('useChainTrace', () => {
  beforeEach(() => {
    followTraceEntries.mockReset()
  })

  it('starts one follower after StrictMode effect replay and closes it on real unmount', async () => {
    const signals: AbortSignal[] = []
    followTraceEntries.mockImplementation((
      _threadId: string,
      _filter: unknown,
      options: { signal?: AbortSignal },
    ) => {
      if (options.signal) signals.push(options.signal)
      return (async function* (): AsyncGenerator<TraceEntryEvent> {
        yield { type: 'snapshot', snapshot: page }
        await new Promise<void>((resolve) => {
          if (options.signal?.aborted) resolve()
          else options.signal?.addEventListener('abort', () => resolve(), { once: true })
        })
      })()
    })
    const { result, unmount } = renderHook(() => useChainTrace({
      threadId: 'thread-1',
      active: true,
      filter: { kinds: ['tool'] },
    }), { reactStrictMode: true })

    await waitFor(() => expect(result.current.state.phase).toBe('ready'))
    expect(followTraceEntries).toHaveBeenCalledTimes(1)
    expect(signals).toHaveLength(1)
    expect(signals[0].aborted).toBe(false)

    unmount()
    await waitFor(() => expect(signals[0].aborted).toBe(true))
  })

  it('applies filtered removals and aborts the owned follower on unmount', async () => {
    let releaseUpdate: (() => void) | undefined
    let capturedSignal: AbortSignal | undefined
    followTraceEntries.mockImplementation((
      _threadId: string,
      _filter: unknown,
      options: { signal?: AbortSignal },
    ) => {
      capturedSignal = options.signal
      return (async function* (): AsyncGenerator<TraceEntryEvent> {
        yield { type: 'snapshot', snapshot: page }
        await new Promise<void>((resolve) => { releaseUpdate = resolve })
        if (options.signal?.aborted) return
        yield {
          type: 'update',
          update: {
            asOfSeq: 2,
            turnUpserts: [],
            turnRemoves: ['turn-1'],
            upserts: [],
            removes: ['running-tool'],
            facets: { ...page.facets, kinds: {}, statuses: {} },
            completeness: page.completeness,
          },
        }
        await new Promise<void>((resolve) => {
          if (options.signal?.aborted) resolve()
          else options.signal?.addEventListener('abort', () => resolve(), { once: true })
        })
      })()
    })
    const { result, unmount } = renderHook(() => useChainTrace({
      threadId: 'thread-1',
      active: true,
      filter: { kinds: ['tool'] },
    }))

    await waitFor(() => expect(result.current.state.phase).toBe('ready'))
    act(() => releaseUpdate?.())
    await waitFor(() => {
      expect(result.current.state.phase).toBe('ready')
      if (result.current.state.phase === 'ready') {
        expect(result.current.state.page.items).toEqual([])
        expect(result.current.state.page.turns).toEqual([])
      }
    })
    unmount()
    await waitFor(() => expect(capturedSignal?.aborted).toBe(true))
  })

  it('replaces the follower for filter, thread and retry changes', async () => {
    const calls: Array<{
      threadId: string
      filter: unknown
      signal?: AbortSignal
    }> = []
    followTraceEntries.mockImplementation((
      threadId: string,
      filter: unknown,
      options: { signal?: AbortSignal },
    ) => {
      calls.push({ threadId, filter, signal: options.signal })
      return (async function* (): AsyncGenerator<TraceEntryEvent> {
        yield { type: 'snapshot', snapshot: page }
        await new Promise<void>((resolve) => {
          if (options.signal?.aborted) resolve()
          else options.signal?.addEventListener('abort', () => resolve(), { once: true })
        })
      })()
    })
    const { result, rerender, unmount } = renderHook(({
      active,
      threadId,
      kinds,
    }: {
      active: boolean
      threadId: string
      kinds: Array<'tool' | 'provider'>
    }) => useChainTrace({
      threadId,
      active,
      filter: { kinds },
    }), {
      initialProps: { active: true, threadId: 'thread-1', kinds: ['tool'] },
    })

    await waitFor(() => expect(calls).toHaveLength(1))
    rerender({ active: true, threadId: 'thread-1', kinds: ['provider'] })
    await waitFor(() => {
      expect(calls).toHaveLength(2)
      expect(calls[0].signal?.aborted).toBe(true)
      expect(calls[1].signal?.aborted).toBe(false)
    })

    rerender({ active: true, threadId: 'thread-2', kinds: ['provider'] })
    await waitFor(() => {
      expect(calls).toHaveLength(3)
      expect(calls[1].signal?.aborted).toBe(true)
      expect(calls[2].threadId).toBe('thread-2')
    })

    act(() => result.current.retry())
    await waitFor(() => {
      expect(calls).toHaveLength(4)
      expect(calls[2].signal?.aborted).toBe(true)
      expect(calls[3].signal?.aborted).toBe(false)
    })

    rerender({ active: false, threadId: 'thread-2', kinds: ['provider'] })
    await waitFor(() => {
      expect(calls).toHaveLength(4)
      expect(calls[3].signal?.aborted).toBe(true)
      expect(result.current.state.phase).toBe('idle')
    })

    rerender({ active: true, threadId: 'thread-2', kinds: ['provider'] })
    await waitFor(() => {
      expect(calls).toHaveLength(5)
      expect(calls[4].signal?.aborted).toBe(false)
    })

    unmount()
    await waitFor(() => expect(calls[4].signal?.aborted).toBe(true))
  })

  it('ignores an event from a follower replaced by a new filter', async () => {
    let releaseStale: (() => void) | undefined
    let callCount = 0
    const currentPage: TraceEntryPage = {
      ...page,
      asOfSeq: 2,
      items: [{ ...page.items[0], id: 'current-provider', kind: 'provider' }],
    }
    const stalePage: TraceEntryPage = {
      ...page,
      asOfSeq: 3,
      items: [{ ...page.items[0], id: 'stale-tool' }],
    }
    followTraceEntries.mockImplementation((
      _threadId: string,
      _filter: unknown,
      options: { signal?: AbortSignal },
    ) => {
      callCount += 1
      const currentCall = callCount
      return (async function* (): AsyncGenerator<TraceEntryEvent> {
        if (currentCall === 1) {
          yield { type: 'snapshot', snapshot: page }
          await new Promise<void>((resolve) => { releaseStale = resolve })
          yield { type: 'snapshot', snapshot: stalePage }
          return
        }
        yield { type: 'snapshot', snapshot: currentPage }
        await new Promise<void>((resolve) => {
          if (options.signal?.aborted) resolve()
          else options.signal?.addEventListener('abort', () => resolve(), { once: true })
        })
      })()
    })
    const { result, rerender, unmount } = renderHook(({
      kinds,
    }: {
      kinds: Array<'tool' | 'provider'>
    }) => useChainTrace({
      threadId: 'thread-1',
      active: true,
      filter: { kinds },
    }), { initialProps: { kinds: ['tool'] } })

    await waitFor(() => expect(result.current.state.phase).toBe('ready'))
    rerender({ kinds: ['provider'] })
    await waitFor(() => {
      expect(result.current.state.phase).toBe('ready')
      if (result.current.state.phase === 'ready') {
        expect(result.current.state.page.items[0].id).toBe('current-provider')
      }
    })

    await act(async () => {
      releaseStale?.()
      await Promise.resolve()
    })
    expect(result.current.state.phase).toBe('ready')
    if (result.current.state.phase === 'ready') {
      expect(result.current.state.page.items[0].id).toBe('current-provider')
    }
    unmount()
  })

  it.each(['event', 'throw', 'eof'] as const)(
    'enters the error phase after a terminal %s outcome',
    async (outcome) => {
      followTraceEntries.mockImplementation(() => (
        async function* (): AsyncGenerator<TraceEntryEvent> {
          yield { type: 'snapshot', snapshot: page }
          if (outcome === 'event') {
            yield { type: 'error', code: 'trace_unavailable' }
          } else if (outcome === 'throw') {
            throw new Error('follow failed')
          }
        }
      )())
      const { result, unmount } = renderHook(() => useChainTrace({
        threadId: 'thread-1',
        active: true,
        filter: {},
      }))

      await waitFor(() => expect(result.current.state.phase).toBe('error'))
      unmount()
    },
  )

  it('merges a new Turn and its first entry without resetting earlier data', async () => {
    followTraceEntries.mockImplementation((
      _threadId: string,
      _filter: unknown,
      options: { signal?: AbortSignal },
    ) => (
      async function* (): AsyncGenerator<TraceEntryEvent> {
        yield { type: 'snapshot', snapshot: page }
        yield {
          type: 'update',
          update: {
            asOfSeq: 2,
            turnUpserts: [{
              id: 'turn-2',
              ordinal: 2,
              startedAt: '2026-08-31T00:01:00Z',
              userMessage: {
                id: 'message-2',
                traceSeq: 2,
                namespace: [],
                runId: 'run-2',
                role: 'user',
                content: 'Continue',
                contentOmitted: false,
                status: 'completed',
                createdAt: '2026-08-31T00:01:00Z',
              },
            }],
            turnRemoves: [],
            upserts: [{
              id: 'agent-2',
              turnId: 'turn-2',
              kind: 'agent',
              status: 'running',
              name: 'Agent',
              runId: 'run-2',
              namespace: [],
              startedAt: '2026-08-31T00:01:00Z',
              startedSeq: 2,
              updatedSeq: 2,
              requestOmitted: false,
              resultOmitted: false,
              hooks: [],
            }],
            removes: [],
            facets: {
              ...page.facets,
              kinds: { ...page.facets.kinds, agent: 1 },
            },
            completeness: page.completeness,
          },
        }
        await new Promise<void>((resolve) => {
          if (options.signal?.aborted) resolve()
          else options.signal?.addEventListener('abort', () => resolve(), { once: true })
        })
      }
    )())
    const { result, unmount } = renderHook(() => useChainTrace({
      threadId: 'thread-1',
      active: true,
      filter: {},
    }))

    await waitFor(() => {
      expect(result.current.state.phase).toBe('ready')
      if (result.current.state.phase === 'ready') {
        expect(result.current.state.page.turns.map((turn) => turn.id)).toEqual([
          'turn-1',
          'turn-2',
        ])
        expect(result.current.state.page.items.map((item) => item.id)).toEqual([
          'agent-2',
          'running-tool',
        ])
      }
    })
    unmount()
  })
})
