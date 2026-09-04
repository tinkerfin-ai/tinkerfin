import type { JsonValue } from '../../types'
import { requestEventStream, requestJson } from '../shared/http'
import { ConversationError } from './errors'
import { parseJsonSseStream } from './sse'

export type TraceGraphNodeKind =
  | 'human_message'
  | 'assistant_message'
  | 'system_message'
  | 'agent'
  | 'model'
  | 'tool'
  | 'subagent'
  | 'skill'
  | 'middleware'
  | 'memory'
  | 'guardrail'
  | 'retrieval'
  | 'custom'
  | 'plan'
  | 'interaction'
  | 'run'
  | 'runtime_task'

export type TraceGraphNodeStatus =
  | 'running'
  | 'waiting'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'abandoned'
  | 'unknown'

export type TraceGraphLinkIssue =
  | 'missing_parent'
  | 'missing_model_output'
  | 'missing_tool_proposal'
  | 'missing_tool_execution'

export interface TraceGraphFailure {
  errorType: string
  message?: string | null
  code?: string | null
}

export interface TraceGraphTurn {
  id: string
  ordinal: number
  rootNodeId: string
  startedAt: string
}

export interface TraceGraphNode {
  id: string
  turnId: string
  parentId?: string | null
  structuralParentId?: string | null
  kind: TraceGraphNodeKind
  status: TraceGraphNodeStatus
  name: string
  runId: string
  namespace: string[]
  agentName?: string | null
  provider?: string | null
  model?: string | null
  sourceId?: string | null
  startedAt: string
  firstOutputAt?: string | null
  completedAt?: string | null
  startedSeq: number
  updatedSeq: number
  content?: JsonValue | null
  contentOmitted: boolean
  request?: JsonValue | null
  requestOmitted: boolean
  result?: JsonValue | null
  resultOmitted: boolean
  usage?: JsonValue | null
  responseMetadata?: JsonValue | null
  failure?: TraceGraphFailure | null
  className?: string | null
  hooks: string[]
  sourcePath?: string | null
  linkIssues: TraceGraphLinkIssue[]
}

export interface TraceGraphFacets {
  kinds: Partial<Record<TraceGraphNodeKind, number>>
  statuses: Partial<Record<TraceGraphNodeStatus, number>>
  agents: Record<string, number>
  middleware: Record<string, number>
  skills: Record<string, number>
  providers: Record<string, number>
  models: Record<string, number>
}

export interface TraceGraphCompleteness {
  callTrackingMissing: boolean
  relationshipEvidenceMissing: boolean
  detailsOmitted: boolean
}

export interface TraceGraphPage {
  turns: TraceGraphTurn[]
  nodes: TraceGraphNode[]
  orderedNodeIds: string[]
  rootNodeIds: string[]
  matchedNodeIds: string[]
  nextCursor: string | null
  asOfSeq: number
  facets: TraceGraphFacets
  completeness: TraceGraphCompleteness
}

export type TraceGraph = Omit<TraceGraphPage, 'nextCursor'>

export interface TraceGraphDelta {
  asOfSeq: number
  nextCursor: string | null
  turnUpserts: TraceGraphTurn[]
  turnRemoves: string[]
  nodeUpserts: TraceGraphNode[]
  nodeRemoves: string[]
  orderedNodeIds: string[]
  rootNodeIds: string[]
  matchedNodeIds: string[]
  facets: TraceGraphFacets
  completeness: TraceGraphCompleteness
}

export interface TraceGraphFilter {
  kinds?: TraceGraphNodeKind[]
  statuses?: TraceGraphNodeStatus[]
  parentId?: string
  agents?: string[]
  middleware?: string[]
  skills?: string[]
  providers?: string[]
  models?: string[]
  namespaces?: string[][]
  query?: string
  startedAfter?: string
  startedBefore?: string
  includeTechnicalNodes?: boolean
  includeAncestorNodes?: boolean
}

export type TraceGraphEvent =
  | { type: 'snapshot'; snapshot: TraceGraphPage }
  | { type: 'update'; update: TraceGraphDelta }
  | { type: 'error'; code: 'trace_unavailable' }

