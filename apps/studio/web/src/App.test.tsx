import { StrictMode, useCallback, useEffect, useState } from 'react'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type {
  ConversationEventEnvelope,
  ConversationHistoryDetail,
  ConversationHistoryListItem,
  ConversationHistoryListResponse,
} from './api/conversation/history'
import type { ChatRequestPayload, ConversationAgUiEvent } from './api/conversation/types'
import type { AgentModelCatalog } from './api/models/types'
import { subscribeApiErrors } from './api/shared/http'
import { AUTH_SESSION_STORAGE_KEY, clearAuthSession, saveAuthSession } from './auth/session'
import { ToastViewport } from './components/ToastViewport'
import type { ToastItem, ToastKind } from './components/ToastViewport'
import { WorkspaceScreen } from './features/workspace/WorkspaceScreen'
import {
  readActiveRunSession,
  writeActiveRunSession,
} from './features/conversation/stream/activeRunSession'
import { normalizeAppLocation } from './lib/threadRoute'

const TEST_USER = {
  user_id: 7,
  username: 'yunsan',
  display_name: '云杉',
  roles: [],
  disabled: false,
}

function App() {
  const [toasts, setToasts] = useState<ToastItem[]>([])
  const onToast = useCallback((kind: ToastKind, message: string) => {
    setToasts((current) => [...current, {
      id: `test-toast-${current.length + 1}`,
      kind,
      message,
    }])
  }, [])
  useEffect(() => subscribeApiErrors((error) => {
    onToast('error', error.message)
  }), [onToast])
  return (
    <>
      <WorkspaceScreen user={TEST_USER} onLogout={vi.fn()} onToast={onToast} />
      <ToastViewport
        toasts={toasts}
        onDismiss={(id) => setToasts((current) => current.filter((toast) => toast.id !== id))}
      />
    </>
  )
}

const THREAD_ID = 'thread-live-1'
const SECOND_THREAD_ID = 'thread-live-2'
const FIRST_RUN_ID = 'run-main-1'
const SECOND_RUN_ID = 'run-main-2'
const WRITE_TODOS_CALL_ID = 'call-write-todos'
const WRITE_FILE_CALL_ID = 'call-write-file'
const INTERRUPT_ID = 'interrupt-write-file'
const BASE_TIME = '2026-08-03T09:00:00.000Z'

const originalWriteArgs = {
  file_path: 'result.txt',
  content: '原始写入内容',
}

const editedWriteArgs = {
  file_path: 'result.txt',
  content: '编辑后的写入内容',
}

function sseResponse(
  events: ConversationAgUiEvent[],
  keepOpen = false,
  onOpen?: (controller: ReadableStreamDefaultController<Uint8Array>) => void,
) {
  const payload = events
    .map((event) => `data: ${JSON.stringify(event)}\n\n`)
    .join('')

  return new Response(
    new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode(payload))
        if (!keepOpen) controller.close()
        else onOpen?.(controller)
      },
    }),
    {
      status: 200,
      headers: {
        'Content-Type': 'text/event-stream',
      },
    },
  )
}

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify({ code: 0, message: 'success', data: body }), {
    status: 200,
    headers: {
      'Content-Type': 'application/json',
    },
  })
}

function fetchCallUrl(input: RequestInfo | URL) {
  if (typeof input === 'string') return input
  if (input instanceof URL) return input.toString()
  return input.url
}

function fetchCallMethod(input: RequestInfo | URL, init?: RequestInit) {
  return init?.method
    ?? (typeof Request !== 'undefined' && input instanceof Request ? input.method : 'GET')
}

function historyListItem(overrides: Partial<ConversationHistoryListResponse['items'][number]> = {}) {
  return {
    id: 1,
    threadId: THREAD_ID,
    title: '默认历史会话',
    status: 'idle',
    lastRunId: FIRST_RUN_ID,
    lastModel: 'GPT-5.5',
    lastSeq: 0,
    messageCount: 1,
    toolCallCount: 0,
    hasPendingInterrupt: false,
    pinned: false,
    createdAt: BASE_TIME,
    updatedAt: BASE_TIME,
    ...overrides,
  }
}

function historyDetail(overrides: Partial<ConversationHistoryDetail> & { threadId: string; title: string }): ConversationHistoryDetail {
  const { threadId, title, ...rest } = overrides
  const assistantContent = `来自 ${title} 的历史回复`
  return {
    id: 1,
    threadId,
    title,
    status: 'idle',
    lastRunId: FIRST_RUN_ID,
    lastModel: 'GPT-5.5',
    lastSeq: 5,
    snapshotSeq: 0,
    snapshotVersion: 2,
    messageCount: 1,
    toolCallCount: 0,
    hasPendingInterrupt: false,
    snapshot: null,
    // 反映「后端返回全量事件」的恢复模型：前端 baseline 只从快照取用户消息，
    // assistant 文本/工具/HITL 均由回放事件重建。这里给出重建该条 assistant 消息
    // 的事件序列（与快照里的 assistant 内容一致），使刷新后内容与实时一致。
    events: assistantHistoryEvents(threadId, FIRST_RUN_ID, `${threadId}-assistant-1`, assistantContent),
    createdAt: BASE_TIME,
    updatedAt: BASE_TIME,
    ...rest,
    pinned: rest.pinned ?? false,
  }
}

function assistantHistoryEvents(
  threadId: string,
  runId: string,
  messageId: string,
  content: string,
): ConversationEventEnvelope[] {
  const mainRawEvent = {
    streamMode: 'messages' as const,
    source: { agentType: 'main' as const, agentName: 'main', namespace: [] },
    runId,
  }
  return [
    {
      seq: 1,
      eventId: `${messageId}-run-start`,
      eventType: 'RUN_STARTED',
      runId,
      event: { type: 'RUN_STARTED', threadId, runId },
      createdAt: BASE_TIME,
    },
    {
      seq: 2,
      eventId: `${messageId}-msg-start`,
      eventType: 'TEXT_MESSAGE_START',
      runId,
      event: { type: 'TEXT_MESSAGE_START', rawEvent: mainRawEvent, messageId, role: 'assistant' },
      createdAt: BASE_TIME,
    },
    {
      seq: 3,
      eventId: `${messageId}-msg-content`,
      eventType: 'TEXT_MESSAGE_CONTENT',
      runId,
      event: { type: 'TEXT_MESSAGE_CONTENT', rawEvent: mainRawEvent, messageId, delta: content },
      createdAt: BASE_TIME,
    },
    {
      seq: 4,
      eventId: `${messageId}-msg-end`,
      eventType: 'TEXT_MESSAGE_END',
      runId,
      event: { type: 'TEXT_MESSAGE_END', rawEvent: mainRawEvent, messageId },
      createdAt: BASE_TIME,
    },
    {
      seq: 5,
      eventId: `${messageId}-run-finish`,
      eventType: 'RUN_FINISHED',
      runId,
      event: { type: 'RUN_FINISHED', threadId, runId, outcome: { type: 'success' } },
      createdAt: BASE_TIME,
    },
  ]
}

type FetchMockOptions = {
  streams?: ConversationAgUiEvent[][]
  keepOpen?: boolean
  historyLists?: ConversationHistoryListResponse[]
  historyListResolver?: (requestIndex: number, chatRequestCount: number) => ConversationHistoryListResponse
  historyDetails?: Record<string, ConversationHistoryDetail>
  eventEnvelopes?: Record<string, ConversationEventEnvelope[]>
  eventEnvelopeResolver?: (
    threadId: string,
    afterSeq: number | null,
    requestIndex: number,
  ) => ConversationEventEnvelope[]
  modelCatalog?: AgentModelCatalog
}

const DEFAULT_MODEL_CATALOG: AgentModelCatalog = {
  items: [
    { modelId: 'GPT-5.5', displayName: 'GPT-5.5', reasoningEnabled: false, isDefault: true },
    { modelId: 'DeepSeek-V4-Pro', displayName: 'DeepSeek-V4-Pro', reasoningEnabled: true, isDefault: false },
    { modelId: 'Qwen-3.7', displayName: 'Qwen-3.7', reasoningEnabled: false, isDefault: false },
  ],
  defaultModelId: 'GPT-5.5',
}

