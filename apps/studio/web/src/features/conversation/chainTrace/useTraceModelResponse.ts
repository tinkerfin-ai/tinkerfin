import { useEffect, useState } from 'react'

import {
  queryTraceGraph,
  type TraceGraphNode,
} from '../../../api/conversation/traceGraph'

export type TraceModelResponseState =
  | { phase: 'idle' }
  | { phase: 'loading'; modelId: string; responseRevision: string }
  | { phase: 'ready'; modelId: string; responseRevision: string; entries: TraceGraphNode[] }
  | { phase: 'error'; modelId: string; responseRevision: string }

export function useTraceModelResponse({
  threadId,
  modelId,
  enabled,
  responseRevision,
}: {
  threadId: string
  modelId?: string
  enabled: boolean
  responseRevision: string
}) {
  const [retryEpoch, setRetryEpoch] = useState(0)
  const [state, setState] = useState<TraceModelResponseState>({ phase: 'idle' })

  useEffect(() => {
    if (!enabled || !threadId || !modelId) {
      setState({ phase: 'idle' })
      return
    }
    const controller = new AbortController()
    setState({ phase: 'loading', modelId, responseRevision })
    void queryTraceGraph(threadId, {
      parentId: modelId,
      kinds: ['assistant_message', 'tool'],
      includeTechnicalNodes: false,
      includeAncestorNodes: false,
    }, {
      limit: 1000,
      signal: controller.signal,
    }).then((page) => {
      if (controller.signal.aborted) return
      if (page.nextCursor != null || page.completeness.detailsOmitted) {
        setState({ phase: 'error', modelId, responseRevision })
        return
      }
      setState({ phase: 'ready', modelId, responseRevision, entries: page.nodes })
    }).catch(() => {
      if (!controller.signal.aborted) {
        setState({ phase: 'error', modelId, responseRevision })
      }
    })
    return () => controller.abort()
  }, [enabled, modelId, responseRevision, retryEpoch, threadId])

  return {
    state,
    retry: () => setRetryEpoch((value) => value + 1),
  }
}