const isRecord = (value: unknown): value is Record<string, unknown> => (
  value !== null && typeof value === 'object' && !Array.isArray(value)
)

const NODE_KINDS = new Set<TraceGraphNodeKind>([
  'human_message',
  'assistant_message',
  'system_message',
  'agent',
  'model',
  'tool',
  'subagent',
  'skill',
  'middleware',
  'memory',
  'guardrail',
  'retrieval',
  'custom',
  'plan',
  'interaction',
  'run',
  'runtime_task',
])

const NODE_STATUSES = new Set<TraceGraphNodeStatus>([
  'running',
  'waiting',
  'succeeded',
  'failed',
  'cancelled',
  'abandoned',
  'unknown',
])

const LINK_ISSUES = new Set<TraceGraphLinkIssue>([
  'missing_parent',
  'missing_model_output',
  'missing_tool_proposal',
  'missing_tool_execution',
])

const TURN_KEYS = new Set(['id', 'ordinal', 'rootNodeId', 'startedAt'])
const FAILURE_KEYS = new Set(['errorType', 'message', 'code'])
const NODE_KEYS = new Set([
  'id',
  'turnId',
  'parentId',
  'structuralParentId',
  'kind',
  'status',
  'name',
  'runId',
  'namespace',
  'agentName',
  'provider',
  'model',
  'sourceId',
  'startedAt',
  'firstOutputAt',
  'completedAt',
  'startedSeq',
  'updatedSeq',
  'content',
  'contentOmitted',
  'request',
  'requestOmitted',
  'result',
  'resultOmitted',
  'usage',
  'responseMetadata',
  'failure',
  'className',
  'hooks',
  'sourcePath',
  'linkIssues',
])
const FACET_KEYS = new Set([
  'kinds',
  'statuses',
  'agents',
  'middleware',
  'skills',
  'providers',
  'models',
])
const COMPLETENESS_KEYS = new Set([
  'callTrackingMissing',
  'relationshipEvidenceMissing',
  'detailsOmitted',
])
const GRAPH_KEYS = new Set([
  'turns',
  'nodes',
  'orderedNodeIds',
  'rootNodeIds',
  'matchedNodeIds',
  'asOfSeq',
  'facets',
  'completeness',
])
const PAGE_KEYS = new Set([...GRAPH_KEYS, 'nextCursor'])
const DELTA_KEYS = new Set([
  'asOfSeq',
  'nextCursor',
  'turnUpserts',
  'turnRemoves',
  'nodeUpserts',
  'nodeRemoves',
  'orderedNodeIds',
  'rootNodeIds',
  'matchedNodeIds',
  'facets',
  'completeness',
])
const SNAPSHOT_EVENT_KEYS = new Set(['type', 'snapshot'])
const UPDATE_EVENT_KEYS = new Set(['type', 'update'])
const ERROR_EVENT_KEYS = new Set(['type', 'code'])

const hasOnlyKeys = (value: Record<string, unknown>, keys: Set<string>) => (
  Object.keys(value).every((key) => keys.has(key))
)

const isOptionalString = (value: unknown) => (
  value === undefined || value === null || typeof value === 'string'
)

const isTimestamp = (value: unknown) => (
  typeof value === 'string' && Number.isFinite(Date.parse(value))
)

const isOptionalTimestamp = (value: unknown) => (
  value === undefined || value === null || isTimestamp(value)
)

const isStringArray = (value: unknown): value is string[] => (
  Array.isArray(value) && value.every((item) => typeof item === 'string')
)

const isCountRecord = (value: unknown): value is Record<string, number> => (
  isRecord(value)
  && Object.values(value).every((count) => (
    typeof count === 'number' && Number.isSafeInteger(count) && count >= 0
  ))
)