function installFetchMock(options: FetchMockOptions = {}) {
  const {
    streams = [],
    keepOpen = false,
    historyLists = [{ items: [], nextCursor: null }],
    historyListResolver,
    historyDetails = {},
    eventEnvelopes = {},
    eventEnvelopeResolver,
    modelCatalog = DEFAULT_MODEL_CATALOG,
  } = options

  let streamIndex = 0
  let historyListIndex = 0
  let eventRequestIndex = 0
  const threadStore = new Map<string, ConversationHistoryListItem>(
    (historyLists[0]?.items ?? []).map((item) => [item.threadId, { ...item }]),
  )
  const patchCalls: Array<{ threadId: string; body: { title?: string; pinned?: boolean } }> = []
  const deleteCalls: string[] = []
  const openStreams = new Map<ReadableStreamDefaultController<Uint8Array>, string>()

  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const requestUrl = typeof input === 'string'
      ? input
      : input instanceof URL
        ? input.toString()
        : input.url
    const url = new URL(requestUrl, 'http://localhost')
    const request = typeof Request !== 'undefined' && input instanceof Request ? input : null
    const method = init?.method ?? request?.method ?? 'GET'

    if (url.pathname.endsWith('/api/models')) {
      return jsonResponse(modelCatalog)
    }

    if (method === 'POST' && /\/api\/conversation\/[^/]+\/runs\/[^/]+\/cancel$/.test(url.pathname)) {
      for (const [controller, runId] of openStreams) {
        controller.enqueue(new TextEncoder().encode(`data: ${JSON.stringify({
          type: 'RUN_ERROR',
          rawEvent: { runId },
          message: '聊天生成已取消',
          code: 'cancelled',
        })}\n\n`))
        controller.close()
      }
      openStreams.clear()
      return jsonResponse({ cancelled: true })
    }

    if (method === 'POST' && url.pathname.endsWith('/api/conversation/chat')) {
      const events = streams[streamIndex]
      streamIndex += 1
      if (!events) {
        throw new Error(`unexpected chat fetch call #${streamIndex}`)
      }
      const rawBody = init?.body != null
        ? String(init.body)
        : request
          ? await request.clone().text()
          : ''
      const chatRequest = rawBody
        ? JSON.parse(rawBody) as ChatRequestPayload
        : null
      const runId = chatRequest?.runId ?? FIRST_RUN_ID
      const serverEvents = chatRequest
        ? events.map((event) => {
            if (
              event.type !== 'RUN_STARTED'
              || event.rawEvent?.source?.agentType === 'subagent'
            ) return event
            const firstUserContent = chatRequest.messages.find(
              (message) => message.role === 'user',
            )?.content.trim()
            const title = event.title
              ?? threadStore.get(event.threadId)?.title
              ?? (firstUserContent ? firstUserContent.slice(0, 60) : undefined)
            return {
              ...event,
              ...(title ? { title } : {}),
              input: event.input ?? {
                ...chatRequest,
                threadId: event.threadId,
                messages: chatRequest.messages.map((message, index) => ({
                  ...message,
                  id: `message-server-${runId}-${index}`,
                })),
              },
            }
          })
        : events
      return sseResponse(serverEvents, keepOpen, (controller) => openStreams.set(controller, runId))
    }

    if (url.pathname.endsWith('/api/conversation/history')) {
      const response = historyListResolver
        ? historyListResolver(historyListIndex, streamIndex)
        : historyLists[Math.min(historyListIndex, historyLists.length - 1)] ?? { items: [], nextCursor: null }
      historyListIndex += 1
      return jsonResponse(response)
    }

    const historyMatch = url.pathname.match(/\/api\/conversation\/([^/]+)\/history$/)
    if (historyMatch) {
      const threadId = decodeURIComponent(historyMatch[1])
      const response = historyDetails[threadId]
      if (!response) throw new Error(`missing history detail for ${threadId}`)
      return jsonResponse(response)
    }

    const eventsMatch = url.pathname.match(/\/api\/conversation\/([^/]+)\/events$/)
    if (eventsMatch) {
      const threadId = decodeURIComponent(eventsMatch[1])
      const afterSeqText = url.searchParams.get('afterSeq')
      const afterSeq = afterSeqText == null ? null : Number(afterSeqText)
      const response = eventEnvelopeResolver
        ? eventEnvelopeResolver(threadId, afterSeq, eventRequestIndex)
        : (eventEnvelopes[threadId] ?? [])
      eventRequestIndex += 1
      return jsonResponse(response)
    }

    const threadMatch = url.pathname.match(/^\/api\/conversation\/([^/]+)$/)
    if (threadMatch) {
      const threadId = decodeURIComponent(threadMatch[1])
      if (method === 'PATCH') {
        const rawBody = init?.body != null
          ? String(init.body)
          : request
            ? await request.clone().text()
            : ''
        const body = rawBody ? (JSON.parse(rawBody) as { title?: string; pinned?: boolean }) : {}
        const existing = threadStore.get(threadId)
        if (!existing) throw new Error(`PATCH unknown thread ${threadId}`)
        const updated = {
          ...existing,
          ...(body.title != null ? { title: body.title } : {}),
          ...(body.pinned != null ? { pinned: body.pinned } : {}),
          updatedAt: BASE_TIME,
        }
        threadStore.set(threadId, updated)
        patchCalls.push({ threadId, body })
        return jsonResponse(updated)
      }
      if (method === 'DELETE') {
        threadStore.delete(threadId)
        deleteCalls.push(threadId)
        return new Response(null, { status: 204 })
      }
    }

    throw new Error(`unexpected fetch: ${method} ${url.pathname}`)
  })

  const instrumentedFetch = Object.assign(fetchMock, { patchCalls, deleteCalls })
  vi.stubGlobal('fetch', instrumentedFetch)
  return instrumentedFetch
}

async function sendMessage(message: string) {
  const user = userEvent.setup()
  await waitFor(() => expect(screen.getByRole('button', { name: '选择模型' })).not.toHaveTextContent('加载模型…'))
  await user.type(screen.getByLabelText('消息输入'), message)
  await user.click(screen.getByRole('button', { name: '发送消息' }))
  return user
}

function chatRequestAt(fetchMock: ReturnType<typeof installFetchMock>, index: number) {
  const chatCalls = fetchMock.mock.calls
    .filter(([input, init]) => (
      fetchCallMethod(input, init) === 'POST'
      && new URL(fetchCallUrl(input), 'http://localhost').pathname.endsWith('/api/conversation/chat')
    ))
  return chatCalls[index]?.[1]
}

