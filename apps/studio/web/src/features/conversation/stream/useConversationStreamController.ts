import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type Dispatch,
  type SetStateAction,
} from 'react'

import {
  cancelConversationRun,
  resumeConversationRun,
  startConversationRun,
} from '../../../api/conversation/client'
import {
  ConversationError,
  conversationErrorMessage,
  hasConversationErrorCode,
} from '../../../api/conversation/errors'
import {
  fetchConversationHistoryDetail,
  followConversationTrace,
} from '../../../api/conversation/history'
import type { ChatRequestPayload } from '../../../api/conversation/types'
import type { TaskTraceSnapshot } from '../../../api/conversation/taskTrace'
import { translateCurrent } from '../../../i18n'
import type {
  Conversation,
  WebTaskTraceViewState,
  WorkspaceState,
} from '../../../types'
import {
  applyConversationEvent,
  markConversationDetached,
} from '../agui'
import {
  applyConversationTraceUpdate,
  restoreConversationFromTrace,
} from '../trace/runtime'
import { LiveTodoTraceProjector } from '../todoTrace/liveProjection'
import { TaskTraceFollowOwnership } from '../todoTrace/followOwnership'
import { InvalidStateDeltaError } from '../agui/jsonPatch'
import {
  selectCurrentConversation,
  updateConversation,
  upsertConversation,
} from '../../../lib/workspace'
import {
  clearActiveRunSession,
  writeActiveRunSession,
  type ActiveRunSession,
} from './activeRunSession'

const RECONNECT_MAX_DELAY_MS = 5000
const ACTIVE_RUN_PERSIST_INTERVAL_MS = 250
const TEXT_RENDER_INTERVAL_MS = 50
const DETACHED_TRACE_RECONNECT_LIMIT = 3

const taskTraceView = (snapshot: TaskTraceSnapshot): WebTaskTraceViewState => (
  snapshot.status === 'ready'
    ? { phase: 'ready', snapshot }
    : { phase: 'unavailable', snapshot }
)

