import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'

import type { AgentMode, ChatRequestPayload } from '../../api/conversation/types'
import { conversationErrorMessage } from '../../api/conversation/errors'
import type { AuthUser } from '../../api/auth/types'
import { Button, ErrorBoundary, useThemePreference } from '../../components/ui'
import type { ToastKind } from '../../components/ui/ToastViewport'
import { useI18n } from '../../i18n'
import {
  ApprovalCard,
  type ApprovalSubmissionDecision,
} from '../conversation/components/ApprovalCard'
import { Composer } from '../conversation/components/Composer'
import { PlanQuestionComposer } from '../conversation/components/PlanQuestionComposer'
import { PlanReviewCard } from '../conversation/components/PlanReviewCard'
import { parseComposerSubmission } from '../conversation/composerCommand'
import { useLocalAttachments } from '../conversation/useLocalAttachments'
import { ComposerModelPicker } from './components/ComposerModelPicker'
import { Sidebar } from './components/Sidebar'
import { TaskDrawer } from './components/TaskDrawer'
import {
  ConversationViewport,
  type ConversationDisplayEntry,
} from './components/ConversationViewport'
import { EmptyConversationBrand } from './components/EmptyConversation'
import { WorkspaceDialogs } from './components/WorkspaceDialogs'
import { WorkspaceHeader } from './components/WorkspaceHeader'
import { WorkspaceStatus } from './components/WorkspaceStatus'
import { SettingsDialog } from '../settings/SettingsDialog'
import { useWorkspaceNavigation } from './useWorkspaceNavigation'
import { useModelCatalog } from './useModelCatalog'
import { useWorkspaceHistory } from './useWorkspaceHistory'
import { useConversationScroll } from './useConversationScroll'
import { useConversationManagement } from './useConversationManagement'
import { useTaskDrawerState } from './useTaskDrawerState'
import { useWorkspaceLayoutAnimation } from './useWorkspaceLayoutAnimation'
import '../conversation/conversation.css'
import './workspace.css'
import {
  buildInitialPayload,
  buildPlanAbandonPayload,
  buildPlanResumePayload,
  buildResumePayload,
  prepareResumeSubmission,
} from '../conversation/agui'
import { useConversationStreamController } from '../conversation/stream/useConversationStreamController'
import {
  clearActiveRunSession,
  readActiveRunSession,
} from '../conversation/stream/activeRunSession'
import {
  buildEmptyConversation,
  createEmptyWorkspace,
  updateConversation,
} from '../../lib/workspace'
import { readThreadFromLocation, writeThreadToLocation } from '../../lib/threadRoute'
import type {
  ApprovalState,
  Conversation,
  PlanInteraction,
  PlanQuestionState,
  PlanReviewState,
  WorkspaceState,
} from '../../types'

interface PendingResume {
  kind: 'tool' | 'plan'
  threadId: string
  payload: ChatRequestPayload
  expectedInterruptIds: readonly string[]
}

const MESSAGE_RENDER_BATCH_SIZE = 100

const matchesApprovalGroup = (
  approval: ApprovalState | undefined,
  expectedInterruptIds: readonly string[],
) => Boolean(
  approval
  && approval.items.length === expectedInterruptIds.length
  && approval.items.every(
    (item, index) => item.interruptId === expectedInterruptIds[index],
  ),
)

const withFinalApprovalDecision = (
  approval: ApprovalState,
  finalDecision?: ApprovalSubmissionDecision,
) => {
  if (!finalDecision) return approval
  const activeIndex = approval.items.findIndex(
    (item) => item.interruptId === finalDecision.interruptId,
  )
  if (activeIndex < 0) return approval
  return {
    ...approval,
    activeIndex,
    mode: 'options' as const,
    error: undefined,
    items: approval.items.map((item, index) => index === activeIndex
      ? {
          ...item,
          decision: finalDecision.decision,
          rejectionReason: finalDecision.decision === 'rejected'
            ? finalDecision.rejectionReason
            : undefined,
        }
      : item),
  }
}

