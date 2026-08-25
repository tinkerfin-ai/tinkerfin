import { act, renderHook, waitFor } from '@testing-library/react'
import { createElement, StrictMode, useState, type ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { StreamedAgUiEvent } from '../../../api/conversation/client'
import type { ConversationEventEnvelope } from '../../../api/conversation/history'
import type { ChatRequestPayload } from '../../../api/conversation/types'
import { LANGUAGE_STORAGE_KEY } from '../../../i18n'
import type { Conversation, WorkspaceState } from '../../../types'
import { readActiveRunSession } from './activeRunSession'
import { useConversationStreamController } from './useConversationStreamController'

const clientMocks = vi.hoisted(() => ({
  start: vi.fn(),
  resume: vi.fn(),
  cancel: vi.fn(),
}))
const historyMocks = vi.hoisted(() => ({
  fetchEvents: vi.fn(),
}))

vi.mock('../../../api/conversation/client', () => ({
  startConversationRun: clientMocks.start,
  resumeConversationRun: clientMocks.resume,
  cancelConversationRun: clientMocks.cancel,
}))

vi.mock('../../../api/conversation/history', () => ({
  fetchConversationEvents: historyMocks.fetchEvents,
}))

const THREAD_ID = 'thread-controller'
const RUN_ID = 'run-controller'
const BASE_TIME = '2026-08-09T00:00:00.000Z'

const payload: ChatRequestPayload = {
  threadId: THREAD_ID,
  runId: RUN_ID,
  state: {},
  messages: [],
  tools: [],
  context: [],
  forwardedProps: { model: 'main', command: { plan: 'off' } },
}

const conversation = (overrides: Partial<Conversation> = {}): Conversation => ({
  threadId: THREAD_ID,
  title: 'Controller test',
  pinned: false,
  updatedAt: BASE_TIME,
  model: 'GPT-5.5',
  messages: [],
  todos: [],
  runStatus: 'streaming',
  activeRunId: RUN_ID,
  serverState: {},
  lastSeq: 0,
  isHydrated: true,
  ...overrides,
  mode: overrides.mode ?? 'default',
})

async function* streamItems(
  items: StreamedAgUiEvent[],
): AsyncGenerator<StreamedAgUiEvent> {
  for (const item of items) yield item
}

function useControllerHarness(
  initialConversation: Conversation,
  initialDraft: Conversation | null = null,
) {
  const [workspace, setWorkspace] = useState<WorkspaceState>({
    conversations: [initialConversation],
    currentThreadId: initialConversation.threadId,
  })
  const [draftConversation, setDraftConversation] = useState<Conversation | null>(initialDraft)
  const controller = useConversationStreamController({
    workspace,
    setWorkspace,
    setDraftConversation,
  })
  return { controller, workspace, draftConversation, setDraftConversation }
}

function deferred<T>() {
  let resolve: (value: T) => void = () => undefined
  let reject: (reason?: unknown) => void = () => undefined
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

describe('useConversationStreamController', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    window.localStorage.clear()
    window.sessionStorage.clear()
    vi.stubGlobal(
      'requestAnimationFrame',
      (callback: FrameRequestCallback) => window.setTimeout(
        () => callback(performance.now()),
        0,
      ),
    )
    vi.stubGlobal(
      'cancelAnimationFrame',
      (frameId: number) => window.clearTimeout(frameId),
    )
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('drops duplicate seq and fills a persisted gap before the live event', async () => {
    const missingEnvelope: ConversationEventEnvelope = {
      seq: 2,
      eventId: 'event-missing',
      eventType: 'STATE_DELTA',
      runId: RUN_ID,
      event: {
        type: 'STATE_DELTA',
        delta: [{ op: 'add', path: '/missing', value: 2 }],
      },
      createdAt: BASE_TIME,
    }
    historyMocks.fetchEvents.mockResolvedValueOnce([missingEnvelope])
    clientMocks.start.mockImplementation(() => streamItems([
      {
        seq: 1,
        event: {
          type: 'STATE_DELTA',
          delta: [{ op: 'add', path: '/duplicate', value: true }],
        },
      },
      {
        seq: 3,
        event: {
          type: 'STATE_DELTA',
          delta: [{ op: 'add', path: '/live', value: 3 }],
        },
      },
      {
        seq: 4,
        event: {
          type: 'RUN_FINISHED',
          threadId: THREAD_ID,
          runId: RUN_ID,
          outcome: { type: 'success' },
        },
      },
    ]))
    const { result } = renderHook(() => useControllerHarness(
      conversation({ lastSeq: 1 }),
    ))

    await act(async () => {
      await result.current.controller.streamRun(THREAD_ID, payload, 'start')
    })

    await waitFor(() => {
      const updated = result.current.workspace.conversations[0]
      expect(updated.lastSeq).toBe(4)
      expect(updated.serverState).toEqual({ missing: 2, live: 3 })
      expect(updated.runStatus).toBe('idle')
    })
    expect(historyMocks.fetchEvents).toHaveBeenCalledWith(THREAD_ID, {
      afterSeq: 1,
      limit: 2,
      suppressGlobalError: true,
      signal: expect.any(AbortSignal),
    })
  })

  it('reconnects the same canonical run with its last durable sequence', async () => {
    vi.useFakeTimers()
    let attempt = 0
    clientMocks.start.mockImplementation(async function* (
      submitted: ChatRequestPayload,
      _signal?: AbortSignal,
      afterSeq?: number,
    ): AsyncGenerator<StreamedAgUiEvent> {
      attempt += 1
      if (attempt === 1) {
        expect(afterSeq).toBeUndefined()
        yield {
          seq: 1,
          event: {
            type: 'RUN_STARTED',
            threadId: 'thread-canonical',
            runId: RUN_ID,
          },
        }
        throw new TypeError('network disconnected')
      }
      expect(submitted.threadId).toBe('thread-canonical')
      expect(afterSeq).toBe(1)
      yield {
        seq: 2,
        event: {
          type: 'RUN_FINISHED',
          threadId: 'thread-canonical',
          runId: RUN_ID,
          outcome: { type: 'success' },
        },
      }
    })
    const { result } = renderHook(() => useControllerHarness(conversation()))
    let running: Promise<void>
    act(() => {
      running = result.current.controller.streamRun(THREAD_ID, payload, 'start')
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(clientMocks.start).toHaveBeenCalledOnce()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(250)
      await running
      await vi.runAllTimersAsync()
    })

    expect(clientMocks.start).toHaveBeenCalledTimes(2)
    expect(result.current.workspace.conversations[0]).toMatchObject({
      threadId: 'thread-canonical',
      lastSeq: 2,
      runStatus: 'idle',
    })
  })

  it('persists the canonical draft identity before waiting for the second event', async () => {
    const releaseTerminal = deferred<void>()
    const canonicalThreadId = 'thread-canonical-draft'
    const draftPayload: ChatRequestPayload = { ...payload, threadId: '' }
    clientMocks.start.mockImplementation(async function* () {
      yield {
        seq: 1,
        event: {
          type: 'RUN_STARTED',
          threadId: canonicalThreadId,
          runId: RUN_ID,
        },
      }
      await releaseTerminal.promise
      yield {
        seq: 2,
        event: {
          type: 'RUN_FINISHED',
          threadId: canonicalThreadId,
          runId: RUN_ID,
          outcome: { type: 'success' },
        },
      }
    })
    const draft = conversation({
      threadId: '',
      title: '新会话',
      lastSeq: 0,
      isHydrated: false,
    })
    const { result } = renderHook(() => useControllerHarness(conversation(), draft))
    let running: Promise<void>
    act(() => {
      running = result.current.controller.streamRun('', draftPayload, 'start', {
        target: 'draft',
        initialConversation: draft,
      })
    })

    await waitFor(() => {
      expect(readActiveRunSession()).toMatchObject({
        threadId: canonicalThreadId,
        lastSeq: 1,
        payload: {
          threadId: canonicalThreadId,
          runId: RUN_ID,
        },
      })
    })

    releaseTerminal.resolve()
    await act(async () => running)
    expect(readActiveRunSession()).toBeNull()
  })

  it('每 250ms 合并运行游标写入并在终态取消待写所有权', async () => {
    vi.useFakeTimers()
    const releaseTerminal = deferred<void>()
    clientMocks.start.mockImplementation(async function* () {
      yield {
        seq: 1,
        event: { type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID },
      }
      yield {
        seq: 2,
        event: { type: 'STATE_DELTA', delta: [{ op: 'add', path: '/step', value: 2 }] },
      }
      await releaseTerminal.promise
      yield {
        seq: 3,
        event: {
          type: 'RUN_FINISHED',
          threadId: THREAD_ID,
          runId: RUN_ID,
          outcome: { type: 'success' },
        },
      }
    })
    const { result } = renderHook(() => useControllerHarness(conversation()))
    let running!: Promise<void>
    act(() => {
      running = result.current.controller.streamRun(THREAD_ID, payload, 'start')
    })
    await act(async () => vi.advanceTimersByTimeAsync(0))

    expect(readActiveRunSession()?.lastSeq).toBe(0)
    await act(async () => vi.advanceTimersByTimeAsync(249))
    expect(readActiveRunSession()?.lastSeq).toBe(0)
    await act(async () => vi.advanceTimersByTimeAsync(1))
    expect(readActiveRunSession()?.lastSeq).toBe(2)

    releaseTerminal.resolve()
    await act(async () => running)
    expect(readActiveRunSession()).toBeNull()
    await act(async () => vi.runAllTimersAsync())
    expect(readActiveRunSession()).toBeNull()
  })

  it('每 50ms 合并文本渲染，并在消息结束边界立即冲刷剩余增量', async () => {
    vi.useFakeTimers()
    const releaseBoundary = deferred<void>()
    clientMocks.start.mockImplementation(async function* () {
      yield {
        seq: 1,
        event: { type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID },
      }
      yield {
        seq: 2,
        event: { type: 'TEXT_MESSAGE_START', messageId: 'assistant-cadence', role: 'assistant' },
      }
      yield {
        seq: 3,
        event: { type: 'TEXT_MESSAGE_CONTENT', messageId: 'assistant-cadence', delta: 'A' },
      }
      yield {
        seq: 4,
        event: { type: 'TEXT_MESSAGE_CONTENT', messageId: 'assistant-cadence', delta: 'B' },
      }
      await releaseBoundary.promise
      yield {
        seq: 5,
        event: { type: 'TEXT_MESSAGE_CONTENT', messageId: 'assistant-cadence', delta: 'C' },
      }
      yield {
        seq: 6,
        event: { type: 'TEXT_MESSAGE_END', messageId: 'assistant-cadence' },
      }
      yield {
        seq: 7,
        event: {
          type: 'RUN_FINISHED',
          threadId: THREAD_ID,
          runId: RUN_ID,
          outcome: { type: 'success' },
        },
      }
    })
    const { result } = renderHook(() => useControllerHarness(conversation()))
    let running!: Promise<void>
    act(() => {
      running = result.current.controller.streamRun(THREAD_ID, payload, 'start')
    })
    await act(async () => vi.advanceTimersByTimeAsync(0))

    expect(result.current.workspace.conversations[0]?.messages[0]?.content).toBe('')
    await act(async () => vi.advanceTimersByTimeAsync(49))
    expect(result.current.workspace.conversations[0]?.messages[0]?.content).toBe('')
    await act(async () => vi.advanceTimersByTimeAsync(1))
    expect(result.current.workspace.conversations[0]?.messages[0]?.content).toBe('AB')

    releaseBoundary.resolve()
    await act(async () => running)
    expect(result.current.workspace.conversations[0]).toMatchObject({
      runStatus: 'idle',
      lastSeq: 7,
      messages: [expect.objectContaining({ content: 'ABC' })],
    })
  })

  it('页面进入后台时立即提交最新运行游标', async () => {
    vi.useFakeTimers()
    clientMocks.start.mockImplementation(async function* (
      _payload: ChatRequestPayload,
      signal?: AbortSignal,
    ) {
      yield {
        seq: 1,
        event: { type: 'STATE_DELTA', delta: [{ op: 'add', path: '/step', value: 1 }] },
      }
      await new Promise<void>((resolve) => {
        if (signal?.aborted) resolve()
        else signal?.addEventListener('abort', () => resolve(), { once: true })
      })
    })
    const { result } = renderHook(() => useControllerHarness(conversation()))
    let running!: Promise<void>
    act(() => {
      running = result.current.controller.streamRun(THREAD_ID, payload, 'start')
    })
    await act(async () => vi.advanceTimersByTimeAsync(0))
    expect(readActiveRunSession()?.lastSeq).toBe(0)

    act(() => window.dispatchEvent(new Event('pagehide')))
    expect(readActiveRunSession()?.lastSeq).toBe(1)

    act(() => result.current.controller.detachThreadStream(THREAD_ID, '测试清理'))
    await act(async () => running)
  })

  it('attaches a restored run from its first durable cursor and clears persistence on terminal', async () => {
    clientMocks.start.mockImplementation(async function* (
      submitted: ChatRequestPayload,
      _signal?: AbortSignal,
      afterSeq?: number,
    ): AsyncGenerator<StreamedAgUiEvent> {
      expect(submitted).toEqual(payload)
      expect(afterSeq).toBe(41)
      expect(readActiveRunSession()).toMatchObject({
        threadId: THREAD_ID,
        lastSeq: 41,
        payload: { runId: RUN_ID },
      })
      yield {
        seq: 42,
        event: {
          type: 'RUN_FINISHED',
          threadId: THREAD_ID,
          runId: RUN_ID,
          outcome: { type: 'success' },
        },
      }
    })
    const restored = conversation({ runStatus: 'detached', lastSeq: 41 })
    const { result } = renderHook(() => useControllerHarness(restored))

    await act(async () => {
      await result.current.controller.streamRun(THREAD_ID, payload, 'start', {
        target: 'workspace',
        initialAfterSeq: 41,
      })
    })

    await waitFor(() => {
      expect(result.current.workspace.conversations[0]).toMatchObject({
        lastSeq: 42,
        runStatus: 'idle',
      })
    })
    expect(readActiveRunSession()).toBeNull()
  })

  it('keeps a restored run and retries when the first attach drops before any event', async () => {
    vi.useFakeTimers()
    let attempt = 0
    clientMocks.start.mockImplementation(async function* (
      submitted: ChatRequestPayload,
      _signal?: AbortSignal,
      afterSeq?: number,
    ): AsyncGenerator<StreamedAgUiEvent> {
      attempt += 1
      expect(submitted).toEqual(payload)
      expect(afterSeq).toBe(41)
      if (attempt === 1) {
        expect(readActiveRunSession()).toMatchObject({
          threadId: THREAD_ID,
          lastSeq: 41,
          payload: { runId: RUN_ID },
        })
        throw new TypeError('network disconnected before first event')
      }
      expect(readActiveRunSession()).toMatchObject({
        threadId: THREAD_ID,
        lastSeq: 41,
        payload: { runId: RUN_ID },
      })
      yield {
        seq: 42,
        event: {
          type: 'RUN_FINISHED',
          threadId: THREAD_ID,
          runId: RUN_ID,
          outcome: { type: 'success' },
        },
      }
    })
    const restored = conversation({ runStatus: 'detached', lastSeq: 41 })
    const { result } = renderHook(() => useControllerHarness(restored))
    let running: Promise<void>
    act(() => {
      running = result.current.controller.streamRun(THREAD_ID, payload, 'start', {
        target: 'workspace',
        initialAfterSeq: 41,
      })
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(clientMocks.start).toHaveBeenCalledOnce()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(250)
      await running
      await vi.runAllTimersAsync()
    })

    expect(clientMocks.start).toHaveBeenCalledTimes(2)
    expect(result.current.workspace.conversations[0]).toMatchObject({
      lastSeq: 42,
      runStatus: 'idle',
    })
    expect(readActiveRunSession()).toBeNull()
  })

  it('requests backend cancellation without detaching the active stream', async () => {
    clientMocks.cancel.mockResolvedValue({ cancelled: true })
    clientMocks.start.mockImplementation(async function* (
      _payload: ChatRequestPayload,
      signal?: AbortSignal,
    ): AsyncGenerator<StreamedAgUiEvent> {
      await new Promise<void>((resolve) => {
        if (signal?.aborted) resolve()
        else signal?.addEventListener('abort', () => resolve(), { once: true })
      })
      yield* []
    })
    const { result } = renderHook(() => useControllerHarness(conversation()))
    let running: Promise<void>
    act(() => {
      running = result.current.controller.streamRun(THREAD_ID, payload, 'start')
    })
    await waitFor(() => expect(result.current.controller.hasActiveStream()).toBe(true))

    let cancelled = false
    await act(async () => {
      cancelled = await result.current.controller.cancelActiveRun()
    })

    expect(cancelled).toBe(true)
    expect(clientMocks.cancel).toHaveBeenCalledWith(THREAD_ID, RUN_ID)
    expect(result.current.controller.hasActiveStream()).toBe(true)
    act(() => result.current.controller.detachThreadStream(THREAD_ID, '测试清理'))
    await act(async () => running)
  })

  it('owns active stream cancellation and flushes queued state before detach', async () => {
    clientMocks.start.mockImplementation(async function* (
      _payload: ChatRequestPayload,
      signal?: AbortSignal,
    ): AsyncGenerator<StreamedAgUiEvent> {
      yield {
        seq: 1,
        event: { type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID },
      }
      await new Promise<void>((resolve) => {
        if (signal?.aborted) resolve()
        else signal?.addEventListener('abort', () => resolve(), { once: true })
      })
    })
    const { result } = renderHook(() => useControllerHarness(conversation()))

    let running: Promise<void>
    act(() => {
      running = result.current.controller.streamRun(THREAD_ID, payload, 'start')
    })
    await waitFor(() => expect(result.current.controller.hasActiveStream()).toBe(true))

    act(() => {
      result.current.controller.detachThreadStream(THREAD_ID, '测试主动断开')
    })
    await act(async () => {
      await running
    })

    expect(result.current.controller.hasActiveStream()).toBe(false)
    expect(result.current.controller.getActiveThreadId()).toBeNull()
    await waitFor(() => {
      const updated = result.current.workspace.conversations[0]
      expect(updated.lastSeq).toBe(1)
      expect(updated.runStatus).toBe('detached')
      expect(updated.notice).toEqual({ kind: 'info', content: '测试主动断开' })
      expect(updated.messages).toEqual([])
    })
  })

  it('stores a request failure as an id-free local notice', async () => {
    window.localStorage.setItem(LANGUAGE_STORAGE_KEY, 'en')
    clientMocks.start.mockImplementation(() => {
      throw new Error('接口暂不可用')
    })
    const { result } = renderHook(() => useControllerHarness(conversation({
      runStatus: 'streaming',
      messages: [],
    })))

    await act(async () => {
      await result.current.controller.streamRun(THREAD_ID, payload, 'start')
    })

    await waitFor(() => {
      const updated = result.current.workspace.conversations[0]
      expect(updated.runStatus).toBe('error')
      expect(updated.notice).toEqual({
        kind: 'error',
        content: 'The conversation request failed. Try again',
      })
      expect(updated.notice?.content).not.toContain('接口暂不可用')
      expect(updated.messages).toEqual([])
    })
  })

  it('非法 Patch 在进入 React 队列前失败并保留最后有效状态', async () => {
    historyMocks.fetchEvents.mockResolvedValue([])
    clientMocks.start.mockImplementation(() => streamItems([{
      seq: 1,
      event: {
        type: 'STATE_DELTA',
        delta: [{ op: 'remove', path: '/missing' }],
      },
    }]))
    const { result } = renderHook(() => useControllerHarness(conversation({
      serverState: { stable: true },
      lastSeq: 0,
    })))

    await act(async () => {
      await result.current.controller.streamRun(THREAD_ID, payload, 'start')
    })

    const updated = result.current.workspace.conversations[0]
    expect(updated.serverState).toEqual({ stable: true })
    expect(updated.lastSeq).toBe(0)
    expect(updated.runStatus).toBe('detached')
    expect(updated.notice).toEqual({
      kind: 'info',
      content: '会话状态更新失败，请重试',
    })
  })

  it('stores a catch-up failure as an id-free local notice', async () => {
    historyMocks.fetchEvents.mockRejectedValueOnce(new Error('事件库暂不可用'))
    const { result } = renderHook(() => useControllerHarness(conversation({
      runStatus: 'detached',
      activeRunId: undefined,
      lastSeq: 9,
      messages: [],
    })))

    await act(async () => {
      await result.current.controller.catchUpDetachedConversation(THREAD_ID)
    })

    const updated = result.current.workspace.conversations[0]
    expect(updated.notice).toEqual({
      kind: 'error',
      content: '实时事件恢复失败，请重试',
    })
    expect(updated.messages).toEqual([])
  })

  it('does not abort the active stream when asked to detach another thread', async () => {
    let streamSignal: AbortSignal | undefined
    clientMocks.start.mockImplementation(async function* (
      _payload: ChatRequestPayload,
      signal?: AbortSignal,
    ): AsyncGenerator<StreamedAgUiEvent> {
      streamSignal = signal
      await new Promise<void>((resolve) => {
        if (signal?.aborted) resolve()
        else signal?.addEventListener('abort', () => resolve(), { once: true })
      })
      yield* []
    })
    const { result } = renderHook(() => useControllerHarness(conversation()))
    let running: Promise<void>
    act(() => {
      running = result.current.controller.streamRun(THREAD_ID, payload, 'start')
    })
    await waitFor(() => expect(streamSignal).toBeDefined())

    act(() => {
      result.current.controller.detachThreadStream('thread-other', '不应断开')
    })
    const stayedActive = result.current.controller.hasActiveStream()
      && streamSignal?.aborted === false

    act(() => {
      result.current.controller.detachThreadStream(THREAD_ID, '测试清理')
    })
    await act(async () => {
      await running
    })
    expect(stayedActive).toBe(true)
  })

  it('does not replace a newer thread draft with a queued detached draft', async () => {
    vi.useFakeTimers()
    const newerDraft = conversation({
      threadId: 'thread-new-draft',
      title: 'New draft',
      runStatus: 'idle',
      activeRunId: undefined,
    })
    clientMocks.start.mockImplementation(async function* (
      _payload: ChatRequestPayload,
      signal?: AbortSignal,
    ): AsyncGenerator<StreamedAgUiEvent> {
      yield {
        seq: 1,
        event: {
          type: 'TEXT_MESSAGE_CONTENT',
          messageId: 'assistant-queued',
          delta: '旧草稿增量',
        },
      }
      await new Promise<void>((resolve) => {
        if (signal?.aborted) resolve()
        else signal?.addEventListener('abort', () => resolve(), { once: true })
      })
    })
    const draftToStream = conversation()
    const { result } = renderHook(() => useControllerHarness(newerDraft, newerDraft))
    let running: Promise<void>
    act(() => {
      running = result.current.controller.streamRun(THREAD_ID, payload, 'start', {
        target: 'draft',
        initialConversation: draftToStream,
      })
    })
    await act(async () => vi.advanceTimersByTimeAsync(0))

    act(() => {
      result.current.controller.detachThreadStream(THREAD_ID, '旧草稿断开')
    })
    await act(async () => {
      await running
    })

    expect(result.current.draftConversation?.threadId).toBe('thread-new-draft')
    expect(result.current.draftConversation?.messages).toEqual([])
  })

  it('aborts stale catch-up when a live stream starts for the same thread', async () => {
    const historyRequest = deferred<ConversationEventEnvelope[]>()
    historyMocks.fetchEvents.mockReturnValueOnce(historyRequest.promise)
    clientMocks.start.mockImplementation(async function* (
      _payload: ChatRequestPayload,
      signal?: AbortSignal,
    ): AsyncGenerator<StreamedAgUiEvent> {
      await new Promise<void>((resolve) => {
        if (signal?.aborted) resolve()
        else signal?.addEventListener('abort', () => resolve(), { once: true })
      })
      yield* []
    })
    const { result } = renderHook(() => useControllerHarness(conversation({
      runStatus: 'detached',
      activeRunId: undefined,
    })))
    let catchUp: Promise<void>
    act(() => {
      catchUp = result.current.controller.catchUpDetachedConversation(THREAD_ID)
    })
    await waitFor(() => expect(historyMocks.fetchEvents).toHaveBeenCalledOnce())
    const catchUpSignal = historyMocks.fetchEvents.mock.calls[0]?.[1]?.signal as
      | AbortSignal
      | undefined
    let running: Promise<void>
    act(() => {
      running = result.current.controller.streamRun(THREAD_ID, payload, 'start')
    })
    await waitFor(() => expect(result.current.controller.hasActiveStream()).toBe(true))
    const wasAborted = catchUpSignal?.aborted ?? false
    historyRequest.reject(new DOMException('请求已取消', 'AbortError'))
    await act(async () => {
      await catchUp
    })

    act(() => {
      result.current.controller.detachThreadStream(THREAD_ID, '测试清理')
    })
    await act(async () => {
      await running
    })
    expect(catchUpSignal).toBeInstanceOf(AbortSignal)
    expect(wasAborted).toBe(true)
    expect(result.current.workspace.conversations[0]?.messages).not.toEqual(
      expect.arrayContaining([expect.objectContaining({ role: 'error' })]),
    )
  })

  it('starts delayed catch-up even when the aborted request has not settled', async () => {
    const staleHistoryRequest = deferred<ConversationEventEnvelope[]>()
    historyMocks.fetchEvents
      .mockReturnValueOnce(staleHistoryRequest.promise)
      .mockResolvedValueOnce([])
    clientMocks.start.mockImplementation(() => streamItems([]))
    const { result } = renderHook(() => useControllerHarness(conversation({
      runStatus: 'detached',
      activeRunId: undefined,
    })))
    let staleCatchUp: Promise<void>
    act(() => {
      staleCatchUp = result.current.controller.catchUpDetachedConversation(THREAD_ID)
    })
    await waitFor(() => expect(historyMocks.fetchEvents).toHaveBeenCalledOnce())

    await act(async () => {
      await result.current.controller.streamRun(THREAD_ID, payload, 'start')
    })

    await waitFor(() => expect(historyMocks.fetchEvents).toHaveBeenCalledTimes(2))
    staleHistoryRequest.reject(new DOMException('请求已取消', 'AbortError'))
    await act(async () => {
      await staleCatchUp
    })
  })

  it('cancels an in-flight catch-up when the controller unmounts', async () => {
    const historyRequest = deferred<ConversationEventEnvelope[]>()
    historyMocks.fetchEvents.mockReturnValueOnce(historyRequest.promise)
    const { result, unmount } = renderHook(() => useControllerHarness(conversation({
      runStatus: 'detached',
      activeRunId: undefined,
    })))
    act(() => {
      void result.current.controller.catchUpDetachedConversation(THREAD_ID)
    })
    await waitFor(() => expect(historyMocks.fetchEvents).toHaveBeenCalledOnce())
    const catchUpSignal = historyMocks.fetchEvents.mock.calls[0]?.[1]?.signal as
      | AbortSignal
      | undefined

    unmount()
    const wasAborted = catchUpSignal?.aborted ?? false
    historyRequest.reject(new DOMException('请求已取消', 'AbortError'))

    expect(catchUpSignal).toBeInstanceOf(AbortSignal)
    expect(wasAborted).toBe(true)
  })

  it('deduplicates cancellation and keeps the stop owner until the stream terminates', async () => {
    const cancelResponse = deferred<{ cancelled: boolean }>()
    clientMocks.cancel.mockReturnValueOnce(cancelResponse.promise)
    clientMocks.start.mockImplementation(async function* (
      _payload: ChatRequestPayload,
      signal?: AbortSignal,
    ): AsyncGenerator<StreamedAgUiEvent> {
      await new Promise<void>((resolve) => {
        if (signal?.aborted) resolve()
        else signal?.addEventListener('abort', () => resolve(), { once: true })
      })
      yield* []
    })
    const { result } = renderHook(() => useControllerHarness(conversation()))
    let running: Promise<void>
    act(() => {
      running = result.current.controller.streamRun(THREAD_ID, payload, 'start')
    })
    await waitFor(() => expect(result.current.controller.hasActiveStream()).toBe(true))

    let first!: Promise<boolean>
    let second!: Promise<boolean>
    act(() => {
      first = result.current.controller.cancelActiveRun()
      second = result.current.controller.cancelActiveRun()
    })

    expect(second).toBe(first)
    expect(result.current.controller.cancelPendingRunId).toBe(RUN_ID)
    expect(clientMocks.cancel).toHaveBeenCalledOnce()

    await act(async () => {
      cancelResponse.resolve({ cancelled: true })
      await expect(first).resolves.toBe(true)
    })
    expect(result.current.controller.cancelPendingRunId).toBe(RUN_ID)

    act(() => result.current.controller.detachThreadStream(THREAD_ID, '测试断开'))
    await act(async () => running)
    expect(result.current.controller.cancelPendingRunId).toBeNull()
  })

  it('aborts an active stream on unmount without cancelling the durable run', async () => {
    let streamSignal: AbortSignal | undefined
    clientMocks.start.mockImplementation(async function* (
      _payload: ChatRequestPayload,
      signal?: AbortSignal,
    ): AsyncGenerator<StreamedAgUiEvent> {
      streamSignal = signal
      await new Promise<void>((resolve) => {
        if (signal?.aborted) resolve()
        else signal?.addEventListener('abort', () => resolve(), { once: true })
      })
      yield* []
    })
    const { result, unmount } = renderHook(() => useControllerHarness(conversation()))
    let running: Promise<void>
    act(() => {
      running = result.current.controller.streamRun(THREAD_ID, payload, 'start')
    })
    await waitFor(() => expect(result.current.controller.hasActiveStream()).toBe(true))

    unmount()
    await act(async () => running)

    expect(streamSignal?.aborted).toBe(true)
    expect(clientMocks.cancel).not.toHaveBeenCalled()
  })

  it('clears delayed catch-up after unmount', async () => {
    vi.useFakeTimers()
    clientMocks.start.mockImplementation(() => streamItems([]))
    const { result, unmount } = renderHook(() => useControllerHarness(conversation()))

    await act(async () => {
      await result.current.controller.streamRun(THREAD_ID, payload, 'start')
      await vi.advanceTimersByTimeAsync(0)
    })
    unmount()
    await vi.advanceTimersByTimeAsync(100)

    expect(historyMocks.fetchEvents).not.toHaveBeenCalled()
  })

  it('allows catch-up after the StrictMode effect replay', async () => {
    historyMocks.fetchEvents.mockResolvedValueOnce([])
    const wrapper = ({ children }: { children: ReactNode }) => (
      createElement(StrictMode, null, children)
    )
    const { result } = renderHook(() => useControllerHarness(conversation({
      runStatus: 'detached',
      activeRunId: undefined,
    })), { wrapper })

    await act(async () => {
      await result.current.controller.catchUpDetachedConversation(THREAD_ID)
    })

    expect(historyMocks.fetchEvents).toHaveBeenCalledOnce()
  })
})
