import type { AgentMode, JsonObject, JsonValue } from "../../types"

export type { AgentMode }

export interface ChatMessageInput {
  id: string
  role: "user"
  content: string
}

export type ChatResumePayload =
  | { type: "approve" }
  | { type: "reject"; message?: string }
  | {
    type: "edit"
    edited_action: { name: string; args: JsonObject }
  }
  | {
    type: "respond"
    answers: Array<
      | { questionId: string; optionId: string }
      | { questionId: string; answer: string }
    >
  }
  | { type: "approve"; baseRevision: number }
  | { type: "edit"; baseRevision: number; draft: JsonObject }
  | { type: "respond"; baseRevision: number; message: string }
  | { type: "reject"; baseRevision: number; message?: string }

export interface ChatResumeEntry {
  interruptId: string
  status: "resolved" | "cancelled"
  payload?: ChatResumePayload
}

export interface ChatRequestPayload {
  threadId: string
  runId: string
  state: JsonObject
  messages: ChatMessageInput[]
  tools: JsonValue[]
  context: JsonValue[]
  forwardedProps: JsonObject
  resume?: ChatResumeEntry[]
}

export interface EventSourceInfo {
  agentType: "main" | "subagent"
  agentName: string
  namespace: string[]
  graphTaskId?: string | null
}

export interface RawEventContext {
  streamMode?: "messages" | "tasks" | "values"
  source?: EventSourceInfo
  runId?: string
  relatedRunId?: string
  /** Project extension: the main run that spawned this sub-agent run. */
  parentAgentRunId?: string
  /** Project extension: the parent task tool call that spawned this sub-agent. */
  parentToolCallId?: string
  /** Project extension: the validated task description for this sub-agent. */
  subagentInput?: string
  langgraphNode?: string
  interruptId?: string
  /** Project extension preserving LangChain ToolMessage.status. */
  toolResultStatus?: "success" | "error"
}

export interface RunStartedInputMessage {
  id: string
  role: string
  content: string
}

export interface RunStartedResumeEntry {
  interruptId: string
  status: "resolved" | "cancelled"
  payload?: JsonValue
}

export interface RunStartedInput {
  threadId: string
  runId: string
  state?: JsonObject
  messages?: RunStartedInputMessage[]
  tools?: JsonValue[]
  context?: JsonValue[]
  forwardedProps?: JsonObject
  resume?: RunStartedResumeEntry[]
  parentRunId?: string
}

export interface RunStartedEvent {
  type: "RUN_STARTED"
  threadId: string
  runId: string
  parentRunId?: string
  /** Project extension carrying sub-agent provenance for synthesized runs. */
  rawEvent?: RawEventContext
  /** Studio 扩展：服务端已持久化的权威会话标题 */
  title?: string
  /** Present only on client-initiated main runs; absent on synthesized sub-agent runs. */
  input?: RunStartedInput
}

export interface MessageSnapshotItem {
  id: string
  role: string
  content?: JsonValue
}

export interface MessagesSnapshotEvent {
  type: "MESSAGES_SNAPSHOT"
  rawEvent?: RawEventContext
  messages: MessageSnapshotItem[]
}

export interface StateSnapshotEvent {
  type: "STATE_SNAPSHOT"
  /** Optional project extension; standard AG-UI producers may omit it. */
  rawEvent?: RawEventContext
  snapshot: JsonObject
}

export interface StateDeltaOperation {
  op: "add" | "remove" | "replace"
  path: string
  value?: JsonValue
}

export interface StateDeltaEvent {
  type: "STATE_DELTA"
  rawEvent?: RawEventContext
  delta: StateDeltaOperation[]
}

export interface TextMessageStartEvent {
  type: "TEXT_MESSAGE_START"
  rawEvent?: RawEventContext
  messageId: string
  role: string
  name?: string
}

export interface TextMessageContentEvent {
  type: "TEXT_MESSAGE_CONTENT"
  rawEvent?: RawEventContext
  messageId: string
  delta: string
}

export interface TextMessageEndEvent {
  type: "TEXT_MESSAGE_END"
  rawEvent?: RawEventContext
  messageId: string
}

export interface ReasoningStartEvent {
  type: "REASONING_START"
  rawEvent?: RawEventContext
  messageId: string
}

export interface ReasoningMessageStartEvent {
  type: "REASONING_MESSAGE_START"
  rawEvent?: RawEventContext
  messageId: string
  role: "reasoning"
}

export interface ReasoningMessageContentEvent {
  type: "REASONING_MESSAGE_CONTENT"
  rawEvent?: RawEventContext
  messageId: string
  delta: string
}

export interface ReasoningMessageEndEvent {
  type: "REASONING_MESSAGE_END"
  rawEvent?: RawEventContext
  messageId: string
}

export interface ReasoningEndEvent {
  type: "REASONING_END"
  rawEvent?: RawEventContext
  messageId: string
}

export interface ToolCallStartEvent {
  type: "TOOL_CALL_START"
  rawEvent?: RawEventContext
  toolCallId: string
  toolCallName: string
  parentMessageId?: string
}

export interface ToolCallArgsEvent {
  type: "TOOL_CALL_ARGS"
  rawEvent?: RawEventContext
  toolCallId: string
  delta: string
}

export interface ToolCallEndEvent {
  type: "TOOL_CALL_END"
  rawEvent?: RawEventContext
  toolCallId: string
}

export interface ToolCallResultEvent {
  type: "TOOL_CALL_RESULT"
  rawEvent?: RawEventContext
  messageId: string
  toolCallId: string
  content: string
  role: string
}

export interface CustomEvent {
  type: "CUSTOM"
  rawEvent?: RawEventContext
  name: string
  value: JsonValue
}

export interface RawStreamEvent {
  type: "RAW"
  rawEvent?: JsonObject
  event: JsonObject
  source?: string
}

export interface InterruptEvent {
  id: string
  reason: string
  message?: string
  toolCallId?: string
  responseSchema?: JsonObject
  metadata?: JsonObject
}

export interface RunFinishedSuccessOutcome {
  type: "success"
}

export interface RunFinishedInterruptOutcome {
  type: "interrupt"
  interrupts: InterruptEvent[]
}

export interface RunFinishedEvent {
  type: "RUN_FINISHED"
  rawEvent?: RawEventContext
  threadId: string
  runId: string
  /** ag-ui-protocol permits producers to omit outcome; omission means success. */
  outcome?: RunFinishedSuccessOutcome | RunFinishedInterruptOutcome
}

export interface RunErrorEvent {
  type: "RUN_ERROR"
  rawEvent?: RawEventContext
  message?: string
  code?: string
  details?: JsonValue
}

export type ConversationAgUiEvent =
  | RunStartedEvent
  | MessagesSnapshotEvent
  | StateSnapshotEvent
  | StateDeltaEvent
  | TextMessageStartEvent
  | TextMessageContentEvent
  | TextMessageEndEvent
  | ReasoningStartEvent
  | ReasoningMessageStartEvent
  | ReasoningMessageContentEvent
  | ReasoningMessageEndEvent
  | ReasoningEndEvent
  | ToolCallStartEvent
  | ToolCallArgsEvent
  | ToolCallEndEvent
  | ToolCallResultEvent
  | CustomEvent
  | RawStreamEvent
  | RunFinishedEvent
  | RunErrorEvent
