import { useEffect, useMemo, useRef, useState } from 'react'

import {
  followTraceGraph,
  parseTraceGraphPage,
  queryTraceGraph,
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
  if (update.asOfSeq <= page.asOfSeq) {
    throw new ConversationError('stream_event_invalid')
  }
  const turns = new Map(page.turns.map((turn) => [turn.id, turn]))
  update.turnRemoves.forEach((turnId) => turns.delete(turnId))
  update.turnUpserts.forEach((turn) => {
    const current = turns.get(turn.id)
    if (current && (
      current.ordinal !== turn.ordinal
      || current.startedAt !== turn.startedAt
    )) throw new ConversationError('stream_event_invalid')
    turns.set(turn.id, turn)
  })
  const nodes = new Map(page.nodes.map((node) => [node.id, node]))
  update.nodeRemoves.forEach((nodeId) => nodes.delete(nodeId))
  update.nodeUpserts.forEach((node) => {
    const current = nodes.get(node.id)
    if (current && (
      node.updatedSeq < current.updatedSeq
      || node.turnId !== current.turnId
      || node.kind !== current.kind
      || node.name !== current.name
      || JSON.stringify(node.graphNamespace) !== JSON.stringify(current.graphNamespace)
    )) throw new ConversationError('stream_event_invalid')
    nodes.set(node.id, node)
  })
  const orderedNodes = update.orderedNodeIds.map((nodeId) => {
    const node = nodes.get(nodeId)
    if (!node) throw new ConversationError('stream_event_invalid')
    return node
  })
  if (orderedNodes.length !== nodes.size) {
    throw new ConversationError('stream_event_invalid')
  }
  return parseTraceGraphPage({
    ...page,
    asOfSeq: update.asOfSeq,
    nextCursor: update.nextCursor,
    turns: [...turns.values()].sort((left, right) => (
      left.ordinal - right.ordinal || left.id.localeCompare(right.id)
    )),
    nodes: orderedNodes,
    orderedNodeIds: update.orderedNodeIds,
    matchedNodeIds: update.matchedNodeIds,
    completeness: update.completeness,
  })
}

export function useChainTrace({
  threadId,
  active,
  live,
  observedAt,
  filter,
  limit,
}: {
  threadId: string
  active: boolean
  live: boolean
  /** 会话权威观测到达后，重新读取终态链路以补齐最后提交的节点 */
  observedAt?: string
  filter: TraceGraphFilter
  limit: number
}) {
  const [retryEpoch, setRetryEpoch] = useState(0)
  const [visible, setVisible] = useState(document.visibilityState !== 'hidden')
  const [state, setState] = useState<ChainTraceState>({ phase: 'idle' })
  const filterKey = useMemo(() => JSON.stringify(filter), [filter])
  const currentFilter = useRef(filter)
  currentFilter.current = filter
  const snapshotObservedAt = live ? undefined : observedAt
  const displayedQuery = useRef<string | null>(null)

  useEffect(() => {
    const updateVisibility = () => setVisible(document.visibilityState !== 'hidden')
    document.addEventListener('visibilitychange', updateVisibility)
    return () => document.removeEventListener('visibilitychange', updateVisibility)
  }, [])

  useEffect(() => {
    if (!active || !visible || !threadId) {
      setState({ phase: 'idle' })
      return
    }
    const controller = new AbortController()
    let disposed = false
    let currentPage: TraceGraphPage | undefined
    const resolvedFilter = currentFilter.current
    const queryKey = JSON.stringify([threadId, filterKey, limit])
    const sameQuery = displayedQuery.current === queryKey
    displayedQuery.current = queryKey
    // 同一查询刷新时保留阅读内容，新快照到达后再替换；查询范围变化则显示加载状态
    setState(current => sameQuery && current.phase === 'ready' ? current : { phase: 'loading' })

    const load = async () => {
      try {
        if (!live) {
          const page = await queryTraceGraph(threadId, resolvedFilter, {
            limit,
            signal: controller.signal,
          })
          if (!disposed && !controller.signal.aborted) setState({ phase: 'ready', page })
          return
        }
        for await (const event of followTraceGraph(threadId, resolvedFilter, {
          limit,
          signal: controller.signal,
        })) {
          if (disposed || controller.signal.aborted) return
          if (event.type === 'snapshot') {
            if (currentPage) throw new ConversationError('stream_event_invalid')
            currentPage = event.snapshot
            setState({ phase: 'ready', page: event.snapshot })
          } else if (event.type === 'update') {
            if (!currentPage) throw new ConversationError('stream_event_invalid')
            currentPage = applyUpdate(currentPage, event.update)
            setState({ phase: 'ready', page: currentPage })
          } else {
            controller.abort()
            setState({ phase: 'error' })
            return
          }
        }
        if (!disposed && !controller.signal.aborted) {
          setState({ phase: 'error' })
        }
      } catch {
        if (!disposed) {
          controller.abort()
          setState({ phase: 'error' })
        }
      }
    }
    // StrictMode 会先同步重放 setup/cleanup；只让仍存活的 Effect 发起读取
    queueMicrotask(() => {
      if (!disposed) void load()
    })
    return () => {
      disposed = true
      controller.abort()
    }
  }, [active, filterKey, limit, live, retryEpoch, snapshotObservedAt, threadId, visible])

  return {
    state,
    retry: () => setRetryEpoch((value) => value + 1),
  }
}
