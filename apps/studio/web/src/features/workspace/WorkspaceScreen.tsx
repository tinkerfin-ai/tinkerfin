import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'

import type { AgentMode, ChatRequestPayload } from '../../api/conversation/types'
import type { AuthUser } from '../../api/auth/types'
import { Button, ErrorBoundary } from '../../components/ui'
import type { ToastKind } from '../../components/ui/ToastViewport'
import { Composer } from '../conversation/components/Composer'
import { Sidebar } from './components/Sidebar'
import { TaskDrawer } from './components/TaskDrawer'
import { ConversationViewport } from './components/ConversationViewport'
import { WorkspaceDialogs } from './components/WorkspaceDialogs'
import { WorkspaceHeader } from './components/WorkspaceHeader'
import { WorkspaceStatus } from './components/WorkspaceStatus'
import { useWorkspaceNavigation } from './useWorkspaceNavigation'
import { useModelCatalog } from './useModelCatalog'
import { useWorkspaceHistory } from './useWorkspaceHistory'
import { useConversationScroll } from './useConversationScroll'
import { useConversationManagement } from './useConversationManagement'
import { useTaskDrawerState } from './useTaskDrawerState'
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
  WorkspaceState,
} from '../../types'

interface PendingResume {
  kind: 'tool' | 'plan'
  threadId: string
  payload: ChatRequestPayload
  expectedInterruptIds: readonly string[]
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
  const [workspace, setWorkspace] = useState<WorkspaceState>(createEmptyWorkspace)
  const [draftConversation, setDraftConversation] = useState<Conversation | null>(null)
  const [draftModel, setDraftModel] = useState('')
  const [draft, setDraft] = useState('')
  const [isModelPickerOpen, setModelPickerOpen] = useState(false)
  const [isAgentPresetPickerOpen, setAgentPresetPickerOpen] = useState(false)
  const [pendingResume, setPendingResume] = useState<PendingResume | null>(null)
  const navigation = useWorkspaceNavigation()
  const {
    status: modelCatalogStatus,
    modelIds,
    defaultModelId,
    displayName: modelDisplayName,
    retry: retryModelCatalog,
  } = useModelCatalog()
  const appShell = useRef<HTMLDivElement>(null)
  const latestWorkspace = useRef(workspace)
  const startedResumeRunIds = useRef(new Set<string>())
  const reattachedRunIds = useRef(new Set<string>())
  latestWorkspace.current = workspace
  const pushToast = onToast

  useEffect(() => {
    if (defaultModelId) setDraftModel((current) => current || defaultModelId)
  }, [defaultModelId])

