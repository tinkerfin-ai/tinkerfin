import { act, renderHook, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { TraceGraphPage } from '../../../api/conversation/traceGraph'
import { traceGraphWithNodes, traceGraphNode } from '../../../test/traceFixtures'
import { useTraceModelResponse } from './useTraceModelResponse'

const queryTraceGraph = vi.hoisted(() => vi.fn())
vi.mock('../../../api/conversation/traceGraph', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../../api/conversation/traceGraph')>(),
  queryTraceGraph,
}))

const responsePage = (modelId: string): TraceGraphPage => ({
  ...traceGraphWithNodes([
    traceGraphNode({
      id: `assistant-${modelId}`,
      parentId: null,
      structuralParentId: modelId,
      kind: 'assistant_message',
      name: 'AssistantMessage',
      content: '完整响应',
    }),
  ], 2),
  nextCursor: null,
})

describe('useTraceModelResponse', () => {
  beforeEach(() => queryTraceGraph.mockReset())

  it('stays idle until filtered Model details require a direct query', () => {
    const { result } = renderHook(() => useTraceModelResponse({
      threadId: 'thread-1',
      modelId: 'model-1',
      enabled: false,
      responseRevision: '1',
    }))

    expect(result.current.state.phase).toBe('idle')
    expect(queryTraceGraph).not.toHaveBeenCalled()
  })

  it('loads direct Assistant and Tool children without opening another follower', async () => {
    queryTraceGraph.mockResolvedValue(responsePage('model-1'))
    const { result } = renderHook(() => useTraceModelResponse({
      threadId: 'thread-1',
      modelId: 'model-1',
      enabled: true,
      responseRevision: '1',
    }))

    await waitFor(() => expect(result.current.state.phase).toBe('ready'))
    expect(queryTraceGraph).toHaveBeenCalledWith(
      'thread-1',
      {
        parentId: 'model-1',
        kinds: ['assistant_message', 'tool'],
        includeTechnicalNodes: false,
        includeAncestorNodes: false,
      },
      expect.objectContaining({ limit: 1000, signal: expect.any(AbortSignal) }),
    )
    if (result.current.state.phase === 'ready') {
      expect(result.current.state.entries[0]?.content).toBe('完整响应')
    }
  })

  it('aborts an obsolete query and ignores its late result', async () => {
    let resolveFirst: ((page: TraceGraphPage) => void) | undefined
    const firstResponse = new Promise<TraceGraphPage>((resolve) => {
      resolveFirst = resolve
    })
    queryTraceGraph
      .mockReturnValueOnce(firstResponse)
      .mockResolvedValueOnce(responsePage('model-2'))
    const { result, rerender } = renderHook(
      ({ modelId }: { modelId: string }) => useTraceModelResponse({
        threadId: 'thread-1',
        modelId,
        enabled: true,
        responseRevision: modelId,
      }),
      { initialProps: { modelId: 'model-1' } },
    )

    await waitFor(() => expect(queryTraceGraph).toHaveBeenCalledTimes(1))
    const firstSignal = queryTraceGraph.mock.calls[0]?.[2]?.signal as AbortSignal
    rerender({ modelId: 'model-2' })
    await waitFor(() => expect(result.current.state.phase).toBe('ready'))
    expect(firstSignal.aborted).toBe(true)
    expect(queryTraceGraph.mock.calls.map((args) => args.length)).toEqual([3, 3])
    await act(async () => {
      resolveFirst?.(responsePage('model-1'))
      await Promise.resolve()
    })
    expect(queryTraceGraph.mock.calls.map((args) => args.length)).toEqual([3, 3])
    expect(result.current.state).toMatchObject({ phase: 'ready', modelId: 'model-2' })
  })

  it('refreshes an initially empty response when the selected Model revision advances', async () => {
    queryTraceGraph
      .mockResolvedValueOnce({ ...traceGraphWithNodes([], 1), nextCursor: null })
      .mockResolvedValueOnce(responsePage('model-1'))
    const { result, rerender } = renderHook(
      ({ responseRevision }: { responseRevision: string }) => useTraceModelResponse({
        threadId: 'thread-1',
        modelId: 'model-1',
        enabled: true,
        responseRevision,
      }),
      { initialProps: { responseRevision: '10' } },
    )

    await waitFor(() => expect(result.current.state).toMatchObject({
      phase: 'ready',
      entries: [],
    }))
    rerender({ responseRevision: '11' })
    await waitFor(() => expect(queryTraceGraph).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(result.current.state).toMatchObject({
      phase: 'ready',
      entries: [expect.objectContaining({ id: 'assistant-model-1' })],
    }))
  })

  it('fails closed on a paginated response query and supports retry', async () => {
    queryTraceGraph
      .mockResolvedValueOnce({ ...responsePage('model-1'), nextCursor: 'more' })
      .mockResolvedValueOnce(responsePage('model-1'))
    const { result } = renderHook(() => useTraceModelResponse({
      threadId: 'thread-1',
      modelId: 'model-1',
      enabled: true,
      responseRevision: '1',
    }))

    await waitFor(() => expect(result.current.state.phase).toBe('error'))
    act(() => result.current.retry())
    await waitFor(() => expect(result.current.state.phase).toBe('ready'))
    expect(queryTraceGraph).toHaveBeenCalledTimes(2)
  })

  it('fails closed when the server omitted response details', async () => {
    queryTraceGraph.mockResolvedValue({
      ...responsePage('model-1'),
      completeness: {
        ...responsePage('model-1').completeness,
        detailsOmitted: true,
      },
    })
    const { result } = renderHook(() => useTraceModelResponse({
      threadId: 'thread-1',
      modelId: 'model-1',
      enabled: true,
      responseRevision: '1',
    }))

    await waitFor(() => expect(result.current.state.phase).toBe('error'))
  })
})