const parseTurn = (value: unknown): TraceGraphTurn => {
  if (!isRecord(value)
    || !hasOnlyKeys(value, TURN_KEYS)
    || typeof value.id !== 'string'
    || !Number.isSafeInteger(value.ordinal)
    || Number(value.ordinal) < 1
    || typeof value.rootNodeId !== 'string'
    || !isTimestamp(value.startedAt)
  ) throw new ConversationError('stream_event_invalid')
  return value as unknown as TraceGraphTurn
}

const parseFailure = (value: unknown): TraceGraphFailure | null | undefined => {
  if (value === undefined || value === null) return value
  if (!isRecord(value)
    || !hasOnlyKeys(value, FAILURE_KEYS)
    || typeof value.errorType !== 'string'
    || !isOptionalString(value.message)
    || !isOptionalString(value.code)
  ) throw new ConversationError('stream_event_invalid')
  return value as unknown as TraceGraphFailure
}

const parseNode = (value: unknown): TraceGraphNode => {
  if (!isRecord(value)
    || !hasOnlyKeys(value, NODE_KEYS)
    || typeof value.id !== 'string'
    || typeof value.turnId !== 'string'
    || typeof value.name !== 'string'
    || typeof value.runId !== 'string'
    || typeof value.kind !== 'string'
    || !NODE_KINDS.has(value.kind as TraceGraphNodeKind)
    || typeof value.status !== 'string'
    || !NODE_STATUSES.has(value.status as TraceGraphNodeStatus)
    || !isStringArray(value.namespace)
    || !isTimestamp(value.startedAt)
    || !Number.isSafeInteger(value.startedSeq)
    || Number(value.startedSeq) < 1
    || !Number.isSafeInteger(value.updatedSeq)
    || Number(value.updatedSeq) < Number(value.startedSeq)
    || !isOptionalString(value.parentId)
    || !isOptionalString(value.structuralParentId)
    || !isOptionalString(value.agentName)
    || !isOptionalString(value.provider)
    || !isOptionalString(value.model)
    || !isOptionalString(value.sourceId)
    || !isOptionalTimestamp(value.firstOutputAt)
    || !isOptionalTimestamp(value.completedAt)
    || typeof value.contentOmitted !== 'boolean'
    || typeof value.requestOmitted !== 'boolean'
    || typeof value.resultOmitted !== 'boolean'
    || !isOptionalString(value.className)
    || !isOptionalString(value.sourcePath)
    || !isStringArray(value.hooks)
    || !Array.isArray(value.linkIssues)
    || value.linkIssues.some((item) => (
      typeof item !== 'string' || !LINK_ISSUES.has(item as TraceGraphLinkIssue)
    ))
  ) throw new ConversationError('stream_event_invalid')
  parseFailure(value.failure)
  return value as unknown as TraceGraphNode
}

const parseFacets = (value: unknown): TraceGraphFacets => {
  if (!isRecord(value)
    || !hasOnlyKeys(value, FACET_KEYS)
    || !isCountRecord(value.kinds)
    || Object.keys(value.kinds).some((kind) => !NODE_KINDS.has(kind as TraceGraphNodeKind))
    || !isCountRecord(value.statuses)
    || Object.keys(value.statuses).some((status) => !NODE_STATUSES.has(status as TraceGraphNodeStatus))
    || !isCountRecord(value.agents)
    || !isCountRecord(value.middleware)
    || !isCountRecord(value.skills)
    || !isCountRecord(value.providers)
    || !isCountRecord(value.models)
  ) throw new ConversationError('stream_event_invalid')
  return value as unknown as TraceGraphFacets
}

const parseCompleteness = (value: unknown): TraceGraphCompleteness => {
  if (!isRecord(value)
    || !hasOnlyKeys(value, COMPLETENESS_KEYS)
    || typeof value.callTrackingMissing !== 'boolean'
    || typeof value.relationshipEvidenceMissing !== 'boolean'
    || typeof value.detailsOmitted !== 'boolean'
  ) throw new ConversationError('stream_event_invalid')
  return value as unknown as TraceGraphCompleteness
}

const uniqueIds = (ids: string[]) => new Set(ids).size === ids.length

