import { act, renderHook, waitFor } from '@testing-library/react'
import { useState } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { ConversationHistoryDetail } from '../../api/conversation/history'
import { upsertConversation } from '../../lib/workspace'
import { emptyTraceGraph } from '../../test/traceFixtures'
import type { Conversation, WorkspaceState } from '../../types'
import { restoreConversationFromTrace } from '../conversation/trace/runtime'
import {
  historyItemFromDetail,
  mergeHistoryConversations,
  useWorkspaceHistory,
} from './useWorkspaceHistory'

const historyMocks = vi.hoisted(() => ({
  detail: vi.fn(),
  groupConfig: vi.fn(),
  list: vi.fn(),
}))

vi.mock('../../api/conversation/history', () => ({
  fetchConversationHistoryDetail: historyMocks.detail,
  fetchConversationHistoryGroupConfig: historyMocks.groupConfig,
  fetchConversationHistoryList: historyMocks.list,
}))

const THREAD_ID = 'thread-history-race'
const RUN_ID = 'run-history-race'
const BASE_TIME = '2026-08-28T00:00:00.000Z'

type ReadyTaskTrace = Extract<Conversation['taskTrace'], { phase: 'ready' }>

const readyTaskTrace = (suffix: string): ReadyTaskTrace => ({
  phase: 'ready',
  snapshot: {
    status: 'ready',
    todoGroups: [{
      id: `todo-group:${suffix}`,
      userMessageId: `message:${suffix}`,
      userMessagePreview: `任务 ${suffix}`,
      groupToolCallId: `tool:${suffix}`,
      createdAt: BASE_TIME,
      status: 'running',
      todos: [],
    }],
  },
})

const detail = (
  overrides: Partial<ConversationHistoryDetail> = {},
): ConversationHistoryDetail => ({
  id: 1,
  threadId: THREAD_ID,
  title: '分页竞态',
  lastModel: 'main',
  pinned: false,
  asOfSeq: 5,
  headRunId: RUN_ID,
  availableHeads: [RUN_ID],
  historyCursor: 'cursor-1',
  messageCount: 1,
  toolCallCount: 0,
  messages: [{
    id: 'message-1',
    traceSeq: 1,
    sourceId: 'assistant-1',
    namespace: [],
    runId: RUN_ID,
    role: 'assistant',
    content: '初始内容',
    contentOmitted: false,
    status: 'completed',
    createdAt: BASE_TIME,
    completedAt: BASE_TIME,
  }],
  reasoning: [],
  state: { root: {}, subgraphs: {} },
  interactions: [],
  status: { execution: 'running', headRunId: RUN_ID },
  completeness: { missingPrefix: false, missingTail: false, payloadOmitted: false },
  createdAt: BASE_TIME,
  updatedAt: BASE_TIME,
  ...overrides,
  graph: overrides.graph ?? emptyTraceGraph(overrides.asOfSeq ?? 5),
  taskTrace: overrides.taskTrace ?? { status: 'ready', todoGroups: [] },
})

function deferred<T>() {
  let resolve: (value: T) => void = () => undefined
  let reject: (reason?: unknown) => void = () => undefined
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, reject, resolve }
}

