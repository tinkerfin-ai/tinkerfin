import type { ChatRequestPayload, ConversationAgUiEvent } from './types'
import { conversationChatUrl } from './config'
import { ConversationError } from './errors'
import { parseConversationAgUiEvent } from './eventParser'
import { requestEventStream, requestJson } from '../shared/http'

export interface StreamedAgUiEvent {
  event: ConversationAgUiEvent
  /** SSE `id:` 行提供的持久化事件序号；生产端省略时为 null */
  seq: number | null
}

const MAX_SSE_DATA_BYTES = 4 * 1024 * 1024
const MAX_SSE_LINE_BYTES = MAX_SSE_DATA_BYTES + 'data: '.length
const MAX_SSE_FRAME_BYTES = MAX_SSE_LINE_BYTES + 64
const MAX_SSE_FRAME_LINES = 4096

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
  const encoder = new TextEncoder()
  const reader = body.getReader()
  let currentLine = ''
  let currentLineBytes = 0
  let frameLines: string[] = []
  let frameBytes = 0
  let frameDataBytes = 0
  let previousWasCarriageReturn = false
  let aborted = false
  let naturalEof = false
  let cancelTask: Promise<void> | null = null

  const cancelReader = (reason: unknown) => {
    cancelTask ??= reader.cancel(reason).then(() => undefined).catch(() => undefined)
    return cancelTask
  }
  const onAbort = () => {
    aborted = true
    void cancelReader(signal?.reason)
  }
  if (signal?.aborted) onAbort()
  else signal?.addEventListener('abort', onAbort)

  const rejectLimit = (): never => {
    throw new ConversationError('stream_limit_exceeded')
  }

  const appendLineText = (text: string) => {
    if (!text) return
    currentLine += text
    currentLineBytes += encoder.encode(text).byteLength
    if (currentLineBytes > MAX_SSE_LINE_BYTES) rejectLimit()
  }

  const finishLine = () => {
    if (currentLine === '') {
      const parsed = frameLines.length > 0 ? parseFrame(frameLines) : null
      frameLines = []
      frameBytes = 0
      frameDataBytes = 0
      return parsed
    }
    if (frameLines.length >= MAX_SSE_FRAME_LINES) rejectLimit()
    frameBytes += currentLineBytes + 2
    if (frameBytes > MAX_SSE_FRAME_BYTES) rejectLimit()
    if (currentLine.startsWith('data:')) {
      const data = currentLine.slice(5).replace(/^ /, '')
      frameDataBytes += encoder.encode(data).byteLength
        + (frameDataBytes > 0 ? 1 : 0)
      if (frameDataBytes > MAX_SSE_DATA_BYTES) rejectLimit()
    }
    frameLines.push(currentLine)
    currentLine = ''
    currentLineBytes = 0
    return null
  }

  function* consumeText(text: string): Generator<StreamedAgUiEvent> {
    let segmentStart = 0
    for (let index = 0; index < text.length; index += 1) {
      const character = text[index]
      if (previousWasCarriageReturn) {
        previousWasCarriageReturn = false
        if (character === '\n') {
          segmentStart = index + 1
          continue
        }
      }
      if (character === '\r') {
        appendLineText(text.slice(segmentStart, index))
        const parsed = finishLine()
        if (parsed) yield parsed
        previousWasCarriageReturn = true
        segmentStart = index + 1
      } else if (character === '\n') {
        appendLineText(text.slice(segmentStart, index))
        const parsed = finishLine()
        if (parsed) yield parsed
        segmentStart = index + 1
      }
    }
    appendLineText(text.slice(segmentStart))
  }

  try {
    while (true) {
      if (aborted) break
      const { done, value } = await reader.read()
      if (done) {
        naturalEof = !aborted
        break
      }
      for (const parsed of consumeText(decoder.decode(value, { stream: true }))) yield parsed
    }
    if (aborted) return
    for (const parsed of consumeText(decoder.decode())) yield parsed
    previousWasCarriageReturn = false
    if (currentLine !== '') finishLine()
    if (frameLines.length > 0) {
      const parsed = parseFrame(frameLines)
      if (parsed) yield parsed
    }
  } catch (error) {
    await cancelReader(error)
    throw error
  } finally {
    signal?.removeEventListener('abort', onAbort)
    if (!naturalEof) {
      await cancelReader(signal?.reason ?? new Error('事件流消费在 EOF 前结束'))
    } else if (cancelTask) {
      await cancelTask
    }
    reader.releaseLock()
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
  } catch (error) {
    throw new ConversationError('stream_data_invalid', error)
  }
  try {
    const event = parseConversationAgUiEvent(parsedValue)
    return { event, seq }
  } catch (error) {
    throw new ConversationError('stream_event_invalid', error)
  }
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
  if (!response.body) throw new ConversationError('stream_body_missing')
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
