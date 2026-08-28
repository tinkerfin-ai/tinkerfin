import { act, renderHook, waitFor } from '@testing-library/react'
import { useState } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { ConversationHistoryDetail } from '../../api/conversation/history'
import { upsertConversation } from '../../lib/workspace'
import type { WorkspaceState } from '../../types'
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

const detail = (
  overrides: Partial<ConversationHistoryDetail> = {},
): ConversationHistoryDetail => ({
  id: 1,
  threadId: THREAD_ID,
  title: '分页竞态',
  lastModel: 'main',
  runtimeProfile: 'deepagents-v2',
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
  nodes: [],
  state: { root: {}, subgraphs: {} },
  interactions: [],
  status: { execution: 'running', headRunId: RUN_ID },
  completeness: { missingPrefix: false, missingTail: false, payloadOmitted: false },
  createdAt: BASE_TIME,
  updatedAt: BASE_TIME,
  ...overrides,
})

function deferred<T>() {
  let resolve: (value: T) => void = () => undefined
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise
  })
  return { promise, resolve }
}

function useHarness(initial: ConversationHistoryDetail) {
  const [workspace, setWorkspace] = useState<WorkspaceState>({
    conversations: [{
      ...restoreConversationFromTrace(initial, { model: 'main' }),
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
    onToast: vi.fn(),
  })
  const advanceTrace = (next: ConversationHistoryDetail) => {
    setWorkspace((state) => upsertConversation(
      state,
      { ...restoreConversationFromTrace(next, { model: 'main' }), isHydrated: true },
    ))
  }
  const startOwnedRun = () => {
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
            }
          : conversation
      )),
    }))
  }
  return { advanceTrace, history, startOwnedRun, workspace }
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

  it('does not let a stale list summary roll back a hydrated Trace terminal', () => {
    const initial = detail()
    const terminal = detail({
      asOfSeq: 6,
      status: { execution: 'succeeded', headRunId: RUN_ID },
      updatedAt: '2026-08-28T00:00:10.000Z',
      lastModel: 'trace-model',
    })
    const current = {
      ...restoreConversationFromTrace(terminal, { model: 'fallback' }),
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