const parseTraceGraphValue = (
  value: unknown,
  allowedKeys: Set<string>,
): TraceGraph => {
  if (!isRecord(value)
    || !hasOnlyKeys(value, allowedKeys)
    || !Array.isArray(value.turns)
    || !Array.isArray(value.nodes)
    || !isStringArray(value.orderedNodeIds)
    || !isStringArray(value.rootNodeIds)
    || !isStringArray(value.matchedNodeIds)
    || !Number.isSafeInteger(value.asOfSeq)
    || Number(value.asOfSeq) < 1
  ) throw new ConversationError('stream_event_invalid')
  const turns = value.turns.map(parseTurn)
  const nodes = value.nodes.map(parseNode)
  const nodeIds = nodes.map((node) => node.id)
  const nodeIdSet = new Set(nodeIds)
  const orderedNodeIds = value.orderedNodeIds
  const rootNodeIds = value.rootNodeIds
  const matchedNodeIds = value.matchedNodeIds
  const matchedNodeIdSet = new Set(matchedNodeIds)
  if (!uniqueIds(nodeIds)
    || !uniqueIds(orderedNodeIds)
    || orderedNodeIds.length !== nodeIds.length
    || orderedNodeIds.some((id) => !nodeIdSet.has(id))
    || !uniqueIds(rootNodeIds)
    || rootNodeIds.some((id) => !nodeIdSet.has(id))
    || !uniqueIds(matchedNodeIds)
    || matchedNodeIds.some((id) => !nodeIdSet.has(id))
    || orderedNodeIds.filter((id) => matchedNodeIdSet.has(id))
      .some((id, index) => matchedNodeIds[index] !== id)
    || nodes.some((node) => node.parentId && !nodeIdSet.has(node.parentId))
  ) throw new ConversationError('stream_event_invalid')
  return {
    turns,
    nodes,
    orderedNodeIds,
    rootNodeIds,
    matchedNodeIds,
    asOfSeq: value.asOfSeq as number,
    facets: parseFacets(value.facets),
    completeness: parseCompleteness(value.completeness),
  }
}

export const parseTraceGraph = (value: unknown): TraceGraph => (
  parseTraceGraphValue(value, GRAPH_KEYS)
)

export const parseTraceGraphPage = (value: unknown): TraceGraphPage => {
  if (!isRecord(value)
    || (value.nextCursor !== null && typeof value.nextCursor !== 'string')
  ) throw new ConversationError('stream_event_invalid')
  return {
    ...parseTraceGraphValue(value, PAGE_KEYS),
    nextCursor: value.nextCursor,
  }
}

export const parseTraceGraphDelta = (value: unknown): TraceGraphDelta => {
  if (!isRecord(value)
    || !hasOnlyKeys(value, DELTA_KEYS)
    || !Number.isSafeInteger(value.asOfSeq)
    || Number(value.asOfSeq) < 1
    || (value.nextCursor !== null && typeof value.nextCursor !== 'string')
    || !Array.isArray(value.turnUpserts)
    || !isStringArray(value.turnRemoves)
    || !Array.isArray(value.nodeUpserts)
    || !isStringArray(value.nodeRemoves)
    || !isStringArray(value.orderedNodeIds)
    || !isStringArray(value.rootNodeIds)
    || !isStringArray(value.matchedNodeIds)
  ) throw new ConversationError('stream_event_invalid')
  const orderedNodeIds = value.orderedNodeIds
  const rootNodeIds = value.rootNodeIds
  const matchedNodeIds = value.matchedNodeIds
  const orderedNodeIdSet = new Set(orderedNodeIds)
  const matchedNodeIdSet = new Set(matchedNodeIds)
  if (!uniqueIds(orderedNodeIds)
    || !uniqueIds(rootNodeIds)
    || !uniqueIds(matchedNodeIds)
    || rootNodeIds.some((id) => !orderedNodeIdSet.has(id))
    || matchedNodeIds.some((id) => !orderedNodeIdSet.has(id))
    || orderedNodeIds.filter((id) => matchedNodeIdSet.has(id))
      .some((id, index) => matchedNodeIds[index] !== id)
  ) throw new ConversationError('stream_event_invalid')
  return {
    asOfSeq: value.asOfSeq as number,
    nextCursor: value.nextCursor,
    turnUpserts: value.turnUpserts.map(parseTurn),
    turnRemoves: value.turnRemoves,
    nodeUpserts: value.nodeUpserts.map(parseNode),
    nodeRemoves: value.nodeRemoves,
    orderedNodeIds,
    rootNodeIds,
    matchedNodeIds,
    facets: parseFacets(value.facets),
    completeness: parseCompleteness(value.completeness),
  }
}