describe('App', () => {
  beforeEach(() => {
    window.localStorage.clear()
    window.sessionStorage.clear()
    window.history.replaceState(null, '', '/')
    saveAuthSession({
      token: 'workspace-token',
      tokenType: 'Bearer',
      expiresAt: null,
      user: TEST_USER,
    })
  })

  afterEach(() => {
    vi.useRealTimers()
    clearAuthSession()
    vi.unstubAllGlobals()
  })

  it('renders the new empty-state without legacy demo content', async () => {
    installFetchMock()

    render(<App />)

    expect(await screen.findByText('发送一条消息，开始新的真实对话流。')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '回到底部' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '模型设置' })).not.toBeInTheDocument()
    expect(screen.queryByText('研究助手')).not.toBeInTheDocument()
    expect(screen.queryByText('季度现金流分析与风险建议')).not.toBeInTheDocument()
  })

  it('uses the shared overflow marquee before the selected model indicator', async () => {
    installFetchMock()
    const observe = vi.fn()
    const resizeCallbacks: Array<() => void> = []
    vi.stubGlobal('ResizeObserver', class {
      constructor(callback: ResizeObserverCallback) {
        resizeCallbacks.push(() => callback([], {} as ResizeObserver))
      }
      observe = observe
      disconnect = vi.fn()
    })
    const user = userEvent.setup()
    render(<App />)

    await user.click(screen.getByRole('button', { name: '选择模型' }))

    const selectedOption = screen.getByRole('option', { name: 'GPT-5.5' })
    expect(selectedOption).toHaveClass('overflow-marquee-trigger')
    expect(selectedOption.querySelector('.model-option-label')).toContainElement(within(selectedOption).getByText('GPT-5.5'))
    expect(selectedOption.querySelector('.model-option-check')).toContainElement(selectedOption.querySelector('.lucide-check'))
    expect(observe).toHaveBeenCalledWith(selectedOption.querySelector('.model-option-label'))

    const longOption = screen.getByRole('option', { name: 'DeepSeek-V4-Pro' })
    const longViewport = longOption.querySelector('.model-option-label') as HTMLElement
    const longLabel = within(longOption).getByText('DeepSeek-V4-Pro')
    Object.defineProperty(longViewport, 'clientWidth', { configurable: true, value: 60 })
    Object.defineProperty(longLabel, 'scrollWidth', { configurable: true, value: 96 })
    act(() => resizeCallbacks.forEach((resize) => resize()))

    expect(longLabel).toHaveClass('is-overflowing')
    expect(longLabel.style.getPropertyValue('--overflow-marquee-distance')).toBe('36px')
    expect(longLabel.style.getPropertyValue('--overflow-marquee-pause-duration')).toBe('800ms')
    expect(longLabel.style.getPropertyValue('--overflow-marquee-travel-duration')).toBe('1000ms')
  })

  it('uses lowercase Agent modes and forwards the selected plan mode', async () => {
    const fetchMock = installFetchMock({ streams: [[]] })
    const user = userEvent.setup()
    render(<App />)

    const presetButton = screen.getByRole('button', { name: '当前 Agent 预设' })
    await user.click(presetButton)

    expect(screen.getByRole('option', { name: 'default' })).toHaveAttribute('aria-selected', 'true')
    await user.click(screen.getByRole('option', { name: 'plan' }))

    expect(presetButton).toHaveTextContent('plan')
    expect(within(presetButton).getByText('plan')).toHaveClass('agent-preset-label')
    expect(presetButton).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByRole('listbox', { name: 'Agent 预设选项' })).not.toBeInTheDocument()

    await sendMessage('按计划处理')
    const request = JSON.parse(String(chatRequestAt(fetchMock, 0)?.body)) as ChatRequestPayload
    expect(request.forwardedProps).toEqual({ model: 'GPT-5.5', mode: 'plan' })
  })

  it('uses the backend model catalog instead of a hardcoded frontend list', async () => {
    const fetchMock = installFetchMock({
      streams: [[]],
      modelCatalog: {
        items: [
          { modelId: 'database-main', displayName: 'Database Main', reasoningEnabled: true, isDefault: true },
        ],
        defaultModelId: 'database-main',
      },
    })
    render(<App />)

    await waitFor(() => expect(screen.getByRole('button', { name: '选择模型' })).toHaveTextContent('Database Main'))
    await sendMessage('使用数据库模型')
    const request = JSON.parse(String(chatRequestAt(fetchMock, 0)?.body)) as ChatRequestPayload

    expect(request.forwardedProps).toEqual({ model: 'database-main', mode: 'default' })
    expect(screen.queryByText('GPT-5.5')).not.toBeInTheDocument()
  })

  it('supports the complete keyboard listbox model and returns focus to each trigger', async () => {
    installFetchMock()
    const user = userEvent.setup()
    render(<App />)
    const modelTrigger = screen.getByRole('button', { name: '选择模型' })

    await user.click(modelTrigger)
    const modelListbox = screen.getByRole('listbox', { name: '模型选项' })
    expect(modelListbox).toHaveFocus()
    expect(document.getElementById(modelListbox.getAttribute('aria-activedescendant') ?? ''))
      .toHaveTextContent('GPT-5.5')

    fireEvent.keyDown(modelListbox, { key: 'End' })
    expect(document.getElementById(modelListbox.getAttribute('aria-activedescendant') ?? ''))
      .toHaveTextContent('Qwen-3.7')
    fireEvent.keyDown(modelListbox, { key: 'Home' })
    fireEvent.keyDown(modelListbox, { key: 'ArrowDown' })
    expect(document.getElementById(modelListbox.getAttribute('aria-activedescendant') ?? ''))
      .toHaveTextContent('DeepSeek-V4-Pro')
    fireEvent.keyDown(modelListbox, { key: 'Enter' })
    expect(modelTrigger).toHaveTextContent('DeepSeek-V4-Pro')
    expect(modelTrigger).toHaveFocus()

    await user.click(modelTrigger)
    fireEvent.keyDown(screen.getByRole('listbox', { name: '模型选项' }), { key: 'Escape' })
    expect(screen.queryByRole('listbox', { name: '模型选项' })).not.toBeInTheDocument()
    expect(modelTrigger).toHaveFocus()

    const presetTrigger = screen.getByRole('button', { name: '当前 Agent 预设' })
    await user.click(presetTrigger)
    const presetListbox = screen.getByRole('listbox', { name: 'Agent 预设选项' })
    fireEvent.keyDown(presetListbox, { key: 'ArrowUp' })
    fireEvent.keyDown(presetListbox, { key: ' ' })
    expect(presetTrigger).toHaveTextContent('plan')
    expect(presetTrigger).toHaveFocus()
  })

  it('places the persisted three-segment theme switcher before the Agent preset', async () => {
    installFetchMock()
    render(<App />)

    const themeSwitcher = screen.getByRole('group', { name: '主题' })
    const presetButton = screen.getByRole('button', { name: '当前 Agent 预设' })
    const relativePosition = themeSwitcher.compareDocumentPosition(presetButton)

    expect(relativePosition & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.getByRole('radio', { name: '跟随系统' })).not.toBeChecked()
    expect(screen.getByRole('radio', { name: '浅色' })).toBeChecked()
    expect(screen.getByRole('radio', { name: '深色' })).not.toBeChecked()
  })

  it('shows non-success business codes through the global error toast', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(
      JSON.stringify({
        code: 1_001_004_001,
        message: '历史分页游标已失效',
        data: null,
      }),
      { status: 422, headers: { 'Content-Type': 'application/json' } },
    )))

    render(<App />)

    expect(await screen.findByRole('alert')).toHaveTextContent('历史分页游标已失效')
  })

  it('does not surface authentication failures through the workspace toast channel', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(
      JSON.stringify({
        code: 401,
        message: '登录已失效，请重新登录',
        data: null,
      }),
      { status: 401, headers: { 'Content-Type': 'application/json' } },
    )))

    render(<App />)

    await waitFor(() => expect(window.localStorage.getItem(AUTH_SESSION_STORAGE_KEY)).toBeNull())
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('hydrates backend history list/detail and switches conversations from the sidebar', async () => {
    installFetchMock({
      historyLists: [{
        items: [
          historyListItem({ threadId: THREAD_ID, title: '第一条会话' }),
          historyListItem({ id: 2, threadId: SECOND_THREAD_ID, title: '第二条会话', lastRunId: 'run-second-1' }),
        ],
        nextCursor: null,
      }],
      historyDetails: {
        [THREAD_ID]: historyDetail({ threadId: THREAD_ID, title: '第一条会话' }),
        [SECOND_THREAD_ID]: historyDetail({ threadId: SECOND_THREAD_ID, title: '第二条会话' }),
      },
    })

    const user = userEvent.setup()
    render(<App />)

    expect(await screen.findByText('来自 第一条会话 的历史回复')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '打开会话：第二条会话' }))
    expect(await screen.findByText('来自 第二条会话 的历史回复')).toBeInTheDocument()
  })

  it('cancels stale hydration when switching threads and disables the composer meanwhile', async () => {
    let firstDetailSignal: AbortSignal | undefined
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const request = input instanceof Request ? input : new Request(input)
      const url = new URL(request.url)
      if (url.pathname.endsWith('/api/models')) return jsonResponse(DEFAULT_MODEL_CATALOG)
      if (url.pathname.endsWith('/api/conversation/history')) {
        return jsonResponse({
          items: [
            historyListItem({ threadId: THREAD_ID, title: '慢会话' }),
            historyListItem({ id: 2, threadId: SECOND_THREAD_ID, title: '快会话' }),
          ],
          nextCursor: null,
        })
      }
      if (url.pathname.endsWith(`/${THREAD_ID}/history`)) {
        firstDetailSignal = request.signal
        return await new Promise<Response>((_resolve, reject) => {
          request.signal.addEventListener('abort', () => {
            reject(new DOMException('请求已取消', 'AbortError'))
          }, { once: true })
        })
      }
      if (url.pathname.endsWith(`/${SECOND_THREAD_ID}/history`)) {
        return jsonResponse(historyDetail({ threadId: SECOND_THREAD_ID, title: '快会话' }))
      }
      if (url.pathname.endsWith('/events')) return jsonResponse([])
      throw new Error(`unexpected fetch: ${url.pathname}`)
    }))
    const user = userEvent.setup()
    render(<App />)

    await waitFor(() => expect(firstDetailSignal).toBeDefined())
    expect(screen.getByLabelText('消息输入')).toBeDisabled()
    expect(screen.getByPlaceholderText('正在加载会话…')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '打开会话：快会话' }))

    expect(await screen.findByText('来自 快会话 的历史回复')).toBeInTheDocument()
    expect(firstDetailSignal?.aborted).toBe(true)
  })

  it('releases the composer after hydration fails and retries without losing the draft', async () => {
    let detailRequestCount = 0
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const request = input instanceof Request ? input : new Request(input)
      const url = new URL(request.url)
      if (url.pathname.endsWith('/api/models')) return jsonResponse(DEFAULT_MODEL_CATALOG)
      if (url.pathname.endsWith('/api/conversation/history')) {
        return jsonResponse({
          items: [historyListItem({ threadId: THREAD_ID, title: '可重试会话' })],
          nextCursor: null,
        })
      }
      if (url.pathname.endsWith(`/${THREAD_ID}/history`)) {
        detailRequestCount += 1
        if (detailRequestCount === 1) {
          return new Response(JSON.stringify({
            code: 1_001_005_001,
            message: '详情暂时不可用',
            data: null,
          }), {
            status: 503,
            headers: { 'Content-Type': 'application/json' },
          })
        }
        return jsonResponse(historyDetail({ threadId: THREAD_ID, title: '可重试会话' }))
      }
      if (url.pathname.endsWith(`/${THREAD_ID}/events`)) return jsonResponse([])
      throw new Error(`unexpected fetch: ${url.pathname}`)
    }))
    const user = userEvent.setup()
    render(<App />)

    await waitFor(() => expect(detailRequestCount).toBe(1))
    const input = screen.getByLabelText('消息输入')
    await waitFor(() => expect(input).toBeEnabled())
    expect(await screen.findByText('会话加载失败，请重试')).toBeInTheDocument()

    await user.type(input, '保留的草稿')
    await user.click(screen.getByRole('button', { name: '发送消息' }))

    expect(await screen.findByText('来自 可重试会话 的历史回复')).toBeInTheDocument()
    expect(detailRequestCount).toBe(2)
    expect(input).toHaveValue('保留的草稿')
  })

  it('cancels the history bootstrap request when the workspace unmounts', async () => {
    let bootstrapSignal: AbortSignal | undefined
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const request = input instanceof Request ? input : new Request(input)
      bootstrapSignal = request.signal
      return await new Promise<Response>((_resolve, reject) => {
        request.signal.addEventListener('abort', () => {
          reject(new DOMException('请求已取消', 'AbortError'))
        }, { once: true })
      })
    }))
    const view = render(<App />)
    await waitFor(() => expect(bootstrapSignal).toBeDefined())

    view.unmount()

    expect(bootstrapSignal?.aborted).toBe(true)
  })

  it('restores the conversation from ?thread= on boot and keeps the URL in sync on switch', async () => {
    // Simulate a refresh that landed on /?thread=SECOND_THREAD_ID.
    window.history.replaceState(null, '', `/?thread=${SECOND_THREAD_ID}`)
    installFetchMock({
      historyLists: [{
        items: [
          historyListItem({ threadId: THREAD_ID, title: '第一条会话' }),
          historyListItem({ id: 2, threadId: SECOND_THREAD_ID, title: '第二条会话', lastRunId: 'run-second-1' }),
        ],
        nextCursor: null,
      }],
      historyDetails: {
        [THREAD_ID]: historyDetail({ threadId: THREAD_ID, title: '第一条会话' }),
        [SECOND_THREAD_ID]: historyDetail({ threadId: SECOND_THREAD_ID, title: '第二条会话' }),
      },
    })

    const user = userEvent.setup()
    render(<App />)

    // Boot selects the second conversation (from ?thread=), not conversations[0].
    expect(await screen.findByText('来自 第二条会话 的历史回复')).toBeInTheDocument()
    expect(window.location.search).toContain(`thread=${SECOND_THREAD_ID}`)

    // Switching to the first conversation updates the URL.
    await user.click(screen.getByRole('button', { name: '打开会话：第一条会话' }))
    expect(await screen.findByText('来自 第一条会话 的历史回复')).toBeInTheDocument()
    expect(window.location.search).toContain(`thread=${THREAD_ID}`)
  })

  it('reattaches a persisted active run after refresh using its durable cursor', async () => {
    window.history.replaceState(null, '', `/?thread=${THREAD_ID}`)
    const activePayload: ChatRequestPayload = {
      threadId: THREAD_ID,
      runId: FIRST_RUN_ID,
      state: {},
      messages: [{ id: 'request-first-run', role: 'user', content: '刷新后继续' }],
      tools: [],
      context: [],
      forwardedProps: { model: 'GPT-5.5', mode: 'default' },
    }
    writeActiveRunSession({
      threadId: THREAD_ID,
      payload: activePayload,
      mode: 'start',
      lastSeq: 3,
    })
    const rawEvent = {
      streamMode: 'messages' as const,
      source: { agentType: 'main' as const, agentName: 'main', namespace: [] },
      runId: FIRST_RUN_ID,
    }
    const fetchMock = installFetchMock({
      historyLists: [{
        items: [historyListItem({
          threadId: THREAD_ID,
          title: '刷新续传',
          status: 'running',
          lastRunId: FIRST_RUN_ID,
          lastSeq: 3,
        })],
        nextCursor: null,
      }],
      historyDetails: {
        [THREAD_ID]: historyDetail({
          threadId: THREAD_ID,
          title: '刷新续传',
          status: 'running',
          lastRunId: FIRST_RUN_ID,
          lastSeq: 3,
          events: [
            {
              seq: 1,
              eventId: 'refresh-run-start',
              eventType: 'RUN_STARTED',
              runId: FIRST_RUN_ID,
              event: {
                type: 'RUN_STARTED',
                threadId: THREAD_ID,
                runId: FIRST_RUN_ID,
                input: {
                  ...activePayload,
                  messages: [{ id: 'server-user', role: 'user', content: '刷新后继续' }],
                },
              },
              createdAt: BASE_TIME,
            },
            {
              seq: 2,
              eventId: 'refresh-text-start',
              eventType: 'TEXT_MESSAGE_START',
              runId: FIRST_RUN_ID,
              event: {
                type: 'TEXT_MESSAGE_START',
                rawEvent,
                messageId: 'refresh-assistant',
                role: 'assistant',
              },
              createdAt: BASE_TIME,
            },
            {
              seq: 3,
              eventId: 'refresh-text-content',
              eventType: 'TEXT_MESSAGE_CONTENT',
              runId: FIRST_RUN_ID,
              event: {
                type: 'TEXT_MESSAGE_CONTENT',
                rawEvent,
                messageId: 'refresh-assistant',
                delta: '刷新前',
              },
              createdAt: BASE_TIME,
            },
          ],
        }),
      },
      streams: [[
        {
          type: 'TEXT_MESSAGE_CONTENT',
          rawEvent,
          messageId: 'refresh-assistant',
          delta: '刷新后继续',
        },
        {
          type: 'TEXT_MESSAGE_END',
          rawEvent,
          messageId: 'refresh-assistant',
        },
        {
          type: 'RUN_FINISHED',
          threadId: THREAD_ID,
          runId: FIRST_RUN_ID,
          outcome: { type: 'success' },
        },
      ]],
    })

    render(<App />)

    expect(await screen.findByText('刷新前刷新后继续')).toBeInTheDocument()
    await waitFor(() => {
      const request = chatRequestAt(fetchMock, 0)
      expect(new Headers(request?.headers).get('Last-Event-ID')).toBe('3')
      expect(JSON.parse(String(request?.body))).toEqual(activePayload)
    })
    expect(readActiveRunSession()).toBeNull()
    expect(screen.queryByRole('button', { name: '停止任务' })).not.toBeInTheDocument()
  })

  it('restores a routed conversation that is outside the first history page', async () => {
    window.history.replaceState(null, '', `/?thread=${SECOND_THREAD_ID}`)
    const fetchMock = installFetchMock({
      historyLists: [{
        items: [historyListItem({ threadId: THREAD_ID, title: '首屏置顶会话', pinned: true })],
        nextCursor: 'next-page',
      }],
      historyDetails: {
        [SECOND_THREAD_ID]: historyDetail({ threadId: SECOND_THREAD_ID, title: '首屏之外的会话' }),
      },
    })

    render(<StrictMode><App /></StrictMode>)

    expect(await screen.findByText('来自 首屏之外的会话 的历史回复')).toBeInTheDocument()
    expect(window.location.search).toContain(`thread=${SECOND_THREAD_ID}`)
    expect(screen.getByRole('button', { name: '打开会话：首屏之外的会话' })).toBeInTheDocument()
    const requestUrls = fetchMock.mock.calls.map(([input]) => fetchCallUrl(input))
    expect(requestUrls.filter((url) => url.endsWith('/api/conversation/history?pageSize=5'))).toHaveLength(1)
    expect(requestUrls.filter((url) => url.endsWith(`/api/conversation/${SECOND_THREAD_ID}/history`))).toHaveLength(1)
  })

  it('restores the current conversation scroll position after remount', async () => {
    window.history.replaceState(null, '', `/?thread=${THREAD_ID}`)
    installFetchMock({
      historyLists: [{
        items: [historyListItem({ threadId: THREAD_ID, title: '滚动位置会话' })],
        nextCursor: null,
      }],
      historyDetails: {
        [THREAD_ID]: historyDetail({ threadId: THREAD_ID, title: '滚动位置会话' }),
      },
    })

    const firstMount = render(<App />)
    await screen.findByText('来自 滚动位置会话 的历史回复')
    const firstPane = screen.getByLabelText('对话内容')
    firstPane.scrollTop = 240
    fireEvent.scroll(firstPane)
    firstMount.unmount()

    render(<App />)
    await screen.findByText('来自 滚动位置会话 的历史回复')
    expect(screen.getByLabelText('对话内容').scrollTop).toBe(240)
  })

  it('restores the routed conversation scroll position after correcting an unsupported path', async () => {
    window.history.replaceState(null, '', `/?thread=${THREAD_ID}`)
    normalizeAppLocation()
    window.sessionStorage.setItem(`tinkerfin:conversation-scroll:${THREAD_ID}`, '240')
    window.history.replaceState(null, '', '/sssssssssssssssd#top')
    normalizeAppLocation()
    installFetchMock({
      historyLists: [{
        items: [historyListItem({ threadId: THREAD_ID, title: '非法路径前的会话' })],
        nextCursor: null,
      }],
      historyDetails: {
        [THREAD_ID]: historyDetail({ threadId: THREAD_ID, title: '非法路径前的会话' }),
      },
    })

    render(<App />)

    await screen.findByText('来自 非法路径前的会话 的历史回复')
    expect(window.location.pathname).toBe('/')
    expect(window.location.search).toContain(`thread=${THREAD_ID}`)
    expect(screen.getByLabelText('对话内容').scrollTop).toBe(240)
  })

  it('restores each conversation scroll position when switching through history', async () => {
    window.history.replaceState(null, '', `/?thread=${THREAD_ID}`)
    installFetchMock({
      historyLists: [{
        items: [
          historyListItem({ threadId: THREAD_ID, title: '第一条滚动会话' }),
          historyListItem({ id: 2, threadId: SECOND_THREAD_ID, title: '第二条滚动会话', lastRunId: 'run-second-1' }),
        ],
        nextCursor: null,
      }],
      historyDetails: {
        [THREAD_ID]: historyDetail({ threadId: THREAD_ID, title: '第一条滚动会话' }),
        [SECOND_THREAD_ID]: historyDetail({ threadId: SECOND_THREAD_ID, title: '第二条滚动会话' }),
      },
    })

    const user = userEvent.setup()
    render(<App />)

    await screen.findByText('来自 第一条滚动会话 的历史回复')
    const pane = screen.getByLabelText('对话内容')
    Object.defineProperties(pane, {
      clientHeight: { configurable: true, value: 500 },
      scrollHeight: { configurable: true, value: 1200 },
      scrollTop: { configurable: true, writable: true, value: 0 },
    })

    pane.scrollTop = 240
    fireEvent.scroll(pane)
    expect(window.sessionStorage.getItem(`tinkerfin:conversation-scroll:${THREAD_ID}`)).toBe('240')

    await user.click(screen.getByRole('button', { name: '打开会话：第二条滚动会话' }))
    await screen.findByText('来自 第二条滚动会话 的历史回复')
    pane.scrollTop = 420
    fireEvent.scroll(pane)
    expect(window.sessionStorage.getItem(`tinkerfin:conversation-scroll:${SECOND_THREAD_ID}`)).toBe('420')

    await user.click(screen.getByRole('button', { name: '打开会话：第一条滚动会话' }))
    await screen.findByText('来自 第一条滚动会话 的历史回复')
    await waitFor(() => expect(pane.scrollTop).toBe(240))

    await user.click(screen.getByRole('button', { name: '打开会话：第二条滚动会话' }))
    await screen.findByText('来自 第二条滚动会话 的历史回复')
    await waitFor(() => expect(pane.scrollTop).toBe(420))
  })

  it('pins a conversation through the sidebar context menu (PATCH + pinned group)', async () => {
    const fetchMock = installFetchMock({
      historyLists: [{
        items: [
          historyListItem({ threadId: THREAD_ID, title: '第一条会话' }),
          historyListItem({ id: 2, threadId: SECOND_THREAD_ID, title: '第二条会话' }),
        ],
        nextCursor: null,
      }],
      historyDetails: {
        [THREAD_ID]: historyDetail({ threadId: THREAD_ID, title: '第一条会话' }),
        [SECOND_THREAD_ID]: historyDetail({ threadId: SECOND_THREAD_ID, title: '第二条会话' }),
      },
    })
    const user = userEvent.setup()
    render(<App />)

    await screen.findByText('来自 第一条会话 的历史回复')

    await user.click(screen.getByRole('button', { name: '管理会话：第一条会话' }))
    await user.click(screen.getByRole('button', { name: '置顶' }))

    await waitFor(() => {
      expect(fetchMock.patchCalls).toContainEqual({ threadId: THREAD_ID, body: { pinned: true } })
    })
    expect(await screen.findByRole('img', { name: '已置顶' })).toBeInTheDocument()
  })

  it('renames a conversation with the custom dialog and reports success through a toast', async () => {
    const fetchMock = installFetchMock({
      historyLists: [{
        items: [historyListItem({ threadId: THREAD_ID, title: '旧名称' })],
        nextCursor: null,
      }],
      historyDetails: {
        [THREAD_ID]: historyDetail({ threadId: THREAD_ID, title: '旧名称' }),
      },
    })
    const user = userEvent.setup()
    render(<App />)

    await screen.findByText('来自 旧名称 的历史回复')
    await user.click(screen.getByRole('button', { name: '管理会话：旧名称' }))
    await user.click(screen.getByRole('button', { name: '重命名' }))

    const dialog = await screen.findByRole('dialog', { name: '重命名会话' })
    const input = within(dialog).getByRole('textbox', { name: '会话名称' })
    await user.clear(input)
    await user.type(input, '新名称')
    await user.click(within(dialog).getByRole('button', { name: '保存' }))

    await waitFor(() => {
      expect(fetchMock.patchCalls).toContainEqual({ threadId: THREAD_ID, body: { title: '新名称' } })
    })
    expect(await screen.findByRole('button', { name: '打开会话：新名称' })).toBeInTheDocument()
    expect(await screen.findByText('会话已重命名')).toBeInTheDocument()
  })

  it('returns focus to the conversation menu trigger when rename is cancelled', async () => {
    installFetchMock({
      historyLists: [{
        items: [historyListItem({ threadId: THREAD_ID, title: '焦点会话' })],
        nextCursor: null,
      }],
      historyDetails: {
        [THREAD_ID]: historyDetail({ threadId: THREAD_ID, title: '焦点会话' }),
      },
    })
    const user = userEvent.setup()
    render(<App />)
    await screen.findByText('来自 焦点会话 的历史回复')
    const trigger = screen.getByRole('button', { name: '管理会话：焦点会话' })

    await user.click(trigger)
    await user.click(screen.getByRole('button', { name: '重命名' }))
    fireEvent.keyDown(await screen.findByRole('dialog', { name: '重命名会话' }), { key: 'Escape' })

    expect(trigger).toHaveFocus()
  })

  it('deletes a conversation through the sidebar context menu (DELETE + removed from list)', async () => {
    const fetchMock = installFetchMock({
      historyLists: [{
        items: [
          historyListItem({ threadId: THREAD_ID, title: '第一条会话' }),
          historyListItem({ id: 2, threadId: SECOND_THREAD_ID, title: '第二条会话' }),
        ],
        nextCursor: null,
      }],
      historyDetails: {
        [THREAD_ID]: historyDetail({ threadId: THREAD_ID, title: '第一条会话' }),
        [SECOND_THREAD_ID]: historyDetail({ threadId: SECOND_THREAD_ID, title: '第二条会话' }),
      },
    })
    const user = userEvent.setup()
    render(<App />)

    await screen.findByText('来自 第一条会话 的历史回复')

    await user.click(screen.getByRole('button', { name: '管理会话：第二条会话' }))
    await user.click(screen.getByRole('button', { name: '删除' }))
    await user.click(within(await screen.findByRole('dialog', { name: '删除会话' })).getByRole('button', { name: '删除' }))

    await waitFor(() => {
      expect(fetchMock.deleteCalls).toContain(SECOND_THREAD_ID)
    })
    await waitFor(() => {
      expect(screen.queryByRole('button', { name: '打开会话：第二条会话' })).not.toBeInTheDocument()
    })
    expect(await screen.findByText('会话已删除')).toBeInTheDocument()
  })

  it('loads the next history page when scrolling the sidebar list to the bottom', async () => {
    const THIRD_THREAD_ID = 'thread-scroll-3'
    const fetchMock = installFetchMock({
      historyListResolver: (requestIndex) =>
        requestIndex === 0
          ? {
              items: [
                historyListItem({ threadId: THREAD_ID, title: '会话1' }),
                historyListItem({ id: 2, threadId: SECOND_THREAD_ID, title: '会话2' }),
              ],
              nextCursor: 'cursor-2',
            }
          : {
              items: [historyListItem({ id: 3, threadId: THIRD_THREAD_ID, title: '会话3' })],
              nextCursor: null,
            },
      historyDetails: {
        [THREAD_ID]: historyDetail({ threadId: THREAD_ID, title: '会话1' }),
        [SECOND_THREAD_ID]: historyDetail({ threadId: SECOND_THREAD_ID, title: '会话2' }),
        [THIRD_THREAD_ID]: historyDetail({ threadId: THIRD_THREAD_ID, title: '会话3' }),
      },
    })

    render(<App />)
    expect(await screen.findByRole('button', { name: '打开会话：会话1' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '打开会话：会话2' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '打开会话：会话3' })).not.toBeInTheDocument()

    const scrollEl = document.querySelector('.conversation-scroll') as HTMLElement
    expect(scrollEl).toBeTruthy()
    fireEvent.scroll(scrollEl)
    fireEvent.scroll(scrollEl)

    expect(await screen.findAllByTestId('history-skeleton')).toHaveLength(5)
    expect(screen.queryByRole('button', { name: '打开会话：会话3' })).not.toBeInTheDocument()
    expect(await screen.findByRole('button', { name: '打开会话：会话3' })).toBeInTheDocument()
    const listCalls = fetchMock.mock.calls.filter(([input, init]) =>
      fetchCallUrl(input).includes('/api/conversation/history')
      && fetchCallMethod(input, init) === 'GET')
    expect(listCalls).toHaveLength(2)

  })

  it('loads each successful history cursor only once when the sentinel stays visible', async () => {
    const observerCallbacks: IntersectionObserverCallback[] = []
    vi.stubGlobal('IntersectionObserver', class {
      readonly root = null
      readonly rootMargin = ''
      readonly thresholds = [0]
      constructor(callback: IntersectionObserverCallback) { observerCallbacks.push(callback) }
      observe() {}
      unobserve() {}
      disconnect() {}
      takeRecords() { return [] }
    })
    const THIRD_THREAD_ID = 'thread-cursor-once-3'
    const fetchMock = installFetchMock({
      historyListResolver: (requestIndex) => requestIndex === 0
        ? {
            items: [historyListItem({ threadId: THREAD_ID, title: '会话1' })],
            nextCursor: 'cursor-once',
          }
        : {
            items: [historyListItem({ id: 3, threadId: THIRD_THREAD_ID, title: '会话3' })],
            nextCursor: 'cursor-once',
          },
      historyDetails: {
        [THREAD_ID]: historyDetail({ threadId: THREAD_ID, title: '会话1' }),
        [THIRD_THREAD_ID]: historyDetail({ threadId: THIRD_THREAD_ID, title: '会话3' }),
      },
    })

    render(<App />)
    expect(await screen.findByRole('button', { name: '打开会话：会话1' })).toBeInTheDocument()
    await waitFor(() => expect(observerCallbacks.length).toBeGreaterThan(0))

    act(() => observerCallbacks.at(-1)?.([{ isIntersecting: true } as IntersectionObserverEntry], {} as IntersectionObserver))
    expect(await screen.findByRole('button', { name: '打开会话：会话3' })).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByLabelText('正在加载更多历史会话')).not.toBeInTheDocument())

    act(() => observerCallbacks.at(-1)?.([{ isIntersecting: true } as IntersectionObserverEntry], {} as IntersectionObserver))
    await new Promise((resolve) => window.setTimeout(resolve, 350))

    const listCalls = fetchMock.mock.calls.filter(([input, init]) =>
      fetchCallUrl(input).includes('/api/conversation/history')
      && fetchCallMethod(input, init) === 'GET')
    expect(listCalls).toHaveLength(2)
  })

  it('does not advance a hydrated event cursor from a newer history-list summary', async () => {
    const THIRD_THREAD_ID = 'thread-cursor-3'
    const fetchMock = installFetchMock({
      historyListResolver: (requestIndex) =>
        requestIndex === 0
          ? {
              items: [
                historyListItem({ threadId: THREAD_ID, title: '游标会话', lastSeq: 5 }),
                historyListItem({ id: 2, threadId: SECOND_THREAD_ID, title: '切换会话' }),
              ],
              nextCursor: 'cursor-2',
            }
          : {
              items: [
                historyListItem({ threadId: THREAD_ID, title: '游标会话', status: 'running', lastSeq: 10 }),
                historyListItem({ id: 3, threadId: THIRD_THREAD_ID, title: '第三条会话' }),
              ],
              nextCursor: null,
            },
      historyDetails: {
        [THREAD_ID]: historyDetail({ threadId: THREAD_ID, title: '游标会话', lastSeq: 5 }),
        [SECOND_THREAD_ID]: historyDetail({ threadId: SECOND_THREAD_ID, title: '切换会话' }),
        [THIRD_THREAD_ID]: historyDetail({ threadId: THIRD_THREAD_ID, title: '第三条会话' }),
      },
      eventEnvelopes: { [THREAD_ID]: [] },
    })

    const user = userEvent.setup()
    render(<App />)
    expect(await screen.findByText('来自 游标会话 的历史回复')).toBeInTheDocument()

    fireEvent.scroll(document.querySelector('.conversation-scroll') as HTMLElement)
    expect(await screen.findByRole('button', { name: '打开会话：第三条会话' })).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '打开会话：切换会话' }))
    expect(await screen.findByText('来自 切换会话 的历史回复')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '打开会话：游标会话' }))

    await waitFor(() => {
      const eventCalls = fetchMock.mock.calls
        .map(([input]) => fetchCallUrl(input))
        .filter((url) => url.includes(`/${THREAD_ID}/events`))
      expect(eventCalls.some((url) => url.includes('afterSeq=5'))).toBe(true)
      expect(eventCalls.some((url) => url.includes('afterSeq=10'))).toBe(false)
    })
  })

  it('does not auto-retry a failed visible pagination sentinel until the retry button is used', async () => {
    const observerCallbacks: IntersectionObserverCallback[] = []
    vi.stubGlobal('IntersectionObserver', class {
      readonly root = null
      readonly rootMargin = ''
      readonly thresholds = [0]
      constructor(callback: IntersectionObserverCallback) { observerCallbacks.push(callback) }
      observe() {}
      unobserve() {}
      disconnect() {}
      takeRecords() { return [] }
    })

    const fetchMock = installFetchMock({
      historyListResolver: (requestIndex) => {
        if (requestIndex === 0) {
          return {
            items: [historyListItem({ threadId: THREAD_ID, title: '会话1' })],
            nextCursor: 'cursor-failing',
          }
        }
        throw new Error('page failed')
      },
      historyDetails: {
        [THREAD_ID]: historyDetail({ threadId: THREAD_ID, title: '会话1' }),
      },
    })

    render(<App />)
    expect(await screen.findByRole('button', { name: '打开会话：会话1' })).toBeInTheDocument()
    await waitFor(() => expect(observerCallbacks.length).toBeGreaterThan(0))

    act(() => observerCallbacks.at(-1)?.([{ isIntersecting: true } as IntersectionObserverEntry], {} as IntersectionObserver))
    expect(await screen.findByText('历史记录加载失败')).toBeInTheDocument()
    expect(screen.queryByText('网络请求失败，请稍后重试。')).not.toBeInTheDocument()
    const callbacksAfterFailure = observerCallbacks.length
    await waitFor(() => expect(observerCallbacks.length).toBeGreaterThanOrEqual(callbacksAfterFailure))

    // A newly attached observer may immediately report the still-visible
    // sentinel again. It must not bypass the explicit retry affordance.
    act(() => observerCallbacks.at(-1)?.([{ isIntersecting: true } as IntersectionObserverEntry], {} as IntersectionObserver))
    await new Promise((resolve) => window.setTimeout(resolve, 350))
    const listCalls = fetchMock.mock.calls.filter(([input, init]) =>
      fetchCallUrl(input).includes('/api/conversation/history')
      && fetchCallMethod(input, init) === 'GET')
    expect(listCalls).toHaveLength(2)

    await userEvent.setup().click(screen.getByRole('button', { name: '重试加载历史' }))
    await waitFor(() => {
      const retriedCalls = fetchMock.mock.calls.filter(([input, init]) =>
        fetchCallUrl(input).includes('/api/conversation/history')
        && fetchCallMethod(input, init) === 'GET')
      expect(retriedCalls).toHaveLength(3)
    })
  })

  it('drains and invalidates queued stream frames before detaching', async () => {
    let nextFrameId = 1
    const frames = new Map<number, FrameRequestCallback>()
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
      const id = nextFrameId
      nextFrameId += 1
      frames.set(id, callback)
      return id
    })
    vi.stubGlobal('cancelAnimationFrame', (id: number) => { frames.delete(id) })
    installFetchMock({
      historyLists: [{ items: [], nextCursor: null }],
      streams: [[{ type: 'RUN_STARTED', threadId: THREAD_ID, runId: FIRST_RUN_ID }]],
      keepOpen: true,
    })

    const user = userEvent.setup()
    render(<App />)
    await sendMessage('同帧停止')
    await waitFor(() => expect(frames.size).toBeGreaterThan(0))
    await act(async () => {
      const pending = [...frames.values()]
      frames.clear()
      pending.forEach((callback) => callback(performance.now()))
    })
    await user.click(await screen.findByRole('button', { name: '停止任务' }))
    await waitFor(() => expect(frames.size).toBeGreaterThan(0))

    await act(async () => {
      const pending = [...frames.values()]
      frames.clear()
      pending.forEach((callback) => callback(performance.now()))
    })
    expect(screen.queryByRole('button', { name: '停止任务' })).not.toBeInTheDocument()
  })

  it('can stop a new draft after the backend reports its canonical threadId', async () => {
    installFetchMock({
      historyLists: [{ items: [], nextCursor: null }],
      streams: [[{ type: 'RUN_STARTED', threadId: THREAD_ID, runId: FIRST_RUN_ID }]],
      keepOpen: true,
    })

    render(<App />)
    const user = await sendMessage('立即停止')
    await user.click(await screen.findByRole('button', { name: '停止任务' }))

    await waitFor(() => expect(screen.queryByRole('button', { name: '停止任务' })).not.toBeInTheDocument())
    expect(screen.getAllByText(/聊天生成已取消/).length).toBeGreaterThan(0)
  })

  it('does not offer an invalid cancel before the backend reports threadId', async () => {
    installFetchMock({
      historyLists: [{ items: [], nextCursor: null }],
      streams: [[]],
      keepOpen: true,
    })

    render(<App />)
    await sendMessage('等待服务端创建会话')

    expect(await screen.findByRole('button', { name: '正在创建会话' })).toBeDisabled()
    expect(screen.queryByRole('button', { name: '停止任务' })).not.toBeInTheDocument()
  })

  it('paginates detached event catch-up until a batch is shorter than the server limit', async () => {
    const rawEvent = {
      streamMode: 'messages' as const,
      source: { agentType: 'main' as const, agentName: 'main', namespace: [] },
      runId: FIRST_RUN_ID,
    }
    const firstBatch: ConversationEventEnvelope[] = Array.from({ length: 1000 }, (_, index) => ({
      seq: index + 2,
      eventId: `state-${index + 2}`,
      eventType: 'STATE_DELTA',
      runId: FIRST_RUN_ID,
      event: { type: 'STATE_DELTA', delta: [{ op: 'add', path: `/page-${index}`, value: index }] },
      createdAt: BASE_TIME,
    }))
    const tailMessageId = 'assistant-after-thousand'
    const tail: ConversationEventEnvelope[] = [
      {
        seq: 1002,
        eventId: 'tail-start',
        eventType: 'TEXT_MESSAGE_START',
        runId: FIRST_RUN_ID,
        event: { type: 'TEXT_MESSAGE_START', rawEvent, messageId: tailMessageId, role: 'assistant' },
        createdAt: BASE_TIME,
      },
      {
        seq: 1003,
        eventId: 'tail-content',
        eventType: 'TEXT_MESSAGE_CONTENT',
        runId: FIRST_RUN_ID,
        event: { type: 'TEXT_MESSAGE_CONTENT', rawEvent, messageId: tailMessageId, delta: '超过千条后的尾消息' },
        createdAt: BASE_TIME,
      },
      {
        seq: 1004,
        eventId: 'tail-end',
        eventType: 'TEXT_MESSAGE_END',
        runId: FIRST_RUN_ID,
        event: { type: 'TEXT_MESSAGE_END', rawEvent, messageId: tailMessageId },
        createdAt: BASE_TIME,
      },
      {
        seq: 1005,
        eventId: 'tail-finish',
        eventType: 'RUN_FINISHED',
        runId: FIRST_RUN_ID,
        event: { type: 'RUN_FINISHED', threadId: THREAD_ID, runId: FIRST_RUN_ID, outcome: { type: 'success' } },
        createdAt: BASE_TIME,
      },
    ]
    const fetchMock = installFetchMock({
      historyLists: [{
        items: [historyListItem({ threadId: THREAD_ID, title: '超长追赶', status: 'running', lastSeq: 1 })],
        nextCursor: null,
      }],
      historyDetails: {
        [THREAD_ID]: historyDetail({
          threadId: THREAD_ID,
          title: '超长追赶',
          status: 'running',
          lastSeq: 1,
          events: [{
            seq: 1,
            eventId: 'initial-run',
            eventType: 'RUN_STARTED',
            runId: FIRST_RUN_ID,
            event: { type: 'RUN_STARTED', threadId: THREAD_ID, runId: FIRST_RUN_ID },
            createdAt: BASE_TIME,
          }],
        }),
      },
      eventEnvelopeResolver: (_threadId, afterSeq) => afterSeq === 1 ? firstBatch : afterSeq === 1001 ? tail : [],
    })

    render(<App />)
    expect(await screen.findByText('超过千条后的尾消息')).toBeInTheDocument()
    const eventCalls = fetchMock.mock.calls
      .map(([input]) => fetchCallUrl(input))
      .filter((url) => url.includes(`/${THREAD_ID}/events`))
    expect(eventCalls.some((url) => url.includes('afterSeq=1'))).toBe(true)
    expect(eventCalls.some((url) => url.includes('afterSeq=1001'))).toBe(true)
  })

  it('keeps the scroll-to-bottom control hidden throughout its smooth scroll', async () => {
    installFetchMock()
    const user = userEvent.setup()
    render(<App />)

    const pane = screen.getByLabelText('对话内容')
    Object.defineProperties(pane, {
      clientHeight: { configurable: true, value: 500 },
      scrollHeight: { configurable: true, value: 1200 },
      scrollTop: { configurable: true, writable: true, value: 100 },
    })
    const scrollTo = vi.fn()
    Object.defineProperty(pane, 'scrollTo', { configurable: true, value: scrollTo })

    fireEvent.scroll(pane)
    const scrollButton = await screen.findByRole('button', { name: '回到底部' })
    await user.click(scrollButton)

    expect(scrollTo).toHaveBeenCalledWith({ top: 1200, behavior: 'smooth' })
    expect(screen.queryByRole('button', { name: '回到底部' })).not.toBeInTheDocument()

    pane.scrollTop = 300
    fireEvent.scroll(pane)
    expect(screen.queryByRole('button', { name: '回到底部' })).not.toBeInTheDocument()
  })

  it('does not arm scroll-button fade from a frame queued before hover', async () => {
    const frames = new Map<number, FrameRequestCallback>()
    let nextFrameId = 1
    vi.stubGlobal('requestAnimationFrame', vi.fn((callback: FrameRequestCallback) => {
      const id = nextFrameId
      nextFrameId += 1
      frames.set(id, callback)
      return id
    }))
    vi.stubGlobal('cancelAnimationFrame', vi.fn((id: number) => frames.delete(id)))
    const flushFrames = () => {
      const pending = [...frames.values()]
      frames.clear()
      act(() => pending.forEach((callback) => callback(performance.now())))
    }
    installFetchMock()
    render(<App />)
    await screen.findByText('发送一条消息，开始新的真实对话流。')
    flushFrames()
    const pane = screen.getByLabelText('对话内容')
    Object.defineProperties(pane, {
      clientHeight: { configurable: true, value: 400 },
      scrollHeight: { configurable: true, value: 1200 },
      scrollTop: { configurable: true, writable: true, value: 100 },
    })
    fireEvent.scroll(pane)
    flushFrames()
    const scrollButton = await screen.findByRole('button', { name: '回到底部' })

    vi.useFakeTimers()
    fireEvent.scroll(pane)
    fireEvent.mouseEnter(scrollButton)
    flushFrames()
    act(() => vi.advanceTimersByTime(1500))

    expect(scrollButton).not.toHaveClass('is-fading')
  })

  it('jumps to the bottom immediately when Enter sends a message', async () => {
    installFetchMock({
      historyLists: [{
        items: [historyListItem({ threadId: THREAD_ID, title: '长对话' })],
        nextCursor: null,
      }],
      historyDetails: {
        [THREAD_ID]: historyDetail({ threadId: THREAD_ID, title: '长对话' }),
      },
      streams: [[
        { type: 'RUN_STARTED', threadId: THREAD_ID, runId: FIRST_RUN_ID },
      ]],
      keepOpen: true,
    })
    const user = userEvent.setup()
    render(<App />)

    await screen.findByText('来自 长对话 的历史回复')
    const pane = screen.getByLabelText('对话内容')
    Object.defineProperties(pane, {
      clientHeight: { configurable: true, value: 500 },
      scrollHeight: {
        configurable: true,
        get: () => pane.querySelector('.user-message') ? 1400 : 1200,
      },
      scrollTop: { configurable: true, writable: true, value: 100 },
    })
    fireEvent.scroll(pane)
    await screen.findByRole('button', { name: '回到底部' })

    const input = screen.getByLabelText('消息输入')
    await user.type(input, '继续说')
    fireEvent.keyDown(input, { key: 'Enter', code: 'Enter' })

    await waitFor(() => expect(pane.scrollTop).toBe(1400))
    expect(screen.queryByRole('button', { name: '回到底部' })).not.toBeInTheDocument()
  })

  it('shows a transient new-session row and sends only the client-generated runId', async () => {
    const fetchMock = installFetchMock({
      historyListResolver: (_requestIndex, chatRequestCount) =>
        chatRequestCount === 0
          ? { items: [], nextCursor: null }
          : {
              items: [
                historyListItem({
                  threadId: THREAD_ID,
                  title: '后端真实会话',
                }),
              ],
              nextCursor: null,
            },
      historyDetails: {
        [THREAD_ID]: historyDetail({ threadId: THREAD_ID, title: '后端真实会话' }),
      },
      streams: [[
        {
          type: 'RUN_STARTED',
          threadId: THREAD_ID,
          runId: FIRST_RUN_ID,
        },
      ]],
      keepOpen: true,
    })

    const user = userEvent.setup()
    render(
      <StrictMode>
        <App />
      </StrictMode>,
    )

    expect(await screen.findByRole('button', { name: '打开会话：新会话' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '新聊天' }))
    expect(screen.getByRole('button', { name: '打开会话：新会话' })).toBeInTheDocument()

    await sendMessage('你好')

    await waitFor(() => {
      const chatCalls = fetchMock.mock.calls.filter(
        ([input, init]) => fetchCallMethod(input, init) === 'POST',
      )
      expect(chatCalls).toHaveLength(1)
    })
    const requestInit = chatRequestAt(fetchMock, 0)
    const request = JSON.parse(String(requestInit?.body)) as ChatRequestPayload
    expect(request.threadId).toBe('')
    expect(request.runId).toMatch(/^run-/)
    expect(request.messages).toEqual([
      { id: `request-${request.runId}`, role: 'user', content: '你好' },
    ])
    expect(request.forwardedProps).toEqual({ model: 'GPT-5.5', mode: 'default' })
  })

  it('promotes the draft into the sidebar on first RUN_STARTED without re-fetching the list', async () => {
    const fetchMock = installFetchMock({
      historyLists: [{ items: [], nextCursor: null }],
      historyDetails: {
        [THREAD_ID]: historyDetail({ threadId: THREAD_ID, title: '后端真实会话' }),
      },
      streams: [[
        {
          type: 'RUN_STARTED',
          threadId: THREAD_ID,
          runId: FIRST_RUN_ID,
          title: '后端真实会话',
          input: {
            threadId: THREAD_ID,
            runId: FIRST_RUN_ID,
            state: {},
            messages: [
              { id: 'message-from-server', role: 'user', content: '你好' },
            ],
            tools: [],
            context: [],
            forwardedProps: { model: 'GPT-5.5', mode: 'default' },
          },
        },
      ]],
      keepOpen: true,
    })

    render(<App />)

    expect(await screen.findByRole('button', { name: '打开会话：新会话' })).toBeInTheDocument()
    await sendMessage('你好')
    expect(await screen.findByRole('button', { name: '打开会话：后端真实会话' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '打开会话：新会话' })).not.toBeInTheDocument()
    const listCalls = fetchMock.mock.calls.filter(([input, init]) =>
      fetchCallUrl(input).includes('/api/conversation/history')
      && fetchCallMethod(input, init) === 'GET')
    expect(listCalls).toHaveLength(1) // 仅首次挂载拉取
  })

  it('does not detach a running conversation before delete confirmation', async () => {
    const fetchMock = installFetchMock({
      historyLists: [{ items: [], nextCursor: null }],
      streams: [[{ type: 'RUN_STARTED', threadId: THREAD_ID, runId: FIRST_RUN_ID }]],
      keepOpen: true,
    })

    const user = userEvent.setup()
    render(<App />)
    await sendMessage('进行中')
    await screen.findByRole('button', { name: '打开会话：进行中' })

    await user.click(screen.getByRole('button', { name: '管理会话：进行中' }))
    await user.click(screen.getByRole('button', { name: '删除' }))

    const dialog = await screen.findByRole('dialog', { name: '删除会话' })
    expect(within(dialog).getByText(/仍在接收实时输出/)).toBeInTheDocument()
    expect(fetchMock.deleteCalls).toHaveLength(0)
    expect(screen.getByRole('button', { name: '停止任务' })).toBeInTheDocument()

    await user.click(within(dialog).getByRole('button', { name: '取消' }))
    expect(screen.queryByRole('dialog', { name: '删除会话' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '停止任务' })).toBeInTheDocument()
  })

  it('uses the unified animated tail while a run is active without assistant text yet', async () => {
    installFetchMock({
      streams: [[
        {
          type: 'RUN_STARTED',
          threadId: THREAD_ID,
          runId: FIRST_RUN_ID,
        },
        {
          type: 'TOOL_CALL_START',
          rawEvent: {
            streamMode: 'messages',
            source: { agentType: 'main', agentName: 'main', namespace: [] },
            langgraphNode: 'model',
          },
          toolCallId: WRITE_TODOS_CALL_ID,
          toolCallName: 'write_todos',
          parentMessageId: 'parent-message-ellipsis',
        },
      ]],
      keepOpen: true,
      historyDetails: {
        [THREAD_ID]: historyDetail({ threadId: THREAD_ID, title: '流式会话' }),
      },
      historyListResolver: (requestIndex) =>
        requestIndex < 1
          ? { items: [], nextCursor: null }
          : { items: [historyListItem({ threadId: THREAD_ID, title: '流式会话' })], nextCursor: null },
    })

    render(<App />)

    await sendMessage('开始执行')

    expect((await screen.findAllByRole('status', { name: '任务仍在继续' })).length).toBeGreaterThan(0)
    expect(screen.queryByText('...')).not.toBeInTheDocument()
    expect(screen.queryByText('处理中，继续等待后续消息或工具结果…')).not.toBeInTheDocument()
  })

  it('keeps the drawer closed until authoritative Todo state arrives', async () => {
    installFetchMock({
      streams: [[
        {
          type: 'RUN_STARTED',
          threadId: THREAD_ID,
          runId: FIRST_RUN_ID,
        },
        {
          type: 'TOOL_CALL_START',
          rawEvent: {
            streamMode: 'messages',
            source: { agentType: 'main', agentName: 'main', namespace: [] },
            langgraphNode: 'model',
          },
          toolCallId: WRITE_TODOS_CALL_ID,
          toolCallName: 'write_todos',
          parentMessageId: 'parent-message-1',
        },
        {
          type: 'TOOL_CALL_ARGS',
          rawEvent: {
            streamMode: 'messages',
            source: { agentType: 'main', agentName: 'main', namespace: [] },
            langgraphNode: 'model',
          },
          toolCallId: WRITE_TODOS_CALL_ID,
          delta: '{"todos":[{"content":"读取 url.json","status":"in_progress"}]}',
        },
        {
          type: 'TOOL_CALL_RESULT',
          toolCallId: WRITE_TODOS_CALL_ID,
          messageId: 'write-todos-result',
          content: 'Updated todo list',
          role: 'tool',
          rawEvent: {
            streamMode: 'messages',
            source: { agentType: 'main', agentName: 'main', namespace: [] },
            langgraphNode: 'tools',
          },
        },
        {
          type: 'TOOL_CALL_START',
          rawEvent: {
            streamMode: 'messages',
            source: { agentType: 'main', agentName: 'main', namespace: [] },
            langgraphNode: 'model',
          },
          toolCallId: 'call-read-file-with-todos',
          toolCallName: 'read_file',
          parentMessageId: 'parent-message-1',
        },
        {
          type: 'TOOL_CALL_RESULT',
          toolCallId: 'call-read-file-with-todos',
          messageId: 'read-file-result',
          content: 'Loaded url.json',
          role: 'tool',
        },
        {
          type: 'STATE_SNAPSHOT',
          rawEvent: {
            streamMode: 'values',
            source: { agentType: 'main', agentName: 'main', namespace: [] },
          },
          snapshot: {
            todos: [
              { content: '读取 url.json', status: 'completed' },
              { content: '访问两个链接', status: 'in_progress' },
            ],
          },
        },
        {
          type: 'RUN_FINISHED',
          threadId: THREAD_ID,
          runId: FIRST_RUN_ID,
          outcome: { type: 'success' },
        },
      ]],
      historyDetails: {
        [THREAD_ID]: historyDetail({ threadId: THREAD_ID, title: '计划会话' }),
      },
      historyListResolver: (requestIndex) =>
        requestIndex < 1
          ? { items: [], nextCursor: null }
          : { items: [historyListItem({ threadId: THREAD_ID, title: '计划会话' })], nextCursor: null },
    })

    render(<App />)

    expect(screen.getByRole('button', { name: '打开任务抽屉' })).toBeInTheDocument()
    expect(screen.queryByLabelText('任务抽屉')).not.toBeInTheDocument()

    await sendMessage('做个计划')

    await waitFor(() => expect(screen.getByLabelText('任务抽屉')).toBeInTheDocument())
    expect(screen.getAllByText('读取 url.json').length).toBeGreaterThan(0)
    expect(screen.queryByText('write_todos')).not.toBeInTheDocument()
    expect(screen.queryByText('Updated todo list')).not.toBeInTheDocument()
    expect(screen.getByText('read_file')).toBeInTheDocument()
    expect(screen.getByText('Loaded url.json')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '关闭任务抽屉' })).toBeInTheDocument()
    const firstTodo = screen.getByRole('button', {
      name: /步骤 1 · 已完成 读取 url\.json/,
    })
    expect(firstTodo.querySelector('.todo-arrow')).toBeNull()
    expect(screen.queryByLabelText('计划')).not.toBeInTheDocument()
  })

  it('restores a manually closed task drawer after refresh', async () => {
    const detailWithTodos = historyDetail({
      threadId: THREAD_ID,
      title: '计划会话',
      lastSeq: 0,
      events: [],
      snapshot: {
        snapshotSeq: 0,
        snapshotVersion: 2,
        messages: [],
        todos: [{ id: 'todo-read-url', content: '读取 url.json', status: 'running' }],
        approval: null,
        runStatus: 'idle',
        serverState: {},
        runs: {},
        activities: [],
        interrupts: [],
      },
    })
    installFetchMock({
      historyLists: [{ items: [historyListItem({ title: '计划会话' })], nextCursor: null }],
      historyDetails: { [THREAD_ID]: detailWithTodos },
    })
    const user = userEvent.setup()
    const firstRender = render(<App />)

    await user.click(await screen.findByRole('button', { name: '关闭任务抽屉' }))
    expect(screen.queryByLabelText('任务抽屉')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '打开任务抽屉' })).toBeInTheDocument()
    expect(window.sessionStorage.getItem(`tinkerfin:task-drawer:${THREAD_ID}`)).toBe('closed')

    firstRender.unmount()
    render(<App />)

    expect(await screen.findByRole('button', { name: '打开任务抽屉' })).toBeInTheDocument()
    expect(screen.queryByLabelText('任务抽屉')).not.toBeInTheDocument()
  })

  it('keeps the task button visible and restores an opened empty drawer after refresh', async () => {
    installFetchMock({
      historyLists: [{ items: [historyListItem({ title: '空任务会话' })], nextCursor: null }],
      historyDetails: {
        [THREAD_ID]: historyDetail({ threadId: THREAD_ID, title: '空任务会话' }),
      },
    })
    const user = userEvent.setup()
    const firstRender = render(<App />)

    await screen.findByText('来自 空任务会话 的历史回复')
    await user.click(screen.getByRole('button', { name: '打开任务抽屉' }))
    expect(screen.getByLabelText('任务抽屉')).toBeInTheDocument()
    expect(screen.getByText('暂无待办')).toBeInTheDocument()
    expect(window.sessionStorage.getItem(`tinkerfin:task-drawer:${THREAD_ID}`)).toBe('open')

    firstRender.unmount()
    render(<App />)

    await screen.findByText('来自 空任务会话 的历史回复')
    expect(screen.getByRole('button', { name: '关闭任务抽屉' })).toBeInTheDocument()
    expect(screen.getByLabelText('任务抽屉')).toBeInTheDocument()
  })

  it('submits edit approvals through resume[] using the backfilled threadId', async () => {
    const fetchMock = installFetchMock({
      streams: [
        [
          {
            type: 'RUN_STARTED',
            threadId: THREAD_ID,
            runId: FIRST_RUN_ID,
          },
          {
            type: 'TOOL_CALL_START',
            rawEvent: {
              streamMode: 'messages',
              source: { agentType: 'main', agentName: 'main', namespace: [] },
              langgraphNode: 'model',
            },
            toolCallId: WRITE_FILE_CALL_ID,
            toolCallName: 'write_file',
            parentMessageId: 'parent-write-file',
          },
          {
            type: 'TOOL_CALL_ARGS',
            rawEvent: {
              streamMode: 'messages',
              source: { agentType: 'main', agentName: 'main', namespace: [] },
              langgraphNode: 'model',
            },
            toolCallId: WRITE_FILE_CALL_ID,
            delta: JSON.stringify(originalWriteArgs),
          },
          {
            type: 'RUN_FINISHED',
            threadId: THREAD_ID,
            runId: FIRST_RUN_ID,
            outcome: {
              type: 'interrupt',
              interrupts: [
                {
                  id: INTERRUPT_ID,
                  reason: 'tool_call',
                  message: '需要人工审批：agent 正准备写入文件。',
                  toolCallId: WRITE_FILE_CALL_ID,
                  responseSchema: {
                    type: 'object',
                    properties: {
                      approved: { type: 'boolean' },
                      editedArgs: { type: 'object' },
                    },
                    required: ['approved'],
                  },
                  metadata: {
                    deepagents: {
                      toolName: 'write_file',
                      allowedDecisions: ['approve', 'edit', 'reject'],
                      originalArgs: originalWriteArgs,
                    },
                  },
                },
              ],
            },
          },
        ],
        [
          {
            type: 'RUN_STARTED',
            threadId: THREAD_ID,
            runId: SECOND_RUN_ID,
          },
          {
            type: 'TOOL_CALL_RESULT',
            rawEvent: {
              streamMode: 'messages',
              source: { agentType: 'main', agentName: 'main', namespace: [] },
              langgraphNode: 'tools',
            },
            messageId: 'tool-result-write-file',
            toolCallId: WRITE_FILE_CALL_ID,
            content: 'Updated file /result.txt',
            role: 'tool',
          },
          {
            type: 'RUN_FINISHED',
            threadId: THREAD_ID,
            runId: SECOND_RUN_ID,
            outcome: { type: 'success' },
          },
        ],
      ],
      historyDetails: {
        [THREAD_ID]: historyDetail({ threadId: THREAD_ID, title: '审批会话' }),
      },
      historyListResolver: (requestIndex) =>
        requestIndex < 1
          ? { items: [], nextCursor: null }
          : { items: [historyListItem({ threadId: THREAD_ID, title: '审批会话' })], nextCursor: null },
    })

    render(<App />)

    const user = await sendMessage('请写入结果')

    await waitFor(() => expect(screen.getByText('需要人工审批：agent 正准备写入文件。')).toBeInTheDocument())
    expect(screen.queryByText('write_file')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '编辑' }))
    const textarea = screen.getByLabelText('编辑参数（JSON 对象）')
    fireEvent.change(textarea, { target: { value: JSON.stringify(editedWriteArgs) } })
    await user.click(screen.getByRole('button', { name: '保存并允许' }))
    await user.click(screen.getByRole('button', { name: '批量提交' }))

    await waitFor(() => {
      const chatCalls = fetchMock.mock.calls.filter(
        ([input, init]) => fetchCallMethod(input, init) === 'POST',
      )
      expect(chatCalls).toHaveLength(2)
    })
    const resumeRequestInit = chatRequestAt(fetchMock, 1)
    const resumeRequest = JSON.parse(String(resumeRequestInit?.body)) as ChatRequestPayload
    expect(resumeRequest.threadId).toBe(THREAD_ID)
    expect(resumeRequest.forwardedProps).toEqual({ model: 'GPT-5.5', mode: 'default' })
    expect(await screen.findByText('write_file')).toBeInTheDocument()
    expect(resumeRequest.resume).toEqual([
      {
        interruptId: INTERRUPT_ID,
        status: 'resolved',
        payload: {
          type: 'edit',
          edited_action: {
            name: 'write_file',
            args: editedWriteArgs,
          },
        },
      },
    ])
  })
})