const latestUserTurn = (conversation: Conversation | undefined) => {
  let message: Conversation['messages'][number] | undefined
  for (let index = (conversation?.messages.length ?? 0) - 1; index >= 0; index -= 1) {
    const candidate = conversation?.messages[index]
    if (candidate?.role !== 'user') continue
    message = candidate
    break
  }
  const runId = message?.meta?.runId
  return message && runId
    ? {
        runId,
        userMessageId: message.id,
        userMessagePreview: message.content,
      }
    : undefined
}

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
  followDetachedConversation: (threadId: string) => Promise<void>
  handoffTaskTraceFollow: (threadId: string) => Promise<void>
  detachThreadStream: (threadId: string, reason: string) => void
  cancelActiveRun: () => Promise<boolean>
  cancelPendingRunId: string | null
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
  const cancelRequest = useRef<{
    runId: string
    promise: Promise<boolean>
  } | null>(null)
  const cancelPendingRunIdRef = useRef<string | null>(null)
  const [cancelPendingRunId, setCancelPendingRunId] = useState<string | null>(null)
  const activeStreamEpoch = useRef(0)
  const workspaceUpdateTimer = useRef<number | null>(null)
  const workspaceFrameUpdates = useRef<Array<{
    epoch: number
    update: (state: WorkspaceState) => WorkspaceState
  }>>([])
  const draftUpdateTimer = useRef<number | null>(null)
  const pendingDraftFrameValue = useRef<{
    epoch: number
    value: Conversation | null
  } | undefined>(undefined)
  const latestWorkspace = useRef(workspace)
  const taskTraceFollowOwnership = useRef(new TaskTraceFollowOwnership())
  const activeTodoProjector = useRef<LiveTodoTraceProjector | null>(null)
  const delayedTraceFollowTimers = useRef(new Set<number>())
  const activeRunPersistence = useRef<{
    runId: string
    session: ActiveRunSession
    timer: number | null
  } | null>(null)
  const isMounted = useRef(true)
  latestWorkspace.current = workspace

  const clearCancelPending = useCallback((runId: string) => {
    if (cancelPendingRunIdRef.current !== runId) return
    cancelPendingRunIdRef.current = null
    if (isMounted.current) setCancelPendingRunId(null)
  }, [])

  const flushActiveRunPersistence = useCallback((runId?: string) => {
    const owner = activeRunPersistence.current
    if (!owner || (runId && owner.runId !== runId)) return
    if (owner.timer != null) {
      window.clearTimeout(owner.timer)
      owner.timer = null
    }
    writeActiveRunSession(owner.session)
  }, [])

  const scheduleActiveRunPersistence = useCallback((
    session: ActiveRunSession,
    immediate = false,
  ) => {
    const runId = session.payload.runId
    let owner = activeRunPersistence.current
    if (!owner || owner.runId !== runId) {
      if (owner?.timer != null) window.clearTimeout(owner.timer)
      owner = { runId, session, timer: null }
      activeRunPersistence.current = owner
    } else {
      owner.session = session
    }

    if (immediate) {
      flushActiveRunPersistence(runId)
      return
    }
    if (owner.timer != null) return
    owner.timer = window.setTimeout(() => {
      if (activeRunPersistence.current !== owner) return
      owner.timer = null
      writeActiveRunSession(owner.session)
    }, ACTIVE_RUN_PERSIST_INTERVAL_MS)
  }, [flushActiveRunPersistence])

  const clearActiveRunPersistence = useCallback((runId: string) => {
    const owner = activeRunPersistence.current
    if (owner?.runId === runId) {
      if (owner.timer != null) window.clearTimeout(owner.timer)
      activeRunPersistence.current = null
    }
    clearActiveRunSession(runId)
  }, [])

  const flushWorkspaceUpdates = useCallback(() => {
    if (workspaceUpdateTimer.current != null) {
      window.clearTimeout(workspaceUpdateTimer.current)
      workspaceUpdateTimer.current = null
    }
    const currentEpoch = activeStreamEpoch.current
    const updates = workspaceFrameUpdates.current
      .splice(0)
      .filter((entry) => entry.epoch === currentEpoch)
    if (updates.length === 0) return
    setWorkspace((state) => updates.reduce((next, entry) => entry.update(next), state))
  }, [setWorkspace])

  const enqueueWorkspaceUpdate = useCallback((
    epoch: number,
    updater: (state: WorkspaceState) => WorkspaceState,
    deferTextRender = false,
  ) => {
    workspaceFrameUpdates.current.push({ epoch, update: updater })
    if (!deferTextRender) {
      flushWorkspaceUpdates()
      return
    }
    if (workspaceUpdateTimer.current != null) return
    workspaceUpdateTimer.current = window.setTimeout(
      flushWorkspaceUpdates,
      TEXT_RENDER_INTERVAL_MS,
    )
  }, [flushWorkspaceUpdates])

  const flushDraftUpdate = useCallback(() => {
    if (draftUpdateTimer.current != null) {
      window.clearTimeout(draftUpdateTimer.current)
      draftUpdateTimer.current = null
    }
    const pending = pendingDraftFrameValue.current
    pendingDraftFrameValue.current = undefined
    if (pending?.epoch === activeStreamEpoch.current) {
      setDraftConversation(pending.value)
    }
  }, [setDraftConversation])

  const enqueueDraftUpdate = useCallback((
    epoch: number,
    value: Conversation | null,
    deferTextRender = false,
  ) => {
    pendingDraftFrameValue.current = { epoch, value }
    if (!deferTextRender) {
      flushDraftUpdate()
      return
    }
    if (draftUpdateTimer.current != null) return
    draftUpdateTimer.current = window.setTimeout(
      flushDraftUpdate,
      TEXT_RENDER_INTERVAL_MS,
    )
  }, [flushDraftUpdate])

  const detachThreadStream = useCallback((threadId: string, reason: string) => {
    if (
      activeAbortController.current == null
      || activeThreadId.current !== threadId
    ) return
    const detachedEpoch = activeStreamEpoch.current
    const detachedRunId = activeRunId.current
    activeStreamEpoch.current += 1
    if (detachedRunId) flushActiveRunPersistence(detachedRunId)
    activeAbortController.current?.abort()
    activeAbortController.current = null
    activeThreadId.current = null
    activeRunId.current = null
    if (detachedRunId) clearCancelPending(detachedRunId)

    if (workspaceUpdateTimer.current != null) {
      window.clearTimeout(workspaceUpdateTimer.current)
      workspaceUpdateTimer.current = null
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

    if (draftUpdateTimer.current != null) {
      window.clearTimeout(draftUpdateTimer.current)
      draftUpdateTimer.current = null
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
  }, [clearCancelPending, flushActiveRunPersistence, setDraftConversation, setWorkspace])

  const followDetachedConversation = useCallback(async (threadId: string) => {
    if (!isMounted.current) return
    const target = latestWorkspace.current.conversations.find(
      (item) => item.threadId === threadId,
    )
    if (!target || target.runStatus !== 'detached' || !target.isHydrated) return
    await taskTraceFollowOwnership.current.follow(threadId, async ({
      includeTaskTrace,
      signal,
    }) => {
      try {
      let reconnectAttempts = 0
      while (!signal.aborted) {
        for await (const event of followConversationTrace(threadId, {
          includeTaskTrace,
          signal,
        })) {
          if (
            signal.aborted
            || !isMounted.current
            || (activeAbortController.current && activeThreadId.current === threadId)
          ) return
          if (event.type === 'error') throw new ConversationError('stream_recovery_failed')
          setWorkspace((state) => updateConversation(state, threadId, (item) => {
            if (event.type === 'snapshot') {
              return restoreConversationFromTrace(event.snapshot, {
                previous: item,
                model: item.model,
                lastDeliveredSeq: item.lastSeq,
                includeTaskTrace,
                taskTrace: item.taskTrace,
              })
            }
            return applyConversationTraceUpdate(
              item,
              event.update,
              event.taskTrace,
              includeTaskTrace,
            )
          }))
          const execution = event.type === 'snapshot'
            ? event.snapshot.status.execution
            : event.update.status.execution
          if (execution !== 'running') {
            const detail = await fetchConversationHistoryDetail(threadId, {
              includeTaskTrace,
              signal,
              suppressGlobalError: true,
            })
            if (!signal.aborted) {
              setWorkspace((state) => updateConversation(
                state,
                threadId,
                (item) => restoreConversationFromTrace(detail, {
                  previous: item,
                  model: item.model,
                  lastDeliveredSeq: item.lastSeq,
                  includeTaskTrace,
                  taskTrace: item.taskTrace,
                }),
              ))
            }
            return
          }
        }
        if (
          signal.aborted
          || !isMounted.current
          || (activeAbortController.current && activeThreadId.current === threadId)
        ) return

        // 网络 EOF 不是终态；重连前先刷新权威快照，避免漏掉断连期间提交的终态
        const detail = await fetchConversationHistoryDetail(threadId, {
          includeTaskTrace,
          signal,
          suppressGlobalError: true,
        })
        if (signal.aborted || !isMounted.current) return
        setWorkspace((state) => updateConversation(
          state,
          threadId,
          (item) => restoreConversationFromTrace(detail, {
            previous: item,
            model: item.model,
            lastDeliveredSeq: item.lastSeq,
            includeTaskTrace,
            taskTrace: item.taskTrace,
          }),
        ))
        if (detail.status.execution !== 'running') return
        reconnectAttempts += 1
        if (reconnectAttempts > DETACHED_TRACE_RECONNECT_LIMIT) {
          throw new ConversationError('stream_recovery_failed')
        }
      }
    } catch (error) {
      if (
        signal.aborted
        || !isMounted.current
        || (activeAbortController.current && activeThreadId.current === threadId)
      ) return
      const message = conversationErrorMessage(error, 'stream_recovery_failed')
      setWorkspace((state) => updateConversation(state, threadId, (item) => ({
        ...item,
        notice: { kind: 'error', content: message },
      })))
      }
    })
  }, [setWorkspace])

  const handoffTaskTraceFollow = useCallback(async (threadId: string) => {
    const handoff = await taskTraceFollowOwnership.current.handoff(threadId)
    for (const demotedThreadId of handoff.demotedThreadIds) {
      void followDetachedConversation(demotedThreadId)
    }
  }, [followDetachedConversation])

  const streamRun = useCallback(async (
    threadIdToStream: string,
    payload: ChatRequestPayload,
    mode: 'start' | 'resume',
    options: StreamRunOptions = { target: 'workspace' },
  ) => {
    activeAbortController.current?.abort()
    await taskTraceFollowOwnership.current.stop(threadIdToStream)
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
    let validationTarget = target === 'workspace'
      ? latestWorkspace.current.conversations.find(
          (item) => item.threadId === targetThreadId,
        )
      : draftTarget
    let todoProjector: LiveTodoTraceProjector | null = null
    let projectedTaskTrace = validationTarget?.taskTrace ?? { phase: 'unloaded' as const }
    let projectedSnapshot: TaskTraceSnapshot | null = null
    if (validationTarget?.taskTrace.phase !== 'unavailable') {
      todoProjector = new LiveTodoTraceProjector()
      const priorHead = validationTarget?.trace?.headRunId
        ?? latestUserTurn(validationTarget)?.runId
      if (
        validationTarget?.taskTrace.phase === 'ready'
        && priorHead
      ) {
        todoProjector.hydrate(validationTarget.taskTrace.snapshot, {
          headRunId: priorHead,
          latestTurn: latestUserTurn(validationTarget),
        })
      }
      const turn = mode === 'start'
        ? latestUserTurn(validationTarget)
        : undefined
      todoProjector.startRun({
        runId: payload.runId,
        inputKind: mode === 'start' ? 'ordinary' : 'resume',
        parentRunId: payload.parentRunId,
        turn,
      })
      projectedSnapshot = todoProjector.snapshot
      projectedTaskTrace = taskTraceView(projectedSnapshot)
      activeTodoProjector.current?.close()
      activeTodoProjector.current = todoProjector
    }
    const applyTaskTrace = (
      current: Conversation,
      event: Parameters<typeof applyConversationEvent>[1],
      receivedAt: string,
    ): Conversation => {
      if (!todoProjector) return current
      const snapshot = todoProjector.consume(event, {
        receivedAt,
        rootState: current.serverState,
      })
      if (snapshot !== projectedSnapshot) {
        projectedSnapshot = snapshot
        projectedTaskTrace = taskTraceView(snapshot)
      }
      return current.taskTrace === projectedTaskTrace
        ? current
        : { ...current, taskTrace: projectedTaskTrace }
    }
    let receivedEvent = false
    let mainTerminalReceived = false
    let traceAuthorityLoaded = false
    let requestPayload: ChatRequestPayload = { ...payload }
    let reconnectAttempt = 0
    let lastAppliedSeq: number | null = (
      options.initialAfterSeq
      ?? (target === 'draft'
        ? draftTarget?.lastSeq
        : latestWorkspace.current.conversations.find(
            (item) => item.threadId === targetThreadId,
          )?.lastSeq)
    ) ?? null
    const persistActiveRun = (immediate = false) => scheduleActiveRunPersistence({
      threadId: targetThreadId,
      payload: requestPayload,
      mode,
      // 尚未收到首帧时使用 0 作为刷新恢复记录中的未知游标哨兵
      lastSeq: lastAppliedSeq ?? 0,
    }, immediate)
    // 首次写入建立刷新恢复所有权，不能等待第一个节流周期
    persistActiveRun(true)

    try {
      while (!mainTerminalReceived) {
        try {
          for await (const { event, seq } of stream(
            requestPayload,
            controller.signal,
            reconnectAttempt === 0
              ? options.initialAfterSeq
              : lastAppliedSeq ?? undefined,
          )) {
        receivedEvent = true
        const eventReceivedAt = new Date().toISOString()
        const reportedThreadId: string = 'threadId' in event && typeof event.threadId === 'string'
          ? event.threadId
          : (activeThreadId.current ?? targetThreadId)
        const canonicalIdentityChanged = targetThreadId !== reportedThreadId
          || requestPayload.threadId !== reportedThreadId
        activeThreadId.current = reportedThreadId
        targetThreadId = reportedThreadId
        if (requestPayload.threadId !== reportedThreadId) {
          requestPayload = { ...requestPayload, threadId: reportedThreadId }
        }

        if (seq != null && lastAppliedSeq != null && seq <= lastAppliedSeq) continue
        if (seq != null && lastAppliedSeq != null && seq !== lastAppliedSeq + 1) {
          throw new ConversationError(
            'stream_sequence_invalid',
            `expected=${lastAppliedSeq + 1}, actual=${seq}`,
          )
        }

        if (target === 'draft' && draftTarget) {
          const deferTextRender = event.type === 'TEXT_MESSAGE_CONTENT'
          const withEvent = applyTaskTrace(
            applyConversationEvent(draftTarget, event),
            event,
            eventReceivedAt,
          )
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
            enqueueWorkspaceUpdate(streamEpoch, (state) => upsertConversation(
              selectCurrentConversation(state, reportedThreadId),
              candidateConversation,
            ))
            enqueueDraftUpdate(streamEpoch, null)
            draftTarget = undefined
            validationTarget = candidateConversation
            target = 'workspace'
            targetThreadId = reportedThreadId
          } else {
            enqueueDraftUpdate(streamEpoch, nextDraft, deferTextRender)
            if (event.type === 'RUN_FINISHED' || event.type === 'RUN_ERROR') {
              enqueueWorkspaceUpdate(
                streamEpoch,
                (state) => upsertConversation(state, nextDraft),
              )
              enqueueDraftUpdate(streamEpoch, null)
            }
          }
          persistActiveRun(canonicalIdentityChanged)
          continue
        }

        // 先在顺序投影中验证协议事件，避免 Patch 等异常延迟到 React updater 后逃逸
        if (!validationTarget) {
          throw new ConversationError('stream_event_invalid', '缺少事件验证目标会话')
        }
        const validated = applyTaskTrace(
          applyConversationEvent(validationTarget, event),
          event,
          eventReceivedAt,
        )
        validationTarget = seq == null
          ? { ...validated, isHydrated: true }
          : { ...validated, lastSeq: seq, isHydrated: true }

        enqueueWorkspaceUpdate(streamEpoch, (state) => {
          const targetConversationId = state.conversations.some(
            (item) => item.threadId === targetThreadId,
          )
            ? targetThreadId
            : state.currentThreadId
          const nextState = updateConversation(state, targetConversationId, (item) => {
            const withEvent = applyConversationEvent(item, event)
            const withTaskTrace = state.currentThreadId === targetConversationId
              ? { ...withEvent, taskTrace: projectedTaskTrace }
              : withEvent
            return seq == null
              ? { ...withTaskTrace, isHydrated: true }
              : { ...withTaskTrace, lastSeq: seq, isHydrated: true }
          })
          return state.currentThreadId === targetConversationId
            && reportedThreadId !== targetConversationId
            ? selectCurrentConversation(nextState, reportedThreadId)
            : nextState
        }, event.type === 'TEXT_MESSAGE_CONTENT')
        if (seq != null) lastAppliedSeq = seq
        persistActiveRun(canonicalIdentityChanged)
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
          clearActiveRunPersistence(payload.runId)
        }
          }
          if (mainTerminalReceived || !receivedEvent) break
          throw new ConversationError('stream_disconnected')
        } catch (error) {
          if (controller.signal.aborted) return
          const canRetry = (
            error instanceof TypeError
            || hasConversationErrorCode(error, 'stream_disconnected')
            || hasConversationErrorCode(error, 'stream_sequence_invalid')
          )
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
      if (mainTerminalReceived && target === 'workspace') {
        await handoffTaskTraceFollow(targetThreadId)
        const detail = await fetchConversationHistoryDetail(targetThreadId, {
          includeTaskTrace: true,
          signal: controller.signal,
          suppressGlobalError: true,
        })
        const authoritative = restoreConversationFromTrace(detail, {
          previous: validationTarget ?? undefined,
          model: validationTarget?.model ?? payload.forwardedProps.model,
          lastDeliveredSeq: lastAppliedSeq ?? undefined,
          includeTaskTrace: true,
        })
        validationTarget = authoritative
        traceAuthorityLoaded = true
        enqueueWorkspaceUpdate(
          streamEpoch,
          (state) => updateConversation(
            state,
            targetThreadId,
            (item) => restoreConversationFromTrace(detail, {
              previous: item,
              model: authoritative.model,
              lastDeliveredSeq: authoritative.lastSeq,
              includeTaskTrace: true,
            }),
          ),
        )
      }
    } catch (error) {
      if (controller.signal.aborted) return
      const stableError = error instanceof InvalidStateDeltaError
        ? new ConversationError('state_patch_invalid', error)
        : error
      // 协议诊断留在错误对象中，notice 只使用稳定错误码对应的恢复文案
      const message = conversationErrorMessage(
        stableError,
        receivedEvent ? 'stream_event_invalid' : 'run_request_failed',
      )

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
          (item) => ({
            ...markConversationDetached(item, message),
            runStatus: 'detached',
            activeRunId: item.activeRunId ?? payload.runId,
            notice: { kind: 'error', content: message },
          }),
        ))
      } else {
        if (!receivedEvent) clearActiveRunPersistence(payload.runId)
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
      if (activeTodoProjector.current === todoProjector) {
        activeTodoProjector.current = null
      }
      todoProjector?.close()
      clearCancelPending(payload.runId)
      if (mainTerminalReceived) clearActiveRunPersistence(payload.runId)
      if (activeAbortController.current === controller) {
        if (!mainTerminalReceived) flushActiveRunPersistence(payload.runId)
        const currentTargetThreadId = activeThreadId.current ?? targetThreadId
        activeAbortController.current = null
        activeThreadId.current = null
        activeRunId.current = null

        if (target === 'draft' && draftTarget) {
          enqueueDraftUpdate(
            streamEpoch,
            markConversationDetached(
              draftTarget,
              translateCurrent('已停止接收实时输出，后端任务可能仍在继续'),
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
                translateCurrent('已停止接收实时输出，后端任务可能仍在继续'),
              ),
            ),
          )
        }
        if ((!mainTerminalReceived || !traceAuthorityLoaded) && target === 'workspace') {
          const timer = window.setTimeout(() => {
            delayedTraceFollowTimers.current.delete(timer)
            if (!isMounted.current || activeStreamEpoch.current !== streamEpoch) return
            void followDetachedConversation(currentTargetThreadId)
          }, 100)
          delayedTraceFollowTimers.current.add(timer)
        }
      }
    }
  }, [
    followDetachedConversation,
    handoffTaskTraceFollow,
    clearActiveRunPersistence,
    clearCancelPending,
    enqueueDraftUpdate,
    enqueueWorkspaceUpdate,
    flushActiveRunPersistence,
    scheduleActiveRunPersistence,
  ])

  const hasActiveStream = useCallback(
    () => activeAbortController.current != null,
    [],
  )
  const getActiveThreadId = useCallback(() => activeThreadId.current, [])
  const cancelActiveRun = useCallback(() => {
    const threadId = activeThreadId.current
    const runId = activeRunId.current
    if (!threadId || runId == null) return Promise.resolve(false)
    const existing = cancelRequest.current
    if (existing?.runId === runId) return existing.promise
    // 停止请求成功只代表后端已受理，按钮所有权要保留到流真正进入终态
    if (cancelPendingRunIdRef.current === runId) return Promise.resolve(true)

    cancelPendingRunIdRef.current = runId
    setCancelPendingRunId(runId)
    const promise = cancelConversationRun(threadId, runId).then((result) => result.cancelled)
    const owner = { runId, promise }
    cancelRequest.current = owner
    void promise.then(
      (cancelled) => {
        if (cancelRequest.current === owner) cancelRequest.current = null
        if (!cancelled) clearCancelPending(runId)
      },
      () => {
        if (cancelRequest.current === owner) cancelRequest.current = null
        clearCancelPending(runId)
      },
    )
    return promise
  }, [clearCancelPending])
  const isActiveThread = useCallback(
    (threadId: string) => (
      activeAbortController.current != null && activeThreadId.current === threadId
    ),
    [],
  )

  useEffect(() => {
    const handlePageHide = () => flushActiveRunPersistence(activeRunId.current ?? undefined)
    window.addEventListener('pagehide', handlePageHide)
    return () => window.removeEventListener('pagehide', handlePageHide)
  }, [flushActiveRunPersistence])

  useEffect(() => {
    const timers = delayedTraceFollowTimers.current
    const followOwnership = taskTraceFollowOwnership.current
    isMounted.current = true
    return () => {
      isMounted.current = false
      activeStreamEpoch.current += 1
      flushActiveRunPersistence(activeRunId.current ?? undefined)
      activeAbortController.current?.abort()
      activeTodoProjector.current?.close()
      activeTodoProjector.current = null
      activeAbortController.current = null
      activeThreadId.current = null
      activeRunId.current = null
      const persistenceOwner = activeRunPersistence.current
      if (persistenceOwner?.timer != null) window.clearTimeout(persistenceOwner.timer)
      activeRunPersistence.current = null
      cancelRequest.current = null
      cancelPendingRunIdRef.current = null
      followOwnership.abortAll()
      for (const timer of timers) window.clearTimeout(timer)
      timers.clear()
      if (workspaceUpdateTimer.current != null) window.clearTimeout(workspaceUpdateTimer.current)
      if (draftUpdateTimer.current != null) window.clearTimeout(draftUpdateTimer.current)
      workspaceUpdateTimer.current = null
      draftUpdateTimer.current = null
      workspaceFrameUpdates.current = []
      pendingDraftFrameValue.current = undefined
    }
  }, [flushActiveRunPersistence])

  return {
    streamRun,
    followDetachedConversation,
    handoffTaskTraceFollow,
    detachThreadStream,
    cancelActiveRun,
    cancelPendingRunId,
    hasActiveStream,
    getActiveThreadId,
    isActiveThread,
  }
}