  const {
    cancelActiveRun,
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
    historyCursor,
    isHistoryLoadingMore,
    historyLoadError,
    isHistoryBootstrapped,
    historyBootstrapStatus,
    hydrationState,
    historyLoadSentinel,
    loadMoreHistory,
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
  const taskDrawer = useTaskDrawerState({
    threadId: conversation.threadId,
    todoCount: conversation.todos.length,
  })
  const sidebarWorkspace = useMemo<WorkspaceState>(() => (
    workspace.currentThreadId
      ? workspace
      : { ...workspace, conversations: [conversation, ...workspace.conversations] }
  ), [conversation, workspace])
  const isRunning = conversation.runStatus === 'streaming'
  const {
    paneRef: conversationPane,
    messageEndRef: messageEnd,
    showScrollToBottom,
    handleScroll: handleConversationScroll,
    scrollToBottomImmediately: scrollConversationToBottomImmediately,
    markUserScrollIntent,
    scrollToBottom: scrollConversationToBottom,
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
  const hiddenApprovalToolCallIds = useMemo(() => {
    if (!conversation.approval || conversation.approval.submitted) return new Set<string>()
    return new Set(
      conversation.approval.items
        .map((item) => item.toolCallId)
        .filter((toolCallId): toolCallId is string => Boolean(toolCallId)),
    )
  }, [conversation.approval])

  const updateCurrent = useCallback((updater: (item: Conversation) => Conversation) => {
    setWorkspace((state) => updateConversation(state, state.currentThreadId, updater))
  }, [])

  useLayoutEffect(() => {
    const shell = appShell.current
    const composer = shell?.querySelector<HTMLElement>('.composer-wrap')
    if (!shell || !composer) return
    const measure = () => shell.style.setProperty(
      '--composer-height',
      `${Math.max(80, composer.getBoundingClientRect().height)}px`,
    )
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(measure)
    observer.observe(composer)
    return () => observer.disconnect()
  }, [])

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
    if (hasActiveStream() || reattachedRunIds.current.has(active.payload.runId)) return

    reattachedRunIds.current.add(active.payload.runId)
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

  const displayMessages = useMemo(() => conversation.messages
    .filter((message) => {
      if (message.role !== 'tool') return true
      if (message.meta?.toolName === 'write_todos') return false
      const toolCallId = message.meta?.toolCallId
      if (toolCallId && hiddenApprovalToolCallIds.has(toolCallId)) return false
      if (message.meta?.sourceAgentName) return false
      if (message.meta?.toolName === 'task') {
        return message.meta.status !== 'running' && !message.meta.subRunId
      }
      return true
    })
    .reduce<Array<{ type: 'message'; message: Conversation['messages'][number] } | { type: 'tools'; messages: Conversation['messages'] }>>((groups, message) => {
      const previous = groups.at(-1)
      if (message.role === 'tool' && message.meta?.batchId && previous?.type === 'tools' && previous.messages[0]?.meta?.batchId === message.meta.batchId) {
        previous.messages.push(message)
      } else if (message.role === 'tool' && message.meta?.batchId) groups.push({ type: 'tools', messages: [message] })
      else groups.push({ type: 'message', message })
      return groups
    }, []), [conversation.messages, hiddenApprovalToolCallIds])

  const beginSend = useCallback((content: string) => {
    const trimmed = content.trim()
    if (!trimmed || isRunning || !conversation.model) return

    const now = new Date().toISOString()
    if (!workspace.currentThreadId) {
      const nextConversation = buildEmptyConversation({
        now,
        model: draftConversation?.model ?? draftModel,
        mode: draftConversation?.mode ?? conversation.mode,
      })
      const payload = buildInitialPayload(nextConversation, trimmed)
      const seededConversation: Conversation = {
        ...nextConversation,
        activeRunId: payload.runId,
        runStatus: 'streaming',
        notice: undefined,
        approval: undefined,
        todos: [],
        plan: null,
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
        approval: item.approval ? { ...item.approval, error: '请先处理当前审批后再发送新消息。' } : item.approval,
        planInteraction: item.planInteraction
          ? { ...item.planInteraction, error: '请先处理当前 Plan 请求后再发送新消息。' }
          : item.planInteraction,
      })))
      return
    }

    const payload = buildInitialPayload(currentConversation, trimmed)
    scrollConversationToBottomImmediately()
    setWorkspace((state) => {
      return updateConversation(state, currentConversation.threadId, (item) => ({
        ...item,
        updatedAt: now,
        activeRunId: payload.runId,
        runStatus: 'streaming',
        notice: undefined,
        approval: undefined,
        todos: [],
        plan: null,
        serverState: {},
      }))
    })
    void streamRun(currentConversation.threadId, payload, 'start')
    setDraft('')
  }, [conversation.mode, conversation.model, draftConversation, draftModel, hydrateConversation, isRunning, scrollConversationToBottomImmediately, streamRun, workspace.conversations, workspace.currentThreadId])

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

  const submitApproval = useCallback((expectedInterruptIds: readonly string[]) => {
    const authoritativeConversation = latestWorkspace.current.conversations.find(
      (item) => item.threadId === conversation.threadId,
    )
    if (
      !authoritativeConversation?.approval
      || authoritativeConversation.runStatus === 'streaming'
      || !workspace.currentThreadId
    ) return
    const incomplete = authoritativeConversation.approval.items.some((item) => !item.decision)
    if (incomplete) {
      updateCurrent((item) => ({
        ...item,
        approval: item.approval ? { ...item.approval, error: '请先处理所有待审批项。' } : item.approval,
      }))
      return
    }

    let payload: ChatRequestPayload
    try {
      payload = buildResumePayload(
        authoritativeConversation,
        expectedInterruptIds,
      )
    } catch {
      return
    }
    setWorkspace((state) => updateConversation(
      state,
      authoritativeConversation.threadId,
      (item) => {
        const prepared = prepareResumeSubmission(item, expectedInterruptIds)
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
  }, [conversation.threadId, updateCurrent, workspace.currentThreadId])

  const submitPlanInteraction = useCallback(() => {
    const authoritative = latestWorkspace.current.conversations.find(
      (item) => item.threadId === conversation.threadId,
    )
    if (!authoritative?.planInteraction || authoritative.runStatus === 'streaming') return
    let payload: ChatRequestPayload
    try {
      payload = buildPlanResumePayload(authoritative)
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Plan 请求无法提交'
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
    const payload = buildPlanAbandonPayload(authoritative)
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
  }, [])

  const send = () => beginSend(draft.trim())

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
  })

  const selectAgentMode = useCallback((mode: AgentMode) => {
    if (isRunning || mode === conversation.mode) return
    if (mode === 'default' && conversation.planInteraction) {
      requestDisablePlan(conversation.threadId)
      return
    }
    if (!workspace.currentThreadId) {
      setDraftConversation((current) => ({ ...(current ?? conversation), mode }))
      return
    }
    setWorkspace((state) => updateConversation(
      state,
      workspace.currentThreadId,
      (current) => ({ ...current, mode }),
    ))
  }, [conversation, isRunning, requestDisablePlan, workspace.currentThreadId])

  const stop = async () => {
    if (!hasActiveStream()) return
    try {
      const cancelled = await cancelActiveRun()
      pushToast('info', cancelled ? '任务已停止' : '任务已经结束')
    } catch {
      pushToast('error', '停止任务失败，请重试')
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

  return (
    <div
      ref={appShell}
      className={`app-shell ${taskDrawer.open ? 'has-drawer' : ''}`}
      id="top"
      data-sidebar-mode={navigation.mode}
      onTransitionEnd={(event) => {
        if (event.target === event.currentTarget) {
          navigation.handleShellTransitionEnd(event.propertyName)
        }
      }}
    >
      <Sidebar
        workspace={sidebarWorkspace}
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
        onRename={renameConversation}
        onDelete={deleteConversation}
        hasMore={historyCursor != null}
        onLoadMore={loadMoreHistory}
        isLoadingMore={isHistoryLoadingMore}
        loadMoreError={historyLoadError ?? undefined}
        onRetryLoadMore={() => loadMoreHistory(true)}
        loadMoreSentinelRef={historyLoadSentinel}
        user={user}
        onLogout={onLogout}
        backgroundInert={taskDrawer.modalActive}
      />
      <main
        id="main-content"
        className="workspace-main"
        aria-hidden={(navigation.mode === 'overlay' && navigation.overlayOpen) || taskDrawer.modalActive || undefined}
        inert={(navigation.mode === 'overlay' && navigation.overlayOpen) || taskDrawer.modalActive || undefined}
      >
        <WorkspaceHeader
          conversationTitle={conversation.title}
          model={conversation.model}
          modelIds={modelIds}
          defaultModelId={defaultModelId}
          modelDisplayName={modelDisplayName}
          modelCatalogStatus={modelCatalogStatus}
          isModelPickerOpen={isModelPickerOpen}
          onModelPickerOpenChange={(open) => {
            setModelPickerOpen(open)
            if (open) setAgentPresetPickerOpen(false)
          }}
          onSelectModel={selectModel}
          mode={conversation.mode}
          isAgentPresetPickerOpen={isAgentPresetPickerOpen}
          onAgentPresetPickerOpenChange={(open) => {
            setAgentPresetPickerOpen(open)
            if (open) setModelPickerOpen(false)
          }}
          onSelectAgentMode={selectAgentMode}
          agentPresetDisabled={isRunning || conversation.approval != null}
          drawerOpen={taskDrawer.open}
          todoCount={conversation.todos.length}
          drawerToggleRef={taskDrawer.toggleRef}
          overlayTriggerRef={navigation.overlayTriggerRef}
          onOpenOverlay={navigation.openOverlay}
          onToggleDrawer={taskDrawer.toggle}
          onRetryModels={retryModelCatalog}
        />
        <ConversationViewport
          conversation={conversation}
          entries={displayMessages}
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
          onScroll={handleConversationScroll}
          onUserScrollIntent={markUserScrollIntent}
          onRetryHistory={retryHistoryBootstrap}
          onRetryHydration={() => void hydrateConversation(conversation.threadId)}
          onChangeApproval={(updater) => changeApproval(conversation.threadId, updater)}
          onSubmitApproval={submitApproval}
          onChangePlan={(updater) => changePlanInteraction(conversation.threadId, updater)}
          onSubmitPlan={submitPlanInteraction}
          onScrollToBottom={scrollConversationToBottom}
        />
        <Composer
          value={draft}
          isRunning={isRunning}
          canStop={Boolean(conversation.threadId)}
          isHydrating={isConversationHydrating}
          disabledReason={isConversationHydrationFailed
            ? '会话加载失败，请先重试'
            : modelCatalogStatus === 'loading'
              ? '正在加载模型…'
              : modelCatalogStatus === 'error'
                ? '模型加载失败，请先重试'
                : !isHistoryBootstrapped || historyBootstrapStatus === 'loading'
                  ? '正在加载历史会话…'
                  : isInitialHistoryUnavailable
                    ? '历史会话加载失败，请先重试'
                    : undefined}
          onChange={setDraft}
          onSend={send}
          onStop={() => void stop()}
        />
      </main>
      {taskDrawer.modalActive && <button type="button" className="task-drawer-scrim" aria-label="关闭任务抽屉遮罩" onClick={taskDrawer.close} />}
      <ErrorBoundary
        resetKey={`${conversation.threadId || 'draft'}:${taskDrawer.open ? 'open' : 'closed'}`}
        fallback={({ reset }) => taskDrawer.open ? (
          <aside id="task-drawer" className="task-drawer is-open task-drawer-error" aria-label="任务抽屉渲染错误">
            <WorkspaceStatus kind="error" title="任务抽屉无法显示" description="对话内容未受影响，可以重试或关闭抽屉。" onRetry={reset} compact />
            <Button onClick={taskDrawer.close}>关闭抽屉</Button>
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
    </div>
  )
}
