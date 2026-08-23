export type MessageRole = 'user' | 'assistant' | 'process' | 'tool' | 'subagent' | 'approval' | 'error'
export type TodoStatus = 'pending' | 'running' | 'completed' | 'failed'
export type ApprovalDecision = 'approved' | 'rejected'
export type ConversationRunStatus = 'idle' | 'streaming' | 'waiting_approval' | 'detached' | 'error'
export type ApprovalMode = 'options' | 'edit' | 'reject'
export type ApprovalAllowedDecision = 'approve' | 'edit' | 'reject' | 'respond'
export type AgentMode = 'default' | 'plan'

export interface JsonObject {
  [key: string]: JsonValue
}

export type JsonValue =
  | string
  | number
  | boolean
  | null
  | JsonObject
  | JsonValue[]

export interface Message {
  id: string
  role: MessageRole
  content: string
  createdAt: string
  meta?: {
    title?: string
    toolName?: string
    params?: string
    input?: string
    result?: string
    reasoning?: string
    status?: 'running' | 'completed' | 'failed' | 'paused'
    batchId?: string
    agentName?: string
    sourceAgentName?: string
    toolCallId?: string
    parentMessageId?: string
    subRunId?: string
    originMainRunId?: string
    lastMainRunId?: string
    graphTaskId?: string
    runId?: string
    completedAt?: string
    durationMs?: number
    interruptId?: string
  }
}

export interface ConversationNotice {
  kind: 'error' | 'info'
  content: string
}

export interface TodoItem {
  id: string
  content: string
  status: TodoStatus
  result?: string
  targetMessageId?: string
}

export interface Plan {
  goal: string
  steps: Array<{ title: string; detail: string }>
}

export interface ApprovalItem {
  id: string
  interruptId: string
  toolCallId?: string
  toolName: string
  params: string
  input: string
  description: string
  originalArgs: JsonObject
  allowedDecisions: ApprovalAllowedDecision[]
  decision?: ApprovalDecision
  editedArgs?: JsonObject
  editedParams?: string
  rejectionReason?: string
}

export interface ApprovalState {
  items: ApprovalItem[]
  activeIndex: number
  submitted: boolean
  mode?: ApprovalMode
  error?: string
}

export interface PlanQuestionOption {
  id: string
  label: string
  description?: string | null
  attributes?: JsonObject | null
}

export interface PlanQuestionItem {
  id: string
  prompt: string
  options: PlanQuestionOption[]
  allowFreeText: boolean
  attributes?: JsonObject | null
  selectedOptionId?: string
  customAnswer?: string
}

export interface PlanQuestionState {
  kind: 'questions'
  interruptId: string
  form: JsonObject
  questions: PlanQuestionItem[]
  submitted: boolean
  error?: string
}

export interface PlanReviewState {
  kind: 'review'
  interruptId: string
  revision: number
  draft: JsonObject
  action?: 'approve' | 'edit' | 'respond' | 'reject'
  editedDraft?: string
  message?: string
  submitted: boolean
  error?: string
}

export type PlanInteraction = PlanQuestionState | PlanReviewState

export interface Conversation {
  threadId: string
  title: string
  pinned: boolean
  updatedAt: string
  model: string
  mode: AgentMode
  messages: Message[]
  notice?: ConversationNotice
  todos: TodoItem[]
  plan: Plan | null
  approval?: ApprovalState
  planInteraction?: PlanInteraction
  runStatus: ConversationRunStatus
  activeRunId?: string
  serverState?: JsonObject
  /** 最后一条已持久化 AG-UI 事件序号，用于 afterSeq 续传 */
  lastSeq?: number
  /** 完整会话详情是否已从后端历史恢复 */
  isHydrated?: boolean
}

export interface WorkspaceState {
  conversations: Conversation[]
  currentThreadId: string
}