function useHarness(
  initial: ConversationHistoryDetail,
  options: {
    onToast?: (kind: 'error', message: string) => void
    prepareTaskTraceOwner?: (threadId: string) => Promise<void>
  } = {},
) {
  const [workspace, setWorkspace] = useState<WorkspaceState>({
    conversations: [{
      ...restoreConversationFromTrace(initial, { model: 'main', includeTaskTrace: true }),
      isHydrated: true,
    }],
    currentThreadId: initial.threadId,
  })
  const history = useWorkspaceHistory({
    workspace,
    setWorkspace,
    defaultModelId: 'main',
    modelCatalogStatus: 'loading',
    followDetachedConversation: vi.fn(),
    prepareTaskTraceOwner: options.prepareTaskTraceOwner ?? vi.fn(async () => undefined),
    onToast: options.onToast ?? vi.fn(),
  })
  const advanceTrace = (next: ConversationHistoryDetail) => {
    setWorkspace((state) => upsertConversation(
      state,
      { ...restoreConversationFromTrace(next, { model: 'main', includeTaskTrace: true }), isHydrated: true },
    ))
  }
  const startOwnedRun = (taskTrace?: Conversation['taskTrace']) => {
    setWorkspace((state) => ({
      ...state,
      conversations: state.conversations.map((conversation) => (
        conversation.threadId === initial.threadId
          ? {
              ...conversation,
              runStatus: 'streaming',
              activeRunId: 'run-owned-new',
              messages: [{
                id: 'message-owned-new',
                role: 'user',
                content: '本次新输入',
                createdAt: BASE_TIME,
              }],
              taskTrace: taskTrace ?? conversation.taskTrace,
            }
          : conversation
      )),
    }))
  }
  const advanceDelivery = (taskTrace: Conversation['taskTrace']) => {
    setWorkspace((state) => ({
      ...state,
      conversations: state.conversations.map((conversation) => (
        conversation.threadId === initial.threadId
          ? {
              ...conversation,
              lastSeq: (conversation.lastSeq ?? 0) + 1,
              messages: [{
                id: 'message-delivered-new',
                role: 'assistant',
                content: '同一 Run 的新投递',
                createdAt: BASE_TIME,
              }],
              taskTrace,
            }
          : conversation
      )),
    }))
  }
  const switchThread = (threadId: string) => {
    setWorkspace((state) => ({ ...state, currentThreadId: threadId }))
  }
  return {
    advanceDelivery,
    advanceTrace,
    history,
    startOwnedRun,
    switchThread,
    workspace,
  }
}

