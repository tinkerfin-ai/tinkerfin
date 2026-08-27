import type {
  ChatRequestPayload,
  ChatResumeEntry,
} from '../../../api/conversation/types'
import type { JsonObject, JsonValue } from '../../../types'

const ACTIVE_RUN_STORAGE_KEY = 'tinkerfin:active-conversation-run'

export interface ActiveRunSession {
  threadId: string
  payload: ChatRequestPayload
  mode: 'start' | 'resume'
  lastSeq: number
}

const isRecord = (value: unknown): value is Record<string, unknown> => (
  value != null && typeof value === 'object' && !Array.isArray(value)
)

const isJsonValue = (value: unknown): value is JsonValue => {
  if (
    value == null
    || typeof value === 'string'
    || typeof value === 'number'
    || typeof value === 'boolean'
  ) return true
  if (Array.isArray(value)) return value.every(isJsonValue)
  return isRecord(value) && Object.values(value).every(isJsonValue)
}

const isJsonObject = (value: unknown): value is JsonObject => (
  isRecord(value) && Object.values(value).every(isJsonValue)
)

const isResumeEntry = (value: unknown): value is ChatResumeEntry => {
  if (!isRecord(value)) return false
  if (typeof value.interruptId !== 'string') return false
  if (value.status !== 'resolved' && value.status !== 'cancelled') return false
  return value.payload === undefined || isJsonValue(value.payload)
}

const isChatRequestPayload = (value: unknown): value is ChatRequestPayload => {
  if (!isRecord(value)) return false
  if (typeof value.threadId !== 'string' || typeof value.runId !== 'string') return false
  if (!isJsonObject(value.state) || !isJsonObject(value.forwardedProps)) return false
  if (!Array.isArray(value.tools) || !value.tools.every(isJsonValue)) return false
  if (!Array.isArray(value.context) || !value.context.every(isJsonValue)) return false
  if (!Array.isArray(value.messages) || !value.messages.every((message) => (
    isRecord(message)
    && Object.keys(message).every((key) => key === 'id' || key === 'role' || key === 'content')
    && typeof message.id === 'string'
    && Boolean(message.id.trim())
    && message.role === 'user'
    && typeof message.content === 'string'
  ))) return false
  return value.resume === undefined
    || (Array.isArray(value.resume) && value.resume.every(isResumeEntry))
}

const parseActiveRunSession = (value: unknown): ActiveRunSession | null => {
  if (!isRecord(value)) return null
  const keys = Object.keys(value).sort()
  if (keys.join('\0') !== ['lastSeq', 'mode', 'payload', 'threadId'].join('\0')) return null
  if (typeof value.threadId !== 'string') return null
  if (value.mode !== 'start' && value.mode !== 'resume') return null
  if (!Number.isSafeInteger(value.lastSeq) || Number(value.lastSeq) < 0) return null
  if (!isChatRequestPayload(value.payload) || !value.payload.runId.trim()) return null
  return {
    threadId: value.threadId,
    payload: value.payload,
    mode: value.mode,
    lastSeq: Number(value.lastSeq),
  }
}

export const readActiveRunSession = (): ActiveRunSession | null => {
  try {
    const raw = window.sessionStorage.getItem(ACTIVE_RUN_STORAGE_KEY)
    if (!raw) return null
    const parsed = parseActiveRunSession(JSON.parse(raw) as unknown)
    if (parsed) return parsed
    window.sessionStorage.removeItem(ACTIVE_RUN_STORAGE_KEY)
    return null
  } catch {
    try {
      window.sessionStorage.removeItem(ACTIVE_RUN_STORAGE_KEY)
    } catch {
      // 存储完全不可用时不能让刷新重连状态阻断页面初始化
    }
    return null
  }
}

export const writeActiveRunSession = (session: ActiveRunSession): void => {
  try {
    window.sessionStorage.setItem(ACTIVE_RUN_STORAGE_KEY, JSON.stringify(session))
  } catch {
    // 浏览器禁用存储时，当前连接仍可正常工作，只是不具备刷新重连能力
  }
}

export const clearActiveRunSession = (runId?: string): void => {
  if (runId) {
    const active = readActiveRunSession()
    if (active && active.payload.runId !== runId) return
  }
  try {
    window.sessionStorage.removeItem(ACTIVE_RUN_STORAGE_KEY)
  } catch {
    // 与写入失败一致，存储不可用时无需额外处理
  }
}
