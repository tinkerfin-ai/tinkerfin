import { StrictMode, useCallback, useEffect, useState } from 'react'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type {
  ConversationEventEnvelope,
  ConversationHistoryDetail,
  ConversationHistoryGroupConfig,
  ConversationHistoryListItem,
  ConversationHistoryListResponse,
  ConversationSnapshotJson,
} from './api/conversation/history'
import type { ChatRequestPayload, ConversationAgUiEvent } from './api/conversation/types'
import type { AgentModelCatalog } from './api/models/types'
import { subscribeApiErrors } from './api/shared/http'
import { AUTH_SESSION_STORAGE_KEY, clearAuthSession, saveAuthSession } from './auth/session'
import { ToastViewport } from './components/ui/ToastViewport'
import type { ToastItem, ToastKind } from './components/ui/ToastViewport'
import { WorkspaceScreen } from './features/workspace/WorkspaceScreen'
import {
  readActiveRunSession,
  writeActiveRunSession,
} from './features/conversation/stream/activeRunSession'
import {
  approvalCollapseKey,
  planQuestionCollapseKey,
  planReviewCollapseKey,
} from './features/conversation/planQuestionCollapse'
import { normalizeAppLocation } from './lib/threadRoute'

const TEST_USER = {
  user_id: 7,
  username: 'yunsan',
  display_name: '云杉',
  avatar_url: null,
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
    pendingInteractionKind: null,
    pinned: false,
    createdAt: BASE_TIME,
    updatedAt: BASE_TIME,
    ...overrides,
  }
}

function historyDetail(
  overrides: Partial<ConversationHistoryDetail> & {
    threadId: string
    title: string
    snapshotMode?: 'default' | 'plan'
  },
): ConversationHistoryDetail {
  const {
    threadId,
    title,
    lastSeq = 5,
    snapshotSeq = lastSeq,
    snapshot,
    events,
    status = 'idle',
    lastRunId = FIRST_RUN_ID,
    snapshotMode = 'default',
    ...rest
  } = overrides
  const assistantContent = `来自 ${title} 的历史回复`
  const currentSnapshot: ConversationSnapshotJson = {
    snapshotSeq,
    messages: [{
      id: `${threadId}-assistant-1`,
      role: 'assistant',
      content: assistantContent,
      createdAt: BASE_TIME,
    }],
    todos: [],
    mode: snapshotMode,
    approval: null,
    runStatus: status === 'running' ? 'streaming' : status === 'error' ? 'error' : 'idle',
    activeRunId: status === 'running' ? lastRunId ?? null : null,
    serverState: {},
    runs: {},
    interrupts: [],
  }
  return {
    id: 1,
    threadId,
    title,
    status,
    lastRunId,
    lastModel: 'GPT-5.5',
    lastSeq,
    snapshotSeq,
    messageCount: 1,
    toolCallCount: 0,
    hasPendingInterrupt: false,
    pendingInteractionKind: null,
    snapshot: snapshot === undefined ? currentSnapshot : snapshot,
    events: events ?? [],
    createdAt: BASE_TIME,
    updatedAt: BASE_TIME,
    ...rest,
    pinned: rest.pinned ?? false,
  }
}

type FetchMockOptions = {
  streams?: ConversationAgUiEvent[][]
  onChatRequest?: (request: ChatRequestPayload, requestIndex: number) => void
  keepOpen?: boolean
  historyLists?: ConversationHistoryListResponse[]
  historyListResolver?: (
    requestIndex: number,
    chatRequestCount: number,
    url: URL,
  ) => ConversationHistoryListResponse
  historyDetails?: Record<string, ConversationHistoryDetail>
  historyGroupConfig?: ConversationHistoryGroupConfig
  eventEnvelopes?: Record<string, ConversationEventEnvelope[]>
  eventEnvelopeResolver?: (
    threadId: string,
    afterSeq: number | null,
    requestIndex: number,
  ) => ConversationEventEnvelope[]
  modelCatalog?: AgentModelCatalog
}