export function WorkspaceScreen({
  user,
  onLogout,
  onToast,
}: {
  user: AuthUser
  onLogout: () => void
  onToast: (kind: ToastKind, message: string) => void
}) {
  const { t } = useI18n()
  const [workspace, setWorkspace] = useState<WorkspaceState>(createEmptyWorkspace)
  const [draftConversation, setDraftConversation] = useState<Conversation | null>(null)
  const [draftModel, setDraftModel] = useState('')
  const [draft, setDraft] = useState('')
  const [isModelPickerOpen, setModelPickerOpen] = useState(false)
  const [pendingResume, setPendingResume] = useState<PendingResume | null>(null)
  const [messageWindow, setMessageWindow] = useState<{
    threadId: string
    start: number | null
  }>({ threadId: '', start: null })
  const [settingsOpen, setSettingsOpen] = useState(false)
  const settingsRestoreFocus = useRef<HTMLElement | null>(null)
  const theme = useThemePreference()
  const navigation = useWorkspaceNavigation()
  const {
    status: modelCatalogStatus,
    modelIds,
    defaultModelId,
    displayName: modelDisplayName,
    retry: retryModelCatalog,
  } = useModelCatalog()
  const localAttachments = useLocalAttachments()
  const appShell = useRef<HTMLDivElement>(null)
  const latestWorkspace = useRef(workspace)
  const startedResumeRunIds = useRef(new Set<string>())
  const earlierMessageAnchor = useRef<{
    threadId: string
    scrollHeight: number
    scrollTop: number
    trigger: HTMLButtonElement
  } | null>(null)
  latestWorkspace.current = workspace
  const pushToast = onToast

  useEffect(() => {
    if (defaultModelId) setDraftModel((current) => current || defaultModelId)
  }, [defaultModelId])

  const {
    cancelActiveRun,
    cancelPendingRunId,
    catchUpDetachedConversation,
    detachThreadStream,
    getActiveThreadId,
    hasActiveStream,
    isActiveThread,
    streamRun,
  } = useConversationStreamController({
    workspace,
    setWorkspace,
    setDraftConversation,
  })
  const {
    historyConversations,
    historyDayRanges,
    historyQuery,
    setHistoryQuery,
    isHistorySearchActive,
    isHistorySearching,
    historyCursor,
    isHistoryLoadingMore,
    historyLoadError,
    isHistoryBootstrapped,
    historyBootstrapStatus,
    hydrationState,
    loadMoreHistory,
    retryHistoryLoad,
    retryHistoryBootstrap,
    hydrateConversation,
  } = useWorkspaceHistory({
    workspace,
    setWorkspace,
    defaultModelId,
    modelCatalogStatus,
    catchUpDetachedConversation,
    onToast: pushToast,
  })

  const selectedConversation = useMemo(
    () => workspace.conversations.find((item) => item.threadId === workspace.currentThreadId),
    [workspace],
  )
  const conversation = useMemo(() => {
    if (workspace.currentThreadId) {
      return selectedConversation
        ?? buildEmptyConversation({
          threadId: workspace.currentThreadId,
          now: new Date().toISOString(),
          model: draftModel,
        })
    }
    return draftConversation ?? buildEmptyConversation({ now: new Date().toISOString(), model: draftModel })
  }, [draftConversation, draftModel, selectedConversation, workspace.currentThreadId])

  useEffect(() => {
    document.title = conversation.threadId && conversation.title.trim()
      ? conversation.title.trim()
      : 'TinkerFin'
  }, [conversation.threadId, conversation.title])

  useEffect(() => () => {
    document.title = 'TinkerFin'
  }, [])

  const taskDrawer = useTaskDrawerState({
    threadId: conversation.threadId,
    todoCount: conversation.todos.length,
  })
  useWorkspaceLayoutAnimation({
    shellRef: appShell,
    layoutKey: `${navigation.mode}:${taskDrawer.open ? 'open' : 'closed'}:${taskDrawer.usesOverlay ? 'overlay' : 'docked'}`,
  })
  const isRunning = conversation.runStatus === 'streaming'
  const {
    paneRef: conversationPane,
    messageEndRef: messageEnd,
    showScrollToBottom,
    fadeScrollToBottom,
    handleScroll: handleConversationScroll,
    scrollToBottomImmediately: scrollConversationToBottomImmediately,
    syncToBottomIfFollowing: syncConversationToBottomIfFollowing,
    markUserScrollIntent,
    scrollBy: scrollConversationBy,
    scrollToBottom: scrollConversationToBottom,
    pauseScrollToBottomFade,
    resumeScrollToBottomFade,
    focusScrollToBottom,
    blurScrollToBottom,
  } = useConversationScroll({ conversation, isRunning })
  const isConversationHydrating = Boolean(
    workspace.currentThreadId
    && selectedConversation
    && !selectedConversation.isHydrated
    && hydrationState?.threadId === workspace.currentThreadId
    && hydrationState.status === 'loading',
  )
  const isConversationHydrationFailed = Boolean(
    workspace.currentThreadId
    && selectedConversation
    && !selectedConversation.isHydrated
    && hydrationState?.threadId === workspace.currentThreadId
    && hydrationState.status === 'failed',
  )
  const isInitialHistoryUnavailable = historyBootstrapStatus === 'error'
    && workspace.conversations.length === 0
    && !draftConversation
  const showConversationHero = conversation.messages.length === 0
    && !conversation.notice
    && isHistoryBootstrapped
    && historyBootstrapStatus === 'ready'
    && !isConversationHydrating
    && !isConversationHydrationFailed
    && !isInitialHistoryUnavailable
  const updateCurrent = useCallback((updater: (item: Conversation) => Conversation) => {
    setWorkspace((state) => updateConversation(state, state.currentThreadId, updater))
  }, [])

  useLayoutEffect(() => {
    const shell = appShell.current
    const composer = shell?.querySelector<HTMLElement>('.composer-dock')
    if (!shell || !composer) return
    const measure = () => {
      shell.style.setProperty(
        '--composer-height',
        `${Math.max(80, composer.getBoundingClientRect().height)}px`,
      )
      // Composer 改变可视高度时只跟随仍停留在底部的会话
      syncConversationToBottomIfFollowing()
    }
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(measure)
    observer.observe(composer)
    return () => observer.disconnect()
  }, [syncConversationToBottomIfFollowing])

  // 当前会话变化时同步进 URL（草稿 currentThreadId==='' → 删除参数），覆盖点击选择、
  // 新建草稿、首条消息后后端回报 reportedThreadId、删除会话等全部来源
  useEffect(() => {
    if (!isHistoryBootstrapped) return
    writeThreadToLocation(workspace.currentThreadId)
  }, [isHistoryBootstrapped, workspace.currentThreadId])

  // 浏览器后退/前进到带其他 `?thread=` 的记录时，轻量同步当前会话（不走 selectConversation
  // 的流断开确认，避免后退突弹确认框）
  useEffect(() => {
    const onPopState = () => {
      const threadId = readThreadFromLocation()
      setWorkspace((state) =>
        state.currentThreadId === threadId
          ? state
          : threadId && state.conversations.some((item) => item.threadId === threadId)
            ? { ...state, currentThreadId: threadId }
            : state,
      )
    }
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
  }, [])

  useEffect(() => {
    if (!workspace.currentThreadId || !selectedConversation?.isHydrated) return
    const active = readActiveRunSession()
    if (!active || (active.threadId && active.threadId !== selectedConversation.threadId)) return
    const runMatches = selectedConversation.activeRunId === active.payload.runId
    if (selectedConversation.runStatus !== 'detached' || !runMatches) {
      if (active.threadId === selectedConversation.threadId && selectedConversation.runStatus !== 'streaming') {
        clearActiveRunSession(active.payload.runId)
      }
      return
    }
    if (hasActiveStream()) return

    const threadId = selectedConversation.threadId
    const payload = { ...active.payload, threadId }
    const afterSeq = Math.max(active.lastSeq, selectedConversation.lastSeq ?? 0)
    setWorkspace((state) => updateConversation(state, threadId, (item) => ({
      ...item,
      runStatus: 'streaming',
      activeRunId: active.payload.runId,
    })))
    void streamRun(threadId, payload, active.mode, {
      target: 'workspace',
      initialAfterSeq: afterSeq,
    })
  }, [
    hasActiveStream,
    selectedConversation?.activeRunId,
    selectedConversation?.isHydrated,
    selectedConversation?.lastSeq,
    selectedConversation?.runStatus,
    selectedConversation?.threadId,
    streamRun,
    workspace.currentThreadId,
  ])

  const childToolsByRunId = useMemo(() => {
    const grouped = new Map<string, Conversation['messages']>()
    for (const message of conversation.messages) {
      if (message.role !== 'tool' || !message.meta?.sourceAgentName || !message.meta.runId) continue
      const tools = grouped.get(message.meta.runId) ?? []
      tools.push(message)
      grouped.set(message.meta.runId, tools)
    }
    return grouped
  }, [conversation.messages])

  const displayMessages = useMemo(() => {
    const pendingApproval = conversation.approval?.submitted === false
      ? conversation.approval
      : undefined
    const activeApprovalIndex = pendingApproval
      ? Math.max(0, Math.min(pendingApproval.activeIndex, pendingApproval.items.length - 1))
      : -1
    const activeApprovalToolCallId = pendingApproval?.items[activeApprovalIndex]?.toolCallId
    const approvalToolCallIds = new Set(
      pendingApproval?.items.flatMap((item) => item.toolCallId ? [item.toolCallId] : []) ?? [],
    )

    return conversation.messages.filter((message) => {
      if (message.role !== 'tool') return true
      if (
        activeApprovalToolCallId
        && message.meta?.toolCallId === activeApprovalToolCallId
      ) return true
      if (
        message.meta?.toolCallId
        && approvalToolCallIds.has(message.meta.toolCallId)
      ) return false
      if (message.meta?.toolName === 'write_todos') return false
      if (message.meta?.toolName === 'PlannerOutcome') return false
      if (message.meta?.sourceAgentName) return false
      if (message.meta?.toolName === 'task') {
        return message.meta.status !== 'running' && !message.meta.subRunId
      }
      return true
    })
    .reduce<ConversationDisplayEntry[]>((groups, message) => {
      const previous = groups.at(-1)
      if (message.role === 'tool' && message.meta?.batchId && previous?.type === 'tools' && previous.messages[0]?.meta?.batchId === message.meta.batchId) {
        previous.messages.push(message)
      } else if (message.role === 'tool' && message.meta?.batchId) groups.push({ type: 'tools', messages: [message] })
      else groups.push({ type: 'message', message })
      return groups
    }, [])
  }, [conversation.approval, conversation.messages])

  const defaultMessageWindowStart = Math.max(
    0,
    displayMessages.length - MESSAGE_RENDER_BATCH_SIZE,
  )
  const messageWindowStart = messageWindow.threadId === conversation.threadId
    && messageWindow.start != null
    ? Math.min(messageWindow.start, displayMessages.length)
    : defaultMessageWindowStart
  const visibleDisplayMessages = useMemo(
    () => displayMessages.slice(messageWindowStart),
    [displayMessages, messageWindowStart],
  )

  useLayoutEffect(() => {
    setMessageWindow((current) => {
      if (current.threadId !== conversation.threadId) {
        earlierMessageAnchor.current = null
        return {
          threadId: conversation.threadId,
          start: displayMessages.length > 0 ? defaultMessageWindowStart : null,
        }
      }
      if (current.start == null && displayMessages.length > 0) {
        return { ...current, start: defaultMessageWindowStart }
      }
      if (current.start != null && current.start > displayMessages.length) {
        return { ...current, start: defaultMessageWindowStart }
      }
      return current
    })
  }, [conversation.threadId, defaultMessageWindowStart, displayMessages.length])

  useLayoutEffect(() => {
    const anchor = earlierMessageAnchor.current
    if (!anchor || anchor.threadId !== conversation.threadId) return
    const pane = conversationPane.current
    if (pane) {
      pane.scrollTop = anchor.scrollTop + (pane.scrollHeight - anchor.scrollHeight)
    }
    if (anchor.trigger.isConnected) anchor.trigger.focus({ preventScroll: true })
    else pane?.focus({ preventScroll: true })
    earlierMessageAnchor.current = null
  }, [conversation.threadId, conversationPane, messageWindowStart])

  const loadEarlierMessages = useCallback((trigger: HTMLButtonElement) => {
    if (messageWindowStart <= 0) return
    const pane = conversationPane.current
    if (pane) {
      earlierMessageAnchor.current = {
        threadId: conversation.threadId,
        scrollHeight: pane.scrollHeight,
        scrollTop: pane.scrollTop,
        trigger,
      }
    }
    setMessageWindow({
      threadId: conversation.threadId,
      start: Math.max(0, messageWindowStart - MESSAGE_RENDER_BATCH_SIZE),
    })
  }, [conversation.threadId, conversationPane, messageWindowStart])

  const beginSend = useCallback((content: string, modeOverride?: AgentMode) => {
    const trimmed = content.trim()
    if (!trimmed || isRunning || !conversation.model) return
    const effectiveMode = modeOverride ?? conversation.mode

    const now = new Date().toISOString()
    if (!workspace.currentThreadId) {
      const nextConversation = buildEmptyConversation({
        now,
        model: draftConversation?.model ?? draftModel,
        mode: effectiveMode,
      })
      const payload = buildInitialPayload(nextConversation, trimmed)
      const requestMessage = payload.messages.at(0)
      if (!requestMessage) return
      const seededConversation: Conversation = {
        ...nextConversation,
        messages: [{
          id: requestMessage.id,
          role: 'user',
          content: requestMessage.content,
          createdAt: now,
          meta: { runId: payload.runId },
        }],
        activeRunId: payload.runId,
        runStatus: 'streaming',
        notice: undefined,
        approval: undefined,
        todos: [],
        serverState: {},
      }

      scrollConversationToBottomImmediately()
      setDraftConversation(seededConversation)
      void streamRun(nextConversation.threadId, payload, 'start', {
        target: 'draft',
        initialConversation: seededConversation,
      })
      setDraft('')
      return
    }

    const currentConversation = workspace.conversations.find((item) => item.threadId === workspace.currentThreadId)
    if (!currentConversation) return
    if (!currentConversation.isHydrated) {
      void hydrateConversation(currentConversation.threadId)
      return
    }
    if (currentConversation.runStatus === 'waiting_approval') {
      setWorkspace((state) => updateConversation(state, currentConversation.threadId, (item) => ({
        ...item,
        approval: item.approval ? { ...item.approval, error: t('请先处理当前审批后再发送新消息') } : item.approval,
        planInteraction: item.planInteraction
          ? { ...item.planInteraction, error: t('请先处理当前 Plan 请求后再发送新消息') }
          : item.planInteraction,
      })))
      return
    }

    const sendingConversation = currentConversation.mode === effectiveMode
      ? currentConversation
      : { ...currentConversation, mode: effectiveMode }
    const payload = buildInitialPayload(sendingConversation, trimmed)
    const requestMessage = payload.messages.at(0)
    if (!requestMessage) return
    scrollConversationToBottomImmediately()
    setWorkspace((state) => {
      return updateConversation(state, currentConversation.threadId, (item) => ({
        ...item,
        mode: effectiveMode,
        updatedAt: now,
        activeRunId: payload.runId,
        runStatus: 'streaming',
        notice: undefined,
        approval: undefined,
        todos: [],
        serverState: {},
        messages: [...item.messages, {
          id: requestMessage.id,
          role: 'user',
          content: requestMessage.content,
          createdAt: now,
          meta: { runId: payload.runId },
        }],
      }))
    })
    void streamRun(currentConversation.threadId, payload, 'start')
    setDraft('')
  }, [conversation.mode, conversation.model, draftConversation?.model, draftModel, hydrateConversation, isRunning, scrollConversationToBottomImmediately, streamRun, t, workspace.conversations, workspace.currentThreadId])

  useEffect(() => {
    if (!pendingResume) return
    const claimedConversation = workspace.conversations.find(
      (item) => item.threadId === pendingResume.threadId,
    )
    const isExpectedGroup = pendingResume.kind === 'tool'
      ? claimedConversation?.approval?.items.length === pendingResume.expectedInterruptIds.length
        && claimedConversation.approval.items.every(
          (item, index) => item.interruptId === pendingResume.expectedInterruptIds[index],
        )
      : claimedConversation?.planInteraction?.interruptId === pendingResume.expectedInterruptIds[0]
    const isClaimed = claimedConversation?.runStatus === 'streaming'
      && claimedConversation.activeRunId === pendingResume.payload.runId
      && (pendingResume.kind === 'tool'
        ? claimedConversation.approval?.submitted === true
        : claimedConversation.planInteraction?.submitted === true)
      && isExpectedGroup
    setPendingResume(null)
    if (!isClaimed || startedResumeRunIds.current.has(pendingResume.payload.runId)) return

    startedResumeRunIds.current.add(pendingResume.payload.runId)
    void streamRun(
      pendingResume.threadId,
      pendingResume.payload,
      'resume',
    ).finally(() => {
      startedResumeRunIds.current.delete(pendingResume.payload.runId)
    })
  }, [pendingResume, streamRun, workspace.conversations])

  const submitApproval = useCallback((
    expectedInterruptIds: readonly string[],
    finalDecision?: ApprovalSubmissionDecision,
  ) => {
    const authoritativeConversation = latestWorkspace.current.conversations.find(
      (item) => item.threadId === conversation.threadId,
    )
    if (
      !authoritativeConversation?.approval
      || authoritativeConversation.runStatus === 'streaming'
      || !workspace.currentThreadId
    ) return
    if (!matchesApprovalGroup(authoritativeConversation.approval, expectedInterruptIds)) {
      updateCurrent((item) => ({
        ...item,
        approval: item.approval
          ? { ...item.approval, error: t('当前审批已更新，请重新检查') }
          : item.approval,
      }))
      return
    }
    const completedApproval = withFinalApprovalDecision(
      authoritativeConversation.approval,
      finalDecision,
    )
    const completedConversation = {
      ...authoritativeConversation,
      approval: completedApproval,
    }
    const incomplete = completedApproval.items.some((item) => !item.decision)
    if (incomplete) {
      updateCurrent((item) => ({
        ...item,
        approval: item.approval ? { ...item.approval, error: t('请先处理所有待审批项') } : item.approval,
      }))
      return
    }

    let payload: ChatRequestPayload
    try {
      payload = buildResumePayload(
        completedConversation,
        expectedInterruptIds,
      )
    } catch (error) {
      updateCurrent((item) => ({
        ...item,
        approval: item.approval
          ? matchesApprovalGroup(item.approval, expectedInterruptIds)
            ? {
              ...completedApproval,
              error: conversationErrorMessage(error, 'approval_stale'),
            }
            : item.approval
          : item.approval,
      }))
      return
    }
    setWorkspace((state) => updateConversation(
      state,
      authoritativeConversation.threadId,
      (item) => {
        if (!matchesApprovalGroup(item.approval, expectedInterruptIds)) return item
        const prepared = prepareResumeSubmission(
          { ...item, approval: completedApproval },
          expectedInterruptIds,
        )
        return prepared === item
          ? item
          : { ...prepared, activeRunId: payload.runId }
      },
    ))
    setPendingResume({
      kind: 'tool',
      threadId: authoritativeConversation.threadId,
      payload,
      expectedInterruptIds: [...expectedInterruptIds],
    })
  }, [conversation.threadId, t, updateCurrent, workspace.currentThreadId])

  const submitPlanInteraction = useCallback(() => {
    const authoritative = latestWorkspace.current.conversations.find(
      (item) => item.threadId === conversation.threadId,
    )
    if (!authoritative?.planInteraction || authoritative.runStatus === 'streaming') return
    let payload: ChatRequestPayload
    try {
      payload = buildPlanResumePayload(authoritative)
    } catch (error) {
      const message = conversationErrorMessage(error, 'plan_submit_failed')
      updateCurrent((item) => ({
        ...item,
        planInteraction: item.planInteraction
          ? { ...item.planInteraction, error: message }
          : item.planInteraction,
      }))
      return
    }
    const interruptId = authoritative.planInteraction.interruptId
    setWorkspace((state) => updateConversation(state, authoritative.threadId, (item) => ({
      ...item,
      runStatus: 'streaming',
      activeRunId: payload.runId,
      planInteraction: item.planInteraction
        ? { ...item.planInteraction, submitted: true, error: undefined }
        : item.planInteraction,
    })))
    setPendingResume({
      kind: 'plan',
      threadId: authoritative.threadId,
      payload,
      expectedInterruptIds: [interruptId],
    })
  }, [conversation.threadId, updateCurrent])

  const abandonPlanInteraction = useCallback((threadId: string) => {
    const authoritative = latestWorkspace.current.conversations.find(
      (item) => item.threadId === threadId,
    )
    if (!authoritative?.planInteraction || authoritative.runStatus === 'streaming') return
    let payload: ChatRequestPayload
    try {
      payload = buildPlanAbandonPayload(authoritative)
    } catch (error) {
      updateCurrent((item) => ({
        ...item,
        planInteraction: item.planInteraction
          ? {
              ...item.planInteraction,
              error: conversationErrorMessage(error, 'plan_submit_failed'),
            }
          : item.planInteraction,
      }))
      return
    }
    const interruptId = authoritative.planInteraction.interruptId
    setWorkspace((state) => updateConversation(state, threadId, (item) => ({
      ...item,
      mode: 'default',
      runStatus: 'streaming',
      activeRunId: payload.runId,
      planInteraction: item.planInteraction
        ? { ...item.planInteraction, submitted: true, error: undefined }
        : item.planInteraction,
    })))
    setPendingResume({
      kind: 'plan',
      threadId,
      payload,
      expectedInterruptIds: [interruptId],
    })
  }, [updateCurrent])

  const changeApproval = useCallback((
    threadId: string,
    updater: (approval: ApprovalState) => ApprovalState,
  ) => {
    setWorkspace((state) => updateConversation(state, threadId, (item) => (
      item.approval
        ? { ...item, approval: updater(item.approval) }
        : item
    )))
  }, [])

  const changePlanInteraction = useCallback((
    threadId: string,
    updater: (interaction: PlanInteraction) => PlanInteraction,
  ) => {
    setWorkspace((state) => updateConversation(state, threadId, (item) => (
      item.planInteraction
        ? { ...item, planInteraction: updater(item.planInteraction) }
        : item
    )))
  }, [])

  const {
    dialog,
    dialogPending,
    dialogError,
    closeDialog,
    confirmDialog,
    selectConversation,
    newConversation,
    pinConversation,
    pinPendingThreadIds,
    renameConversation,
    deleteConversation,
    requestDisablePlan,
  } = useConversationManagement({
    workspace,
    conversation,
    setWorkspace,
    setDraft,
    setDraftConversation,
    setDraftModel,
    catchUpDetachedConversation,
    abandonPlanInteraction,
    cancelActiveRun,
    detachThreadStream,
    getActiveThreadId,
    hasActiveStream,
    isActiveThread,
    onToast: pushToast,
    onConversationBoundary: localAttachments.clearAttachments,
  })

  const setAgentMode = useCallback((mode: AgentMode) => {
    if (mode === conversation.mode) return
    if (!workspace.currentThreadId) {
      setDraftConversation((current) => ({ ...(current ?? conversation), mode }))
      return
    }
    setWorkspace((state) => updateConversation(
      state,
      workspace.currentThreadId,
      (current) => ({ ...current, mode }),
    ))
  }, [conversation, workspace.currentThreadId])

  const exitPlanMode = useCallback(() => {
    if (isRunning || conversation.mode !== 'plan') return
    if (conversation.planInteraction) {
      requestDisablePlan(conversation.threadId)
      return
    }
    setAgentMode('default')
    pushToast('info', t('已关闭 Plan'))
  }, [conversation.mode, conversation.planInteraction, conversation.threadId, isRunning, pushToast, requestDisablePlan, setAgentMode, t])

  const stop = async () => {
    if (!hasActiveStream()) return
    try {
      const cancelled = await cancelActiveRun()
      pushToast('info', cancelled ? t('任务已停止') : t('任务已经结束'))
    } catch {
      pushToast('error', t('停止任务失败，请重试'))
    }
  }

  const selectModel = (model: string) => {
    if (!workspace.currentThreadId) {
      setDraftModel(model)
      setDraftConversation((current) => (current ? { ...current, model } : current))
      setModelPickerOpen(false)
      return
    }
    updateCurrent((item) => ({ ...item, model }))
    setModelPickerOpen(false)
  }

  const send = () => {
    const submission = parseComposerSubmission(draft)
    if (submission.kind === 'plan-off-unsupported') {
      pushToast('info', t('请点击输入框中的 Plan 按钮关闭'))
      return
    }
    if (submission.kind === 'plan-enable') {
      setAgentMode('plan')
      setDraft('')
      pushToast('info', conversation.mode === 'plan' ? t('Plan 已开启') : t('已开启 Plan'))
      return
    }
    if (submission.kind === 'plan-message') {
      beginSend(submission.content, 'plan')
      return
    }
    beginSend(submission.content)
  }

  // Portal 对话框打开时整块工作区退出辅助技术与键盘路径，只保留最上层操作
  const portalModalActive = settingsOpen || dialog != null

  return (
    <div
      ref={appShell}
      className={`app-shell ${taskDrawer.open ? 'has-drawer' : ''}`}
      id="top"
      data-sidebar-mode={navigation.mode}
      aria-hidden={portalModalActive || undefined}
      inert={portalModalActive || undefined}
    >
      <Sidebar
        workspace={workspace}
        historyConversations={historyConversations}
        historyDayRanges={historyDayRanges}
        historyQuery={historyQuery}
        onHistoryQueryChange={setHistoryQuery}
        isHistorySearchActive={isHistorySearchActive}
        isHistorySearching={isHistorySearching}
        mode={navigation.mode}
        settledMode={navigation.settledMode}
        overlayOpen={navigation.overlayOpen}
        wideInteractive={navigation.wideInteractive}
        railInteractive={navigation.railInteractive}
        onToggleMode={navigation.toggleDesktopMode}
        onRequestExpanded={navigation.requestExpanded}
        onCloseOverlay={navigation.closeOverlay}
        onNew={newConversation}
        onSelect={selectConversation}
        onPin={pinConversation}
        pinPendingThreadIds={pinPendingThreadIds}
        onRename={renameConversation}
        onDelete={deleteConversation}
        hasMore={historyCursor != null}
        onLoadMore={loadMoreHistory}
        isLoadingMore={isHistoryLoadingMore}
        loadMoreError={historyLoadError ?? undefined}
        onRetryLoadMore={retryHistoryLoad}
        user={user}
        onOpenSettings={(restoreFocusTo) => {
          taskDrawer.close()
          settingsRestoreFocus.current = restoreFocusTo ?? null
          setSettingsOpen(true)
        }}
        onLogout={onLogout}
        backgroundInert={taskDrawer.modalActive || portalModalActive}
      />
      <main
        data-workspace-layout-target="main"
        id="main-content"
        className="workspace-main"
        aria-hidden={(navigation.mode === 'overlay' && navigation.overlayOpen) || taskDrawer.modalActive || undefined}
        inert={(navigation.mode === 'overlay' && navigation.overlayOpen) || taskDrawer.modalActive || undefined}
      >
        <WorkspaceHeader
          conversationTitle={conversation.title}
          drawerOpen={taskDrawer.open}
          todoCount={conversation.todos.length}
          drawerToggleRef={taskDrawer.toggleRef}
          overlayTriggerRef={navigation.overlayTriggerRef}
          onOpenOverlay={navigation.openOverlay}
          onToggleDrawer={taskDrawer.toggle}
        />
        <ConversationViewport
          conversation={conversation}
          entries={visibleDisplayMessages}
          hasEarlierMessages={messageWindowStart > 0}
          childToolsByRunId={childToolsByRunId}
          paneRef={conversationPane}
          messageEndRef={messageEnd}
          historyStatus={historyBootstrapStatus}
          isHistoryBootstrapped={isHistoryBootstrapped}
          isInitialHistoryUnavailable={isInitialHistoryUnavailable}
          isHydrating={isConversationHydrating}
          isHydrationFailed={isConversationHydrationFailed}
          isRunning={isRunning}
          showScrollToBottom={showScrollToBottom}
          fadeScrollToBottom={fadeScrollToBottom}
          onScroll={handleConversationScroll}
          onUserScrollIntent={markUserScrollIntent}
          onRetryHistory={retryHistoryBootstrap}
          onRetryHydration={() => void hydrateConversation(conversation.threadId)}
          onLoadEarlierMessages={loadEarlierMessages}
          onScrollToBottom={scrollConversationToBottom}
          onScrollToBottomPointerEnter={pauseScrollToBottomFade}
          onScrollToBottomPointerLeave={resumeScrollToBottomFade}
          onScrollToBottomFocus={focusScrollToBottom}
          onScrollToBottomBlur={blurScrollToBottom}
        />
        <Composer
          value={draft}
          isRunning={isRunning}
          canStop={Boolean(conversation.threadId)}
          stopPending={cancelPendingRunId === conversation.activeRunId}
          isHydrating={isConversationHydrating}
          hero={showConversationHero ? <EmptyConversationBrand /> : undefined}
          takeover={conversation.approval && !conversation.approval.submitted
            ? (
              <ApprovalCard
                key={`${conversation.threadId}:${conversation.approval.items[0]?.interruptId ?? ''}`}
                conversation={conversation}
                onChange={(updater) => changeApproval(conversation.threadId, updater)}
                onSubmit={submitApproval}
              />
            )
            : conversation.planInteraction?.kind === 'questions'
              && !conversation.planInteraction.submitted
              ? (
              <PlanQuestionComposer
                threadId={conversation.threadId}
                interaction={conversation.planInteraction}
                onChange={(updater) => changePlanInteraction(
                  conversation.threadId,
                  (current) => current.kind === 'questions'
                    ? updater(current as PlanQuestionState)
                    : current,
                )}
                onSubmit={submitPlanInteraction}
                onAbandon={exitPlanMode}
              />
              )
              : conversation.planInteraction?.kind === 'review'
                && !conversation.planInteraction.submitted
                ? (
                <PlanReviewCard
                  key={`${conversation.threadId}:${conversation.planInteraction.interruptId}`}
                  threadId={conversation.threadId}
                  interaction={conversation.planInteraction}
                  onChange={(updater) => changePlanInteraction(
                    conversation.threadId,
                    (current) => current.kind === 'review'
                      ? updater(current as PlanReviewState)
                      : current,
                  )}
                  onSubmit={submitPlanInteraction}
                />
                )
                : undefined}
          modelControl={(
            <ComposerModelPicker
              model={conversation.model}
              modelIds={modelIds}
              defaultModelId={defaultModelId}
              modelDisplayName={modelDisplayName}
              status={modelCatalogStatus}
              open={isModelPickerOpen}
              onOpenChange={setModelPickerOpen}
              onSelectModel={selectModel}
              onRetry={retryModelCatalog}
            />
          )}
          planActive={conversation.mode === 'plan'}
          planLocked={isRunning}
          attachments={localAttachments.attachments}
          attachmentError={localAttachments.error}
          disabledReason={isConversationHydrationFailed
            ? t('会话加载失败，请先重试')
            : modelCatalogStatus === 'loading'
              ? t('正在加载模型…')
              : modelCatalogStatus === 'error'
                ? t('模型加载失败，请先重试')
                : modelCatalogStatus === 'empty'
                  ? t('未配置可用模型，请联系管理员或重试')
                : !isHistoryBootstrapped || historyBootstrapStatus === 'loading'
                  ? t('正在加载历史会话…')
                  : isInitialHistoryUnavailable
                    ? t('历史会话加载失败，请先重试')
                    : undefined}
          onChange={setDraft}
          onSend={send}
          onStop={() => void stop()}
          onExitPlan={exitPlanMode}
          onAddAttachments={localAttachments.addFiles}
          onRemoveAttachment={localAttachments.removeAttachment}
          onScrollConversation={scrollConversationBy}
        />
      </main>
      {taskDrawer.modalActive && <button type="button" className="task-drawer-scrim" aria-label={t('关闭任务抽屉遮罩')} onClick={taskDrawer.close} />}
      <ErrorBoundary
        resetKey={`${conversation.threadId || 'draft'}:${taskDrawer.open ? 'open' : 'closed'}`}
        fallback={({ reset }) => taskDrawer.open ? (
          <aside id="task-drawer" className="task-drawer is-open task-drawer-error" aria-label={t('任务抽屉渲染错误')}>
            <WorkspaceStatus kind="error" title={t('任务抽屉无法显示')} description={t('对话内容未受影响，可以重试或关闭抽屉')} onRetry={reset} compact />
            <Button onClick={taskDrawer.close}>{t('关闭抽屉')}</Button>
          </aside>
        ) : null}
      >
        <TaskDrawer
          conversation={conversation}
          open={taskDrawer.open}
          onClose={taskDrawer.close}
          focusOnOpen={taskDrawer.modalActive}
        />
      </ErrorBoundary>
      <WorkspaceDialogs
        dialog={dialog}
        pending={dialogPending}
        error={dialogError}
        onConfirm={confirmDialog}
        onCancel={closeDialog}
      />
      <SettingsDialog
        open={settingsOpen}
        user={user}
        themePreference={theme.preference}
        restoreFocusTo={settingsRestoreFocus.current}
        onThemePreferenceChange={theme.selectPreference}
        onClose={() => setSettingsOpen(false)}
      />
    </div>
  )
}
