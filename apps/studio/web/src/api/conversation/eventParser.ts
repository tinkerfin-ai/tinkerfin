import type { JsonObject, JsonValue } from '../../types'
import type {
  ConversationAgUiEvent,
  EventSourceInfo,
  RawEventContext,
  RunStartedInput,
} from './types'

const INVALID_EVENT_MESSAGE = '事件流包含无效的 AG-UI 事件'

const isRecord = (value: unknown): value is Record<string, unknown> => (
  value !== null && typeof value === 'object' && !Array.isArray(value)
)

const isStringArray = (value: unknown): value is string[] => (
  Array.isArray(value) && value.every((item) => typeof item === 'string')
)

const isJsonValue = (value: unknown): value is JsonValue => {
  const pending: unknown[] = [value]
  const visited = new Set<object>()

  while (pending.length > 0) {
    const current = pending.pop()
    if (
      current === null
      || typeof current === 'string'
      || typeof current === 'boolean'
      || (typeof current === 'number' && Number.isFinite(current))
    ) continue

    if (typeof current !== 'object') return false
    if (visited.has(current)) return false
    visited.add(current)

    if (Array.isArray(current)) pending.push(...current)
    else pending.push(...Object.values(current))
  }

  return true
}

const isJsonObject = (value: unknown): value is JsonObject => (
  isRecord(value) && isJsonValue(value)
)

const hasOptionalString = (
  value: Record<string, unknown>,
  key: string,
  allowNull = false,
) => value[key] === undefined
  || typeof value[key] === 'string'
  || (allowNull && value[key] === null)

const hasOptionalBoolean = (value: Record<string, unknown>, key: string) => (
  value[key] === undefined || typeof value[key] === 'boolean'
)

const isEventSourceInfo = (value: unknown): value is EventSourceInfo => {
  if (!isRecord(value)) return false
  return (value.agentType === 'main' || value.agentType === 'subagent')
    && typeof value.agentName === 'string'
    && isStringArray(value.namespace)
    && hasOptionalString(value, 'graphTaskId', true)
    && (value.parentNamespace === undefined
      || value.parentNamespace === null
      || isStringArray(value.parentNamespace))
    && hasOptionalString(value, 'parentToolCallId', true)
    && hasOptionalString(value, 'subagentInput', true)
    && hasOptionalString(value, 'subagentInvocationId', true)
}

const isRawEventContext = (value: unknown): value is RawEventContext => {
  if (!isRecord(value)) return false
  return (value.streamMode === undefined
      || value.streamMode === 'messages'
      || value.streamMode === 'tasks'
      || value.streamMode === 'values')
    && (value.source === undefined || isEventSourceInfo(value.source))
    && hasOptionalString(value, 'runId')
    && hasOptionalString(value, 'relatedSubagentInvocationId')
    && hasOptionalString(value, 'parentToolCallId')
    && hasOptionalString(value, 'subagentInput')
    && hasOptionalString(value, 'langgraphNode')
    && hasOptionalString(value, 'interruptId')
    && hasOptionalBoolean(value, 'initializationFailed')
    && (value.toolResultStatus === undefined
      || value.toolResultStatus === 'success'
      || value.toolResultStatus === 'error')
}

const hasOptionalRawEvent = (value: Record<string, unknown>) => (
  value.rawEvent === undefined || isRawEventContext(value.rawEvent)
)

const isRunStartedInput = (value: unknown): value is RunStartedInput => {
  if (!isRecord(value)) return false
  const messagesValid = value.messages === undefined || (
    Array.isArray(value.messages)
    && value.messages.every((message) => isRecord(message)
      && typeof message.id === 'string'
      && typeof message.role === 'string'
      && typeof message.content === 'string')
  )
  const resumeValid = value.resume === undefined || (
    Array.isArray(value.resume)
    && value.resume.every((entry) => isRecord(entry)
      && typeof entry.interruptId === 'string'
      && (entry.status === 'resolved' || entry.status === 'cancelled')
      && (entry.payload === undefined || isJsonValue(entry.payload)))
  )

  return typeof value.threadId === 'string'
    && typeof value.runId === 'string'
    && hasOptionalString(value, 'parentRunId')
    && (value.state === undefined || isJsonObject(value.state))
    && messagesValid
    && (value.tools === undefined
      || (Array.isArray(value.tools) && value.tools.every(isJsonValue)))
    && (value.context === undefined
      || (Array.isArray(value.context) && value.context.every(isJsonValue)))
    && (value.forwardedProps === undefined || isJsonObject(value.forwardedProps))
    && resumeValid
}

const isMessageSnapshot = (value: unknown) => (
  isRecord(value)
  && typeof value.id === 'string'
  && typeof value.role === 'string'
  && (value.content === undefined || isJsonValue(value.content))
)

