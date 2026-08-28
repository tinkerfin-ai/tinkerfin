import { act, renderHook, waitFor } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { StreamedAgUiEvent } from '../../../api/conversation/client'
import type {
  ConversationHistoryDetail,
  ConversationTraceEvent,
} from '../../../api/conversation/history'
import type { ChatRequestPayload } from '../../../api/conversation/types'
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

vi.mock('../../../api/conversation/history', () => ({
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
  id: 1,
  threadId: THREAD_ID,
  title: 'Trace authority',
  lastModel: 'main',
  runtimeProfile: 'deepagents-v2',
  pinned: false,
  asOfSeq: 5,
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
  nodes: [],
  state: { root: {}, subgraphs: {} },
  interactions: [],
  status: { execution: 'succeeded', headRunId: RUN_ID },
  completeness: { missingPrefix: false, missingTail: false, payloadOmitted: false },
  createdAt: BASE_TIME,
  updatedAt: BASE_TIME,
  ...overrides,
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
  return { controller, workspace, draftConversation }
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
      expect(current?.trace?.asOfSeq).toBe(5)
    })
    expect(traceMocks.detail).toHaveBeenCalledWith(THREAD_ID, {
      signal: expect.any(AbortSignal),
      suppressGlobalError: true,
    })
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
        update: {
          asOfSeq: 3,
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
          nodes: { upserts: [], removes: [] },
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
            update: {
              asOfSeq: 4,
              events: [],
              facts: [],
              messages: { upserts: terminal.messages, removes: [] },
              reasoning: { upserts: [], removes: [] },
              nodes: { upserts: [], removes: [] },
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
      signal: AbortSignal,
    ) {
      await new Promise<void>((resolve) => {
        signal.addEventListener('abort', () => {
          traceAborted = true
          resolve()
        }, { once: true })
      })
      if (signal.aborted) yield { type: 'error', code: 'trace_unavailable' }
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
      first = result.current.controller.cancelActiveRun()
      second = result.current.controller.cancelActiveRun()
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
