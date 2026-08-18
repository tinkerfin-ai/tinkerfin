import {
  useCallback,
  useEffect,
  useRef,
  type Dispatch,
  type SetStateAction,
} from 'react'

import {
  cancelConversationRun,
  resumeConversationRun,
  startConversationRun,
} from '../../../api/conversation/client'
import {
  fetchConversationEvents,
  type ConversationEventEnvelope,
} from '../../../api/conversation/history'
import type { ChatRequestPayload } from '../../../api/conversation/types'
import type { Conversation, WorkspaceState } from '../../../types'
import {
  applyConversationEvent,
  applyHistoryEventEnvelope,
  applyLiveEventEnvelope,
  markConversationDetached,
} from '../agui'
import { updateConversation, upsertConversation } from '../../../lib/workspace'
import {
  clearActiveRunSession,
  writeActiveRunSession,
} from './activeRunSession'

const HISTORY_CATCH_UP_PAGE_SIZE = 1000
const RECONNECT_MAX_DELAY_MS = 5000

const waitForReconnect = (delay: number, signal: AbortSignal): Promise<void> => (
  new Promise((resolve) => {
    if (signal.aborted) {
      resolve()
      return
    }
    const timer = window.setTimeout(() => {
      signal.removeEventListener('abort', onAbort)
      resolve()
    }, delay)
    const onAbort = () => {
      window.clearTimeout(timer)
      resolve()
    }
    signal.addEventListener('abort', onAbort, { once: true })
  })
)

export interface StreamRunOptions {
  target: 'draft' | 'workspace'
  initialConversation?: Conversation
  initialAfterSeq?: number
}

interface ConversationStreamControllerOptions {
  workspace: WorkspaceState
  setWorkspace: Dispatch<SetStateAction<WorkspaceState>>
  setDraftConversation: Dispatch<SetStateAction<Conversation | null>>
}

export interface ConversationStreamController {
  streamRun: (
    threadId: string,
    payload: ChatRequestPayload,
    mode: 'start' | 'resume',
    options?: StreamRunOptions,
  ) => Promise<void>
  catchUpDetachedConversation: (threadId: string) => Promise<void>
  detachThreadStream: (threadId: string, reason: string) => void
  cancelActiveRun: () => Promise<boolean>
  hasActiveStream: () => boolean
  getActiveThreadId: () => string | null
  isActiveThread: (threadId: string) => boolean
}

/**
 * 管理会话实时连接和持久化事件序号状态
 *
 * 页面提供 React 状态边界；控制器负责流取消、epoch、权威线程切换、
 * 序号去重、缺口恢复、断连回放和动画帧合并
 */
