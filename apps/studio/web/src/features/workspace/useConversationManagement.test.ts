import { act, renderHook, waitFor } from '@testing-library/react'
import { useRef, useState } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { ConversationHistoryListItem } from '../../api/conversation/history'
import { buildEmptyConversation } from '../../lib/workspace'
import type { WorkspaceState } from '../../types'
import { useConversationManagement } from './useConversationManagement'

const historyMocks = vi.hoisted(() => ({
  patch: vi.fn(),
  remove: vi.fn(),
}))

vi.mock('../../api/conversation/history', () => ({
  patchConversation: historyMocks.patch,
  deleteConversation: historyMocks.remove,
}))

function deferred<T>() {
  let resolve: (value: T) => void = () => undefined
  let reject: (reason?: unknown) => void = () => undefined
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

const summary = (pinned: boolean): ConversationHistoryListItem => ({
  id: 1,
  threadId: 'thread-pin',
  title: '置顶会话',
  status: 'idle',
  lastRunId: 'run-pin',
  lastModel: 'GPT-5.5',
  lastSeq: 0,
  messageCount: 0,
  toolCallCount: 0,
  hasPendingInterrupt: false,
  pendingInteractionKind: null,
  pinned,
  createdAt: '2026-08-25T00:00:00.000Z',
  updatedAt: '2026-08-25T00:00:00.000Z',
})

function useHarness() {
  const conversation = buildEmptyConversation({
    threadId: 'thread-pin',
    now: '2026-08-25T00:00:00.000Z',
    model: 'GPT-5.5',
  })
  const [workspace, setWorkspace] = useState<WorkspaceState>({
    conversations: [conversation],
    currentThreadId: conversation.threadId,
  })
  const onToast = useRef(vi.fn()).current
  const management = useConversationManagement({
    workspace,
    conversation: workspace.conversations[0] ?? conversation,
    setWorkspace,
    setDraft: vi.fn(),
    setDraftConversation: vi.fn(),
    setDraftModel: vi.fn(),
    catchUpDetachedConversation: vi.fn(async () => undefined),
    abandonPlanInteraction: vi.fn(),
    cancelActiveRun: vi.fn(async () => false),
    detachThreadStream: vi.fn(),
    getActiveThreadId: vi.fn(() => null),
    hasActiveStream: vi.fn(() => false),
    isActiveThread: vi.fn(() => false),
    onToast,
    onConversationBoundary: vi.fn(),
  })
  return { management, workspace, onToast }
}

describe('useConversationManagement pin ownership', () => {
  beforeEach(() => vi.clearAllMocks())

  it('deduplicates a pending mutation and applies the authoritative response', async () => {
    const response = deferred<ConversationHistoryListItem>()
    historyMocks.patch.mockReturnValueOnce(response.promise)
    const { result } = renderHook(useHarness)

    act(() => {
      result.current.management.pinConversation('thread-pin')
      result.current.management.pinConversation('thread-pin')
    })

    expect(historyMocks.patch).toHaveBeenCalledOnce()
    expect(result.current.workspace.conversations[0]?.pinned).toBe(true)
    expect(result.current.management.pinPendingThreadIds.has('thread-pin')).toBe(true)

    await act(async () => {
      response.resolve(summary(false))
      await response.promise
    })

    await waitFor(() => {
      expect(result.current.workspace.conversations[0]?.pinned).toBe(false)
      expect(result.current.management.pinPendingThreadIds.has('thread-pin')).toBe(false)
    })
    expect(result.current.onToast).not.toHaveBeenCalled()
  })

  it('rolls back only the optimistic value owned by the failed request', async () => {
    const response = deferred<ConversationHistoryListItem>()
    historyMocks.patch.mockReturnValueOnce(response.promise)
    const { result } = renderHook(useHarness)
    act(() => result.current.management.pinConversation('thread-pin'))
    expect(result.current.workspace.conversations[0]?.pinned).toBe(true)

    await act(async () => {
      response.reject(new Error('patch failed'))
      await response.promise.catch(() => undefined)
    })

    await waitFor(() => expect(result.current.workspace.conversations[0]?.pinned).toBe(false))
    expect(result.current.onToast).toHaveBeenCalledOnce()
    expect(result.current.onToast).toHaveBeenCalledWith('error', '置顶状态更新失败，请重试')
  })
})
