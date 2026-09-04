import type {
  TraceGraphNode,
  TraceGraphTurn,
} from '../../../api/conversation/traceGraph'
import {
  traceVisualCategory,
  type TraceVisualCategory,
} from './tracePresentation'

export type TraceTimelineLaneId = TraceVisualCategory

export interface TraceTimelineBar {
  node: TraceGraphNode
  track: number
  startMilliseconds: number
  endMilliseconds: number
  leftPercent: number
  widthPercent: number
}

export interface TraceTimelineLane {
  id: TraceTimelineLaneId
  trackCount: number
  bars: TraceTimelineBar[]
}

export interface TraceTimelineLayout {
  startedAt: number
  completedAt: number
  durationMilliseconds: number
  ticks: number[]
  lanes: TraceTimelineLane[]
}

export interface TraceTurnRows {
  turn: TraceGraphTurn
  nodes: TraceGraphNode[]
}

const LANE_ORDER: readonly TraceTimelineLaneId[] = [
  'user',
  'assistant',
  'tool',
  'context',
  'technical',
]

const nodeTimes = (node: TraceGraphNode, now: number) => {
  const start = Date.parse(node.startedAt)
  const end = node.completedAt
    ? Date.parse(node.completedAt)
    : node.status === 'running' || node.status === 'waiting' ? now : start
  return { start, end: Math.max(start, end) }
}

export const buildTraceTimelineLayout = (
  nodes: readonly TraceGraphNode[],
  now: number,
): TraceTimelineLayout | null => {
  if (nodes.length === 0) return null
  let startedAt = Number.POSITIVE_INFINITY
  let completedAt = Number.NEGATIVE_INFINITY
  const timed = nodes.map((node) => {
    const times = nodeTimes(node, now)
    startedAt = Math.min(startedAt, times.start)
    completedAt = Math.max(completedAt, times.end)
    return { node, ...times }
  })
  const durationMilliseconds = Math.max(1, completedAt - startedAt)
  const grouped = new Map<TraceTimelineLaneId, typeof timed>()
  timed.forEach((item) => {
    const lane = traceVisualCategory(item.node.kind)
    grouped.set(lane, [...(grouped.get(lane) ?? []), item])
  })
  const lanes = LANE_ORDER.flatMap((id) => {
    const values = grouped.get(id)
    if (!values?.length) return []
    const trackEnds: number[] = []
    const bars = [...values]
      .sort((left, right) => (
        left.start - right.start
        || left.node.startedSeq - right.node.startedSeq
        || left.node.id.localeCompare(right.node.id)
      ))
      .map(({ node, start, end }) => {
        const occupiedUntil = Math.max(start + 1, end)
        let track = trackEnds.findIndex((value) => value <= start)
        if (track < 0) track = trackEnds.length
        trackEnds[track] = occupiedUntil
        return {
          node,
          track,
          startMilliseconds: start - startedAt,
          endMilliseconds: end - startedAt,
          leftPercent: (start - startedAt) / durationMilliseconds * 100,
          widthPercent: (end - start) / durationMilliseconds * 100,
        }
      })
    return [{ id, bars, trackCount: trackEnds.length } satisfies TraceTimelineLane]
  })
  return {
    startedAt,
    completedAt,
    durationMilliseconds,
    ticks: Array.from({ length: 7 }, (_, index) => durationMilliseconds * index / 6),
    lanes,
  }
}

export const groupTraceNodesByTurn = (
  turns: readonly TraceGraphTurn[],
  nodes: readonly TraceGraphNode[],
): TraceTurnRows[] => {
  const byTurn = new Map<string, TraceGraphNode[]>()
  nodes.forEach((node) => byTurn.set(node.turnId, [
    ...(byTurn.get(node.turnId) ?? []),
    node,
  ]))
  return [...turns]
    .sort((left, right) => left.ordinal - right.ordinal || left.id.localeCompare(right.id))
    .flatMap((turn) => {
      const values = byTurn.get(turn.id)
      if (!values?.length) return []
      return [{
        turn,
        nodes: [...values].sort((left, right) => (
          left.startedSeq - right.startedSeq || left.id.localeCompare(right.id)
        )),
      }]
    })
}

const latest = (nodes: readonly TraceGraphNode[]) => nodes.reduce<TraceGraphNode | undefined>(
  (current, node) => !current
    || node.startedSeq > current.startedSeq
    || (node.startedSeq === current.startedSeq && node.id > current.id)
    ? node
    : current,
  undefined,
)

export const preferredTraceNode = (
  nodes: readonly TraceGraphNode[],
): TraceGraphNode | undefined => latest(
  nodes.filter((node) => node.failure || node.status === 'failed'),
) ?? latest(nodes)
