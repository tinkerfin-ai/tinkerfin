import { useEffect, useMemo, useRef, useState } from 'react'

import {
  followTraceEntries,
  type TraceEntryDelta,
  type TraceEntryFilter,
  type TraceEntryPage,
} from '../../../api/conversation/traceEntries'

export type ChainTraceState =
  | { phase: 'idle' }
  | { phase: 'loading' }
  | { phase: 'ready'; page: TraceEntryPage }
  | { phase: 'error' }

const applyUpdate = (
  page: TraceEntryPage,
  update: TraceEntryDelta,
): TraceEntryPage => {
  if (update.asOfSeq <= page.asOfSeq) return page
  const turns = new Map(page.turns.map((turn) => [turn.id, turn]))
  update.turnRemoves.forEach((turnId) => turns.delete(turnId))
  update.turnUpserts.forEach((turn) => turns.set(turn.id, turn))
  const items = new Map(page.items.map((item) => [item.id, item]))
  update.removes.forEach((entryId) => items.delete(entryId))
  update.upserts.forEach((entry) => items.set(entry.id, entry))
  return {
    ...page,
    asOfSeq: update.asOfSeq,
    turns: [...turns.values()].sort((left, right) => (
      left.ordinal - right.ordinal || left.id.localeCompare(right.id)
    )),
    // 开始时间相同时保留服务端顺序，避免实时更新改变分页顺序
    items: [...items.values()].sort((left, right) => (
      right.startedAt.localeCompare(left.startedAt)
    )),
    facets: update.facets,
    completeness: update.completeness,
  }
}

export function useChainTrace({
  threadId,
  active,
  filter,
}: {
  threadId: string
  active: boolean
  filter: TraceEntryFilter
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

    const follow = async () => {
      try {
        for await (const event of followTraceEntries(threadId, resolvedFilter, {
          limit: 200,
          signal: controller.signal,
        })) {
          if (disposed || controller.signal.aborted) return
          if (event.type === 'snapshot') {
            setState({ phase: 'ready', page: event.snapshot })
          } else if (event.type === 'update') {
            setState((current) => current.phase === 'ready'
              ? { phase: 'ready', page: applyUpdate(current.page, event.update) }
              : current)
          } else {
            setState({ phase: 'error' })
          }
        }
        if (!disposed && !controller.signal.aborted) {
          setState({ phase: 'error' })
        }
      } catch {
        if (!disposed && !controller.signal.aborted) {
          setState({ phase: 'error' })
        }
      }
    }
    // StrictMode 会先同步重放 setup/cleanup；只让仍存活的 Effect 在微任务中建立外部流
    queueMicrotask(() => {
      if (!disposed) void follow()
    })
    return () => {
      disposed = true
      controller.abort()
    }
  }, [active, filterKey, retryEpoch, threadId])

  return {
    state,
    retry: () => setRetryEpoch((value) => value + 1),
  }
}
