import type {
  ApprovalAllowedDecision,
  ApprovalState,
  AgentMode,
  ConversationRunStatus,
  JsonObject,
  Message,
  PlanInteraction,
  TodoItem,
} from '../../types'
import { requestJson } from '../shared/http'
import type { ConversationAgUiEvent } from './types'

export interface ConversationHistoryListItem {
  id: number
  threadId: string
  title: string
  status: string
  lastRunId?: string
  lastModel?: string
  lastSeq: number
  messageCount: number
  toolCallCount: number
  hasPendingInterrupt: boolean
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

export interface ConversationSnapshotJson {
  snapshotSeq: number
  snapshotVersion: 3
  messages: Message[]
  todos: TodoItem[]
  mode: AgentMode
  approval: ApprovalState | null
  planInteraction?: PlanInteraction | null
  runStatus: ConversationRunStatus
  activeRunId?: string | null
  serverState: JsonObject
  runs: Record<string, ConversationSnapshotRun>
  interrupts: ConversationSnapshotInterrupt[]
}

export interface ConversationSnapshotRun {
  runId: string
  status: 'running' | 'success' | 'interrupt' | 'error'
  parentRunId?: string | null
  originMainRunId?: string | null
  lastMainRunId?: string | null
  agentType: 'main' | 'subagent'
  agentName?: string | null
  graphTaskId?: string | null
  startedAt?: string | null
  completedAt?: string | null
}

export interface ConversationSnapshotInterrupt {
  id: string
  reason: string
  toolCallId?: string | null
  message?: string | null
  responseSchema?: JsonObject | null
  metadata?: JsonObject | null
  toolName?: string | null
  allowedDecisions: ApprovalAllowedDecision[]
  originalArgs: JsonObject
}

export interface ConversationEventEnvelope {
  seq: number
  eventId: string
  eventType: string
  runId?: string | null
  event: ConversationAgUiEvent
  createdAt: string
}

export interface ConversationHistoryDetail {
  id: number
  threadId: string
  title: string
  status: string
  lastRunId?: string
  lastModel?: string
  lastSeq: number
  snapshotSeq: number
  snapshotVersion: 3
  messageCount: number
  toolCallCount: number
  hasPendingInterrupt: boolean
  pinned: boolean
  snapshot?: ConversationSnapshotJson | null
  events: ConversationEventEnvelope[]
  createdAt: string
  updatedAt: string
}

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
  const query = search.toString() ? `?${search.toString()}` : ''
  return requestJson<ConversationHistoryListResponse>(
    `${CONVERSATION_API_PATH}/history${query}`,
    {
      signal: params.signal,
      suppressGlobalError: params.suppressGlobalError,
    },
  )
}

export const fetchConversationHistoryGroupConfig = (
  options: { signal?: AbortSignal; suppressGlobalError?: boolean } = {},
): Promise<ConversationHistoryGroupConfig> => requestJson<ConversationHistoryGroupConfig>(
  `${CONVERSATION_API_PATH}/config`,
  options,
)

export const fetchConversationHistoryDetail = (
  threadId: string,
  options: { signal?: AbortSignal; suppressGlobalError?: boolean } = {},
): Promise<ConversationHistoryDetail> =>
  requestJson<ConversationHistoryDetail>(
    `${CONVERSATION_API_PATH}/${encodeURIComponent(threadId)}/history`,
    options,
  )

export const fetchConversationEvents = (
  threadId: string,
  params: {
    afterSeq?: number
    limit?: number
    signal?: AbortSignal
    suppressGlobalError?: boolean
  } = {},
): Promise<ConversationEventEnvelope[]> => {
  const search = new URLSearchParams()
  if (params.afterSeq != null) search.set('afterSeq', String(params.afterSeq))
  if (params.limit) search.set('limit', String(params.limit))
  const query = search.toString() ? `?${search.toString()}` : ''
  return requestJson<ConversationEventEnvelope[]>(
    `${CONVERSATION_API_PATH}/${encodeURIComponent(threadId)}/events${query}`,
    {
      signal: params.signal,
      suppressGlobalError: params.suppressGlobalError,
    },
  )
}

/** 重命名或置顶会话，仅传需要修改的字段并返回更新后的会话摘要 */
export const patchConversation = (
  threadId: string,
  body: { title?: string; pinned?: boolean },
): Promise<ConversationHistoryListItem> =>
  requestJson<ConversationHistoryListItem>(
    `${CONVERSATION_API_PATH}/${encodeURIComponent(threadId)}`,
    {
      method: 'PATCH',
      body,
      suppressGlobalError: true,
    },
  )

/** 删除会话数据库投影 */
export const deleteConversation = async (threadId: string): Promise<void> => {
  await requestJson<void>(`${CONVERSATION_API_PATH}/${encodeURIComponent(threadId)}`, {
    method: 'DELETE',
    suppressGlobalError: true,
  })
}