describe('useWorkspaceHistory Trace pagination authority', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    historyMocks.list.mockResolvedValue({ items: [], nextCursor: null })
    historyMocks.groupConfig.mockResolvedValue({ dayRanges: [] })
  })

  it('discards an old fixed-as-of page after follow advances the Trace', async () => {
    const response = deferred<ConversationHistoryDetail>()
    historyMocks.detail.mockReturnValue(response.promise)
    const initial = detail()
    const newer = detail({
      asOfSeq: 6,
      historyCursor: null,
      status: { execution: 'succeeded', headRunId: RUN_ID },
      messages: [{ ...initial.messages[0]!, content: '并发终态内容' }],
    })
    const olderPage = detail({
      historyCursor: 'cursor-2',
      messages: [{ ...initial.messages[0]!, content: '旧分页内容' }],
    })
    const { result } = renderHook(() => useHarness(initial))
    let loading: Promise<boolean> = Promise.resolve(false)

    act(() => {
      loading = result.current.history.loadOlderTrace(THREAD_ID)
    })
    await waitFor(() => expect(historyMocks.detail).toHaveBeenCalledOnce())
    act(() => result.current.advanceTrace(newer))
    await waitFor(() => {
      expect(result.current.workspace.conversations[0]?.trace?.asOfSeq).toBe(6)
    })

    let loaded = true
    await act(async () => {
      response.resolve(olderPage)
      loaded = await loading
    })

    const current = result.current.workspace.conversations[0]
    expect(loaded).toBe(false)
    expect(current?.trace?.asOfSeq).toBe(6)
    expect(current?.runStatus).toBe('idle')
    expect(current?.messages[0]?.content).toBe('并发终态内容')
  })

  it('discards an old page after an owned run starts before Trace advances', async () => {
    const response = deferred<ConversationHistoryDetail>()
    historyMocks.detail.mockReturnValue(response.promise)
    const initial = detail()
    const olderPage = detail({
      historyCursor: 'cursor-2',
      messages: [{ ...initial.messages[0]!, content: '旧分页内容' }],
    })
    const { result } = renderHook(() => useHarness(initial))
    let loading: Promise<boolean> = Promise.resolve(false)

    act(() => {
      loading = result.current.history.loadOlderTrace(THREAD_ID)
    })
    await waitFor(() => expect(historyMocks.detail).toHaveBeenCalledOnce())
    act(() => result.current.startOwnedRun())
    await waitFor(() => {
      expect(result.current.workspace.conversations[0]?.runStatus).toBe('streaming')
    })

    let loaded = true
    await act(async () => {
      response.resolve(olderPage)
      loaded = await loading
    })

    const current = result.current.workspace.conversations[0]
    expect(loaded).toBe(false)
    expect(current?.runStatus).toBe('streaming')
    expect(current?.activeRunId).toBe('run-owned-new')
    expect(current?.messages[0]?.content).toBe('本次新输入')
    expect(current?.trace?.asOfSeq).toBe(5)
  })

  it('forwards caller cancellation to an older Trace page request', async () => {
    historyMocks.detail.mockImplementation((
      _threadId: string,
      options: { signal?: AbortSignal },
    ) => new Promise<ConversationHistoryDetail>((_resolve, reject) => {
      options.signal?.addEventListener(
        'abort',
        () => reject(new DOMException('aborted', 'AbortError')),
        { once: true },
      )
    }))
    const { result } = renderHook(() => useHarness(detail()))
    const controller = new AbortController()
    let loading: Promise<boolean> = Promise.resolve(false)

    act(() => {
      loading = result.current.history.loadOlderTrace(THREAD_ID, {
        signal: controller.signal,
      })
    })
    await waitFor(() => expect(historyMocks.detail).toHaveBeenCalledOnce())
    const requestSignal = historyMocks.detail.mock.calls[0]?.[1]?.signal as
      | AbortSignal
      | undefined

    controller.abort()
    await expect(loading).resolves.toBe(false)
    expect(requestSignal?.aborted).toBe(true)
  })

  it('reloads an unavailable task trace once when the user explicitly retries', async () => {
    const response = deferred<ConversationHistoryDetail>()
    const initial = detail({
      taskTrace: {
        status: 'unavailable',
        todoGroups: [],
        errorCode: 'trace_incomplete',
      },
    })
    const refreshed = detail({
      asOfSeq: 6,
      taskTrace: { status: 'ready', todoGroups: [] },
    })
    historyMocks.detail.mockReturnValue(response.promise)
    const prepareTaskTraceOwner = vi.fn(async () => undefined)
    const { result } = renderHook(() => useHarness(initial, { prepareTaskTraceOwner }))

    expect(result.current.workspace.conversations[0]?.taskTrace.phase).toBe('unavailable')
    act(() => {
      result.current.history.retryTaskTrace(THREAD_ID)
      result.current.history.retryTaskTrace(THREAD_ID)
    })

    await waitFor(() => expect(historyMocks.detail).toHaveBeenCalledOnce())
    expect(prepareTaskTraceOwner).toHaveBeenCalledOnce()
    expect(prepareTaskTraceOwner).toHaveBeenCalledWith(THREAD_ID)
    expect(historyMocks.detail).toHaveBeenCalledWith(THREAD_ID, expect.objectContaining({
      includeTaskTrace: true,
      suppressGlobalError: true,
    }))
    expect(result.current.workspace.conversations[0]?.taskTrace.phase).toBe('loading')

    await act(async () => response.resolve(refreshed))
    await waitFor(() => {
      expect(result.current.workspace.conversations[0]?.taskTrace).toEqual({
        phase: 'ready',
        snapshot: { status: 'ready', todoGroups: [] },
      })
    })
  })

  it('does not let a retry response replace an owned run started after the request', async () => {
    const response = deferred<ConversationHistoryDetail>()
    const initial = detail({
      taskTrace: {
        status: 'unavailable',
        todoGroups: [],
        errorCode: 'trace_incomplete',
      },
    })
    historyMocks.detail.mockReturnValue(response.promise)
    const { result } = renderHook(() => useHarness(initial))
    const ownedTaskTrace = readyTaskTrace('owned-new')

    act(() => result.current.history.retryTaskTrace(THREAD_ID))
    await waitFor(() => expect(historyMocks.detail).toHaveBeenCalledOnce())
    act(() => result.current.startOwnedRun(ownedTaskTrace))
    await waitFor(() => {
      expect(result.current.workspace.conversations[0]?.activeRunId).toBe('run-owned-new')
    })

    await act(async () => response.resolve(detail({
      status: { execution: 'succeeded', headRunId: RUN_ID },
      taskTrace: { status: 'ready', todoGroups: [] },
    })))

    const current = result.current.workspace.conversations[0]
    expect(current?.runStatus).toBe('streaming')
    expect(current?.activeRunId).toBe('run-owned-new')
    expect(current?.messages).toMatchObject([{
      id: 'message-owned-new',
      content: '本次新输入',
    }])
    expect(current?.taskTrace).toEqual(ownedTaskTrace)
  })

  it('does not let a retry response roll back a newer followed Trace', async () => {
    const response = deferred<ConversationHistoryDetail>()
    const initial = detail({
      taskTrace: {
        status: 'unavailable',
        todoGroups: [],
        errorCode: 'trace_incomplete',
      },
    })
    const followedTaskTrace = readyTaskTrace('followed-newer')
    const newer = detail({
      asOfSeq: 6,
      messages: [{ ...initial.messages[0]!, content: 'follow 推进后的内容' }],
      status: { execution: 'succeeded', headRunId: RUN_ID },
      taskTrace: followedTaskTrace.snapshot,
    })
    historyMocks.detail.mockReturnValue(response.promise)
    const { result } = renderHook(() => useHarness(initial))

    act(() => result.current.history.retryTaskTrace(THREAD_ID))
    await waitFor(() => expect(historyMocks.detail).toHaveBeenCalledOnce())
    act(() => result.current.advanceTrace(newer))
    await waitFor(() => {
      expect(result.current.workspace.conversations[0]?.trace?.asOfSeq).toBe(6)
    })

    await act(async () => response.resolve(detail({
      taskTrace: { status: 'ready', todoGroups: [] },
    })))

    const current = result.current.workspace.conversations[0]
    expect(current?.trace?.asOfSeq).toBe(6)
    expect(current?.messages[0]?.content).toBe('follow 推进后的内容')
    expect(current?.taskTrace).toEqual(followedTaskTrace)
  })

  it('does not let a retry response overwrite a newer delivery in the same Run', async () => {
    const response = deferred<ConversationHistoryDetail>()
    const initial = detail({
      taskTrace: {
        status: 'unavailable',
        todoGroups: [],
        errorCode: 'trace_incomplete',
      },
    })
    historyMocks.detail.mockReturnValue(response.promise)
    const { result } = renderHook(() => useHarness(initial))
    const deliveredTaskTrace = readyTaskTrace('delivered-newer')

    act(() => result.current.history.retryTaskTrace(THREAD_ID))
    await waitFor(() => expect(historyMocks.detail).toHaveBeenCalledOnce())
    act(() => result.current.advanceDelivery(deliveredTaskTrace))
    await waitFor(() => {
      expect(result.current.workspace.conversations[0]?.lastSeq).toBe(1)
    })

    await act(async () => response.resolve(detail({
      taskTrace: { status: 'ready', todoGroups: [] },
    })))

    const current = result.current.workspace.conversations[0]
    expect(current?.lastSeq).toBe(1)
    expect(current?.messages).toMatchObject([{
      id: 'message-delivered-new',
      content: '同一 Run 的新投递',
    }])
    expect(current?.taskTrace).toEqual(deliveredTaskTrace)
  })

  it('does not report a stale retry failure after an owned run is queued', async () => {
    const response = deferred<ConversationHistoryDetail>()
    const initial = detail({
      taskTrace: {
        status: 'unavailable',
        todoGroups: [],
        errorCode: 'trace_incomplete',
      },
    })
    const onToast = vi.fn()
    historyMocks.detail.mockReturnValue(response.promise)
    const { result } = renderHook(() => useHarness(initial, { onToast }))
    const ownedTaskTrace = readyTaskTrace('owned-after-failure')

    act(() => result.current.history.retryTaskTrace(THREAD_ID))
    await waitFor(() => expect(historyMocks.detail).toHaveBeenCalledOnce())
    await act(async () => {
      result.current.startOwnedRun(ownedTaskTrace)
      response.reject(new Error('旧请求失败'))
      await Promise.resolve()
    })

    await waitFor(() => {
      expect(result.current.workspace.conversations[0]?.activeRunId).toBe('run-owned-new')
    })
    expect(result.current.workspace.conversations[0]?.taskTrace).toEqual(ownedTaskTrace)
    expect(result.current.history.taskTraceLoadFailed).toBe(false)
    expect(onToast).not.toHaveBeenCalled()
  })

  it('keeps a current retry failure recoverable and reports it once', async () => {
    const response = deferred<ConversationHistoryDetail>()
    const initial = detail({
      taskTrace: {
        status: 'unavailable',
        todoGroups: [],
        errorCode: 'trace_incomplete',
      },
    })
    const onToast = vi.fn()
    historyMocks.detail.mockReturnValue(response.promise)
    const { result } = renderHook(() => useHarness(initial, { onToast }))

    act(() => result.current.history.retryTaskTrace(THREAD_ID))
    await waitFor(() => expect(historyMocks.detail).toHaveBeenCalledOnce())
    await act(async () => response.reject(new Error('当前请求失败')))

    await waitFor(() => expect(result.current.history.taskTraceLoadFailed).toBe(true))
    expect(result.current.workspace.conversations[0]?.taskTrace.phase).toBe('unloaded')
    expect(onToast).toHaveBeenCalledOnce()
  })

  it('does not apply a retry response after a same-batch thread switch', async () => {
    const response = deferred<ConversationHistoryDetail>()
    const initial = detail({
      taskTrace: {
        status: 'unavailable',
        todoGroups: [],
        errorCode: 'trace_incomplete',
      },
    })
    historyMocks.detail.mockReturnValue(response.promise)
    const { result } = renderHook(() => useHarness(initial))

    act(() => result.current.history.retryTaskTrace(THREAD_ID))
    await waitFor(() => expect(historyMocks.detail).toHaveBeenCalledOnce())
    await act(async () => {
      result.current.switchThread('thread-other')
      response.resolve(detail({
        taskTrace: { status: 'ready', todoGroups: [] },
      }))
      await Promise.resolve()
    })

    expect(result.current.workspace.currentThreadId).toBe('thread-other')
    expect(result.current.workspace.conversations[0]?.taskTrace.phase).toBe('loading')
  })

  it('does not apply or retain a retry failure after a same-batch thread switch', async () => {
    const response = deferred<ConversationHistoryDetail>()
    const initial = detail({
      taskTrace: {
        status: 'unavailable',
        todoGroups: [],
        errorCode: 'trace_incomplete',
      },
    })
    const onToast = vi.fn()
    historyMocks.detail.mockReturnValue(response.promise)
    const { result } = renderHook(() => useHarness(initial, { onToast }))

    act(() => result.current.history.retryTaskTrace(THREAD_ID))
    await waitFor(() => expect(historyMocks.detail).toHaveBeenCalledOnce())
    await act(async () => {
      result.current.switchThread('thread-other')
      response.reject(new Error('切换后的旧失败'))
      await Promise.resolve()
    })

    expect(result.current.workspace.currentThreadId).toBe('thread-other')
    expect(result.current.workspace.conversations[0]?.taskTrace.phase).toBe('loading')
    expect(result.current.history.taskTraceLoadFailed).toBe(false)
    expect(onToast).not.toHaveBeenCalled()
  })

  it('lets a newer externally owned page request replace an aborting locator request', async () => {
    const initial = detail()
    const olderPage = detail({
      historyCursor: null,
      messages: [{ ...initial.messages[0]!, id: 'message-older', content: '更早内容' }],
      taskTrace: null,
    })
    historyMocks.detail
      .mockImplementationOnce((
        _threadId: string,
        options: { signal?: AbortSignal },
      ) => new Promise<ConversationHistoryDetail>((_resolve, reject) => {
        options.signal?.addEventListener(
          'abort',
          () => reject(new DOMException('aborted', 'AbortError')),
          { once: true },
        )
      }))
      .mockResolvedValueOnce(olderPage)
    const { result } = renderHook(() => useHarness(initial))
    const firstController = new AbortController()
    const secondController = new AbortController()
    let first: Promise<boolean> = Promise.resolve(false)
    let second: Promise<boolean> = Promise.resolve(false)

    act(() => {
      first = result.current.history.loadOlderTrace(THREAD_ID, {
        signal: firstController.signal,
      })
    })
    await waitFor(() => expect(historyMocks.detail).toHaveBeenCalledTimes(1))
    firstController.abort()
    act(() => {
      second = result.current.history.loadOlderTrace(THREAD_ID, {
        signal: secondController.signal,
      })
    })

    let firstLoaded = true
    let secondLoaded = false
    await act(async () => {
      firstLoaded = await first
      secondLoaded = await second
    })
    expect(firstLoaded).toBe(false)
    expect(secondLoaded).toBe(true)
    expect(historyMocks.detail).toHaveBeenCalledTimes(2)
    expect(result.current.workspace.conversations[0]?.messages[0]?.content)
      .toBe('更早内容')
  })

  it('does not let a stale list summary roll back a hydrated Trace terminal', () => {
    const initial = detail()
    const terminal = detail({
      asOfSeq: 6,
      status: { execution: 'succeeded', headRunId: RUN_ID },
      updatedAt: '2026-08-28T00:00:10.000Z',
      lastModel: 'trace-model',
    })
    const current = {
      ...restoreConversationFromTrace(terminal, { model: 'fallback', includeTaskTrace: true }),
      isHydrated: true,
    }
    const stale = {
      ...historyItemFromDetail(initial),
      title: '列表新标题',
      pinned: true,
      status: 'running',
      lastRunId: 'run-stale',
      lastModel: 'stale-model',
      updatedAt: '2026-08-28T00:00:01.000Z',
    }

    const merged = mergeHistoryConversations([current], [stale], 'fallback')[0]

    expect(merged).toMatchObject({
      title: '列表新标题',
      pinned: true,
      runStatus: 'idle',
      model: 'trace-model',
      updatedAt: terminal.updatedAt,
    })
    expect(merged?.activeRunId).toBeUndefined()
    expect(merged?.trace?.asOfSeq).toBe(6)
  })

  it('accepts a newer waiting summary and requires fresh Trace hydration', () => {
    const terminal = detail({
      asOfSeq: 6,
      status: { execution: 'succeeded', headRunId: RUN_ID },
      updatedAt: '2026-08-28T00:00:10.000Z',
    })
    const current = {
      ...restoreConversationFromTrace(terminal, { model: 'fallback', includeTaskTrace: true }),
      isHydrated: true,
    }
    const waiting = {
      ...historyItemFromDetail(terminal),
      status: 'waiting_approval',
      pendingInteractionKind: 'plan_review' as const,
      hasPendingInterrupt: true,
      updatedAt: '2026-08-28T00:00:11.000Z',
    }

    const merged = mergeHistoryConversations([current], [waiting], 'fallback')[0]

    expect(merged).toMatchObject({
      runStatus: 'waiting_approval',
      pendingInteractionKind: 'plan_review',
      updatedAt: waiting.updatedAt,
      isHydrated: false,
    })
  })

  it('keeps multiple same-kind Tool groups visible in a synthesized summary', () => {
    const source = detail({
      interactions: [
        {
          id: 'interaction-a',
          traceSeq: 5,
          sourceId: 'interrupt-a',
          namespace: [],
          runId: RUN_ID,
          kind: 'tool_approval',
          toolCallIds: ['call-a'],
          status: 'pending',
          payloadOmitted: false,
          payload: {},
          openedAt: BASE_TIME,
        },
        {
          id: 'interaction-b',
          traceSeq: 6,
          sourceId: 'interrupt-b',
          namespace: ['tools:child'],
          runId: RUN_ID,
          kind: 'tool_approval',
          toolCallIds: ['call-b'],
          status: 'pending',
          payloadOmitted: false,
          payload: {},
          openedAt: BASE_TIME,
        },
      ],
    })

    const item = historyItemFromDetail(source)

    expect(item.hasPendingInterrupt).toBe(true)
    expect(item.pendingInteractionKind).toBe('tool_approval')
  })
})