const isStateDeltaOperation = (value: unknown) => (
  isRecord(value)
  && (value.op === 'add' || value.op === 'remove' || value.op === 'replace')
  && typeof value.path === 'string'
  && (value.value === undefined || isJsonValue(value.value))
)

const isInterrupt = (value: unknown) => (
  isRecord(value)
  && typeof value.id === 'string'
  && typeof value.reason === 'string'
  && hasOptionalString(value, 'message')
  && hasOptionalString(value, 'toolCallId')
  && (value.responseSchema === undefined || isJsonObject(value.responseSchema))
  && (value.metadata === undefined || isJsonObject(value.metadata))
)

const isRunFinishedOutcome = (value: unknown) => {
  if (!isRecord(value)) return false
  if (value.type === 'success') return true
  return value.type === 'interrupt'
    && Array.isArray(value.interrupts)
    && value.interrupts.length > 0
    && value.interrupts.every(isInterrupt)
}

const isConversationAgUiEvent = (value: unknown): value is ConversationAgUiEvent => {
  if (!isRecord(value) || typeof value.type !== 'string') return false

  switch (value.type) {
    case 'RUN_STARTED':
      return typeof value.threadId === 'string'
        && typeof value.runId === 'string'
        && hasOptionalString(value, 'parentRunId')
        && hasOptionalString(value, 'title')
        && hasOptionalRawEvent(value)
        && (value.input === undefined || isRunStartedInput(value.input))

    case 'MESSAGES_SNAPSHOT':
      return hasOptionalRawEvent(value)
        && Array.isArray(value.messages)
        && value.messages.every(isMessageSnapshot)

    case 'STATE_SNAPSHOT':
      return hasOptionalRawEvent(value) && isJsonObject(value.snapshot)

    case 'STATE_DELTA':
      return hasOptionalRawEvent(value)
        && Array.isArray(value.delta)
        && value.delta.every(isStateDeltaOperation)

    case 'TEXT_MESSAGE_START':
      return hasOptionalRawEvent(value)
        && typeof value.messageId === 'string'
        && typeof value.role === 'string'
        && hasOptionalString(value, 'name')

    case 'TEXT_MESSAGE_CONTENT':
    case 'REASONING_MESSAGE_CONTENT':
      return hasOptionalRawEvent(value)
        && typeof value.messageId === 'string'
        && typeof value.delta === 'string'

    case 'TEXT_MESSAGE_END':
    case 'REASONING_START':
    case 'REASONING_MESSAGE_END':
    case 'REASONING_END':
      return hasOptionalRawEvent(value) && typeof value.messageId === 'string'

    case 'REASONING_MESSAGE_START':
      return hasOptionalRawEvent(value)
        && typeof value.messageId === 'string'
        && value.role === 'reasoning'

    case 'TOOL_CALL_START':
      return hasOptionalRawEvent(value)
        && typeof value.toolCallId === 'string'
        && typeof value.toolCallName === 'string'
        && hasOptionalString(value, 'parentMessageId')

    case 'TOOL_CALL_ARGS':
      return hasOptionalRawEvent(value)
        && typeof value.toolCallId === 'string'
        && typeof value.delta === 'string'

    case 'TOOL_CALL_END':
      return hasOptionalRawEvent(value) && typeof value.toolCallId === 'string'

    case 'TOOL_CALL_RESULT':
      return hasOptionalRawEvent(value)
        && typeof value.messageId === 'string'
        && typeof value.toolCallId === 'string'
        && typeof value.content === 'string'
        && typeof value.role === 'string'

    case 'CUSTOM':
      return hasOptionalRawEvent(value)
        && typeof value.name === 'string'
        && isJsonValue(value.value)

    case 'RAW':
      return (value.rawEvent === undefined || isJsonObject(value.rawEvent))
        && isJsonObject(value.event)
        && hasOptionalString(value, 'source')

    case 'RUN_FINISHED':
      return hasOptionalRawEvent(value)
        && typeof value.threadId === 'string'
        && typeof value.runId === 'string'
        && (value.outcome === undefined || isRunFinishedOutcome(value.outcome))

    case 'RUN_ERROR':
      return hasOptionalRawEvent(value)
        && hasOptionalString(value, 'message')
        && hasOptionalString(value, 'code')
        && (value.details === undefined || isJsonValue(value.details))

    default:
      return false
  }
}

/** 将 JSON 边界中的未知值收窄为 Studio 当前支持的 AG-UI 事件 */
export const parseConversationAgUiEvent = (value: unknown): ConversationAgUiEvent => {
  if (!isConversationAgUiEvent(value)) throw new Error(INVALID_EVENT_MESSAGE)
  return value
}
