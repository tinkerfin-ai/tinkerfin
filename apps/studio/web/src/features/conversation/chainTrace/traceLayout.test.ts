import { describe, expect, it } from 'vitest'

import { traceGraphNode } from '../../../test/traceFixtures'
import { buildTraceTimelineLayout, groupTraceNodesByTurn, preferredTraceNode } from './traceLayout'

const startedAt = '2026-09-03T00:00:00.000Z'

describe('Chain Trace layout derivation', () => {
  it('packs overlapping intervals into deterministic tracks and retains zero-duration nodes', () => {
    const layout = buildTraceTimelineLayout([
      traceGraphNode({ id: 'model-a', kind: 'model', startedAt, completedAt: '2026-09-03T00:00:02.000Z' }),
      traceGraphNode({ id: 'model-b', kind: 'model', startedSeq: 2, startedAt: '2026-09-03T00:00:01.000Z', completedAt: '2026-09-03T00:00:03.000Z' }),
      traceGraphNode({ id: 'message', kind: 'assistant_message', startedSeq: 3, startedAt: '2026-09-03T00:00:03.000Z', completedAt: '2026-09-03T00:00:03.000Z' }),
    ], Date.parse('2026-09-03T00:00:04.000Z'))

    const technical = layout?.lanes.find((lane) => lane.id === 'technical')
    const assistant = layout?.lanes.find((lane) => lane.id === 'assistant')
    expect(technical?.trackCount).toBe(2)
    expect(technical?.bars.map((bar) => bar.track)).toEqual([0, 1])
    expect(assistant?.trackCount).toBe(1)
    expect(assistant?.bars.find((bar) => bar.node.id === 'message')?.widthPercent).toBe(0)
  })

  it('extends running nodes to now and groups rows by Turn and sequence', () => {
    const now = Date.parse('2026-09-03T00:00:05.000Z')
    const running = traceGraphNode({
      id: 'running',
      status: 'running',
      startedAt,
      completedAt: null,
    })
    expect(buildTraceTimelineLayout([running], now)?.durationMilliseconds).toBe(5000)
    expect(groupTraceNodesByTurn([
      { id: 'turn', ordinal: 1, rootNodeId: 'first', startedAt },
    ], [
      traceGraphNode({ id: 'second', turnId: 'turn', startedSeq: 2 }),
      traceGraphNode({ id: 'first', turnId: 'turn', startedSeq: 1 }),
    ])[0]?.nodes.map((node) => node.id)).toEqual(['first', 'second'])
  })

  it('prefers the latest failure and otherwise the latest node', () => {
    const nodes = [
      traceGraphNode({ id: 'success-latest', startedSeq: 4 }),
      traceGraphNode({ id: 'failure-old', startedSeq: 2, status: 'failed' }),
      traceGraphNode({ id: 'failure-new', startedSeq: 3, failure: { errorType: 'Error' } }),
    ]
    expect(preferredTraceNode(nodes)?.id).toBe('failure-new')
    expect(preferredTraceNode(nodes.filter((node) => !node.failure && node.status !== 'failed'))?.id)
      .toBe('success-latest')
  })
})
