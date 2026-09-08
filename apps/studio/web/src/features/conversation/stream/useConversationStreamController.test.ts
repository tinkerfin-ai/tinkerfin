import { act, renderHook, waitFor } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { StreamedAgUiEvent } from '../../../api/conversation/client'
import { ApiError } from '../../../api/shared/http'
import { readActiveRunSession } from './activeRunSession'
import type {
  ConversationHistoryDetail,
  ConversationTraceEvent,
} from '../../../api/conversation/history'
import type { ChatRequestPayload } from '../../../api/conversation/types'
import { emptyTraceGraph, emptyTraceGraphDelta } from '../../../test/traceFixtures'
import type { Conversation, WorkspaceState } from '../../../types'
import { useConversationStreamController } from './useConversationStreamController'

const clientMocks = vi.hoisted(() => ({
  start: vi.fn(),
  resume: vi.fn(),
  cancel: vi.fn(),
}))
const traceMocks = vi.hoisted(() => ({
  detail: vi.fn(),
  follow: vi.fn(),
}))

vi.mock('../../../api/conversation/client', () => ({
  startConversationRun: clientMocks.start,
  resumeConversationRun: clientMocks.resume,
  cancelConversationRun: clientMocks.cancel,
}))

vi.mock(import('../../../api/conversation/history'), async (importOriginal) => ({
  ...await importOriginal(),
  fetchConversationHistoryDetail: traceMocks.detail,
  followConversationTrace: traceMocks.follow,
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

const traceDetail = (
  overrides: Partial<ConversationHistoryDetail> = {},
): ConversationHistoryDetail => ({
  titleSource: 'default',
  titleGenerationStatus: 'idle',
  titleSeq: 0,
  id: 1,
  threadId: THREAD_ID,
  title: 'Trace authority',
  lastModel: 'main',
  pinned: false,
  asOfSeq: 5,
  generation: 'generation-test',
  observedAt: '2026-09-05T00:00:00.000000Z',
  headRunId: RUN_ID,
  availableHeads: [RUN_ID],
  historyCursor: null,
  messageCount: 1,
  toolCallCount: 0,
  messages: [{
    id: 'message-authoritative',
    traceSeq: 1,
    sourceId: 'assistant-authoritative',
    namespace: [],
    runId: RUN_ID,
    role: 'assistant',
    content: 'Trace 最终内容',
    contentOmitted: false,
    status: 'completed',
    createdAt: BASE_TIME,
    completedAt: BASE_TIME,
  }],
  reasoning: [],
  state: { root: {}, subgraphs: {} },
  interactions: [],
  status: { execution: 'succeeded', headRunId: RUN_ID },
  completeness: { missingPrefix: false, missingTail: false, payloadOmitted: false },
  createdAt: BASE_TIME,
  updatedAt: BASE_TIME,
  ...overrides,
  graph: overrides.graph ?? emptyTraceGraph(overrides.asOfSeq ?? 5),
  taskTrace: overrides.taskTrace ?? { status: 'ready', todoGroups: [] },
})

const conversation = (overrides: Partial<Conversation> = {}): Conversation => ({
  threadId: THREAD_ID,
  title: 'Controller test',
  pinned: false,
  updatedAt: BASE_TIME,
  model: 'main',
  mode: 'default',
  messages: [],
  todos: [],
  runStatus: 'streaming',
  activeRunId: RUN_ID,
  serverState: {},
  lastSeq: 0,
  isHydrated: true,
  ...overrides,
  taskTrace: overrides.taskTrace ?? {
    phase: 'ready',
    snapshot: { status: 'ready', todoGroups: [] },
  },
})

async function* streamItems(
  items: StreamedAgUiEvent[],
): AsyncGenerator<StreamedAgUiEvent> {
  for (const item of items) yield item
}

async function* traceItems(
  items: ConversationTraceEvent[],
): AsyncGenerator<ConversationTraceEvent> {
  for (const item of items) yield item
}

function useControllerHarness(initialConversation: Conversation) {
  const [workspace, setWorkspace] = useState<WorkspaceState>({
    conversations: [initialConversation],
    currentThreadId: initialConversation.threadId,
  })
  const [draftConversation, setDraftConversation] = useState<Conversation | null>(null)
  const controller = useConversationStreamController({
    workspace,
    setWorkspace,
    setDraftConversation,
  })
  return { controller, workspace, draftConversation, setWorkspace }
}

describe('useConversationStreamController', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    window.localStorage.clear()
    window.sessionStorage.clear()
    traceMocks.detail.mockResolvedValue(traceDetail())
    traceMocks.follow.mockImplementation(() => traceItems([]))
    clientMocks.cancel.mockResolvedValue({ cancelled: true })
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('preserves an unconfirmed submission and retries the same run only on explicit recovery', async () => {
    clientMocks.start.mockImplementation(async function* () {
      throw new ApiError('network unavailable', { status: 0 })
      yield* streamItems([])
    })
    const { result } = renderHook(() => useControllerHarness(conversation()))
    await act(async () => { await result.current.controller.streamRun(THREAD_ID, payload, 'start') })
    expect(result.current.workspace.conversations[0]).toMatchObject({
      runStatus: 'detached', activeRunId: RUN_ID,
      notice: { kind: 'error', content: '连接已中断，尚无法确认任务状态，请恢复连接' },
    })
    expect(result.current.controller.hasActiveStream()).toBe(false)
    expect(readActiveRunSession(THREAD_ID)?.payload).toEqual(payload)
    await act(async () => { await result.current.controller.followDetachedConversation(THREAD_ID) })
    expect(traceMocks.follow).not.toHaveBeenCalled()
    expect(clientMocks.cancel).not.toHaveBeenCalled()
    expect(clientMocks.start).toHaveBeenCalledOnce()
    clientMocks.start.mockImplementation(() => streamItems([
      { event: { type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID }, seq: 1 },
      { event: { type: 'RUN_FINISHED', threadId: THREAD_ID, runId: RUN_ID, outcome: { type: 'success' } }, seq: 2 },
    ]))
    await act(async () => { await result.current.controller.recoverConversation(THREAD_ID) })
    expect(clientMocks.start).toHaveBeenLastCalledWith(payload, expect.any(AbortSignal), 0)
    expect(readActiveRunSession(THREAD_ID)).toBeNull()
    expect(clientMocks.cancel).not.toHaveBeenCalled()
  })

  it('limits repeated disconnections and retains the last applied cursor without cancelling the task', async () => {
    vi.useFakeTimers()
    let attempts = 0
    clientMocks.start.mockImplementation(async function* () {
      attempts += 1
      if (attempts === 1) yield {
        event: { type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID }, seq: 1,
      }
      throw new TypeError('stream interrupted')
    })
    traceMocks.follow.mockImplementation(async function* () { throw new TypeError('offline'); yield* traceItems([]) })
    const { result } = renderHook(() => useControllerHarness(conversation()))
    let running: Promise<void>
    await act(async () => {
      running = result.current.controller.streamRun(THREAD_ID, payload, 'start')
      await vi.advanceTimersByTimeAsync(20_000)
      await running
    })
    expect(attempts).toBe(4)
    expect(result.current.workspace.conversations[0]).toMatchObject({ runStatus: 'detached', activeRunId: RUN_ID, lastSeq: 1 })
    expect(readActiveRunSession(THREAD_ID)?.lastSeq).toBe(1)
    expect(clientMocks.cancel).not.toHaveBeenCalled()
    expect(result.current.controller.hasActiveStream()).toBe(false)
  })

  it('releases a reconnect wait on unmount and never requests backend cancellation', async () => {
    vi.useFakeTimers()
    clientMocks.start.mockImplementation(async function* () {
      yield { event: { type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID }, seq: 1 }
      throw new TypeError('stream interrupted')
    })
    const { result, unmount } = renderHook(() => useControllerHarness(conversation()))
    let running: Promise<void>
    await act(async () => {
      running = result.current.controller.streamRun(THREAD_ID, payload, 'start')
      await vi.advanceTimersByTimeAsync(1)
    })
    unmount()
    await running!
    await vi.advanceTimersByTimeAsync(10_000)
    expect(clientMocks.start).toHaveBeenCalledOnce()
    expect(clientMocks.cancel).not.toHaveBeenCalled()
    expect(vi.getTimerCount()).toBe(0)
  })

  it.each(['success', 'failure'] as const)('keeps a confirmed %s terminal when history fails and retries only history', async (outcome) => {
    const terminal: StreamedAgUiEvent = outcome === 'success'
      ? { event: { type: 'RUN_FINISHED', threadId: THREAD_ID, runId: RUN_ID, outcome: { type: 'success' } }, seq: 2 }
      : { event: { type: 'RUN_ERROR', code: 'runtime_initialization_error', message: 'Agent run failed', rawEvent: { runId: RUN_ID } }, seq: 2 }
    clientMocks.start.mockImplementation(() => streamItems([
      { event: { type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID }, seq: 1 }, terminal,
    ]))
    traceMocks.detail.mockRejectedValue(new ApiError('unavailable', { status: 503 }))
    const { result } = renderHook(() => useControllerHarness(conversation()))
    await act(async () => { await result.current.controller.streamRun(THREAD_ID, payload, 'start') })
    expect(result.current.workspace.conversations[0]).toMatchObject({
      runStatus: outcome === 'success' ? 'idle' : 'error', activeRunId: undefined,
      notice: { recovery: 'history' },
    })
    expect(readActiveRunSession(THREAD_ID)).toBeNull()
    traceMocks.detail.mockResolvedValue(traceDetail({
      status: { execution: outcome === 'success' ? 'succeeded' : 'failed', headRunId: RUN_ID },
    }))
    await act(async () => { await result.current.controller.recoverConversation(THREAD_ID) })
    expect(clientMocks.start).toHaveBeenCalledOnce()
    expect(clientMocks.cancel).not.toHaveBeenCalled()
    expect(result.current.workspace.conversations[0]?.notice).toBeUndefined()
    expect(result.current.workspace.conversations[0]?.runStatus).toBe(outcome === 'success' ? 'idle' : 'error')
    expect(readActiveRunSession(THREAD_ID)).toBeNull()
  })

  it('replaces the temporary AG-UI view with the terminal Trace snapshot', async () => {
    clientMocks.start.mockImplementation(() => streamItems([
      { seq: 1, event: { type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID } },
      {
        seq: 2,
        event: {
          type: 'TEXT_MESSAGE_START',
          messageId: 'temporary',
          role: 'assistant',
        },
      },
      {
        seq: 3,
        event: {
          type: 'TEXT_MESSAGE_CONTENT',
          messageId: 'temporary',
          delta: '临时内容',
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
    const { result } = renderHook(() => useControllerHarness(conversation()))

    await act(async () => {
      await result.current.controller.streamRun(THREAD_ID, payload, 'start')
    })

    await waitFor(() => {
      const current = result.current.workspace.conversations[0]
      expect(current?.messages.map((message) => message.content)).toEqual(['Trace 最终内容'])
      expect(current?.runStatus).toBe('idle')
      expect(current?.lastSeq).toBe(4)
      expect(current?.trace?.asOfSeq).toBe(5)
    })
    expect(traceMocks.detail).toHaveBeenCalledWith(THREAD_ID, {
      includeTaskTrace: true,
      signal: expect.any(AbortSignal),
      suppressGlobalError: true,
    })
  })

  it('accepts the first thread-wide sequence as the baseline when history has no cursor', async () => {
    clientMocks.resume.mockImplementation(() => streamItems([
      { seq: 185, event: { type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID } },
      {
        seq: 186,
        event: {
          type: 'RUN_FINISHED',
          threadId: THREAD_ID,
          runId: RUN_ID,
          outcome: { type: 'success' },
        },
      },
    ]))
    const { result } = renderHook(() => useControllerHarness(
      conversation({ lastSeq: undefined }),
    ))

    await act(async () => {
      await result.current.controller.streamRun(THREAD_ID, payload, 'resume')
    })

    expect(clientMocks.resume).toHaveBeenCalledOnce()
    expect(clientMocks.resume).toHaveBeenCalledWith(
      payload,
      expect.any(AbortSignal),
      undefined,
    )
    expect(result.current.workspace.conversations[0]?.lastSeq).toBe(186)
  })

  it('repairs a sequence gap by reconnecting Messaging from the last applied seq', async () => {
    const calls: Array<number | undefined> = []
    clientMocks.start.mockImplementation((
      _payload: ChatRequestPayload,
      _signal: AbortSignal,
      after?: number,
    ) => {
      calls.push(after)
      return calls.length === 1
        ? streamItems([{
            seq: 3,
            event: { type: 'STATE_SNAPSHOT', snapshot: { skipped: true } },
          }])
        : streamItems([
            { seq: 2, event: { type: 'STATE_SNAPSHOT', snapshot: { replayed: true } } },
            { seq: 3, event: { type: 'STATE_SNAPSHOT', snapshot: { replayed: true, next: true } } },
            {
              seq: 4,
              event: {
                type: 'RUN_FINISHED',
                threadId: THREAD_ID,
                runId: RUN_ID,
                outcome: { type: 'success' },
              },
            },
          ])
    })
    const { result } = renderHook(() => useControllerHarness(
      conversation({ lastSeq: 1 }),
    ))

    await act(async () => {
      await result.current.controller.streamRun(THREAD_ID, payload, 'start')
    })

    expect(calls).toEqual([undefined, 1])
    expect(traceMocks.detail).toHaveBeenCalledOnce()
  })

  it('hydrates and follows a detached run only through Trace events', async () => {
    const snapshot = traceDetail({
      status: { execution: 'running', headRunId: RUN_ID },
      messages: [],
      asOfSeq: 2,
    })
    traceMocks.detail.mockResolvedValue(traceDetail({
      asOfSeq: 3,
      messages: [{ ...traceDetail().messages[0]!, content: 'detached update' }],
    }))
    traceMocks.follow.mockImplementation(() => traceItems([
      { type: 'snapshot', snapshot },
      {
        type: 'update',
        taskTrace: null,
        update: {
          asOfSeq: 3,
          generation: 'generation-test',
          observedAt: '2026-09-05T00:00:00.000001Z',
          events: [],
          facts: [],
          messages: {
            upserts: [{
              ...traceDetail().messages[0]!,
              content: 'detached update',
            }],
            removes: [],
          },
          reasoning: { upserts: [], removes: [] },
          graph: emptyTraceGraphDelta(3),
          interactions: { upserts: [], removes: [] },
          state: { root: {}, subgraphs: {} },
          status: { execution: 'succeeded', headRunId: RUN_ID },
          completeness: { missingPrefix: false, missingTail: false, payloadOmitted: false },
          messageCount: 1,
          toolCallCount: 0,
          projections: {},
        },
      },
    ]))
    const { result } = renderHook(() => useControllerHarness(
      conversation({ runStatus: 'detached', trace: snapshot }),
    ))

    await act(async () => {
      await result.current.controller.followDetachedConversation(THREAD_ID)
    })

    await waitFor(() => {
      const current = result.current.workspace.conversations[0]
      expect(current?.messages[0]?.content).toBe('detached update')
      expect(current?.runStatus).toBe('idle')
      expect(current?.trace?.asOfSeq).toBe(3)
    })
    expect(clientMocks.start).not.toHaveBeenCalled()
  })

  it('refreshes authority and reconnects after a non-terminal Trace EOF', async () => {
    const initial = traceDetail({
      status: { execution: 'running', headRunId: RUN_ID },
      messages: [],
      asOfSeq: 2,
    })
    const refreshed = traceDetail({
      status: { execution: 'running', headRunId: RUN_ID },
      messages: [],
      asOfSeq: 3,
    })
    const terminal = traceDetail({
      asOfSeq: 4,
      messages: [{ ...traceDetail().messages[0]!, content: 'EOF 后终态' }],
    })
    let followCalls = 0
    traceMocks.follow.mockImplementation(() => {
      followCalls += 1
      return followCalls === 1
        ? traceItems([{ type: 'snapshot', snapshot: initial }])
        : traceItems([{
            type: 'update',
            taskTrace: null,
            update: {
              asOfSeq: 4,
              generation: 'generation-test',
              observedAt: '2026-09-05T00:00:00.000001Z',
              events: [],
              facts: [],
              messages: { upserts: terminal.messages, removes: [] },
              reasoning: { upserts: [], removes: [] },
              graph: emptyTraceGraphDelta(4),
              interactions: { upserts: [], removes: [] },
              state: terminal.state,
              status: terminal.status,
              completeness: terminal.completeness,
              messageCount: terminal.messageCount,
              toolCallCount: terminal.toolCallCount,
              projections: {},
            },
          }])
    })
    traceMocks.detail
      .mockResolvedValueOnce(refreshed)
      .mockResolvedValueOnce(terminal)
    const { result } = renderHook(() => useControllerHarness(
      conversation({ runStatus: 'detached', trace: initial }),
    ))

    await act(async () => {
      await result.current.controller.followDetachedConversation(THREAD_ID)
    })

    expect(traceMocks.follow).toHaveBeenCalledTimes(2)
    expect(traceMocks.detail).toHaveBeenCalledTimes(2)
    expect(result.current.workspace.conversations[0]?.trace?.asOfSeq).toBe(4)
    expect(result.current.workspace.conversations[0]?.messages[0]?.content).toBe('EOF 后终态')
    expect(result.current.workspace.conversations[0]?.runStatus).toBe('idle')
  })

  it('aborts an existing Trace follower before starting an owned AG-UI run', async () => {
    let traceAborted = false
    traceMocks.follow.mockImplementation(async function* (
      _threadId: string,
      options: { signal: AbortSignal },
    ) {
      await new Promise<void>((resolve) => {
        options.signal.addEventListener('abort', () => {
          traceAborted = true
          resolve()
        }, { once: true })
      })
      if (options.signal.aborted) yield { type: 'error', code: 'trace_unavailable' }
    })
    clientMocks.start.mockImplementation(() => streamItems([
      { seq: 1, event: { type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID } },
      {
        seq: 2,
        event: {
          type: 'RUN_FINISHED',
          threadId: THREAD_ID,
          runId: RUN_ID,
          outcome: { type: 'success' },
        },
      },
    ]))
    const { result } = renderHook(() => useControllerHarness(
      conversation({ runStatus: 'detached', trace: traceDetail() }),
    ))

    let follower: Promise<void> = Promise.resolve()
    await act(async () => {
      follower = result.current.controller.followDetachedConversation(THREAD_ID)
      await Promise.resolve()
      await result.current.controller.streamRun(THREAD_ID, payload, 'start')
    })
    await follower

    expect(traceAborted).toBe(true)
  })

  it('deduplicates backend cancellation while the owned stream is active', async () => {
    let release: (() => void) | undefined
    clientMocks.start.mockImplementation(async function* () {
      yield { seq: 1, event: { type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID } }
      await new Promise<void>((resolve) => {
        release = resolve
      })
      yield {
        seq: 2,
        event: {
          type: 'RUN_FINISHED',
          threadId: THREAD_ID,
          runId: RUN_ID,
          outcome: { type: 'success' },
        },
      }
    })
    const { result } = renderHook(() => useControllerHarness(conversation()))
    let streaming: Promise<void> = Promise.resolve()
    await act(async () => {
      streaming = result.current.controller.streamRun(THREAD_ID, payload, 'start')
      await Promise.resolve()
    })

    let first: Promise<boolean> = Promise.resolve(false)
    let second: Promise<boolean> = Promise.resolve(false)
    await act(async () => {
      first = result.current.controller.cancelRun(THREAD_ID)
      second = result.current.controller.cancelRun(THREAD_ID)
      await first
    })
    expect(first).toBe(second)
    expect(clientMocks.cancel).toHaveBeenCalledOnce()

    await act(async () => {
      release?.()
      await streaming
    })
  })

  it('clears the temporary live notice after a failed Trace becomes authoritative', async () => {
    traceMocks.detail.mockResolvedValue(traceDetail({
      status: { execution: 'failed', headRunId: RUN_ID },
      messages: [],
    }))
    clientMocks.start.mockImplementation(() => streamItems([
      { seq: 1, event: { type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID } },
      {
        seq: 2,
        event: {
          type: 'RUN_ERROR',
          rawEvent: { runId: RUN_ID },
          code: 'failed',
          message: 'temporary failure',
        },
      },
    ]))
    const { result } = renderHook(() => useControllerHarness(conversation()))

    await act(async () => {
      await result.current.controller.streamRun(THREAD_ID, payload, 'start')
    })

    await waitFor(() => {
      const current = result.current.workspace.conversations[0]
      expect(current?.messages.filter((message) => message.role === 'error')).toHaveLength(1)
      expect(current?.notice).toBeUndefined()
    })
  })
})


function eventFeed() {
  const pending: StreamedAgUiEvent[] = []
  let wake: (() => void) | undefined
  let seq = 0
  return {
    push(event: StreamedAgUiEvent['event']) {
      pending.push({ event, seq: ++seq })
      wake?.()
    },
    async *read(signal: AbortSignal) {
      const onAbort = () => wake?.()
      signal.addEventListener('abort', onAbort)
      try {
        while (!signal.aborted) {
          const next = pending.shift()
          if (next) {
            yield next
            if (next.event.type === 'RUN_FINISHED') return
          } else {
            await new Promise<void>((resolve) => { wake = resolve })
          }
        }
      } finally {
        signal.removeEventListener('abort', onAbort)
      }
    },
  }
}

it('切换与启动 B 保留 A 的原连接，停止 B 不影响 A', async () => {
  const a = eventFeed()
  const b = eventFeed()
  const signals = new Map<string, AbortSignal>()
  clientMocks.start.mockImplementation((request: ChatRequestPayload, signal: AbortSignal) => {
    signals.set(request.threadId, signal)
    return (request.threadId === THREAD_ID ? a : b).read(signal)
  })
  clientMocks.cancel.mockResolvedValue({ cancelled: true })
  traceMocks.detail.mockImplementation((threadId: string) => Promise.resolve(traceDetail({
    threadId, headRunId: threadId === THREAD_ID ? RUN_ID : 'run-b',
  })))
  const { result } = renderHook(() => useControllerHarness(conversation()))
  let runA: Promise<void> = Promise.resolve()
  let runB: Promise<void> = Promise.resolve()
  await act(async () => { runA = result.current.controller.streamRun(THREAD_ID, payload, 'start') })
  await act(async () => {
    a.push({ type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID })
    result.current.setWorkspace((state) => ({ ...state, currentThreadId: 'thread-b', conversations: [...state.conversations, conversation({ threadId: 'thread-b', activeRunId: 'run-b' })] }))
    runB = result.current.controller.streamRun('thread-b', { ...payload, threadId: 'thread-b', runId: 'run-b' }, 'start')
  })
  await waitFor(() => expect(signals.size).toBe(2))
  await act(async () => {
    b.push({ type: 'RUN_STARTED', threadId: 'thread-b', runId: 'run-b' })
    a.push({ type: 'TEXT_MESSAGE_START', messageId: 'a-text', role: 'assistant' })
    a.push({ type: 'TEXT_MESSAGE_CONTENT', messageId: 'a-text', delta: 'A仍在输出' })
    b.push({ type: 'TEXT_MESSAGE_START', messageId: 'b-text', role: 'assistant' })
    b.push({ type: 'TEXT_MESSAGE_CONTENT', messageId: 'b-text', delta: 'B独立输出' })
  })
  await waitFor(() => expect(result.current.workspace.conversations.find((item) => item.threadId === THREAD_ID)?.messages.some((item) => item.content === 'A仍在输出')).toBe(true))
  expect(result.current.workspace.currentThreadId).toBe('thread-b')
  expect(signals.get(THREAD_ID)?.aborted).toBe(false)
  expect(result.current.controller.isActiveThread(THREAD_ID)).toBe(true)
  await act(async () => { await result.current.controller.cancelRun('thread-b') })
  expect(clientMocks.cancel).toHaveBeenCalledWith('thread-b', 'run-b')
  expect(signals.get(THREAD_ID)?.aborted).toBe(false)
  await act(async () => {
    b.push({ type: 'RUN_FINISHED', threadId: 'thread-b', runId: 'run-b' })
    await runB
  })
  expect(result.current.controller.isActiveThread(THREAD_ID)).toBe(true)
  await act(async () => {
    a.push({ type: 'RUN_FINISHED', threadId: THREAD_ID, runId: RUN_ID })
    await runA
  })
  expect(result.current.controller.hasActiveStream()).toBe(false)
})

it('新草稿首帧晚到只登记原会话，不抢回当前页面', async () => {
  const feed = eventFeed()
  clientMocks.start.mockImplementation((_request: ChatRequestPayload, signal: AbortSignal) => feed.read(signal))
  traceMocks.detail.mockResolvedValue(traceDetail({ threadId: 'server-draft' }))
  const { result } = renderHook(() => useControllerHarness(conversation()))
  let running: Promise<void> = Promise.resolve()
  await act(async () => {
    running = result.current.controller.streamRun('', { ...payload, threadId: '' }, 'start', {
      target: 'draft', initialConversation: conversation({ threadId: '' }),
    })
  })
  act(() => result.current.controller.releaseDraft())
  await act(async () => { feed.push({ type: 'RUN_STARTED', threadId: 'server-draft', runId: RUN_ID }) })
  await waitFor(() => expect(result.current.workspace.conversations.some((item) => item.threadId === 'server-draft')).toBe(true))
  expect(result.current.workspace.currentThreadId).toBe(THREAD_ID)
  await act(async () => {
    feed.push({ type: 'RUN_FINISHED', threadId: 'server-draft', runId: RUN_ID })
    await running
  })
})

it.each(['success', 'failure'])('旧Run历史收尾不影响新Run：%s', async (outcome) => {
  const first = eventFeed()
  const second = eventFeed()
  let rejectHistory: (error: Error) => void = () => undefined
  let resolveHistory!: (detail: ConversationHistoryDetail) => void
  const history = new Promise<ConversationHistoryDetail>((resolve, reject) => { resolveHistory = resolve; rejectHistory = reject })
  traceMocks.detail.mockReset()
  traceMocks.detail.mockImplementationOnce(() => history).mockResolvedValue(traceDetail())
  clientMocks.start.mockImplementation((request: ChatRequestPayload, signal: AbortSignal) => {
    if (request.runId === RUN_ID) return first.read(signal)
    return (async function* () {
      for await (const item of second.read(signal)) yield { ...item, seq: (item.seq ?? 0) + 2 }
    })()
  })
  const { result, unmount } = renderHook(() => useControllerHarness(conversation()))
  let oldRun!: Promise<void>
  let newRun!: Promise<void>
  try {
    await act(async () => { oldRun = result.current.controller.streamRun(THREAD_ID, payload, 'start') })
    await act(async () => { first.push({ type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID }) })
    await act(async () => { first.push({ type: 'RUN_FINISHED', threadId: THREAD_ID, runId: RUN_ID }) })
    await waitFor(() => expect(traceMocks.detail).toHaveBeenCalledOnce())
    await act(async () => { newRun = result.current.controller.streamRun(THREAD_ID, { ...payload, runId: 'new-run' }, 'start') })
    await act(async () => { second.push({ type: 'RUN_STARTED', threadId: THREAD_ID, runId: 'new-run' }) })
    expect(result.current.workspace.conversations[0]).toMatchObject({ activeRunId: 'new-run', runStatus: 'streaming' })
    await act(async () => { if (outcome === 'failure') rejectHistory(new Error('历史读取失败')); else resolveHistory(traceDetail()); await oldRun })
    expect(result.current.workspace.conversations[0]?.notice).toBeUndefined()
    expect(result.current.controller.isActiveThread(THREAD_ID)).toBe(true)
    expect(result.current.workspace.conversations[0]).toMatchObject({ activeRunId: 'new-run', runStatus: 'streaming' })
  } finally {
    resolveHistory(traceDetail())
    unmount()
    if (oldRun) await oldRun
    if (newRun) await newRun
  }
})

it('旧Run历史失败保留新Run首响应丢失的恢复状态', async () => {
  const first = eventFeed()
  let rejectHistory!: (error: Error) => void
  const history = new Promise<ConversationHistoryDetail>((_resolve, reject) => { rejectHistory = reject })
  traceMocks.detail.mockReset()
  traceMocks.detail.mockImplementationOnce(() => history).mockResolvedValue(traceDetail())
  traceMocks.follow.mockReset()
  traceMocks.follow.mockImplementation(() => traceItems([]))
  clientMocks.start.mockImplementation((request: ChatRequestPayload, signal: AbortSignal) => {
    if (request.runId === RUN_ID) return first.read(signal)
    return (async function* () {
      yield* streamItems([])
      throw new ApiError('network unavailable', { status: 0 })
    })()
  })
  const { result, unmount } = renderHook(() => useControllerHarness(conversation()))
  let oldRun!: Promise<void>
  try {
    await act(async () => { oldRun = result.current.controller.streamRun(THREAD_ID, payload, 'start') })
    await act(async () => { first.push({ type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID }) })
    await act(async () => { first.push({ type: 'RUN_FINISHED', threadId: THREAD_ID, runId: RUN_ID }) })
    await waitFor(() => expect(traceMocks.detail).toHaveBeenCalledOnce())
    act(() => result.current.setWorkspace((state) => ({ ...state, conversations: state.conversations.map((item) => ({ ...item, activeRunId: 'new-run', runStatus: 'streaming' })) })))
    await act(async () => { await result.current.controller.streamRun(THREAD_ID, { ...payload, runId: 'new-run' }, 'start') })
    expect(result.current.workspace.conversations[0]).toMatchObject({ activeRunId: 'new-run', runStatus: 'detached' })
    await act(async () => { rejectHistory(new Error('old history failed')); await oldRun })
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 200)) })
    expect(result.current.workspace.conversations[0]).toMatchObject({ activeRunId: 'new-run', runStatus: 'detached' })
    expect(traceMocks.follow).not.toHaveBeenCalled()
  } finally {
    rejectHistory(new Error('cleanup'))
    unmount()
    if (oldRun) await oldRun
  }
})
