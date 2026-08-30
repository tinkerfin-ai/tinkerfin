import type { PendingInteractionKind, JsonObject, JsonValue } from '../../types'
import { requestEventStream, requestJson } from '../shared/http'
import { ConversationError } from './errors'
import { parseJsonSseStream } from './sse'

export interface ConversationHistoryListItem {
  id: number
  threadId: string
  title: string
  status: string
  lastRunId?: string | null
  lastModel?: string | null
  messageCount: number
  toolCallCount: number
  hasPendingInterrupt: boolean
  pendingInteractionKind: PendingInteractionKind | null
  pinned: boolean
  createdAt: string
  updatedAt: string
}

export interface ConversationHistoryListResponse {
  items: ConversationHistoryListItem[]
  nextCursor?: string | null
}

export interface ConversationHistoryGroupConfig {
  dayRanges: number[]
}

export interface TraceMessage {
  id: string
  traceSeq: number
  sourceId?: string | null
  namespace: string[]
  runId: string
  role: 'user' | 'assistant' | 'tool' | 'system' | 'other'
  content?: JsonValue | null
  contentOmitted: boolean
  name?: string | null
  toolCallId?: string | null
  status: 'streaming' | 'completed'
  createdAt: string
  completedAt?: string | null
}

export interface TraceReasoning {
  id: string
  traceSeq: number
  messageId: string
  namespace: string[]
  runId: string
  extractor: string
  content?: JsonValue | null
  contentOmitted: boolean
  status: 'streaming' | 'completed'
  createdAt: string
  completedAt?: string | null
}

export interface TraceNode {
  id: string
  traceSeq: number
  parentId?: string | null
  kind: 'turn' | 'run' | 'task' | 'tool' | 'subagent' | 'plan'
  label: string
  runId: string
  namespace: string[]
  sourceId?: string | null
  input?: JsonValue | null
  inputOmitted: boolean
  result?: JsonValue | null
  resultOmitted: boolean
  status: 'running' | 'waiting' | 'succeeded' | 'failed' | 'cancelled' | 'abandoned' | 'unknown'
  startedAt: string
  completedAt?: string | null
}

export interface TraceInteraction {
  id: string
  traceSeq: number
  sourceId: string
  namespace: string[]
  runId: string
  kind: string
  toolCallIds: string[]
  status: 'pending' | 'resolved' | 'cancelled'
  payload?: JsonValue | null
  payloadOmitted: boolean
  openedAt: string
  resolvedAt?: string | null
}

export interface TraceState {
  root: JsonObject
  subgraphs: Record<string, JsonObject>
}

export interface TraceStatus {
  execution: 'running' | 'waiting' | 'succeeded' | 'failed' | 'cancelled' | 'abandoned' | 'unknown'
  headRunId: string
}

export interface TraceCompleteness {
  missingPrefix: boolean
  missingTail: boolean
  payloadOmitted: boolean
}

export interface ConversationHistoryDetail {
  id: number
  threadId: string
  title: string
  lastModel?: string | null
  runtimeProfile: string
  pinned: boolean
  asOfSeq: number
  headRunId: string
  availableHeads: string[]
  historyCursor?: string | null
  messageCount: number
  toolCallCount: number
  messages: TraceMessage[]
  reasoning: TraceReasoning[]
  nodes: TraceNode[]
  state: TraceState
  interactions: TraceInteraction[]
  status: TraceStatus
  completeness: TraceCompleteness
  createdAt: string
  updatedAt: string
}

export interface TraceEntityDelta<T> {
  upserts: T[]
  removes: string[]
}

export interface ConversationTraceUpdate {
  asOfSeq: number
  events: JsonValue[]
  facts: JsonValue[]
  messages: TraceEntityDelta<TraceMessage>
  reasoning: TraceEntityDelta<TraceReasoning>
  nodes: TraceEntityDelta<TraceNode>
  interactions: TraceEntityDelta<TraceInteraction>
  state: TraceState
  status: TraceStatus
  completeness: TraceCompleteness
  messageCount: number
  toolCallCount: number
  projections: Record<string, JsonValue>
}

