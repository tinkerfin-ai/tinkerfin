import type { JsonValue } from '../../types'
import { requestEventStream } from '../shared/http'
import { ConversationError } from './errors'
import type { TraceMessage } from './history'
import { parseJsonSseStream } from './sse'

export type TraceEntryKind =
  | 'agent'
  | 'run'
  | 'model'
  | 'provider'
  | 'tools'
  | 'tool_proposal'
  | 'tool'
  | 'subagent'
  | 'skill'
  | 'middleware'
  | 'memory'
  | 'guardrail'
  | 'retrieval'
  | 'custom'
  | 'task'

export type TraceEntryStatus =
  | 'configured'
  | 'running'
  | 'waiting'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'abandoned'
  | 'unknown'

export interface TraceEntry {
  id: string
  turnId: string
  parentId?: string | null
  proposalId?: string | null
  kind: TraceEntryKind
  status: TraceEntryStatus
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
  request?: JsonValue | null
  requestOmitted: boolean
  result?: JsonValue | null
  resultOmitted: boolean
  usage?: JsonValue | null
  responseMetadata?: JsonValue | null
  failure?: TraceFailure | null
  className?: string | null
  hooks: string[]
  sourcePath?: string | null
}

export interface TraceFailure {
  errorType: string
  message?: string | null
  code?: string | null
}

export interface TraceTurn {
  id: string
  ordinal: number
  startedAt: string
  userMessage?: TraceMessage | null
}

export interface TraceFacets {
  kinds: Partial<Record<TraceEntryKind, number>>
  statuses: Partial<Record<TraceEntryStatus, number>>
  agents: Record<string, number>
  middleware: Record<string, number>
  skills: Record<string, number>
  providers: Record<string, number>
  models: Record<string, number>
}

export interface TraceEntryCompleteness {
  callTrackingMissing: boolean
  executionTreeMissing: boolean
}

export interface TraceEntryPage {
  turns: TraceTurn[]
  items: TraceEntry[]
  nextCursor?: string | null
  asOfSeq: number
  facets: TraceFacets
  completeness: TraceEntryCompleteness
}

export interface TraceEntryDelta {
  asOfSeq: number
  turnUpserts: TraceTurn[]
  turnRemoves: string[]
  upserts: TraceEntry[]
  removes: string[]
  facets: TraceFacets
  completeness: TraceEntryCompleteness
}

export interface TraceEntryFilter {
  kinds?: TraceEntryKind[]
  statuses?: TraceEntryStatus[]
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
  includeAncestors?: boolean
}

export type TraceEntryEvent =
  | { type: 'snapshot'; snapshot: TraceEntryPage }
  | { type: 'update'; update: TraceEntryDelta }
  | { type: 'error'; code: 'trace_unavailable' }

const isRecord = (value: unknown): value is Record<string, unknown> => (
  value !== null && typeof value === 'object' && !Array.isArray(value)
)

const ENTRY_KINDS = new Set<TraceEntryKind>([
  'agent',
  'run',
  'model',
  'provider',
  'tools',
  'tool_proposal',
  'tool',
  'subagent',
  'skill',
  'middleware',
  'memory',
  'guardrail',
  'retrieval',
  'custom',
  'task',
])

const ENTRY_STATUSES = new Set<TraceEntryStatus>([
  'configured',
  'running',
  'waiting',
  'succeeded',
  'failed',
  'cancelled',
  'abandoned',
  'unknown',
])

const isOptionalString = (value: unknown) => (
  value === undefined || value === null || typeof value === 'string'
)

const isOptionalTimestamp = (value: unknown) => (
  value === undefined
  || value === null
  || (typeof value === 'string' && Number.isFinite(Date.parse(value)))
)

const isCountRecord = (value: unknown): value is Record<string, number> => (
  isRecord(value)
  && Object.values(value).every((count) => (
    typeof count === 'number' && Number.isSafeInteger(count) && count >= 0
  ))
)

const MESSAGE_ROLES = new Set(['user', 'assistant', 'tool', 'system', 'other'])
const MESSAGE_STATUSES = new Set(['streaming', 'completed'])

