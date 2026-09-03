import { useEffect, useMemo, useRef, useState } from 'react'

import {
  followTraceGraph,
  queryTraceGraphPage,
  type TraceGraphDelta,
  type TraceGraphFilter,
  type TraceGraphPage,
} from '../../../api/conversation/traceGraph'
import { ConversationError } from '../../../api/conversation/errors'

export type ChainTraceState =
  | { phase: 'idle' }
  | { phase: 'loading' }
  | { phase: 'ready'; page: TraceGraphPage }
  | { phase: 'error' }

const applyUpdate = (
  page: TraceGraphPage,
  update: TraceGraphDelta,
): TraceGraphPage => {
  if (update.asOfSeq <= page.asOfSeq) return page
  const turns = new Map(page.turns.map((turn) => [turn.id, turn]))
  update.turnRemoves.forEach((turnId) => turns.delete(turnId))
  update.turnUpserts.forEach((turn) => turns.set(turn.id, turn))
  const nodes = new Map(page.nodes.map((node) => [node.id, node]))
  update.nodeRemoves.forEach((nodeId) => nodes.delete(nodeId))
  update.nodeUpserts.forEach((node) => nodes.set(node.id, node))
  const orderedNodes = update.orderedNodeIds.map((nodeId) => {
    const node = nodes.get(nodeId)
    if (!node) throw new ConversationError('stream_event_invalid')
    return node
  })
  if (orderedNodes.length !== nodes.size) {
    throw new ConversationError('stream_event_invalid')
  }
  return {
    ...page,
    asOfSeq: update.asOfSeq,
    nextCursor: update.nextCursor,
    turns: [...turns.values()].sort((left, right) => (
      left.ordinal - right.ordinal || left.id.localeCompare(right.id)
    )),
    nodes: orderedNodes,
    orderedNodeIds: update.orderedNodeIds,
    rootNodeIds: update.rootNodeIds,
    facets: update.facets,
    completeness: update.completeness,
  }
}

export function useChainTrace({
  threadId,
  active,
  filter,
  cursor,
  onCursorExpired,
}: {
  threadId: string
  active: boolean
  filter: TraceGraphFilter
  cursor?: string | null
  onCursorExpired?: () => void
}) {
  const [retryEpoch, setRetryEpoch] = useState(0)
  const [state, setState] = useState<ChainTraceState>({ phase: 'idle' })
  const filterKey = useMemo(() => JSON.stringify(filter), [filter])
  const currentFilter = useRef(filter)
  currentFilter.current = filter

  useEffect(() => {
    if (!active || !threadId) {
      setState({ phase: 'idle' })
      return
    }
    const controller = new AbortController()
    let disposed = false
    const resolvedFilter = currentFilter.current
    setState({ phase: 'loading' })

    const load = async () => {
      try {
        if (cursor) {
          const page = await queryTraceGraphPage(threadId, resolvedFilter, {
            cursor,
            limit: 200,
            signal: controller.signal,
          })
          if (!disposed && !controller.signal.aborted) {
            setState({ phase: 'ready', page })
          }
          return
        }
        for await (const event of followTraceGraph(threadId, resolvedFilter, {
          limit: 200,
          signal: controller.signal,
        })) {
          if (disposed || controller.signal.aborted) return
          if (event.type === 'snapshot') {
            setState({ phase: 'ready', page: event.snapshot })
          } else if (event.type === 'update') {
            setState((current) => {
              if (current.phase !== 'ready') return current
              try {
                return { phase: 'ready', page: applyUpdate(current.page, event.update) }
              } catch {
                return { phase: 'error' }
              }
            })
          } else {
            setState({ phase: 'error' })
          }
        }
        if (!disposed && !controller.signal.aborted) {
          setState({ phase: 'error' })
        }
      } catch {
        if (!disposed && !controller.signal.aborted) {
          if (cursor && onCursorExpired) onCursorExpired()
          else setState({ phase: 'error' })
        }
      }
    }
    // StrictMode 会先同步重放 setup/cleanup；只让仍存活的 Effect 在微任务中建立外部流
    queueMicrotask(() => {
      if (!disposed) void load()
    })
    return () => {
      disposed = true
      controller.abort()
    }
  }, [active, cursor, filterKey, onCursorExpired, retryEpoch, threadId])

  return {
    state,
    retry: () => setRetryEpoch((value) => value + 1),
  }
}