export type ConversationTraceEvent =
  | { type: 'snapshot'; snapshot: ConversationHistoryDetail }
  | { type: 'update'; update: ConversationTraceUpdate }
  | { type: 'error'; code: 'trace_unavailable' }

const CONVERSATION_API_PATH = '/api/conversation'

export const fetchConversationHistoryList = (
  params: {
    pageSize?: number
    cursor?: string | null
    query?: string | null
    signal?: AbortSignal
    suppressGlobalError?: boolean
  } = {},
): Promise<ConversationHistoryListResponse> => {
  const search = new URLSearchParams()
  if (params.pageSize) search.set('pageSize', String(params.pageSize))
  if (params.cursor) search.set('cursor', params.cursor)
  if (params.query) search.set('query', params.query)
  const query = search.toString() ? '?' + search.toString() : ''
  return requestJson<ConversationHistoryListResponse>(
    CONVERSATION_API_PATH + '/history' + query,
    {
      signal: params.signal,
      suppressGlobalError: params.suppressGlobalError,
    },
  )
}

export const fetchConversationHistoryGroupConfig = (
  options: { signal?: AbortSignal; suppressGlobalError?: boolean } = {},
): Promise<ConversationHistoryGroupConfig> => requestJson<ConversationHistoryGroupConfig>(
  CONVERSATION_API_PATH + '/config',
  options,
)

export const fetchConversationHistoryDetail = (
  threadId: string,
  options: {
    historyCursor?: string | null
    limit?: number
    signal?: AbortSignal
    suppressGlobalError?: boolean
  } = {},
): Promise<ConversationHistoryDetail> => {
  const search = new URLSearchParams()
  if (options.historyCursor) search.set('historyCursor', options.historyCursor)
  if (options.limit) search.set('limit', String(options.limit))
  const query = search.toString() ? '?' + search.toString() : ''
  return requestJson<ConversationHistoryDetail>(
    CONVERSATION_API_PATH + '/' + encodeURIComponent(threadId) + '/history' + query,
    {
      signal: options.signal,
      suppressGlobalError: options.suppressGlobalError,
    },
  )
}

const isTraceEvent = (value: unknown): value is ConversationTraceEvent => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const record = value as Record<string, unknown>
  if (record.type === 'snapshot') return Boolean(record.snapshot && typeof record.snapshot === 'object')
  if (record.type === 'update') return Boolean(record.update && typeof record.update === 'object')
  return record.type === 'error' && record.code === 'trace_unavailable'
}

export async function* followConversationTrace(
  threadId: string,
  signal?: AbortSignal,
): AsyncGenerator<ConversationTraceEvent> {
  const response = await requestEventStream(
    CONVERSATION_API_PATH + '/' + encodeURIComponent(threadId) + '/trace',
    { signal, suppressGlobalError: true },
  )
  if (!response.body) throw new ConversationError('stream_body_missing')
  for await (const frame of parseJsonSseStream(response.body, signal)) {
    if (frame.event !== 'trace' || !isTraceEvent(frame.data)) {
      throw new ConversationError('stream_event_invalid')
    }
    yield frame.data
  }
}

/** 重命名或置顶会话，仅传需要修改的字段并返回更新后的会话摘要 */
export const patchConversation = (
  threadId: string,
  body: { title?: string; pinned?: boolean },
): Promise<ConversationHistoryListItem> =>
  requestJson<ConversationHistoryListItem>(
    CONVERSATION_API_PATH + '/' + encodeURIComponent(threadId),
    {
      method: 'PATCH',
      body,
      suppressGlobalError: true,
    },
  )

/** 删除 Trace、Checkpoint、Messaging 与 Studio 自有会话记录 */
export const deleteConversation = async (threadId: string): Promise<void> => {
  await requestJson<void>(CONVERSATION_API_PATH + '/' + encodeURIComponent(threadId), {
    method: 'DELETE',
    suppressGlobalError: true,
  })
}
