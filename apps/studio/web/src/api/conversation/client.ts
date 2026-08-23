import type { ChatRequestPayload, ConversationAgUiEvent } from './types'
import { conversationChatUrl } from './config'
import { parseConversationAgUiEvent } from './eventParser'
import { requestEventStream, requestJson } from '../shared/http'

export interface StreamedAgUiEvent {
  event: ConversationAgUiEvent
  /** SSE `id:` 行提供的持久化事件序号；生产端省略时为 null */
  seq: number | null
}

/**
 * 将 SSE 流解析为 AG-UI 事件，并保留 `id:` 供调用方维护续传所需的 `lastSeq`
 *
 * AG-UI `HttpAgent` 只读取 `data:` 并丢弃 `id:`，会导致续传游标丢失；
 * 此解析器保留其全部 `data:` 事件，同时额外暴露 `id:` 序号
 */
async function* parseAgUiSseStream(
  body: ReadableStream<Uint8Array>,
  signal?: AbortSignal,
): AsyncGenerator<StreamedAgUiEvent> {
  const decoder = new TextDecoder('utf-8', { fatal: false })
  const reader = body.getReader()
  let currentLine = ''
  let frameLines: string[] = []
  let previousWasCarriageReturn = false
  let aborted = false
  const onAbort = () => {
    aborted = true
    reader.cancel().catch(() => undefined)
  }
  signal?.addEventListener('abort', onAbort)

  const finishLine = () => {
    if (currentLine === '') {
      const parsed = frameLines.length > 0 ? parseFrame(frameLines) : null
      frameLines = []
      return parsed
    }
    frameLines.push(currentLine)
    currentLine = ''
    return null
  }

  const consumeText = (text: string) => {
    const parsedEvents: StreamedAgUiEvent[] = []
    for (const character of text) {
      if (previousWasCarriageReturn) {
        previousWasCarriageReturn = false
        if (character === '\n') continue
      }
      if (character === '\r') {
        const parsed = finishLine()
        if (parsed) parsedEvents.push(parsed)
        previousWasCarriageReturn = true
      } else if (character === '\n') {
        const parsed = finishLine()
        if (parsed) parsedEvents.push(parsed)
      } else {
        currentLine += character
      }
    }
    return parsedEvents
  }

  try {
    while (true) {
      if (aborted) break
      const { done, value } = await reader.read()
      if (done) break
      const parsedEvents = consumeText(decoder.decode(value, { stream: true }))
      for (const parsed of parsedEvents) yield parsed
    }
    for (const parsed of consumeText(decoder.decode())) yield parsed
    previousWasCarriageReturn = false
    if (currentLine !== '') frameLines.push(currentLine)
    if (frameLines.length > 0) {
      const parsed = parseFrame(frameLines)
      if (parsed) yield parsed
    }
  } finally {
    signal?.removeEventListener('abort', onAbort)
    reader.releaseLock?.()
  }
}

function parseFrame(lines: string[]): StreamedAgUiEvent | null {
  let seq: number | null = null
  const dataLines: string[] = []
  for (const line of lines) {
    if (line.startsWith('id:')) {
      const value = line.slice(3).trim()
      const parsed = /^\d+$/.test(value) ? Number(value) : Number.NaN
      seq = Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null
    } else if (line.startsWith('data:')) {
      dataLines.push(line.slice(5).replace(/^ /, ''))
    }
  }
  if (dataLines.length === 0) return null
  let parsedValue: unknown
  try {
    parsedValue = JSON.parse(dataLines.join('\n')) as unknown
  } catch {
    throw new Error('事件流包含无法解析的数据')
  }
  const event = parseConversationAgUiEvent(parsedValue)
  return { event, seq }
}

async function* streamConversationEvents(
  payload: ChatRequestPayload,
  signal?: AbortSignal,
  afterSeq?: number,
): AsyncGenerator<StreamedAgUiEvent> {
  const response = await requestEventStream(conversationChatUrl(), {
    method: 'POST',
    body: payload,
    signal,
    headers: afterSeq == null
      ? undefined
      : { 'Last-Event-ID': String(afterSeq) },
    suppressGlobalError: true,
  })
  if (!response.body) throw new Error('chat 接口没有返回事件流')
  for await (const item of parseAgUiSseStream(response.body, signal)) {
    yield item
  }
}

export const startConversationRun = streamConversationEvents
export const resumeConversationRun = streamConversationEvents

export interface CancelConversationRunResult {
  cancelled: boolean
}

export const cancelConversationRun = (
  threadId: string,
  runId: string,
  signal?: AbortSignal,
): Promise<CancelConversationRunResult> => requestJson<CancelConversationRunResult>(
  `/api/conversation/${encodeURIComponent(threadId)}/runs/${encodeURIComponent(runId)}/cancel`,
  {
    method: 'POST',
    signal,
    suppressGlobalError: true,
  },
)