function deferred<T>() {
  let resolve: (value: T) => void = () => undefined
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise
  })
  return { promise, resolve }
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
    onChatRequest,
    keepOpen = false,
    historyLists = [{ items: [], nextCursor: null }],
    historyListResolver,
    historyDetails = {},
    historyGroupConfig = { dayRanges: [7, 30] },
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

    if (url.pathname.endsWith('/api/conversation/config')) {
      return jsonResponse(historyGroupConfig)
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
      if (chatRequest) onChatRequest?.(chatRequest, streamIndex - 1)
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
            }
          })
        : events
      return sseResponse(serverEvents, keepOpen, (controller) => openStreams.set(controller, runId))
    }

    if (url.pathname.endsWith('/api/conversation/history')) {
      const requestIndex = historyListIndex
      historyListIndex += 1
      const response = historyListResolver
        ? historyListResolver(requestIndex, streamIndex, url)
        : historyLists[Math.min(requestIndex, historyLists.length - 1)] ?? { items: [], nextCursor: null }
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
  const input = screen.getByLabelText('消息输入')
  await waitFor(() => expect(input).toBeEnabled())
  await user.type(input, message)
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

function chatRequests(fetchMock: ReturnType<typeof installFetchMock>): ChatRequestPayload[] {
  return fetchMock.mock.calls
    .filter(([input, init]) => (
      fetchCallMethod(input, init) === 'POST'
      && new URL(fetchCallUrl(input), 'http://localhost').pathname.endsWith('/api/conversation/chat')
    ))
    .map(([, init]) => JSON.parse(String(init?.body)) as ChatRequestPayload)
}

function installHistoryIntersectionObserver() {
  const callbacks: IntersectionObserverCallback[] = []
  vi.stubGlobal('IntersectionObserver', class {
    readonly root = null
    readonly rootMargin = ''
    readonly thresholds = [0]
    constructor(callback: IntersectionObserverCallback) { callbacks.push(callback) }
    observe() {}
    unobserve() {}
    disconnect() {}
    takeRecords() { return [] }
  })
  return callbacks
}

describe('App', () => {
  beforeEach(() => {
    window.localStorage.clear()
    window.sessionStorage.clear()
    window.history.replaceState(null, '', '/')
    saveAuthSession({
      token: 'workspace-token',
      tokenType: 'Bearer',
      expiresAt: '2099-01-01T00:00:00.000Z',
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

    expect(await screen.findByText('发送一条消息，开始新的真实对话流')).toHaveClass('visually-hidden')
    const emptyBrand = document.querySelector<HTMLElement>('.empty-brand-lockup')
    expect(emptyBrand).toHaveTextContent('TinkerFinPlus')
    expect(emptyBrand?.querySelector('.brand-mark svg')).toHaveAttribute('width', '34')
    expect(emptyBrand?.closest('.composer-dock')).toHaveClass('is-hero')
    const conversationPane = screen.getByRole('region', { name: '对话内容' })
    expect(conversationPane).toHaveClass('ui-scrollbar')
    expect(conversationPane).toHaveAttribute('tabindex', '0')
    expect(conversationPane.parentElement?.querySelector('.ui-scrollbar-overlay')).not.toBeInTheDocument()
    expect(conversationPane.parentElement?.querySelector('.ui-overlay-scrollbar')).toHaveAttribute('data-visibility', 'persistent')
    expect(conversationPane).not.toContainElement(emptyBrand)
    expect(screen.queryByRole('button', { name: '回到底部' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '模型设置' })).not.toBeInTheDocument()
    expect(screen.queryByText('研究助手')).not.toBeInTheDocument()
    expect(screen.queryByText('季度现金流分析与风险建议')).not.toBeInTheDocument()
  })

  it('clears a deleted thread and active-run session after conversation data reset', async () => {
    window.history.replaceState(null, '', '/?thread=deleted-thread')
    writeActiveRunSession({
      threadId: 'deleted-thread',
      mode: 'start',
      lastSeq: 12,
      payload: {
        threadId: 'deleted-thread',
        runId: 'deleted-run',
        state: {},
        messages: [{ id: 'deleted-message', role: 'user', content: 'stale' }],
        tools: [],
        context: [],
        forwardedProps: { model: 'GPT-5.5', command: { plan: 'off' } },
      },
    })
    installFetchMock({ historyLists: [{ items: [], nextCursor: null }] })

    render(<App />)

    expect(await screen.findByText('发送一条消息，开始新的真实对话流')).toBeInTheDocument()
    await waitFor(() => expect(readActiveRunSession()).toBeNull())
    await waitFor(() => expect(window.location.search).toBe(''))
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
    expect(longLabel.style.getPropertyValue('--overflow-marquee-travel-duration')).toBe('1125ms')
  })

  it('enables Plan through /plan and forwards command.plan on the next message', async () => {
    const fetchMock = installFetchMock({ streams: [[]] })
    render(<App />)

    await sendMessage('/plan')
    expect(screen.getByRole('button', { name: 'Plan 已开启，点击关闭' })).toBeEnabled()
    expect(chatRequestAt(fetchMock, 0)).toBeUndefined()
    expect(screen.queryByRole('button', { name: '当前 Agent 预设' })).not.toBeInTheDocument()

    await sendMessage('按计划处理')
    const request = JSON.parse(String(chatRequestAt(fetchMock, 0)?.body)) as ChatRequestPayload
    expect(request.forwardedProps).toEqual({ model: 'GPT-5.5', command: { plan: 'on' } })
  })

  it('strips /plan from a plan message and reserves closing for the Plan chip', async () => {
    const fetchMock = installFetchMock({ streams: [[]] })
    render(<App />)

    await sendMessage('/plan 生成发布清单')
    const request = JSON.parse(String(chatRequestAt(fetchMock, 0)?.body)) as ChatRequestPayload
    expect(request.messages[0]?.content).toBe('生成发布清单')
    expect(request.forwardedProps.command.plan).toBe('on')
    expect(screen.getByRole('button', { name: 'Plan 已开启，点击关闭' })).toBeInTheDocument()

    await waitFor(() => expect(screen.getByLabelText('消息输入')).toBeEnabled())
    await userEvent.setup().type(screen.getByLabelText('消息输入'), '/plan off')
    await userEvent.setup().click(screen.getByRole('button', { name: '发送消息' }))
    expect(chatRequestAt(fetchMock, 1)).toBeUndefined()
    expect(screen.getByText('请点击输入框中的 Plan 按钮关闭')).toBeInTheDocument()
  })

  it('keeps local attachments across text sends without adding them to the request', async () => {
    const fetchMock = installFetchMock({ streams: [[]] })
    const user = userEvent.setup()
    render(<App />)
    await waitFor(() => expect(screen.getByLabelText('消息输入')).toBeEnabled())

    const fileInput = document.querySelector('input[type="file"]')
    if (!(fileInput instanceof HTMLInputElement)) throw new Error('缺少本地附件输入')
    const documentFile = new File(['pdf'], 'local-only.pdf', { type: 'application/pdf' })
    fireEvent.change(fileInput, { target: { files: [documentFile] } })
    expect(screen.getByText('local-only.pdf')).toBeInTheDocument()

    await sendMessage('只发送这段文字')
    const request = JSON.parse(String(chatRequestAt(fetchMock, 0)?.body)) as ChatRequestPayload
    expect(request.messages).toEqual([
      expect.objectContaining({ role: 'user', content: '只发送这段文字' }),
    ])
    expect(JSON.stringify(request)).not.toContain('local-only.pdf')
    expect(screen.getByText('local-only.pdf')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '新会话' }))
    expect(screen.queryByText('local-only.pdf')).not.toBeInTheDocument()
  })

  it('locks the active Plan chip until an active stream is cancelled and cleaned up', async () => {
    installFetchMock({
      streams: [[{ type: 'RUN_STARTED', threadId: THREAD_ID, runId: FIRST_RUN_ID }]],
      keepOpen: true,
    })
    render(<App />)

    await sendMessage('/plan')
    await sendMessage('保持流运行')
    const planButton = screen.getByRole('button', { name: 'Plan 已开启，点击关闭' })
    await waitFor(() => expect(planButton).toBeDisabled())

    await userEvent.setup().click(await screen.findByRole('button', { name: '停止任务' }))
    await waitFor(() => expect(planButton).toBeEnabled())
  })

  it('requires an explicit review decision without exposing a close action', async () => {
    const secondChatRequest = deferred<ChatRequestPayload>()
    const fetchMock = installFetchMock({
      streams: [
        [
          { type: 'RUN_STARTED', threadId: THREAD_ID, runId: FIRST_RUN_ID },
          {
            type: 'RUN_FINISHED',
            threadId: THREAD_ID,
            runId: FIRST_RUN_ID,
            outcome: {
              type: 'interrupt',
              interrupts: [{
                id: 'plan-review-app',
                reason: 'tinkerfin:plan_review',
                message: '请确认 Plan',
                responseSchema: { type: 'object' },
                metadata: {
                  runtimeInterrupt: {
                    schema: 'tinkerfin.runtime-interrupt',
                    nativeInterruptId: 'plan-review-app',
                    envelope: {
                      schema: 'tinkerfin.runtime-interrupt',
                      kind: 'tinkerfin:plan_review',
                      responseSchema: { type: 'object' },
                      metadata: {
                        origin: 'plan',
                        review: {
                          draft: {
                            revision: 1,
                            contentSchema: {
                              fingerprint: '0'.repeat(64),
                              mediaType: 'text/markdown',
                            },
                            content: {
                              description: '切换实现模式并保持父图稳定',
                              markdown: '# 实现模式切换\n\n保持父图稳定',
                            },
                          },
                        },
                      },
                    },
                  },
                },
              }],
            },
          },
        ],
        [
          { type: 'RUN_STARTED', threadId: THREAD_ID, runId: SECOND_RUN_ID },
          { type: 'RUN_FINISHED', threadId: THREAD_ID, runId: SECOND_RUN_ID, outcome: { type: 'success' } },
        ],
      ],
      onChatRequest: (request, requestIndex) => {
        if (requestIndex === 1) secondChatRequest.resolve(request)
      },
    })
    const user = userEvent.setup()
    render(<App />)
    await sendMessage('/plan 先生成计划')
    const review = await screen.findByLabelText('Plan 审阅')
    expect(screen.queryByRole('button', { name: 'Plan 已开启，点击关闭' })).not.toBeInTheDocument()
    expect(within(review).queryByRole('button', { name: /关闭|放弃/ })).not.toBeInTheDocument()
    await user.click(within(review).getByRole('button', { name: '拒绝' }))
    await user.click(within(review).getByRole('button', { name: '提交决定' }))

    const decisionRequest = await secondChatRequest.promise
    const requests = chatRequests(fetchMock)
    expect(requests.filter((request) => request.messages.length > 0 && request.forwardedProps.command.plan === 'on')).toHaveLength(1)
    expect(decisionRequest.forwardedProps).toEqual({ model: 'GPT-5.5', command: { plan: 'off' } })
    expect(decisionRequest.resume).toEqual([{
      interruptId: 'plan-review-app',
      status: 'resolved',
      payload: { type: 'reject', baseRevision: 1 },
    }])
  })

  it('does not expose the removed mode selector while a Tool approval is pending', async () => {
    const fetchMock = installFetchMock({
      streams: [[
        { type: 'RUN_STARTED', threadId: THREAD_ID, runId: FIRST_RUN_ID },
        {
          type: 'RUN_FINISHED',
          threadId: THREAD_ID,
          runId: FIRST_RUN_ID,
          outcome: {
            type: 'interrupt',
            interrupts: [{
              id: INTERRUPT_ID,
              reason: 'tool_call',
              message: '确认写入',
              toolCallId: WRITE_FILE_CALL_ID,
              metadata: {
                langgraphValue: {
                  action_requests: [{ name: 'write_file', args: originalWriteArgs }],
                  review_configs: [{
                    action_name: 'write_file',
                    allowed_decisions: ['approve', 'reject'],
                  }],
                },
                deepagents: {
                  schema: 'tinkerfin.deepagents.tool-review',
                  nativeInterruptId: INTERRUPT_ID,
                  actionIndex: 0,
                  toolName: 'write_file',
                  allowedDecisions: ['approve', 'reject'],
                  originalArgs: originalWriteArgs,
                },
              },
            }],
          },
        },
      ]],
    })
    render(<App />)
    await sendMessage('等待 Tool 审批')
    expect(await screen.findByRole('region', { name: '等待审批' })).toHaveTextContent('确认写入')

    expect(screen.queryByRole('button', { name: '当前 Agent 预设' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Plan 已开启，点击关闭' })).not.toBeInTheDocument()
    expect(screen.getByRole('region', { name: '等待审批' })).toHaveTextContent('确认写入')
    expect(chatRequestAt(fetchMock, 1)).toBeUndefined()
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

    expect(request.forwardedProps).toEqual({ model: 'database-main', command: { plan: 'off' } })
    expect(screen.queryByText('GPT-5.5')).not.toBeInTheDocument()
  })

  it('blocks composition with a recoverable empty model catalog state', async () => {
    installFetchMock({
      modelCatalog: { items: [], defaultModelId: null },
    })
    render(<App />)

    expect(await screen.findByText('未配置可用模型')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '重试' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '选择模型' })).not.toBeInTheDocument()
    expect(screen.getByLabelText('消息输入')).toBeDisabled()
    expect(screen.getByLabelText('消息输入')).toHaveAttribute(
      'placeholder',
      '未配置可用模型，请联系管理员或重试',
    )
    expect(screen.getByRole('button', { name: '发送消息' })).toBeDisabled()
  })

  it('keeps the composer disabled until the initial model and history bootstrap completes', async () => {
    let resolveModel!: (response: Response) => void
    const modelResponse = new Promise<Response>((resolve) => {
      resolveModel = resolve
    })
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const request = input instanceof Request ? input : new Request(input)
      const url = new URL(request.url)
      if (url.pathname.endsWith('/api/models')) return await modelResponse
      if (url.pathname.endsWith('/api/conversation/config')) return jsonResponse({ dayRanges: [7, 30] })
      if (url.pathname.endsWith('/api/conversation/history')) {
        return jsonResponse({ items: [], nextCursor: null })
      }
      throw new Error(`unexpected fetch: ${url.pathname}`)
    }))
    render(<App />)

    const input = screen.getByLabelText('消息输入')
    expect(input).toBeDisabled()
    expect(input).toHaveAttribute('placeholder', '正在加载模型…')

    await act(async () => resolveModel(jsonResponse(DEFAULT_MODEL_CATALOG)))

    expect(await screen.findByRole('heading', { name: '暂无消息' })).toBeInTheDocument()
    await waitFor(() => expect(input).toBeEnabled())
  })

  it('shows a local model-catalog error and recovers through its retry action', async () => {
    let modelRequestCount = 0
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const request = input instanceof Request ? input : new Request(input)
      const url = new URL(request.url)
      if (url.pathname.endsWith('/api/models')) {
        modelRequestCount += 1
        if (modelRequestCount === 1) {
          return new Response(JSON.stringify({ code: 503, message: '模型目录暂不可用', data: null }), {
            status: 503,
            headers: { 'Content-Type': 'application/json' },
          })
        }
        return jsonResponse(DEFAULT_MODEL_CATALOG)
      }
      if (url.pathname.endsWith('/api/conversation/config')) return jsonResponse({ dayRanges: [7, 30] })
      if (url.pathname.endsWith('/api/conversation/history')) {
        return jsonResponse({ items: [], nextCursor: null })
      }
      throw new Error(`unexpected fetch: ${url.pathname}`)
    }))
    const user = userEvent.setup()
    render(<App />)

    const error = await screen.findByText('模型加载失败')
    const retry = within(error.parentElement!).getByRole('button', { name: '重试' })
    expect(error.closest('.composer')).not.toBeNull()
    expect(screen.queryByRole('button', { name: '选择模型' })).not.toBeInTheDocument()
    expect(screen.getByLabelText('消息输入')).toBeDisabled()
    expect(screen.getByLabelText('消息输入')).toHaveAttribute('placeholder', '模型加载失败，请先重试')
    await user.click(retry)

    await waitFor(() => expect(screen.getByRole('button', { name: '选择模型' })).toHaveTextContent('GPT-5.5'))
    await waitFor(() => expect(screen.getByLabelText('消息输入')).toBeEnabled())
    expect(modelRequestCount).toBe(2)
    expect(screen.queryByText('模型加载失败')).not.toBeInTheDocument()
  })

  it('shows a local history bootstrap error and retries without reloading the page', async () => {
    let historyRequestCount = 0
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const request = input instanceof Request ? input : new Request(input)
      const url = new URL(request.url)
      if (url.pathname.endsWith('/api/models')) return jsonResponse(DEFAULT_MODEL_CATALOG)
      if (url.pathname.endsWith('/api/conversation/config')) return jsonResponse({ dayRanges: [7, 30] })
      if (url.pathname.endsWith('/api/conversation/history')) {
        historyRequestCount += 1
        if (historyRequestCount === 1) {
          return new Response(JSON.stringify({ code: 503, message: '历史服务暂不可用', data: null }), {
            status: 503,
            headers: { 'Content-Type': 'application/json' },
          })
        }
        return jsonResponse({ items: [], nextCursor: null })
      }
      throw new Error(`unexpected fetch: ${url.pathname}`)
    }))
    const user = userEvent.setup()
    render(<App />)

    const title = await screen.findByText('历史会话加载失败')
    expect(screen.getByLabelText('消息输入')).toBeDisabled()
    expect(screen.getByLabelText('消息输入')).toHaveAttribute('placeholder', '历史会话加载失败，请先重试')
    await user.click(within(title.closest('.workspace-status')!).getByRole('button', { name: '重试' }))

    expect(await screen.findByRole('heading', { name: '暂无消息' })).toBeInTheDocument()
    await waitFor(() => expect(screen.getByLabelText('消息输入')).toBeEnabled())
    expect(historyRequestCount).toBe(2)
    expect(screen.queryByText('历史会话加载失败')).not.toBeInTheDocument()
  })

  it('keeps the model listbox in Composer with complete keyboard and focus behavior', async () => {
    installFetchMock()
    const user = userEvent.setup()
    render(<App />)
    const modelTrigger = screen.getByRole('button', { name: '选择模型' })
    expect(modelTrigger.closest('.composer')).not.toBeNull()
    expect(modelTrigger.closest('.chat-header')).toBeNull()

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
  })

  it('moves the persisted theme controls from Header into Settings', async () => {
    installFetchMock()
    render(<App />)

    expect(screen.queryByRole('group', { name: '主题' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '打开任务抽屉' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '当前 Agent 预设' })).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: '打开用户菜单' }))
    await userEvent.click(screen.getByRole('menuitem', { name: '设置' }))

    expect(screen.getByRole('dialog', { name: '设置' })).toBeInTheDocument()
    expect(document.querySelector('.app-shell')).toHaveAttribute('inert')
    expect(document.querySelector('.app-shell')).toHaveAttribute('aria-hidden', 'true')
    await userEvent.click(screen.getByRole('button', { name: '通用' }))
    expect(screen.getByRole('radio', { name: '跟随系统' })).not.toBeChecked()
    expect(screen.getByRole('radio', { name: '浅色' })).toBeChecked()
    expect(screen.getByRole('radio', { name: '深色' })).not.toBeChecked()

    await userEvent.click(screen.getByRole('button', { name: '关闭对话框' }))
    expect(document.querySelector('.app-shell')).not.toHaveAttribute('inert')
    expect(screen.getByRole('button', { name: '打开用户菜单' })).toHaveFocus()
  })

  it('shows non-success business codes through the global error toast', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const request = input instanceof Request ? input : new Request(input)
      const url = new URL(request.url)
      if (url.pathname.endsWith('/api/models')) return jsonResponse(DEFAULT_MODEL_CATALOG)
      if (url.pathname.endsWith('/api/conversation/config')) return jsonResponse({ dayRanges: [7, 30] })
      if (url.pathname.endsWith('/api/conversation/history')) {
        return new Response(
          JSON.stringify({
            code: 1_001_004_001,
            message: '历史分页游标已失效',
            data: null,
          }),
          { status: 422, headers: { 'Content-Type': 'application/json' } },
        )
      }
      throw new Error(`unexpected fetch: ${url.pathname}`)
    }))

    render(<App />)

    expect((await screen.findByText('历史分页游标已失效')).closest('[role="alert"]')).not.toBeNull()
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
    expect(screen.queryByText('登录已失效，请重新登录')).not.toBeInTheDocument()
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
        [THREAD_ID]: historyDetail({
          threadId: THREAD_ID,
          title: '第一条会话',
          snapshotMode: 'plan',
        }),
        [SECOND_THREAD_ID]: historyDetail({
          threadId: SECOND_THREAD_ID,
          title: '第二条会话',
          snapshotMode: 'default',
        }),
      },
    })

    const user = userEvent.setup()
    render(<App />)

    expect(await screen.findByText('来自 第一条会话 的历史回复')).toBeInTheDocument()
    expect(document.querySelector('.composer-dock')).not.toHaveClass('is-hero')
    expect(document.title).toBe('第一条会话')
    expect(screen.getByRole('button', { name: 'Plan 已开启，点击关闭' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '打开会话：第二条会话' }))
    expect(await screen.findByText('来自 第二条会话 的历史回复')).toBeInTheDocument()
    expect(document.title).toBe('第二条会话')
    expect(screen.queryByRole('button', { name: 'Plan 已开启，点击关闭' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '打开会话：第一条会话' }))
    expect(await screen.findByText('来自 第一条会话 的历史回复')).toBeInTheDocument()
    expect(document.title).toBe('第一条会话')
    expect(screen.getByRole('button', { name: 'Plan 已开启，点击关闭' })).toBeInTheDocument()
  })

  it('keeps a Plan clarification blue from the initial summary through detail hydration', async () => {
    const responseSchema = { type: 'object' } as const
    const planSnapshot: ConversationSnapshotJson = {
      snapshotSeq: 6,
      messages: [],
      todos: [],
      mode: 'plan',
      approval: null,
      runStatus: 'waiting_approval',
      activeRunId: null,
      serverState: {},
      runs: {},
      interrupts: [{
        id: 'plan-summary-interrupt',
        reason: 'tinkerfin:plan_clarification',
        responseSchema,
        metadata: {
          runtimeInterrupt: {
            schema: 'tinkerfin.runtime-interrupt',
            nativeInterruptId: 'plan-summary-interrupt',
            envelope: {
              schema: 'tinkerfin.runtime-interrupt',
              kind: 'tinkerfin:plan_clarification',
              responseSchema,
              metadata: {
                origin: 'plan',
                clarification: {
                  form: {
                    title: '确认范围',
                    description: '确认本次回归范围',
                    questions: [{
                      id: 'scope',
                      answerType: 'text',
                      prompt: '需要覆盖哪些路径？',
                      required: true,
                    }],
                  },
                },
              },
            },
          },
        },
        allowedDecisions: [],
        originalArgs: {},
      }],
    }
    installFetchMock({
      historyLists: [{
        items: [
          historyListItem({ threadId: THREAD_ID, title: '普通会话' }),
          historyListItem({
            id: 2,
            threadId: SECOND_THREAD_ID,
            title: 'Plan 澄清会话',
            status: 'waiting_approval',
            lastRunId: 'run-plan-summary',
            lastSeq: 6,
            messageCount: 0,
            hasPendingInterrupt: true,
            pendingInteractionKind: 'plan_clarification',
          }),
        ],
        nextCursor: null,
      }],
      historyDetails: {
        [THREAD_ID]: historyDetail({ threadId: THREAD_ID, title: '普通会话' }),
        [SECOND_THREAD_ID]: historyDetail({
          threadId: SECOND_THREAD_ID,
          title: 'Plan 澄清会话',
          status: 'waiting_approval',
          lastRunId: 'run-plan-summary',
          lastSeq: 6,
          snapshotSeq: 6,
          messageCount: 0,
          hasPendingInterrupt: true,
          pendingInteractionKind: 'plan_clarification',
          snapshot: planSnapshot,
        }),
      },
    })

    const user = userEvent.setup()
    render(<App />)

    expect(await screen.findByText('来自 普通会话 的历史回复')).toBeInTheDocument()
    const planButton = screen.getByRole('button', { name: '打开会话：Plan 澄清会话，等待处理' })
    expect(planButton.querySelector('.conversation-attention-dot')).toHaveClass('is-plan')

    await user.click(planButton)

    expect(await screen.findByRole('region', { name: 'Plan 澄清问题' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '打开会话：Plan 澄清会话，等待处理' })
      .querySelector('.conversation-attention-dot')).toHaveClass('is-plan')
  })

  it('cancels stale hydration when switching threads and disables the composer meanwhile', async () => {
    let firstDetailSignal: AbortSignal | undefined
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const request = input instanceof Request ? input : new Request(input)
      const url = new URL(request.url)
      if (url.pathname.endsWith('/api/models')) return jsonResponse(DEFAULT_MODEL_CATALOG)
      if (url.pathname.endsWith('/api/conversation/config')) return jsonResponse({ dayRanges: [7, 30] })
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

  it('keeps the composer protected after hydration fails and retries explicitly', async () => {
    let detailRequestCount = 0
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const request = input instanceof Request ? input : new Request(input)
      const url = new URL(request.url)
      if (url.pathname.endsWith('/api/models')) return jsonResponse(DEFAULT_MODEL_CATALOG)
      if (url.pathname.endsWith('/api/conversation/config')) return jsonResponse({ dayRanges: [7, 30] })
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
    expect(input).toBeDisabled()
    expect(await screen.findByText('会话加载失败，请重试')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '重试' }))

    expect(await screen.findByText('来自 可重试会话 的历史回复')).toBeInTheDocument()
    expect(detailRequestCount).toBe(2)
    await waitFor(() => expect(input).toBeEnabled())
    await user.type(input, '恢复后的草稿')
    expect(input).toHaveValue('恢复后的草稿')
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
    // 模拟刷新后落在 /?thread=SECOND_THREAD_ID
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

    // 启动时选择 ?thread= 指定的第二条会话，而不是 conversations[0]
    expect(await screen.findByText('来自 第二条会话 的历史回复')).toBeInTheDocument()
    expect(window.location.search).toContain(`thread=${SECOND_THREAD_ID}`)

    // 切换到第一条会话时同步更新 URL
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
      forwardedProps: { model: 'GPT-5.5', command: { plan: 'off' } },
    }
    writeActiveRunSession({
      threadId: THREAD_ID,
      payload: activePayload,
      mode: 'start',
      lastSeq: 3,
    })
    const rawEvent = {
      streamMode: 'messages' as const,
      source: { kind: 'root' as const, agentType: 'main' as const, agentName: 'main', namespace: [] },
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
          snapshot: {
            snapshotSeq: 3,
            messages: [
              { id: 'server-user', role: 'user', content: '刷新后继续', createdAt: BASE_TIME },
              {
                id: 'refresh-assistant',
                role: 'assistant',
                content: '刷新前',
                createdAt: BASE_TIME,
                meta: { status: 'running', runId: FIRST_RUN_ID },
              },
            ],
            todos: [],
            mode: 'default',
            approval: null,
            runStatus: 'streaming',
            activeRunId: FIRST_RUN_ID,
            serverState: {},
            runs: {},
            interrupts: [],
          },
          events: [],
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

  it('reattaches the same restored run again after an explicit detach and reselect', async () => {
    window.history.replaceState(null, '', `/?thread=${THREAD_ID}`)
    const activePayload: ChatRequestPayload = {
      threadId: THREAD_ID,
      runId: FIRST_RUN_ID,
      state: {},
      messages: [{ id: 'request-first-run', role: 'user', content: '继续运行' }],
      tools: [],
      context: [],
      forwardedProps: { model: 'GPT-5.5', command: { plan: 'off' } },
    }
    writeActiveRunSession({
      threadId: THREAD_ID,
      payload: activePayload,
      mode: 'start',
      lastSeq: 3,
    })
    const rawEvent = {
      streamMode: 'messages' as const,
      source: { kind: 'root' as const, agentType: 'main' as const, agentName: 'main', namespace: [] },
      runId: FIRST_RUN_ID,
    }
    const firstDetail = historyDetail({
      threadId: THREAD_ID,
      title: '可重复附着',
      status: 'running',
      lastRunId: FIRST_RUN_ID,
      lastSeq: 3,
      events: [
        {
          seq: 1,
          eventId: 'repeat-attach-start',
          eventType: 'RUN_STARTED',
          runId: FIRST_RUN_ID,
          event: { type: 'RUN_STARTED', threadId: THREAD_ID, runId: FIRST_RUN_ID },
          createdAt: BASE_TIME,
        },
        {
          seq: 2,
          eventId: 'repeat-attach-text-start',
          eventType: 'TEXT_MESSAGE_START',
          runId: FIRST_RUN_ID,
          event: {
            type: 'TEXT_MESSAGE_START',
            rawEvent,
            messageId: 'history-message',
            role: 'assistant',
          },
          createdAt: BASE_TIME,
        },
        {
          seq: 3,
          eventId: 'repeat-attach-text-content',
          eventType: 'TEXT_MESSAGE_CONTENT',
          runId: FIRST_RUN_ID,
          event: {
            type: 'TEXT_MESSAGE_CONTENT',
            rawEvent,
            messageId: 'history-message',
            delta: '历史内容',
          },
          createdAt: BASE_TIME,
        },
      ],
    })
    const fetchMock = installFetchMock({
      historyLists: [{
        items: [
          historyListItem({
            threadId: THREAD_ID,
            title: '可重复附着',
            status: 'running',
            lastRunId: FIRST_RUN_ID,
            lastSeq: 3,
          }),
          historyListItem({ id: 2, threadId: SECOND_THREAD_ID, title: '其他会话' }),
        ],
        nextCursor: null,
      }],
      historyDetails: {
        [THREAD_ID]: firstDetail,
        [SECOND_THREAD_ID]: historyDetail({
          threadId: SECOND_THREAD_ID,
          title: '其他会话',
        }),
      },
      streams: [
        [{ type: 'TEXT_MESSAGE_CONTENT', rawEvent, messageId: 'history-message', delta: '第一次附着' }],
        [{ type: 'TEXT_MESSAGE_CONTENT', rawEvent, messageId: 'history-message', delta: '第二次附着' }],
      ],
      keepOpen: true,
    })
    const user = userEvent.setup()

    render(<App />)

    await waitFor(() => expect(chatRequests(fetchMock)).toHaveLength(1))
    await user.click(screen.getByRole('button', { name: '打开会话：其他会话' }))
    const dialog = await screen.findByRole('dialog', { name: '断开实时输出？' })
    await user.click(within(dialog).getByRole('button', { name: '断开并切换' }))
    expect(await screen.findByText('来自 其他会话 的历史回复')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '打开会话：可重复附着' }))

    await waitFor(() => expect(chatRequests(fetchMock)).toHaveLength(2))
    expect(readActiveRunSession()).toMatchObject({
      threadId: THREAD_ID,
      payload: { runId: FIRST_RUN_ID },
    })
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
    expect(requestUrls.filter((url) => url.endsWith('/api/conversation/history?pageSize=100'))).toHaveLength(1)
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
    fireEvent.wheel(pane, { deltaY: -120 })
    fireEvent.scroll(pane)
    expect(window.sessionStorage.getItem(`tinkerfin:conversation-scroll:${THREAD_ID}`)).toBeNull()

    await user.click(screen.getByRole('button', { name: '打开会话：第二条滚动会话' }))
    await screen.findByText('来自 第二条滚动会话 的历史回复')
    expect(window.sessionStorage.getItem(`tinkerfin:conversation-scroll:${THREAD_ID}`)).toBe('240')
    pane.scrollTop = 420
    fireEvent.wheel(pane, { deltaY: -120 })
    fireEvent.scroll(pane)
    expect(window.sessionStorage.getItem(`tinkerfin:conversation-scroll:${SECOND_THREAD_ID}`)).toBeNull()

    await user.click(screen.getByRole('button', { name: '打开会话：第一条滚动会话' }))
    await screen.findByText('来自 第一条滚动会话 的历史回复')
    expect(window.sessionStorage.getItem(`tinkerfin:conversation-scroll:${SECOND_THREAD_ID}`)).toBe('420')
    await waitFor(() => expect(pane.scrollTop).toBe(240))
    expect(screen.queryByRole('button', { name: '回到底部' })).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '打开会话：第二条滚动会话' }))
    await screen.findByText('来自 第二条滚动会话 的历史回复')
    await waitFor(() => expect(pane.scrollTop).toBe(420))
    expect(screen.queryByRole('button', { name: '回到底部' })).not.toBeInTheDocument()
  })

  it('长会话每次只向前展开 100 条并保持滚动锚点与按钮焦点', async () => {
    const messages = Array.from({ length: 250 }, (_, index) => ({
      id: `history-message-${index + 1}`,
      role: 'assistant' as const,
      content: `历史消息 ${index + 1}`,
      createdAt: BASE_TIME,
    }))
    installFetchMock({
      historyLists: [{
        items: [historyListItem({
          threadId: THREAD_ID,
          title: '二百五十条会话',
          lastSeq: 250,
          messageCount: 250,
        })],
        nextCursor: null,
      }],
      historyDetails: {
        [THREAD_ID]: historyDetail({
          threadId: THREAD_ID,
          title: '二百五十条会话',
          lastSeq: 250,
          snapshotSeq: 250,
          messageCount: 250,
          events: [],
          snapshot: {
            snapshotSeq: 250,
            messages,
            todos: [],
            mode: 'default',
            approval: null,
            runStatus: 'idle',
            activeRunId: null,
            serverState: {},
            runs: {},
            interrupts: [],
          },
        }),
      },
    })
    const user = userEvent.setup()
    render(<App />)

    expect(await screen.findByText('历史消息 250')).toBeInTheDocument()
    expect(screen.queryByText('历史消息 150')).not.toBeInTheDocument()
    expect(screen.getByText('历史消息 151')).toBeInTheDocument()
    const pane = screen.getByRole('region', { name: '对话内容' })
    Object.defineProperties(pane, {
      clientHeight: { configurable: true, value: 500 },
      scrollHeight: {
        configurable: true,
        get: () => document.querySelectorAll('.message-list > .message').length * 40,
      },
      scrollTop: { configurable: true, writable: true, value: 200 },
    })
    const loadEarlier = screen.getByRole('button', { name: '加载更早消息' })

    await user.click(loadEarlier)

    expect(await screen.findByText('历史消息 51')).toBeInTheDocument()
    expect(screen.queryByText('历史消息 50')).not.toBeInTheDocument()
    expect(loadEarlier).toHaveFocus()
    expect(pane.scrollTop).toBe(4200)
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
    expect(await screen.findByRole('heading', { name: '置顶' })).toBeInTheDocument()
    expect(screen.queryByText('会话已置顶')).not.toBeInTheDocument()
  })

  it('renames a conversation with the custom dialog without a success toast', async () => {
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
    expect(dialog).toHaveClass('modal-dialog--action', 'has-input')
    expect(document.querySelector('.app-shell')).toHaveAttribute('inert')
    expect(document.querySelector('.app-shell')).toHaveAttribute('aria-hidden', 'true')
    expect(within(dialog).queryByText('输入一个便于在历史记录中识别的名称。')).not.toBeInTheDocument()
    const input = within(dialog).getByRole('textbox', { name: '会话名称' })
    await user.clear(input)
    await user.type(input, '新名称')
    await user.click(within(dialog).getByRole('button', { name: '保存' }))

    await waitFor(() => {
      expect(fetchMock.patchCalls).toContainEqual({ threadId: THREAD_ID, body: { title: '新名称' } })
    })
    expect(await screen.findByRole('button', { name: '打开会话：新名称' })).toBeInTheDocument()
    expect(document.title).toBe('新名称')
    expect(screen.queryByText('会话已重命名')).not.toBeInTheDocument()
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
    window.sessionStorage.setItem(approvalCollapseKey(SECOND_THREAD_ID), 'collapsed')
    window.sessionStorage.setItem(planQuestionCollapseKey(SECOND_THREAD_ID), 'collapsed')
    window.sessionStorage.setItem(planReviewCollapseKey(SECOND_THREAD_ID), 'collapsed')
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
    expect(screen.queryByText('会话已删除')).not.toBeInTheDocument()
    expect(window.sessionStorage.getItem(approvalCollapseKey(SECOND_THREAD_ID))).toBeNull()
    expect(window.sessionStorage.getItem(planQuestionCollapseKey(SECOND_THREAD_ID))).toBeNull()
    expect(window.sessionStorage.getItem(planReviewCollapseKey(SECOND_THREAD_ID))).toBeNull()
  })

  it('loads the next history page when scrolling the sidebar list to the bottom', async () => {
    const observerCallbacks = installHistoryIntersectionObserver()
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

    await waitFor(() => expect(observerCallbacks.length).toBeGreaterThan(0))
    const historyListCalls = () => fetchMock.mock.calls.filter(([input, init]) =>
      fetchCallUrl(input).includes('/api/conversation/history')
      && fetchCallMethod(input, init) === 'GET')
    expect(historyListCalls()).toHaveLength(1)
    await act(async () => {
      observerCallbacks.at(-1)?.(
        [{ isIntersecting: true } as IntersectionObserverEntry],
        {} as IntersectionObserver,
      )
      await Promise.resolve()
    })

    expect(screen.queryByTestId('history-skeleton')).not.toBeInTheDocument()
    expect(historyListCalls()).toHaveLength(2)
    expect(await screen.findByRole('button', { name: '打开会话：会话3' })).toBeInTheDocument()
    expect(historyListCalls()).toHaveLength(2)
    expect(new URL(fetchCallUrl(historyListCalls()[1]![0])).searchParams.get('pageSize')).toBe('100')
  })

  it('keeps 300ms between consecutive cursor requests without delaying the first page', async () => {
    const observerCallbacks = installHistoryIntersectionObserver()
    const THIRD_THREAD_ID = 'thread-throttle-3'
    const FOURTH_THREAD_ID = 'thread-throttle-4'
    const FIFTH_THREAD_ID = 'thread-throttle-5'
    const fetchMock = installFetchMock({
      historyListResolver: (requestIndex) => {
        if (requestIndex === 0) {
          return {
            items: [historyListItem({ threadId: THREAD_ID, title: '会话1' })],
            nextCursor: 'cursor-throttle-2',
          }
        }
        if (requestIndex === 1) {
          return {
            items: [historyListItem({ id: 3, threadId: THIRD_THREAD_ID, title: '会话3' })],
            nextCursor: 'cursor-throttle-3',
          }
        }
        if (requestIndex === 2) {
          return {
            items: [historyListItem({ id: 4, threadId: FOURTH_THREAD_ID, title: '会话4' })],
            nextCursor: 'cursor-throttle-4',
          }
        }
        return {
          items: [historyListItem({ id: 5, threadId: FIFTH_THREAD_ID, title: '会话5' })],
          nextCursor: null,
        }
      },
      historyDetails: {
        [THREAD_ID]: historyDetail({ threadId: THREAD_ID, title: '会话1' }),
      },
    })

    const view = render(<App />)
    expect(await screen.findByRole('button', { name: '打开会话：会话1' })).toBeInTheDocument()
    await waitFor(() => expect(observerCallbacks.length).toBeGreaterThan(0))
    const historyListCalls = () => fetchMock.mock.calls.filter(([input, init]) =>
      fetchCallUrl(input).includes('/api/conversation/history')
      && fetchCallMethod(input, init) === 'GET')

    vi.useFakeTimers({ toFake: ['Date', 'setTimeout', 'clearTimeout'] })
    await act(async () => {
      observerCallbacks.at(-1)?.(
        [{ isIntersecting: true } as IntersectionObserverEntry],
        {} as IntersectionObserver,
      )
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(historyListCalls()).toHaveLength(2)
    expect(screen.getByRole('button', { name: '打开会话：会话3' })).toBeInTheDocument()

    act(() => observerCallbacks.at(-1)?.(
      [{ isIntersecting: false } as IntersectionObserverEntry],
      {} as IntersectionObserver,
    ))
    await act(async () => {
      observerCallbacks.at(-1)?.(
        [{ isIntersecting: true } as IntersectionObserverEntry],
        {} as IntersectionObserver,
      )
      await Promise.resolve()
    })
    expect(screen.getByText('正在加载更多历史会话')).toBeInTheDocument()
    await act(async () => { await vi.advanceTimersByTimeAsync(299) })
    expect(historyListCalls()).toHaveLength(2)
    await act(async () => { await vi.advanceTimersByTimeAsync(1) })
    expect(historyListCalls()).toHaveLength(3)
    expect(screen.getByRole('button', { name: '打开会话：会话4' })).toBeInTheDocument()

    act(() => observerCallbacks.at(-1)?.(
      [{ isIntersecting: false } as IntersectionObserverEntry],
      {} as IntersectionObserver,
    ))
    act(() => observerCallbacks.at(-1)?.(
      [{ isIntersecting: true } as IntersectionObserverEntry],
      {} as IntersectionObserver,
    ))
    expect(screen.getByText('正在加载更多历史会话')).toBeInTheDocument()
    view.unmount()
    await act(async () => { await vi.advanceTimersByTimeAsync(300) })
    expect(historyListCalls()).toHaveLength(3)
    vi.useRealTimers()
  })

  it('使用后端模糊搜索独立分页，并在清空后恢复普通历史缓存', async () => {
    const observerCallbacks = installHistoryIntersectionObserver()
    const SEARCH_THREAD_1 = 'thread-search-1'
    const SEARCH_THREAD_2 = 'thread-search-2'
    const fetchMock = installFetchMock({
      historyListResolver: (_requestIndex, _chatRequestCount, url) => {
        const query = url.searchParams.get('query')
        const cursor = url.searchParams.get('cursor')
        if (!query) {
          return {
            items: [historyListItem({ threadId: THREAD_ID, title: '普通缓存会话' })],
            nextCursor: null,
          }
        }
        if (query !== '目标') throw new Error(`unexpected history query: ${query}`)
        if (!cursor) {
          return {
            items: [historyListItem({ id: 11, threadId: SEARCH_THREAD_1, title: '目标会话一' })],
            nextCursor: 'search-cursor-2',
          }
        }
        if (cursor === 'search-cursor-2') {
          return {
            items: [historyListItem({ id: 12, threadId: SEARCH_THREAD_2, title: '目标会话二' })],
            nextCursor: 'search-cursor-3',
          }
        }
        if (cursor === 'search-cursor-3') {
          return {
            items: [historyListItem({ id: 13, threadId: 'thread-search-3', title: '不应加载的会话' })],
            nextCursor: null,
          }
        }
        throw new Error(`unexpected history cursor: ${cursor}`)
      },
      historyDetails: {
        [THREAD_ID]: historyDetail({ threadId: THREAD_ID, title: '普通缓存会话' }),
      },
    })
    const user = userEvent.setup()
    const view = render(<App />)

    expect(await screen.findByRole('button', { name: '打开会话：普通缓存会话' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '搜索会话' }))
    await user.type(screen.getByRole('textbox', { name: '搜索会话' }), '目标')

    expect(await screen.findByRole('button', { name: '打开会话：目标会话一' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '打开会话：普通缓存会话' })).not.toBeInTheDocument()

    await waitFor(() => expect(observerCallbacks.length).toBeGreaterThan(1))
    const historyListCalls = () => fetchMock.mock.calls.filter(([input, init]) =>
      fetchCallUrl(input).includes('/api/conversation/history')
      && fetchCallMethod(input, init) === 'GET')
    expect(historyListCalls()).toHaveLength(2)
    vi.useFakeTimers({ toFake: ['Date', 'setTimeout', 'clearTimeout'] })
    await act(async () => {
      observerCallbacks.at(-1)?.(
        [{ isIntersecting: true } as IntersectionObserverEntry],
        {} as IntersectionObserver,
      )
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(historyListCalls()).toHaveLength(3)
    expect(screen.getByRole('button', { name: '打开会话：目标会话二' })).toBeInTheDocument()

    act(() => observerCallbacks.at(-1)?.(
      [{ isIntersecting: false } as IntersectionObserverEntry],
      {} as IntersectionObserver,
    ))
    act(() => observerCallbacks.at(-1)?.(
      [{ isIntersecting: true } as IntersectionObserverEntry],
      {} as IntersectionObserver,
    ))
    expect(screen.getByText('正在加载更多历史会话')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '清除搜索' }))
    await act(async () => { await vi.advanceTimersByTimeAsync(300) })
    expect(screen.getByRole('button', { name: '打开会话：普通缓存会话' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '打开会话：目标会话一' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '打开会话：不应加载的会话' })).not.toBeInTheDocument()

    const listUrls = fetchMock.mock.calls
      .map(([input]) => new URL(fetchCallUrl(input), 'http://localhost'))
      .filter((url) => url.pathname.endsWith('/api/conversation/history'))
    expect(listUrls.map((url) => url.searchParams.get('pageSize'))).toEqual(['100', '100', '100'])
    expect(listUrls.filter((url) => !url.searchParams.has('query'))).toHaveLength(1)
    expect(listUrls.filter((url) => url.searchParams.get('query') === '目标').map((url) => (
      url.searchParams.get('cursor')
    ))).toEqual([null, 'search-cursor-2'])
    view.unmount()
    vi.useRealTimers()
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
    await waitFor(() => expect(screen.queryByText('正在加载更多历史会话')).not.toBeInTheDocument())

    act(() => observerCallbacks.at(-1)?.([{ isIntersecting: true } as IntersectionObserverEntry], {} as IntersectionObserver))
    await new Promise((resolve) => window.setTimeout(resolve, 350))

    const listCalls = fetchMock.mock.calls.filter(([input, init]) =>
      fetchCallUrl(input).includes('/api/conversation/history')
      && fetchCallMethod(input, init) === 'GET')
    expect(listCalls).toHaveLength(2)
  })

  it('does not advance a hydrated event cursor from a newer history-list summary', async () => {
    const observerCallbacks = installHistoryIntersectionObserver()
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

    await waitFor(() => expect(observerCallbacks.length).toBeGreaterThan(0))
    act(() => observerCallbacks.at(-1)?.(
      [{ isIntersecting: true } as IntersectionObserverEntry],
      {} as IntersectionObserver,
    ))
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

  it('shows pagination progress and recovers through the explicit retry action', async () => {
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
        if (requestIndex === 1) throw new Error('page failed')
        return {
          items: [historyListItem({ id: 2, threadId: SECOND_THREAD_ID, title: '会话2' })],
          nextCursor: null,
        }
      },
      historyDetails: {
        [THREAD_ID]: historyDetail({ threadId: THREAD_ID, title: '会话1' }),
      },
    })

    const user = userEvent.setup()
    render(<App />)
    expect(await screen.findByRole('button', { name: '打开会话：会话1' })).toBeInTheDocument()
    await waitFor(() => expect(observerCallbacks.length).toBeGreaterThan(0))

    act(() => observerCallbacks.at(-1)?.([{ isIntersecting: true } as IntersectionObserverEntry], {} as IntersectionObserver))
    expect(screen.getByText('正在加载更多历史会话')).toBeInTheDocument()
    await waitFor(() => {
      const failedCalls = fetchMock.mock.calls.filter(([input, init]) =>
        fetchCallUrl(input).includes('/api/conversation/history')
        && fetchCallMethod(input, init) === 'GET')
      expect(failedCalls).toHaveLength(2)
    })
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('历史记录加载失败')

    await user.click(within(alert).getByRole('button', { name: '重试加载历史' }))

    expect(await screen.findByRole('button', { name: '打开会话：会话2' })).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
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
    expect(document.querySelector('.conversation-notice.is-info')).toHaveTextContent('任务已停止')
    expect(document.querySelector('.conversation-notice.is-error')).toBeNull()
    expect(screen.queryByText('聊天生成已取消')).not.toBeInTheDocument()
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
      source: { kind: 'root' as const, agentType: 'main' as const, agentName: 'main', namespace: [] },
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

    fireEvent.wheel(pane, { deltaY: -120 })
    fireEvent.scroll(pane)
    const scrollButton = await screen.findByRole('button', { name: '回到底部' })
    await user.click(scrollButton)

    expect(scrollTo).toHaveBeenCalledWith({ top: 1200, behavior: 'smooth' })
    expect(screen.queryByRole('button', { name: '回到底部' })).not.toBeInTheDocument()

    pane.scrollTop = 300
    fireEvent.scroll(pane)
    expect(screen.queryByRole('button', { name: '回到底部' })).not.toBeInTheDocument()
  })

  it('keeps the centered text scroll-to-bottom pill discoverable outside the scroll pane', async () => {
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
    await screen.findByText('发送一条消息，开始新的真实对话流')
    flushFrames()
    const pane = screen.getByLabelText('对话内容')
    Object.defineProperties(pane, {
      clientHeight: { configurable: true, value: 400 },
      scrollHeight: { configurable: true, value: 1200 },
      scrollTop: { configurable: true, writable: true, value: 100 },
    })
    fireEvent.wheel(pane, { deltaY: -120 })
    fireEvent.scroll(pane)
    flushFrames()
    const scrollButton = await screen.findByRole('button', { name: '回到底部' })
    const actionRail = scrollButton.closest('.conversation-scroll-action')

    expect(actionRail).not.toBeNull()
    expect(scrollButton).toHaveTextContent('回到底部')
    expect(scrollButton.querySelector('.lucide-arrow-down')).toBeInTheDocument()
    expect(pane.contains(scrollButton)).toBe(false)
    const scrollbar = actionRail?.previousElementSibling
    expect(scrollbar).toHaveClass('ui-overlay-scrollbar')
    expect(scrollbar?.previousElementSibling).toBe(pane)

    expect(screen.getByRole('button', { name: '回到底部' })).toBe(scrollButton)
    expect(scrollButton).toHaveClass('is-visible')
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
    fireEvent.wheel(pane, { deltaY: -120 })
    fireEvent.scroll(pane)
    await screen.findByRole('button', { name: '回到底部' })

    const input = screen.getByLabelText('消息输入')
    await user.type(input, '继续说')
    fireEvent.keyDown(input, { key: 'Enter', code: 'Enter' })

    await waitFor(() => expect(pane.scrollTop).toBe(1400))
    expect(screen.queryByRole('button', { name: '回到底部' })).not.toBeInTheDocument()
  })

  it('keeps a new draft out of history and renders its first user message before the backend confirms it', async () => {
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
      streams: [[]],
      keepOpen: true,
    })

    const user = userEvent.setup()
    render(
      <StrictMode>
        <App />
      </StrictMode>,
    )

    await waitFor(() => expect(screen.getByRole('button', { name: '新会话' })).toBeEnabled())
    expect(screen.queryByRole('button', { name: '打开会话：新会话' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '新会话' }))
    expect(screen.queryByRole('button', { name: '打开会话：新会话' })).not.toBeInTheDocument()
    expect(document.title).toBe('TinkerFin')

    await sendMessage('你好')

    expect(screen.getByText('你好')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '打开会话：新会话' })).not.toBeInTheDocument()

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
    expect(request.forwardedProps).toEqual({ model: 'GPT-5.5', command: { plan: 'off' } })
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
        },
      ]],
      keepOpen: true,
    })

    render(<App />)

    await waitFor(() => expect(screen.getByRole('button', { name: '新会话' })).toBeEnabled())
    expect(screen.queryByRole('button', { name: '打开会话：新会话' })).not.toBeInTheDocument()
    await sendMessage('你好')
    expect(await screen.findByRole('button', { name: '打开会话：后端真实会话' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '打开会话：新会话' })).not.toBeInTheDocument()
    expect(document.title).toBe('后端真实会话')
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
    expect(dialog).toHaveClass('modal-dialog--action', 'is-danger')
    expect(within(dialog).getByText(/仍在接收实时输出/)).toBeInTheDocument()
    expect(fetchMock.deleteCalls).toHaveLength(0)
    expect(screen.queryByRole('button', { name: '停止任务' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '停止任务', hidden: true })).toBeInTheDocument()

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
            source: { kind: 'root', agentType: 'main', agentName: 'main', namespace: [] },
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
            source: { kind: 'root', agentType: 'main', agentName: 'main', namespace: [] },
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
            source: { kind: 'root', agentType: 'main', agentName: 'main', namespace: [] },
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
            source: { kind: 'root', agentType: 'main', agentName: 'main', namespace: [] },
            langgraphNode: 'tools',
          },
        },
        {
          type: 'TOOL_CALL_START',
          rawEvent: {
            streamMode: 'messages',
            source: { kind: 'root', agentType: 'main', agentName: 'main', namespace: [] },
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
            source: { kind: 'root', agentType: 'main', agentName: 'main', namespace: [] },
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

    const drawerTrigger = screen.getByRole('button', { name: '打开任务抽屉' })
    expect(drawerTrigger).toBeInTheDocument()
    expect(drawerTrigger.querySelector('.lucide-panel-right')).not.toBeInTheDocument()
    expect(screen.getByLabelText('任务抽屉')).toHaveAttribute('aria-hidden', 'true')
    expect(screen.getByLabelText('任务抽屉')).toHaveAttribute('inert')

    await sendMessage('做个计划')

    await waitFor(() => expect(screen.getByLabelText('任务抽屉')).not.toHaveAttribute('aria-hidden'))
    expect(screen.queryByRole('button', { name: '打开任务抽屉' })).not.toBeInTheDocument()
    expect(document.querySelector('.workspace-main')).toHaveAttribute('inert')
    expect(screen.getByLabelText('会话导航', { selector: 'aside' })).toHaveAttribute('inert')
    expect(screen.getByRole('button', { name: '关闭任务抽屉遮罩' })).toBeInTheDocument()
    await waitFor(() => expect(screen.getByRole('button', { name: '关闭任务详情' })).toHaveFocus())
    expect(screen.getAllByText('读取 url.json').length).toBeGreaterThan(0)
    expect(screen.queryByText('write_todos')).not.toBeInTheDocument()
    expect(screen.queryByText('Updated todo list')).not.toBeInTheDocument()
    expect(document.querySelector('[data-tool-name="read_file"]')).not.toBeNull()
    expect(screen.getByText('Read')).toBeInTheDocument()
    expect(screen.getByText('Loaded url.json')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '关闭任务详情' })).toBeInTheDocument()
    const firstTodo = screen.getByLabelText('步骤 1 · 已完成').closest('.todo-item')
    expect(firstTodo?.tagName).toBe('DIV')
    expect(firstTodo?.querySelector('button')).toBeNull()
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
        messages: [],
        todos: [{ id: 'todo-read-url', content: '读取 url.json', status: 'running' }],
        mode: 'default',
        approval: null,
        runStatus: 'idle',
        activeRunId: null,
        serverState: {},
        runs: {},
        interrupts: [],
      },
    })
    installFetchMock({
      historyLists: [{ items: [historyListItem({ title: '计划会话' })], nextCursor: null }],
      historyDetails: { [THREAD_ID]: detailWithTodos },
    })
    const user = userEvent.setup()
    const firstRender = render(<App />)

    await user.click(await screen.findByRole('button', { name: '关闭任务详情' }))
    expect(screen.getByLabelText('任务抽屉')).toHaveAttribute('aria-hidden', 'true')
    expect(screen.getByLabelText('任务抽屉')).toHaveAttribute('inert')
    expect(screen.getByRole('button', { name: '打开任务抽屉' })).toBeInTheDocument()
    expect(window.sessionStorage.getItem(`tinkerfin:task-drawer:${THREAD_ID}`)).toBe('closed')

    firstRender.unmount()
    render(<App />)

    expect(await screen.findByRole('button', { name: '打开任务抽屉' })).toBeInTheDocument()
    expect(screen.getByLabelText('任务抽屉')).toHaveAttribute('aria-hidden', 'true')
  })

  it('hides the task button while restoring an opened empty drawer after refresh', async () => {
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
    expect(screen.queryByRole('button', { name: '打开任务抽屉' })).not.toBeInTheDocument()
    expect(screen.getByLabelText('任务抽屉')).toBeInTheDocument()
    expect(screen.getByText('暂无待办')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByRole('button', { name: '关闭任务详情' })).toHaveFocus())
    expect(document.querySelector('.workspace-main')).toHaveAttribute('inert')
    expect(screen.getByLabelText('会话导航', { selector: 'aside' })).toHaveAttribute('inert')
    expect(window.sessionStorage.getItem(`tinkerfin:task-drawer:${THREAD_ID}`)).toBe('open')

    firstRender.unmount()
    render(<App />)

    await screen.findByText('来自 空任务会话 的历史回复')
    expect(screen.queryByRole('button', { name: '打开任务抽屉' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '关闭任务详情' })).toBeInTheDocument()
    expect(screen.getByLabelText('任务抽屉')).toBeInTheDocument()
    expect(document.querySelector('.workspace-main')).toHaveAttribute('inert')
    expect(screen.getByLabelText('会话导航', { selector: 'aside' })).toHaveAttribute('inert')
    expect(screen.getByRole('button', { name: '关闭任务抽屉遮罩' })).toBeInTheDocument()
  })

  it('submits sequential Tool approvals once through the ordered resume[] group', async () => {
    const secondWriteCallId = 'call-write-file-second'
    const secondWriteArgs = {
      file_path: 'second-result.txt',
      content: '第二份写入内容',
    }
    const nativeRequests = [
      { name: 'write_file', args: originalWriteArgs },
      { name: 'write_file', args: secondWriteArgs },
    ]
    const nativePolicies = nativeRequests.map(() => ({
      action_name: 'write_file',
      allowed_decisions: ['approve', 'reject'],
    }))
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
              source: { kind: 'root', agentType: 'main', agentName: 'main', namespace: [] },
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
              source: { kind: 'root', agentType: 'main', agentName: 'main', namespace: [] },
              langgraphNode: 'model',
            },
            toolCallId: WRITE_FILE_CALL_ID,
            delta: JSON.stringify(originalWriteArgs),
          },
          {
            type: 'TOOL_CALL_START',
            rawEvent: {
              streamMode: 'messages',
              source: { kind: 'root', agentType: 'main', agentName: 'main', namespace: [] },
              langgraphNode: 'model',
            },
            toolCallId: secondWriteCallId,
            toolCallName: 'write_file',
            parentMessageId: 'parent-write-file',
          },
          {
            type: 'TOOL_CALL_ARGS',
            rawEvent: {
              streamMode: 'messages',
              source: { kind: 'root', agentType: 'main', agentName: 'main', namespace: [] },
              langgraphNode: 'model',
            },
            toolCallId: secondWriteCallId,
            delta: JSON.stringify(secondWriteArgs),
          },
          {
            type: 'RUN_FINISHED',
            threadId: THREAD_ID,
            runId: FIRST_RUN_ID,
            outcome: {
              type: 'interrupt',
              interrupts: [
                {
                  id: `${INTERRUPT_ID}#0`,
                  reason: 'tool_call',
                  message: '需要人工审批：Agent 正准备写入第一份文件',
                  toolCallId: WRITE_FILE_CALL_ID,
                  metadata: {
                    langgraphValue: {
                      action_requests: nativeRequests,
                      review_configs: nativePolicies,
                    },
                    deepagents: {
                      schema: 'tinkerfin.deepagents.tool-review',
                      nativeInterruptId: INTERRUPT_ID,
                      actionIndex: 0,
                      toolName: 'write_file',
                      allowedDecisions: ['approve', 'reject'],
                      originalArgs: originalWriteArgs,
                    },
                  },
                },
                {
                  id: `${INTERRUPT_ID}#1`,
                  reason: 'tool_call',
                  message: '需要人工审批：Agent 正准备写入第二份文件',
                  toolCallId: secondWriteCallId,
                  metadata: {
                    langgraphValue: {
                      action_requests: nativeRequests,
                      review_configs: nativePolicies,
                    },
                    deepagents: {
                      schema: 'tinkerfin.deepagents.tool-review',
                      nativeInterruptId: INTERRUPT_ID,
                      actionIndex: 1,
                      toolName: 'write_file',
                      allowedDecisions: ['approve', 'reject'],
                      originalArgs: secondWriteArgs,
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
              source: { kind: 'root', agentType: 'main', agentName: 'main', namespace: [] },
              langgraphNode: 'tools',
            },
            messageId: 'tool-result-write-file',
            toolCallId: WRITE_FILE_CALL_ID,
            content: 'Updated file /result.txt',
            role: 'tool',
          },
          {
            type: 'TOOL_CALL_RESULT',
            rawEvent: {
              streamMode: 'messages',
              source: { kind: 'root', agentType: 'main', agentName: 'main', namespace: [] },
              langgraphNode: 'tools',
            },
            messageId: 'tool-result-write-file-second',
            toolCallId: secondWriteCallId,
            content: 'Skipped file second-result.txt',
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

    expect(await screen.findByRole('region', { name: '等待审批' })).toHaveTextContent('result.txt')
    expect(document.querySelectorAll('.message-list [data-tool-name="write_file"]')).toHaveLength(1)
    expect(document.querySelector('.message-list [data-tool-name="write_file"]'))
      .toHaveTextContent('result.txt')
    expect(screen.queryByRole('button', { name: '编辑' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '批量提交' })).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '允许' }))
    expect(screen.getByRole('region', { name: '等待审批' })).toHaveTextContent('second-result.txt')
    expect(document.querySelectorAll('.message-list [data-tool-name="write_file"]')).toHaveLength(1)
    expect(document.querySelector('.message-list [data-tool-name="write_file"]'))
      .toHaveTextContent('second-result.txt')
    await user.click(screen.getByRole('button', { name: '拒绝' }))
    await user.type(screen.getByLabelText('拒绝原因（可选）'), '不写入第二份文件')
    await user.click(screen.getByRole('button', { name: '确认拒绝' }))

    await waitFor(() => {
      const chatCalls = fetchMock.mock.calls.filter(
        ([input, init]) => fetchCallMethod(input, init) === 'POST',
      )
      expect(chatCalls).toHaveLength(2)
    })
    const resumeRequestInit = chatRequestAt(fetchMock, 1)
    const resumeRequest = JSON.parse(String(resumeRequestInit?.body)) as ChatRequestPayload
    expect(resumeRequest.threadId).toBe(THREAD_ID)
    expect(resumeRequest.forwardedProps).toEqual({ model: 'GPT-5.5', command: { plan: 'off' } })
    await waitFor(() => {
      expect(document.querySelectorAll('.message-list [data-tool-name="write_file"]')).toHaveLength(2)
    })
    expect(screen.getAllByText('Write').length).toBeGreaterThanOrEqual(2)
    expect(resumeRequest.resume).toEqual([
      {
        interruptId: `${INTERRUPT_ID}#0`,
        status: 'resolved',
        payload: { type: 'approve' },
      },
      {
        interruptId: `${INTERRUPT_ID}#1`,
        status: 'resolved',
        payload: { type: 'reject', message: '不写入第二份文件' },
      },
    ])
  })
})