const parseTraceGraphEvent = (value: unknown): TraceGraphEvent => {
  if (!isRecord(value)) throw new ConversationError('stream_event_invalid')
  if (value.type === 'snapshot' && hasOnlyKeys(value, SNAPSHOT_EVENT_KEYS)) {
    return { type: 'snapshot', snapshot: parseTraceGraphPage(value.snapshot) }
  }
  if (value.type === 'update' && hasOnlyKeys(value, UPDATE_EVENT_KEYS)) {
    return { type: 'update', update: parseTraceGraphDelta(value.update) }
  }
  if (value.type === 'error'
    && value.code === 'trace_unavailable'
    && hasOnlyKeys(value, ERROR_EVENT_KEYS)
  ) {
    return { type: 'error', code: 'trace_unavailable' }
  }
  throw new ConversationError('stream_event_invalid')
}

const appendFilter = (search: URLSearchParams, filter: TraceGraphFilter) => {
  filter.kinds?.forEach((value) => search.append('kind', value))
  filter.statuses?.forEach((value) => search.append('status', value))
  if (filter.parentId) search.set('parentId', filter.parentId)
  filter.agents?.forEach((value) => search.append('agent', value))
  filter.middleware?.forEach((value) => search.append('middleware', value))
  filter.skills?.forEach((value) => search.append('skill', value))
  filter.providers?.forEach((value) => search.append('provider', value))
  filter.models?.forEach((value) => search.append('model', value))
  filter.namespaces?.forEach((value) => search.append(
    'namespace',
    value.length === 0 ? 'root' : value.join('|'),
  ))
  if (filter.query) search.set('query', filter.query)
  if (filter.startedAfter) search.set('startedAfter', filter.startedAfter)
  if (filter.startedBefore) search.set('startedBefore', filter.startedBefore)
  search.set('includeTechnicalNodes', String(filter.includeTechnicalNodes ?? false))
  search.set('includeAncestorNodes', String(filter.includeAncestorNodes ?? true))
}

const graphUrl = (
  threadId: string,
  filter: TraceGraphFilter,
  options: { follow: boolean; limit: number },
) => {
  const search = new URLSearchParams()
  appendFilter(search, filter)
  search.set('limit', String(options.limit))
  const suffix = options.follow ? '/follow' : ''
  return `/api/conversation/${encodeURIComponent(threadId)}/trace/graph${suffix}?${search}`
}

export const queryTraceGraph = async (
  threadId: string,
  filter: TraceGraphFilter,
  options: { limit?: number; signal?: AbortSignal } = {},
): Promise<TraceGraphPage> => parseTraceGraphPage(await requestJson<unknown>(
  graphUrl(threadId, filter, {
    follow: false,
    limit: options.limit ?? 100,
  }),
  { signal: options.signal, suppressGlobalError: true },
))

export async function* followTraceGraph(
  threadId: string,
  filter: TraceGraphFilter,
  options: { limit?: number; signal?: AbortSignal } = {},
): AsyncGenerator<TraceGraphEvent> {
  const response = await requestEventStream(
    graphUrl(threadId, filter, {
      follow: true,
      limit: options.limit ?? 100,
    }),
    { signal: options.signal, suppressGlobalError: true },
  )
  if (!response.body) throw new ConversationError('stream_body_missing')
  for await (const frame of parseJsonSseStream(response.body, options.signal)) {
    yield parseTraceGraphEvent(frame.data)
  }
}
