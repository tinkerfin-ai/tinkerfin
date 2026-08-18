export type MessageRole = 'user' | 'assistant' | 'process' | 'tool' | 'subagent' | 'approval' | 'error'
export type TodoStatus = 'pending' | 'running' | 'completed' | 'failed'
export type ApprovalDecision = 'approved' | 'rejected'
export type ConversationRunStatus = 'idle' | 'streaming' | 'waiting_approval' | 'detached' | 'error'
export type ApprovalMode = 'options' | 'edit' | 'reject'
export type ApprovalAllowedDecision = 'approve' | 'edit' | 'reject' | 'respond'

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
    parentRunId?: string
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

export interface Conversation {
  threadId: string
  title: string
  pinned: boolean
  updatedAt: string
  model: string
  messages: Message[]
  notice?: ConversationNotice
  todos: TodoItem[]
  plan: Plan | null
  approval?: ApprovalState
  runStatus: ConversationRunStatus
  activeRunId?: string
  serverState?: JsonObject
  /** Last persisted AG-UI event seq; used for afterSeq resumption. */
  lastSeq?: number
  /** True when the full conversation detail has been restored from backend history. */
  isHydrated?: boolean
}

export interface WorkspaceState {
  conversations: Conversation[]
  currentThreadId: string
}