const parseTraceMessage = (value: unknown): TraceMessage => {
  if (!isRecord(value)
    || typeof value.id !== 'string'
    || !Number.isSafeInteger(value.traceSeq)
    || Number(value.traceSeq) < 1
    || !isOptionalString(value.sourceId)
    || !Array.isArray(value.namespace)
    || value.namespace.some((item) => typeof item !== 'string')
    || typeof value.runId !== 'string'
    || typeof value.role !== 'string'
    || !MESSAGE_ROLES.has(value.role)
    || typeof value.contentOmitted !== 'boolean'
    || !isOptionalString(value.name)
    || !isOptionalString(value.toolCallId)
    || typeof value.status !== 'string'
    || !MESSAGE_STATUSES.has(value.status)
    || typeof value.createdAt !== 'string'
    || !Number.isFinite(Date.parse(value.createdAt))
    || !isOptionalTimestamp(value.completedAt)
  ) throw new ConversationError('stream_event_invalid')
  return value as unknown as TraceMessage
}

const parseTurn = (value: unknown): TraceTurn => {
  if (!isRecord(value)
    || typeof value.id !== 'string'
    || !Number.isSafeInteger(value.ordinal)
    || Number(value.ordinal) < 1
    || typeof value.startedAt !== 'string'
    || !Number.isFinite(Date.parse(value.startedAt))
    || (value.userMessage !== undefined
      && value.userMessage !== null
      && !isRecord(value.userMessage))
  ) throw new ConversationError('stream_event_invalid')
  if (value.userMessage !== undefined && value.userMessage !== null) {
    parseTraceMessage(value.userMessage)
  }
  return value as unknown as TraceTurn
}

const parseFailure = (value: unknown): TraceFailure | null | undefined => {
  if (value === undefined || value === null) return value
  if (!isRecord(value)
    || typeof value.errorType !== 'string'
    || !isOptionalString(value.message)
    || !isOptionalString(value.code)
  ) throw new ConversationError('stream_event_invalid')
  return value as unknown as TraceFailure
}

const parseEntry = (value: unknown): TraceEntry => {
  if (!isRecord(value)
    || typeof value.id !== 'string'
    || typeof value.turnId !== 'string'
    || typeof value.name !== 'string'
    || typeof value.runId !== 'string'
    || typeof value.kind !== 'string'
    || !ENTRY_KINDS.has(value.kind as TraceEntryKind)
    || typeof value.status !== 'string'
    || !ENTRY_STATUSES.has(value.status as TraceEntryStatus)
    || !Array.isArray(value.namespace)
    || value.namespace.some((item) => typeof item !== 'string')
    || typeof value.startedAt !== 'string'
    || !Number.isFinite(Date.parse(value.startedAt))
    || !Number.isSafeInteger(value.startedSeq)
    || Number(value.startedSeq) < 1
    || !Number.isSafeInteger(value.updatedSeq)
    || Number(value.updatedSeq) < Number(value.startedSeq)
    || !isOptionalString(value.parentId)
    || !isOptionalString(value.proposalId)
    || !isOptionalString(value.agentName)
    || !isOptionalString(value.provider)
    || !isOptionalString(value.model)
    || !isOptionalString(value.sourceId)
    || !isOptionalTimestamp(value.firstOutputAt)
    || !isOptionalTimestamp(value.completedAt)
    || typeof value.requestOmitted !== 'boolean'
    || typeof value.resultOmitted !== 'boolean'
    || !isOptionalString(value.className)
    || !isOptionalString(value.sourcePath)
    || !Array.isArray(value.hooks)
    || value.hooks.some((item) => typeof item !== 'string')
  ) throw new ConversationError('stream_event_invalid')
  parseFailure(value.failure)
  return value as unknown as TraceEntry
}

const parseFacets = (value: unknown): TraceFacets => {
  if (!isRecord(value)
    || !isCountRecord(value.kinds)
    || Object.keys(value.kinds).some((kind) => !ENTRY_KINDS.has(kind as TraceEntryKind))
    || !isCountRecord(value.statuses)
    || Object.keys(value.statuses).some((status) => !ENTRY_STATUSES.has(status as TraceEntryStatus))
    || !isCountRecord(value.agents)
    || !isCountRecord(value.middleware)
    || !isCountRecord(value.skills)
    || !isCountRecord(value.providers)
    || !isCountRecord(value.models)
  ) throw new ConversationError('stream_event_invalid')
  return value as unknown as TraceFacets
}