export function useConversationStreamController({
  workspace,
  setWorkspace,
  setDraftConversation,
}: ConversationStreamControllerOptions): ConversationStreamController {
  const activeAbortController = useRef<AbortController | null>(null)
  const activeThreadId = useRef<string | null>(null)
  const activeRunId = useRef<string | null>(null)
  const activeStreamEpoch = useRef(0)
  const workspaceFrame = useRef<number | null>(null)
  const workspaceFrameUpdates = useRef<Array<{
    epoch: number
    update: (state: WorkspaceState) => WorkspaceState
  }>>([])
  const draftFrame = useRef<number | null>(null)
  const pendingDraftFrameValue = useRef<{
    epoch: number
    value: Conversation | null
  } | undefined>(undefined)
  const latestWorkspace = useRef(workspace)
  const catchUpRequests = useRef(new Set<string>())
  const catchUpControllers = useRef(new Map<string, AbortController>())
  const delayedCatchUpTimers = useRef(new Set<number>())
  const isMounted = useRef(true)
  latestWorkspace.current = workspace

  const enqueueWorkspaceUpdate = useCallback((
    epoch: number,
    updater: (state: WorkspaceState) => WorkspaceState,
  ) => {
    workspaceFrameUpdates.current.push({ epoch, update: updater })
    if (workspaceFrame.current != null) return
    workspaceFrame.current = window.requestAnimationFrame(() => {
      workspaceFrame.current = null
      const currentEpoch = activeStreamEpoch.current
      const updates = workspaceFrameUpdates.current
        .splice(0)
        .filter((entry) => entry.epoch === currentEpoch)
      if (updates.length === 0) return
      setWorkspace((state) => updates.reduce((next, entry) => entry.update(next), state))
    })
  }, [setWorkspace])

  const enqueueDraftUpdate = useCallback((epoch: number, value: Conversation | null) => {
    pendingDraftFrameValue.current = { epoch, value }
    if (draftFrame.current != null) return
    draftFrame.current = window.requestAnimationFrame(() => {
      draftFrame.current = null
      const pending = pendingDraftFrameValue.current
      pendingDraftFrameValue.current = undefined
      if (pending?.epoch === activeStreamEpoch.current) {
        setDraftConversation(pending.value)
      }
    })
  }, [setDraftConversation])

  const detachThreadStream = useCallback((threadId: string, reason: string) => {
    if (
      activeAbortController.current == null
      || activeThreadId.current !== threadId
    ) return
    const detachedEpoch = activeStreamEpoch.current
    activeStreamEpoch.current += 1
    activeAbortController.current?.abort()
    activeAbortController.current = null
    activeThreadId.current = null
    activeRunId.current = null

    if (workspaceFrame.current != null) {
      window.cancelAnimationFrame(workspaceFrame.current)
      workspaceFrame.current = null
    }
    const queuedWorkspaceUpdates = workspaceFrameUpdates.current
      .splice(0)
      .filter((entry) => entry.epoch === detachedEpoch)
    setWorkspace((state) => {
      const flushed = queuedWorkspaceUpdates.reduce(
        (next, entry) => entry.update(next),
        state,
      )
      return updateConversation(
        flushed,
        threadId,
        (item) => markConversationDetached(item, reason),
      )
    })

    if (draftFrame.current != null) {
      window.cancelAnimationFrame(draftFrame.current)
      draftFrame.current = null
    }
    const queuedDraft = pendingDraftFrameValue.current?.epoch === detachedEpoch
      ? pendingDraftFrameValue.current.value
      : undefined
    pendingDraftFrameValue.current = undefined
    setDraftConversation((current) => {
      if (current && current.threadId !== threadId) return current
      if (queuedDraft === null) return null
      const candidate = queuedDraft ?? current
      return candidate?.threadId === threadId
        ? markConversationDetached(candidate, reason)
        : candidate
    })
  }, [setDraftConversation, setWorkspace])

  const catchUpDetachedConversation = useCallback(async (threadId: string) => {
    if (!isMounted.current) return
    const target = latestWorkspace.current.conversations.find(
      (item) => item.threadId === threadId,
    )
    if (!target || target.lastSeq == null) return
    if (target.runStatus !== 'detached' && target.runStatus !== 'idle') return
    if (catchUpRequests.current.has(threadId)) return
    const controller = new AbortController()
    catchUpRequests.current.add(threadId)
    catchUpControllers.current.set(threadId, controller)
    try {
      let afterSeq = target.lastSeq
      while (true) {
        if (
          controller.signal.aborted
          || !isMounted.current
          || (activeAbortController.current && activeThreadId.current === threadId)
        ) return
        const envelopes = await fetchConversationEvents(threadId, {
          afterSeq,
          limit: HISTORY_CATCH_UP_PAGE_SIZE,
          signal: controller.signal,
          suppressGlobalError: true,
        })
        if (
          controller.signal.aborted
          || !isMounted.current
          || (activeAbortController.current && activeThreadId.current === threadId)
        ) return
        if (!envelopes.length) return
        const nextAfterSeq = envelopes.at(-1)?.seq ?? afterSeq
        if (nextAfterSeq <= afterSeq) return
        setWorkspace((state) => {
          if (
            controller.signal.aborted
            || !isMounted.current
            || (activeAbortController.current && activeThreadId.current === threadId)
          ) {
            return state
          }
          return updateConversation(
            state,
            threadId,
            (item) => envelopes.reduce(applyHistoryEventEnvelope, item),
          )
        })
        afterSeq = nextAfterSeq
        if (envelopes.length < HISTORY_CATCH_UP_PAGE_SIZE) return
      }
    } catch (error) {
      if (
        controller.signal.aborted
        || !isMounted.current
        || (activeAbortController.current && activeThreadId.current === threadId)
      ) return
      const message = error instanceof Error ? error.message : '历史事件补拉失败'
      setWorkspace((state) => {
        if (
          controller.signal.aborted
          || !isMounted.current
          || (activeAbortController.current && activeThreadId.current === threadId)
        ) return state
        return updateConversation(state, threadId, (item) => ({
          ...item,
          notice: {
            kind: 'error',
            content: `事件恢复失败：${message}`,
          },
        }))
      })
    } finally {
      if (catchUpControllers.current.get(threadId) === controller) {
        catchUpControllers.current.delete(threadId)
        catchUpRequests.current.delete(threadId)
      }
    }
  }, [setWorkspace])

  const streamRun = useCallback(async (
    threadIdToStream: string,
    payload: ChatRequestPayload,
    mode: 'start' | 'resume',
    options: StreamRunOptions = { target: 'workspace' },
  ) => {
    activeAbortController.current?.abort()
    const staleCatchUpController = catchUpControllers.current.get(threadIdToStream)
    if (staleCatchUpController) {
      staleCatchUpController.abort()
      if (catchUpControllers.current.get(threadIdToStream) === staleCatchUpController) {
        catchUpControllers.current.delete(threadIdToStream)
        catchUpRequests.current.delete(threadIdToStream)
      }
    }
    const streamEpoch = activeStreamEpoch.current + 1
    activeStreamEpoch.current = streamEpoch
    const controller = new AbortController()
    activeAbortController.current = controller
    activeThreadId.current = threadIdToStream
    activeRunId.current = payload.runId

    const stream = mode === 'resume' ? resumeConversationRun : startConversationRun
    let target = options.target
    let targetThreadId = threadIdToStream
    let draftTarget = options.initialConversation
    let receivedEvent = false
    let mainTerminalReceived = false
    let requestPayload: ChatRequestPayload = { ...payload }
    let reconnectAttempt = 0
    let lastAppliedSeq = (
      options.initialAfterSeq
      ?? (target === 'draft'
        ? draftTarget?.lastSeq
        : latestWorkspace.current.conversations.find(
            (item) => item.threadId === targetThreadId,
          )?.lastSeq)
    ) ?? 0
    const persistActiveRun = () => writeActiveRunSession({
      threadId: targetThreadId,
      payload: requestPayload,
      mode,
      lastSeq: lastAppliedSeq,
    })
    persistActiveRun()

    const fetchMissingEvents = async (
      threadId: string,
      currentSeq: number,
    ): Promise<ConversationEventEnvelope[]> => {
      const missing: ConversationEventEnvelope[] = []
      let cursor = lastAppliedSeq
      while (cursor + 1 < currentSeq) {
        const envelopes = await fetchConversationEvents(threadId, {
          afterSeq: cursor,
          limit: Math.min(HISTORY_CATCH_UP_PAGE_SIZE, currentSeq - cursor),
          signal: controller.signal,
          suppressGlobalError: true,
        })
        if (!envelopes.length) {
          throw new Error(
            `无法补齐会话事件: expected=${cursor + 1}, actual=${currentSeq}`,
          )
        }
        let progressed = false
        for (const envelope of envelopes) {
          if (envelope.seq <= cursor) continue
          if (envelope.seq !== cursor + 1) {
            throw new Error(
              `会话事件序号不连续: expected=${cursor + 1}, actual=${envelope.seq}`,
            )
          }
          if (envelope.seq >= currentSeq) break
          missing.push(envelope)
          cursor = envelope.seq
          progressed = true
        }
        if (!progressed && cursor + 1 < currentSeq) {
          throw new Error(
            `无法补齐会话事件: expected=${cursor + 1}, actual=${currentSeq}`,
          )
        }
      }
      return missing
    }

    try {
      while (!mainTerminalReceived) {
        try {
          for await (const { event, seq } of stream(
            requestPayload,
            controller.signal,
            reconnectAttempt === 0 ? options.initialAfterSeq : lastAppliedSeq,
          )) {
        receivedEvent = true
        const reportedThreadId: string = 'threadId' in event && typeof event.threadId === 'string'
          ? event.threadId
          : (activeThreadId.current ?? targetThreadId)
        activeThreadId.current = reportedThreadId
        targetThreadId = reportedThreadId
        if (requestPayload.threadId !== reportedThreadId) {
          requestPayload = { ...requestPayload, threadId: reportedThreadId }
        }

        if (seq != null && seq <= lastAppliedSeq) continue
        const missingEnvelopes = seq != null && seq > lastAppliedSeq + 1
          ? await fetchMissingEvents(reportedThreadId, seq)
          : []
        if (missingEnvelopes.length > 0) {
          lastAppliedSeq = missingEnvelopes.at(-1)?.seq ?? lastAppliedSeq
        }
        if (seq != null && seq !== lastAppliedSeq + 1) {
          throw new Error(
            `会话事件序号不连续: expected=${lastAppliedSeq + 1}, actual=${seq}`,
          )
        }

        if (target === 'draft' && draftTarget) {
          const caughtUpDraft = missingEnvelopes.reduce(
            applyLiveEventEnvelope,
            draftTarget,
          )
          const withEvent = applyConversationEvent(caughtUpDraft, event)
          const nextDraft = seq == null
            ? withEvent
            : { ...withEvent, lastSeq: seq, isHydrated: true }
          draftTarget = nextDraft
          if (seq != null) lastAppliedSeq = seq

          if (event.type === 'RUN_STARTED') {
            const candidateConversation = {
              ...nextDraft,
              threadId: reportedThreadId,
              isHydrated: true,
            }
            enqueueWorkspaceUpdate(streamEpoch, (state) => ({
              ...upsertConversation(state, candidateConversation),
              currentThreadId: reportedThreadId,
            }))
            enqueueDraftUpdate(streamEpoch, null)
            draftTarget = undefined
            target = 'workspace'
            targetThreadId = reportedThreadId
          } else {
            enqueueDraftUpdate(streamEpoch, nextDraft)
            if (event.type === 'RUN_FINISHED' || event.type === 'RUN_ERROR') {
              enqueueWorkspaceUpdate(
                streamEpoch,
                (state) => upsertConversation(state, nextDraft),
              )
              enqueueDraftUpdate(streamEpoch, null)
            }
          }
          persistActiveRun()
          continue
        }

        enqueueWorkspaceUpdate(streamEpoch, (state) => {
          const targetConversationId = state.conversations.some(
            (item) => item.threadId === targetThreadId,
          )
            ? targetThreadId
            : state.currentThreadId
          const nextState = updateConversation(state, targetConversationId, (item) => {
            const caughtUp = missingEnvelopes.reduce(applyLiveEventEnvelope, item)
            const withEvent = applyConversationEvent(caughtUp, event)
            return seq == null
              ? { ...withEvent, isHydrated: true }
              : { ...withEvent, lastSeq: seq, isHydrated: true }
          })
          return state.currentThreadId === targetConversationId
            && reportedThreadId !== targetConversationId
            ? { ...nextState, currentThreadId: reportedThreadId }
            : nextState
        })
        if (seq != null) lastAppliedSeq = seq
        persistActiveRun()
        if (
          (event.type === 'RUN_FINISHED' && event.runId === payload.runId)
          || (
            event.type === 'RUN_ERROR'
            && (
              event.rawEvent?.runId === payload.runId
              || event.rawEvent?.source?.agentType !== 'subagent'
            )
          )
        ) {
          mainTerminalReceived = true
          clearActiveRunSession(payload.runId)
        }
          }
          if (mainTerminalReceived || !receivedEvent) break
          throw new TypeError('实时输出连接在主终态前断开')
        } catch (error) {
          if (controller.signal.aborted) return
          const canRetry = error instanceof TypeError
            && (receivedEvent || options.initialAfterSeq != null)
          if (!canRetry) throw error
          reconnectAttempt += 1
          await waitForReconnect(
            Math.min(250 * (2 ** (reconnectAttempt - 1)), RECONNECT_MAX_DELAY_MS),
            controller.signal,
          )
          if (controller.signal.aborted) return
        }
      }
    } catch (error) {
      if (controller.signal.aborted) return
      const message = error instanceof Error ? error.message : 'chat 接口请求失败'

      if (target === 'draft' && draftTarget) {
        const erroredConversation: Conversation = {
          ...draftTarget,
          runStatus: 'error',
          activeRunId: undefined,
          notice: { kind: 'error', content: message },
          isHydrated: true,
        }
        draftTarget = erroredConversation
        enqueueDraftUpdate(streamEpoch, erroredConversation)
      } else if (receivedEvent) {
        const currentTargetThreadId = activeThreadId.current ?? targetThreadId
        enqueueWorkspaceUpdate(streamEpoch, (state) => updateConversation(
          state,
          currentTargetThreadId,
          (item) => markConversationDetached(
            item,
            `实时事件处理失败：${message}。将从已持久化事件继续恢复。`,
          ),
        ))
      } else {
        if (!receivedEvent) clearActiveRunSession(payload.runId)
        const currentTargetThreadId = activeThreadId.current ?? targetThreadId
        enqueueWorkspaceUpdate(
          streamEpoch,
          (state) => updateConversation(state, currentTargetThreadId, (item) => ({
            ...item,
            runStatus: 'error',
            activeRunId: undefined,
            notice: { kind: 'error', content: message },
          })),
        )
      }
    } finally {
      if (mainTerminalReceived) clearActiveRunSession(payload.runId)
      if (activeAbortController.current === controller) {
        const currentTargetThreadId = activeThreadId.current ?? targetThreadId
        activeAbortController.current = null
        activeThreadId.current = null
        activeRunId.current = null

        if (target === 'draft' && draftTarget) {
          enqueueDraftUpdate(
            streamEpoch,
            markConversationDetached(
              draftTarget,
              '实时输出连接已断开，后端任务可能仍在继续。',
            ),
          )
        } else {
          enqueueWorkspaceUpdate(
            streamEpoch,
            (state) => updateConversation(
              state,
              currentTargetThreadId,
              (item) => markConversationDetached(
                item,
                '实时输出连接已断开，后端任务可能仍在继续。',
              ),
            ),
          )
        }
        if (!mainTerminalReceived && target === 'workspace') {
          const timer = window.setTimeout(() => {
            delayedCatchUpTimers.current.delete(timer)
            if (!isMounted.current || activeStreamEpoch.current !== streamEpoch) return
            void catchUpDetachedConversation(currentTargetThreadId)
          }, 100)
          delayedCatchUpTimers.current.add(timer)
        }
      }
    }
  }, [
    catchUpDetachedConversation,
    enqueueDraftUpdate,
    enqueueWorkspaceUpdate,
  ])

  const hasActiveStream = useCallback(
    () => activeAbortController.current != null,
    [],
  )
  const getActiveThreadId = useCallback(() => activeThreadId.current, [])
  const cancelActiveRun = useCallback(async () => {
    const threadId = activeThreadId.current
    const runId = activeRunId.current
    if (!threadId || runId == null) return false
    const result = await cancelConversationRun(threadId, runId)
    return result.cancelled
  }, [])
  const isActiveThread = useCallback(
    (threadId: string) => (
      activeAbortController.current != null && activeThreadId.current === threadId
    ),
    [],
  )

  useEffect(() => {
    const controllers = catchUpControllers.current
    const requests = catchUpRequests.current
    const timers = delayedCatchUpTimers.current
    isMounted.current = true
    return () => {
      isMounted.current = false
      activeStreamEpoch.current += 1
      activeAbortController.current?.abort()
      activeAbortController.current = null
      activeThreadId.current = null
      activeRunId.current = null
      for (const controller of controllers.values()) controller.abort()
      controllers.clear()
      requests.clear()
      for (const timer of timers) window.clearTimeout(timer)
      timers.clear()
      if (workspaceFrame.current != null) window.cancelAnimationFrame(workspaceFrame.current)
      if (draftFrame.current != null) window.cancelAnimationFrame(draftFrame.current)
      workspaceFrame.current = null
      draftFrame.current = null
      workspaceFrameUpdates.current = []
      pendingDraftFrameValue.current = undefined
    }
  }, [])

  return {
    streamRun,
    catchUpDetachedConversation,
    detachThreadStream,
    cancelActiveRun,
    hasActiveStream,
    getActiveThreadId,
    isActiveThread,
  }
}
