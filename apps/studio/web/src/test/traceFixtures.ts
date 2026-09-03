import type {
  TraceGraph,
  TraceGraphDelta,
  TraceGraphFacets,
  TraceGraphNode,
} from '../api/conversation/traceGraph'

const EMPTY_FACETS: TraceGraphFacets = {
  kinds: {},
  statuses: {},
  agents: {},
  middleware: {},
  skills: {},
  providers: {},
  models: {},
}

export const emptyTraceGraph = (asOfSeq: number): TraceGraph => ({
  turns: [],
  nodes: [],
  orderedNodeIds: [],
  rootNodeIds: [],
  asOfSeq,
  facets: structuredClone(EMPTY_FACETS),
  completeness: {
    callTrackingMissing: false,
    relationshipEvidenceMissing: false,
    detailsOmitted: false,
  },
})

export const emptyTraceGraphDelta = (asOfSeq: number): TraceGraphDelta => ({
  asOfSeq,
  nextCursor: null,
  turnUpserts: [],
  turnRemoves: [],
  nodeUpserts: [],
  nodeRemoves: [],
  orderedNodeIds: [],
  rootNodeIds: [],
  facets: structuredClone(EMPTY_FACETS),
  completeness: {
    callTrackingMissing: false,
    relationshipEvidenceMissing: false,
    detailsOmitted: false,
  },
})

export const traceGraphNode = (
  overrides: Partial<TraceGraphNode> & Pick<TraceGraphNode, 'id'>,
): TraceGraphNode => {
  const startedSeq = overrides.startedSeq ?? 1
  return {
    turnId: 'turn-fixture',
    parentId: null,
    structuralParentId: null,
    kind: 'tool',
    status: 'succeeded',
    name: 'tool',
    runId: 'run-fixture',
    namespace: [],
    startedAt: '2026-08-28T00:00:00Z',
    startedSeq,
    updatedSeq: overrides.updatedSeq ?? startedSeq,
    contentOmitted: false,
    requestOmitted: false,
    resultOmitted: false,
    hooks: [],
    linkIssues: [],
    ...overrides,
  }
}

export const traceGraphWithNodes = (
  nodes: TraceGraphNode[],
  asOfSeq: number,
): TraceGraph => {
  const nodeIds = new Set(nodes.map((node) => node.id))
  const orderedNodes = nodes.map((node) => ({
    ...node,
    parentId: node.parentId && nodeIds.has(node.parentId) ? node.parentId : null,
  })).sort((left, right) => (
    left.startedSeq - right.startedSeq || left.id.localeCompare(right.id)
  ))
  const kinds: TraceGraphFacets['kinds'] = {}
  const statuses: TraceGraphFacets['statuses'] = {}
  orderedNodes.forEach((node) => {
    kinds[node.kind] = (kinds[node.kind] ?? 0) + 1
    statuses[node.status] = (statuses[node.status] ?? 0) + 1
  })
  const rootNodeIds = orderedNodes
    .filter((node) => node.parentId == null)
    .map((node) => node.id)
  return {
    turns: orderedNodes.length === 0
      ? []
      : [{
          id: 'turn-fixture',
          ordinal: 1,
          rootNodeId: rootNodeIds[0] ?? orderedNodes[0]!.id,
          startedAt: orderedNodes[0]!.startedAt,
        }],
    nodes: orderedNodes,
    orderedNodeIds: orderedNodes.map((node) => node.id),
    rootNodeIds,
    asOfSeq,
    facets: { ...structuredClone(EMPTY_FACETS), kinds, statuses },
    completeness: {
      callTrackingMissing: false,
      relationshipEvidenceMissing: false,
      detailsOmitted: false,
    },
  }
}