export const parseTraceEntryPage = (value: unknown): TraceEntryPage => {
  if (!isRecord(value)
    || !Array.isArray(value.turns)
    || !Array.isArray(value.items)
    || !Number.isSafeInteger(value.asOfSeq)
    || Number(value.asOfSeq) < 1
    || !isRecord(value.completeness)
    || typeof value.completeness.callTrackingMissing !== 'boolean'
    || typeof value.completeness.executionTreeMissing !== 'boolean'
    || (value.nextCursor !== undefined
      && value.nextCursor !== null
      && typeof value.nextCursor !== 'string')
  ) throw new ConversationError('stream_event_invalid')
  return {
    turns: value.turns.map(parseTurn),
    items: value.items.map(parseEntry),
    nextCursor: typeof value.nextCursor === 'string' ? value.nextCursor : null,
    asOfSeq: value.asOfSeq as number,
    facets: parseFacets(value.facets),
    completeness: value.completeness as unknown as TraceEntryCompleteness,
  }
}

const parseTraceEntryDelta = (value: unknown): TraceEntryDelta => {
  if (!isRecord(value)
    || !Number.isSafeInteger(value.asOfSeq)
    || Number(value.asOfSeq) < 1
    || !Array.isArray(value.turnUpserts)
    || !Array.isArray(value.turnRemoves)
    || value.turnRemoves.some((item) => typeof item !== 'string')
    || !Array.isArray(value.upserts)
    || !Array.isArray(value.removes)
    || value.removes.some((item) => typeof item !== 'string')
    || !isRecord(value.completeness)
    || typeof value.completeness.callTrackingMissing !== 'boolean'
    || typeof value.completeness.executionTreeMissing !== 'boolean'
  ) throw new ConversationError('stream_event_invalid')
  return {
    asOfSeq: value.asOfSeq as number,
    turnUpserts: value.turnUpserts.map(parseTurn),
    turnRemoves: value.turnRemoves as string[],
    upserts: value.upserts.map(parseEntry),
    removes: value.removes as string[],
    facets: parseFacets(value.facets),
    completeness: value.completeness as unknown as TraceEntryCompleteness,
  }
}

const parseTraceEntryEvent = (value: unknown): TraceEntryEvent => {
  if (!isRecord(value)) throw new ConversationError('stream_event_invalid')
  if (value.type === 'snapshot') {
    return { type: 'snapshot', snapshot: parseTraceEntryPage(value.snapshot) }
  }
  if (value.type === 'update') {
    return { type: 'update', update: parseTraceEntryDelta(value.update) }
  }
  if (value.type === 'error' && value.code === 'trace_unavailable') {
    return { type: 'error', code: 'trace_unavailable' }
  }
  throw new ConversationError('stream_event_invalid')
}

const appendFilter = (search: URLSearchParams, filter: TraceEntryFilter) => {
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
  search.set('includeAncestors', String(filter.includeAncestors ?? true))
}

const entryUrl = (
  threadId: string,
  filter: TraceEntryFilter,
  options: { limit?: number; follow?: boolean } = {},
) => {
  const search = new URLSearchParams()
  appendFilter(search, filter)
  if (options.limit) search.set('limit', String(options.limit))
  return `/api/conversation/${encodeURIComponent(threadId)}/trace/entries${options.follow ? '/follow' : ''}?${search}`
}

export async function* followTraceEntries(
  threadId: string,
  filter: TraceEntryFilter,
  options: { limit?: number; signal?: AbortSignal } = {},
): AsyncGenerator<TraceEntryEvent> {
  const response = await requestEventStream(
    entryUrl(threadId, filter, { limit: options.limit, follow: true }),
    { signal: options.signal, suppressGlobalError: true },
  )
  if (!response.body) throw new ConversationError('stream_body_missing')
  for await (const frame of parseJsonSseStream(response.body, options.signal)) {
    yield parseTraceEntryEvent(frame.data)
  }
}
