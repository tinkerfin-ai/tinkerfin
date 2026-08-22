import { ArrowDown, Check, ChevronDown, GitBranch, Menu, PanelRight } from 'lucide-react'
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'

import {
  deleteConversation as deleteConversationApi,
  fetchConversationHistoryDetail,
  fetchConversationHistoryList,
  patchConversation,
  type ConversationHistoryDetail,
  type ConversationHistoryListItem,
} from '../../api/conversation/history'
import type { AgentMode, ChatRequestPayload } from '../../api/conversation/types'
import type { AuthUser } from '../../api/auth/types'
import { fetchModelCatalog } from '../../api/models/client'
import type { AgentModelCatalogItem } from '../../api/models/types'
import { ActivityDots } from '../../components/ActivityDots'
import { ApprovalCard } from '../../components/ApprovalCard'
import { Composer } from '../../components/Composer'
import { EmptyConversation } from '../../components/EmptyConversation'
import { ConversationNotice, MessageBlock, ToolCallBatch } from '../../components/MessageBlock'
import { ListboxPicker } from '../../components/ListboxPicker'
import { ModalDialog } from '../../components/ModalDialog'
import { OverflowMarquee } from '../../components/OverflowMarquee'
import { PlanQuestionCard } from '../../components/PlanQuestionCard'
import { PlanReviewCard } from '../../components/PlanReviewCard'
import { Sidebar } from '../../components/Sidebar'
import { TaskDrawer } from '../../components/TaskDrawer'
import { ThemePicker } from '../../components/ThemePicker'
import type { ToastKind } from '../../components/ToastViewport'
import {
  buildInitialPayload,
  buildPlanAbandonPayload,
  buildPlanResumePayload,
  buildResumePayload,
  prepareResumeSubmission,
  restoreConversationFromHistory,
} from '../conversation/agui'
import { useConversationStreamController } from '../conversation/stream/useConversationStreamController'
import {
  clearActiveRunSession,
  readActiveRunSession,
} from '../conversation/stream/activeRunSession'
import {
  buildEmptyConversation,
  createEmptyWorkspace,
  createNewConversation,
  removeConversation,
  updateConversation,
  upsertConversation,
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

const AGENT_MODES = ['default', 'plan'] as const satisfies readonly AgentMode[]
const HISTORY_PAGE_SIZE = 5
const HISTORY_LOAD_DEBOUNCE_MS = 300
const CONVERSATION_SCROLL_KEY_PREFIX = 'tinkerfin:conversation-scroll:'
const TASK_DRAWER_PREFERENCE_KEY_PREFIX = 'tinkerfin:task-drawer:'

const conversationScrollKey = (threadId: string) => `${CONVERSATION_SCROLL_KEY_PREFIX}${threadId}`

const readConversationScrollTop = (threadId: string): number | null => {
  try {
    const storedValue = window.sessionStorage.getItem(conversationScrollKey(threadId))
    if (storedValue == null) return null
    const value = Number(storedValue)
    return Number.isFinite(value) && value >= 0 ? value : null
  } catch {
    return null
  }
}

const writeConversationScrollTop = (threadId: string, scrollTop: number): void => {
  try {
    window.sessionStorage.setItem(conversationScrollKey(threadId), String(Math.max(0, scrollTop)))
  } catch {
    // 浏览器禁用会话存储时保留原有滚动行为
  }
}

const taskDrawerPreferenceKey = (threadId: string) => `${TASK_DRAWER_PREFERENCE_KEY_PREFIX}${threadId}`

const readTaskDrawerPreference = (threadId: string): boolean | null => {
  try {
    const storedValue = window.sessionStorage.getItem(taskDrawerPreferenceKey(threadId))
    if (storedValue === 'open') return true
    if (storedValue === 'closed') return false
    return null
  } catch {
    return null
  }
}

const writeTaskDrawerPreference = (threadId: string, isOpen: boolean): void => {
  try {
    window.sessionStorage.setItem(taskDrawerPreferenceKey(threadId), isOpen ? 'open' : 'closed')
  } catch {
    // 浏览器禁用会话存储时保留当前页面内的抽屉行为
  }
}

type AppDialog =
  | { kind: 'rename'; threadId: string; initialValue: string; restoreFocusTo?: HTMLElement | null }
  | { kind: 'delete'; threadId: string; title: string; isRunning: boolean; restoreFocusTo?: HTMLElement | null }
  | { kind: 'detach-select'; threadId: string }
  | { kind: 'detach-new' }
  | { kind: 'disable-plan'; threadId: string }

interface PendingResume {
  kind: 'tool' | 'plan'
  threadId: string
  payload: ChatRequestPayload
  expectedInterruptIds: readonly string[]
}

const sortConversations = (conversations: Conversation[]) =>
  [...conversations].sort((a, b) => Date.parse(b.updatedAt) - Date.parse(a.updatedAt))

const historyStatusToRunStatus = (status: string): Conversation['runStatus'] => {
  switch (status) {
    case 'running':
      return 'detached'
    case 'waiting_approval':
      return 'waiting_approval'
    case 'error':
      return 'error'
    default:
      return 'idle'
  }
}

const conversationFromHistoryItem = (
  item: ConversationHistoryListItem,
  fallbackModel: string,
): Conversation => ({
  threadId: item.threadId,
  title: item.title,
  pinned: item.pinned,
  updatedAt: item.updatedAt,
  model: item.lastModel ?? fallbackModel,
  mode: 'default',
  messages: [],
  todos: [],
  plan: null,
  runStatus: historyStatusToRunStatus(item.status),
  activeRunId: item.lastRunId,
  serverState: {},
  lastSeq: item.lastSeq,
  isHydrated: false,
})

const mergeHistoryConversations = (
  current: Conversation[],
  items: ConversationHistoryListItem[],
  fallbackModel: string,
): Conversation[] => {
  // 并集语义：按 threadId 去重，已存在则用后端字段刷新摘要、保留在跑状态；
  // 新增则加入。既适用于首页加载（current 为空 → 全量），也适用于滚动分页追加（保留已有页）
  const byId = new Map(current.map((conversation) => [conversation.threadId, conversation]))
  for (const item of items) {
    const summary = conversationFromHistoryItem(item, fallbackModel)
    const existing = byId.get(item.threadId)
    byId.set(
      item.threadId,
      existing
        ? {
            ...existing,
            title: summary.title,
            pinned: summary.pinned,
            updatedAt: summary.updatedAt,
            model: summary.model,
            activeRunId: existing.runStatus === 'streaming'
              ? existing.activeRunId
              : summary.activeRunId,
            // `Conversation.lastSeq` 是浏览器已归约游标；列表摘要可能指向尚未拉取的事件，
            // 因此只允许历史详情初始化该字段
            lastSeq: existing.isHydrated ? existing.lastSeq : summary.lastSeq,
            runStatus: existing.runStatus === 'streaming' ? existing.runStatus : summary.runStatus,
          }
        : summary,
    )
  }
  return sortConversations([...byId.values()])
}

function ModelOption({ label, selected }: { label: string; selected: boolean }) {
  return (
    <>
      <OverflowMarquee className="model-option-label">{label}</OverflowMarquee>
      <span className="model-option-check" aria-hidden="true">{selected && <Check size={14} />}</span>
    </>
  )
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
  const [models, setModels] = useState<AgentModelCatalogItem[]>([])
  const [modelCatalogLoaded, setModelCatalogLoaded] = useState(false)
  const [draftModel, setDraftModel] = useState('')
  const [draft, setDraft] = useState('')
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [isModelPickerOpen, setModelPickerOpen] = useState(false)
  const [isAgentPresetPickerOpen, setAgentPresetPickerOpen] = useState(false)
  const [showScrollToBottom, setShowScrollToBottom] = useState(false)
  const [fadeScrollToBottom, setFadeScrollToBottom] = useState(false)
  const isScrollToBottomHovered = useRef(false)
  const [historyCursor, setHistoryCursor] = useState<string | null>(null)
  const [isHistoryLoadingMore, setHistoryLoadingMore] = useState(false)
  const [historyLoadError, setHistoryLoadError] = useState<string | null>(null)
  const [isHistoryBootstrapped, setHistoryBootstrapped] = useState(false)
  const [hydrationState, setHydrationState] = useState<{
    threadId: string
    status: 'loading' | 'failed'
  } | null>(null)
  const [pendingResume, setPendingResume] = useState<PendingResume | null>(null)
  const [dialog, setDialog] = useState<AppDialog | null>(null)
  const [dialogPending, setDialogPending] = useState(false)
  const [dialogError, setDialogError] = useState<string | undefined>()
  const hasHistoryBootstrapStarted = useRef(false)
  const historyBootstrapAbortController = useRef<AbortController | null>(null)
  const historyLoadingRef = useRef(false)
  const historyLoadTimer = useRef<number | null>(null)
  const historyInFlightCursor = useRef<string | null>(null)
  const loadedHistoryCursors = useRef(new Set<string>())
  const historyAbortController = useRef<AbortController | null>(null)
  const conversationPane = useRef<HTMLElement>(null)
  const appShell = useRef<HTMLDivElement>(null)
  const historyLoadSentinel = useRef<HTMLDivElement>(null)
  const followLatest = useRef(true)
  const pendingImmediateScroll = useRef(false)
  const scrollingToBottom = useRef(false)
  const messageEnd = useRef<HTMLDivElement>(null)
  const todoTracker = useRef<{ threadId: string; count: number }>({ threadId: '', count: 0 })
  const scrollButtonFadeTimeout = useRef<number | null>(null)
  const scrollButtonHideTimeout = useRef<number | null>(null)
  const followScrollFrame = useRef<number | null>(null)
  const scrollMeasureFrame = useRef<number | null>(null)
  const latestWorkspace = useRef(workspace)
  const initialThreadId = useRef(readThreadFromLocation())
  const pendingConversationScroll = useRef<{ threadId: string; scrollTop: number | null } | null>(null)
  const prefetchedHistoryDetails = useRef(new Map<string, ConversationHistoryDetail>())
  const hydrationRequests = useRef(new Map<string, AbortController>())
  const startedResumeRunIds = useRef(new Set<string>())
  const reattachedRunIds = useRef(new Set<string>())
  latestWorkspace.current = workspace
  const pushToast = onToast
  const modelIds = useMemo(() => models.map((model) => model.modelId), [models])
  const defaultModelId = useMemo(() => (
    models.find((model) => model.isDefault)?.modelId ?? models[0]?.modelId ?? ''
  ), [models])
  const modelDisplayName = useCallback((modelId: string) => (
    models.find((model) => model.modelId === modelId)?.displayName ?? modelId
  ), [models])

  useEffect(() => {
    const controller = new AbortController()
    void fetchModelCatalog(controller.signal).then((catalog) => {
      if (controller.signal.aborted) return
      setModels(catalog.items)
      const nextDefault = catalog.defaultModelId
        && catalog.items.some((model) => model.modelId === catalog.defaultModelId)
        ? catalog.defaultModelId
        : (catalog.items.find((model) => model.isDefault)?.modelId ?? catalog.items[0]?.modelId ?? '')
      setDraftModel((current) => current || nextDefault)
      setModelCatalogLoaded(true)
    }).catch(() => {
      if (!controller.signal.aborted) setModelCatalogLoaded(true)
    })
    return () => controller.abort()
  }, [])

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
  const sidebarWorkspace = useMemo<WorkspaceState>(() => (
    workspace.currentThreadId
      ? workspace
      : { ...workspace, conversations: [conversation, ...workspace.conversations] }
  ), [conversation, workspace])
  const isRunning = conversation.runStatus === 'streaming'
  const selectAgentMode = useCallback((mode: AgentMode) => {
    if (isRunning || mode === conversation.mode) return
    if (mode === 'default' && conversation.planInteraction) {
      setDialogError(undefined)
      setDialog({ kind: 'disable-plan', threadId: conversation.threadId })
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
  }, [conversation, isRunning, workspace.currentThreadId])
  const isConversationHydrating = Boolean(
    workspace.currentThreadId
    && selectedConversation
    && !selectedConversation.isHydrated
    && hydrationState?.threadId === workspace.currentThreadId
    && hydrationState.status === 'loading',
  )
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

  const clearScrollButtonTimers = useCallback(() => {
    if (scrollButtonFadeTimeout.current != null) {
      window.clearTimeout(scrollButtonFadeTimeout.current)
      scrollButtonFadeTimeout.current = null
    }
    if (scrollButtonHideTimeout.current != null) {
      window.clearTimeout(scrollButtonHideTimeout.current)
      scrollButtonHideTimeout.current = null
    }
  }, [])

  const armScrollButtonFade = useCallback(() => {
    clearScrollButtonTimers()
    scrollButtonFadeTimeout.current = window.setTimeout(() => {
      setFadeScrollToBottom(true)
    }, 1500)
    scrollButtonHideTimeout.current = window.setTimeout(() => {
      setShowScrollToBottom(false)
      setFadeScrollToBottom(false)
    }, 1900)
  }, [clearScrollButtonTimers])

  const handleConversationScroll = useCallback((pane: HTMLElement) => {
    const threadId = latestWorkspace.current.currentThreadId
    if (threadId) writeConversationScrollTop(threadId, pane.scrollTop)
    if (followScrollFrame.current != null) {
      window.cancelAnimationFrame(followScrollFrame.current)
      followScrollFrame.current = null
    }
    if (scrollMeasureFrame.current != null) return
    scrollMeasureFrame.current = window.requestAnimationFrame(() => {
      scrollMeasureFrame.current = null
      const isNearBottom = pane.scrollHeight - pane.scrollTop - pane.clientHeight <= 96
      followLatest.current = isNearBottom
      if (isNearBottom) {
        scrollingToBottom.current = false
        clearScrollButtonTimers()
        setShowScrollToBottom(false)
        setFadeScrollToBottom(false)
        isScrollToBottomHovered.current = false
      } else if (scrollingToBottom.current) {
        setShowScrollToBottom(false)
      } else {
        setShowScrollToBottom(true)
        setFadeScrollToBottom(false)
        if (!isScrollToBottomHovered.current) armScrollButtonFade()
      }
    })
  }, [armScrollButtonFade, clearScrollButtonTimers])

  const scrollConversationToBottomImmediately = useCallback(() => {
    pendingImmediateScroll.current = true
    followLatest.current = true
    scrollingToBottom.current = false
    if (followScrollFrame.current != null) {
      window.cancelAnimationFrame(followScrollFrame.current)
      followScrollFrame.current = null
    }
    clearScrollButtonTimers()
    setShowScrollToBottom(false)
    setFadeScrollToBottom(false)
    isScrollToBottomHovered.current = false

    const pane = conversationPane.current
    if (pane) {
      pane.scrollTop = pane.scrollHeight
      const threadId = latestWorkspace.current.currentThreadId
      if (threadId) writeConversationScrollTop(threadId, pane.scrollTop)
    } else {
      messageEnd.current?.scrollIntoView?.({ behavior: 'auto', block: 'end' })
    }
  }, [clearScrollButtonTimers])

  const refreshHistoryList = useCallback(async (
    options: { preferredThreadId?: string; signal?: AbortSignal } = {},
  ) => {
    // 仅首次对话（挂载 effect）调用此函数拉取历史列表首页；后续新会话靠 streamRun
    // 本地 upsert，不再整表刷新
    try {
      const preferredThreadId = options.preferredThreadId ?? ''
      const [response, preferredDetail] = await Promise.all([
        fetchConversationHistoryList({
          pageSize: HISTORY_PAGE_SIZE,
          signal: options.signal,
        }),
        preferredThreadId
          ? fetchConversationHistoryDetail(preferredThreadId, {
              signal: options.signal,
            }).catch(() => undefined)
          : Promise.resolve(undefined),
      ])
      if (options.signal?.aborted) return
      const activeSession = readActiveRunSession()
      const preferredExists = preferredThreadId
        ? Boolean(preferredDetail || response.items.some((item) => item.threadId === preferredThreadId))
        : true
      if (
        (response.items.length === 0 && !preferredDetail)
        || (
          !preferredExists
          && activeSession?.threadId === preferredThreadId
        )
      ) clearActiveRunSession(activeSession?.payload.runId)
      if (preferredDetail) prefetchedHistoryDetails.current.set(preferredThreadId, preferredDetail)
      setHistoryCursor(response.nextCursor ?? null)
      setWorkspace((state) => {
        const conversations = mergeHistoryConversations(
          state.conversations,
          preferredDetail && !response.items.some((item) => item.threadId === preferredThreadId)
            ? [...response.items, preferredDetail]
            : response.items,
          defaultModelId,
        )
        const hasPreferred = preferredThreadId
          ? conversations.some((item) => item.threadId === preferredThreadId)
          : false
        const hasCurrent = state.currentThreadId
          ? conversations.some((item) => item.threadId === state.currentThreadId)
          : false
        return {
          conversations,
          currentThreadId: hasPreferred
            ? preferredThreadId
            : hasCurrent
              ? state.currentThreadId
              : (conversations[0]?.threadId ?? ''),
        }
      })
    } catch {
      // 历史引导尽力而为；失败保留当前空白视图
    }
  }, [defaultModelId])

  const loadMoreHistory = useCallback((isExplicitRetry = false) => {
    if (historyLoadingRef.current || historyLoadTimer.current != null) return
    if (historyLoadError && !isExplicitRetry) return
    const cursor = historyCursor
    if (!cursor || loadedHistoryCursors.current.has(cursor)) return
    historyLoadingRef.current = true
    historyInFlightCursor.current = cursor
    setHistoryLoadingMore(true)
    setHistoryLoadError(null)
    historyLoadTimer.current = window.setTimeout(() => {
      historyLoadTimer.current = null
      const controller = new AbortController()
      historyAbortController.current = controller
      void fetchConversationHistoryList({
        pageSize: HISTORY_PAGE_SIZE,
        cursor,
        signal: controller.signal,
        suppressGlobalError: true,
      }).then((response) => {
        if (historyInFlightCursor.current !== cursor) return
        loadedHistoryCursors.current.add(cursor)
        setHistoryCursor(response.nextCursor ?? null)
        setWorkspace((state) => ({
          ...state,
          conversations: mergeHistoryConversations(
            state.conversations,
            response.items,
            defaultModelId,
          ),
        }))
      }).catch(() => {
        if (!controller.signal.aborted && historyInFlightCursor.current === cursor) {
          setHistoryLoadError('历史记录加载失败')
        }
      }).finally(() => {
        if (historyInFlightCursor.current !== cursor) return
        historyAbortController.current = null
        historyInFlightCursor.current = null
        historyLoadingRef.current = false
        if (!controller.signal.aborted) setHistoryLoadingMore(false)
      })
    }, HISTORY_LOAD_DEBOUNCE_MS)
  }, [defaultModelId, historyCursor, historyLoadError])

  useEffect(() => {
    const sentinel = historyLoadSentinel.current
    const root = sentinel?.closest('.conversation-scroll')
    if (!sentinel || !(root instanceof HTMLElement) || typeof IntersectionObserver === 'undefined') return
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) loadMoreHistory()
    }, { root, rootMargin: '0px 0px 80px', threshold: 0.01 })
    observer.observe(sentinel)
    return () => observer.disconnect()
  }, [historyCursor, historyLoadError, isHistoryLoadingMore, loadMoreHistory])

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

  const hydrateConversation = useCallback(async (threadId: string) => {
    const target = latestWorkspace.current.conversations.find((item) => item.threadId === threadId)
    if (!target) return
    if (target.isHydrated) {
      void catchUpDetachedConversation(threadId)
      return
    }
    const existingRequest = hydrationRequests.current.get(threadId)
    if (existingRequest && !existingRequest.signal.aborted) return
    if (existingRequest) hydrationRequests.current.delete(threadId)

    const controller = new AbortController()
    hydrationRequests.current.set(threadId, controller)
    setHydrationState({ threadId, status: 'loading' })
    try {
      const detail = prefetchedHistoryDetails.current.get(threadId)
        ?? await fetchConversationHistoryDetail(threadId, {
          signal: controller.signal,
          suppressGlobalError: true,
        })
      if (
        controller.signal.aborted
        || hydrationRequests.current.get(threadId) !== controller
      ) return
      prefetchedHistoryDetails.current.delete(threadId)
      const restored = restoreConversationFromHistory(detail, {
        model: detail.lastModel ?? target.model,
      })
      setHydrationState((current) => current?.threadId === threadId ? null : current)
      setWorkspace((state) => upsertConversation(state, { ...restored, isHydrated: true }))
      if (restored.runStatus === 'detached' || restored.runStatus === 'idle') {
        if (!controller.signal.aborted) void catchUpDetachedConversation(threadId)
      }
    } catch {
      if (
        !controller.signal.aborted
        && hydrationRequests.current.get(threadId) === controller
      ) {
        setHydrationState({ threadId, status: 'failed' })
        pushToast('error', '会话加载失败，请重试')
      }
    } finally {
      if (hydrationRequests.current.get(threadId) === controller) {
        hydrationRequests.current.delete(threadId)
      }
    }
  }, [catchUpDetachedConversation, pushToast])

  useEffect(() => {
    // StrictMode 会重放挂载 effect；启动标记保证一次页面挂载只执行一次历史引导
    if (!modelCatalogLoaded) return
    if (hasHistoryBootstrapStarted.current) return
    hasHistoryBootstrapStarted.current = true
    const controller = new AbortController()
    historyBootstrapAbortController.current = controller
    // 启动时读回 `?thread=`，让刷新/直达链接能回到原会话而非列表第一条
    void refreshHistoryList({
      preferredThreadId: initialThreadId.current,
      signal: controller.signal,
    }).finally(() => {
      if (!controller.signal.aborted) setHistoryBootstrapped(true)
    })
    return () => {
      controller.abort()
      if (historyBootstrapAbortController.current === controller) {
        historyBootstrapAbortController.current = null
      }
      hasHistoryBootstrapStarted.current = false
    }
  }, [modelCatalogLoaded, refreshHistoryList])

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
    if (!workspace.currentThreadId || !selectedConversation || selectedConversation.isHydrated) return
    const threadId = workspace.currentThreadId
    const requests = hydrationRequests.current
    void hydrateConversation(threadId)
    return () => {
      const controller = requests.get(threadId)
      requests.delete(threadId)
      controller?.abort()
      setHydrationState((current) => current?.threadId === threadId ? null : current)
    }
  }, [hydrateConversation, selectedConversation, workspace.currentThreadId])

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

  useEffect(() => {
    const switchedConversation = todoTracker.current.threadId !== conversation.threadId
    if (switchedConversation) {
      todoTracker.current = { threadId: conversation.threadId, count: conversation.todos.length }
      const preference = conversation.threadId
        ? readTaskDrawerPreference(conversation.threadId)
        : null
      setDrawerOpen(preference ?? conversation.todos.length > 0)
      return
    }

    if (conversation.todos.length > 0 && todoTracker.current.count === 0) {
      // 历史 Todo 恢复也会经历 0 → 非 0，需先尊重用户对该会话的明确选择
      setDrawerOpen(readTaskDrawerPreference(conversation.threadId) ?? true)
    }
    todoTracker.current = { threadId: conversation.threadId, count: conversation.todos.length }
  }, [conversation.threadId, conversation.todos.length])

  const toggleTaskDrawer = useCallback(() => {
    const nextIsOpen = !drawerOpen
    if (conversation.threadId) writeTaskDrawerPreference(conversation.threadId, nextIsOpen)
    setDrawerOpen(nextIsOpen)
  }, [conversation.threadId, drawerOpen])

  useLayoutEffect(() => {
    const savedScrollTop = conversation.threadId
      ? readConversationScrollTop(conversation.threadId)
      : null
    pendingConversationScroll.current = conversation.threadId
      ? { threadId: conversation.threadId, scrollTop: savedScrollTop }
      : null
    followLatest.current = savedScrollTop == null
    scrollingToBottom.current = false
    if (followScrollFrame.current != null) {
      window.cancelAnimationFrame(followScrollFrame.current)
      followScrollFrame.current = null
    }
    if (scrollMeasureFrame.current != null) {
      window.cancelAnimationFrame(scrollMeasureFrame.current)
      scrollMeasureFrame.current = null
    }
    clearScrollButtonTimers()
    setShowScrollToBottom(false)
    setFadeScrollToBottom(false)
    isScrollToBottomHovered.current = false
  }, [clearScrollButtonTimers, conversation.threadId])

  useLayoutEffect(() => {
    const pane = conversationPane.current
    if (pendingImmediateScroll.current) {
      pendingImmediateScroll.current = false
      pendingConversationScroll.current = null
      followLatest.current = true
      if (pane) {
        pane.scrollTop = pane.scrollHeight
        if (conversation.threadId) writeConversationScrollTop(conversation.threadId, pane.scrollTop)
      } else {
        messageEnd.current?.scrollIntoView?.({ behavior: 'auto', block: 'end' })
      }
      return
    }
    // 每次激活只恢复一次；历史详情未水合时保留待恢复值，避免空内容把位置钳制为 0
    const pendingScroll = pendingConversationScroll.current
    const shouldRestoreConversationScroll = Boolean(
      pane
      && conversation.threadId
      && pendingScroll?.threadId === conversation.threadId
      && conversation.isHydrated
    )
    if (shouldRestoreConversationScroll && pane && pendingScroll) {
      pendingConversationScroll.current = null
      if (pendingScroll.scrollTop != null) {
        pane.scrollTop = pendingScroll.scrollTop
        handleConversationScroll(pane)
        return
      }
    }
    if (!followLatest.current) return
    if (followScrollFrame.current != null) window.cancelAnimationFrame(followScrollFrame.current)
    followScrollFrame.current = window.requestAnimationFrame(() => {
      followScrollFrame.current = null
      if (!followLatest.current) return
      if (conversationPane.current) conversationPane.current.scrollTop = conversationPane.current.scrollHeight
      else messageEnd.current?.scrollIntoView?.({ behavior: 'auto', block: 'end' })
      clearScrollButtonTimers()
      setShowScrollToBottom(false)
      setFadeScrollToBottom(false)
    })
  }, [clearScrollButtonTimers, conversation.approval?.submitted, conversation.isHydrated, conversation.messages, conversation.notice, conversation.threadId, handleConversationScroll, isRunning])

  useEffect(() => () => {
    clearScrollButtonTimers()
    if (historyLoadTimer.current != null) window.clearTimeout(historyLoadTimer.current)
    historyAbortController.current?.abort()
    for (const controller of hydrationRequests.current.values()) controller.abort()
    hydrationRequests.current.clear()
    if (followScrollFrame.current != null) window.cancelAnimationFrame(followScrollFrame.current)
    if (scrollMeasureFrame.current != null) window.cancelAnimationFrame(scrollMeasureFrame.current)
  }, [clearScrollButtonTimers])

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

  const closeDialog = () => {
    if (dialogPending) return
    setDialog(null)
    setDialogError(undefined)
  }

  const performSelectConversation = (threadId: string) => {
    setDraft('')
    setDraftConversation(null)
    setWorkspace((state) => ({ ...state, currentThreadId: threadId }))
    const target = latestWorkspace.current.conversations.find((item) => item.threadId === threadId)
    if (target?.isHydrated) void catchUpDetachedConversation(threadId)
  }

  const selectConversation = (threadId: string) => {
    if (hasActiveStream() && getActiveThreadId() !== threadId) {
      setDialogError(undefined)
      setDialog({ kind: 'detach-select', threadId })
      return
    }
    performSelectConversation(threadId)
  }

  const performNewConversation = () => {
    setDraft('')
    setDraftConversation(null)
    setDraftModel(conversation.model)
    setWorkspace((state) => createNewConversation(state))
  }

  const newConversation = () => {
    if (hasActiveStream()) {
      setDialogError(undefined)
      setDialog({ kind: 'detach-new' })
      return
    }
    performNewConversation()
  }

  const pinConversation = (threadId: string) => {
    const target = latestWorkspace.current.conversations.find((item) => item.threadId === threadId)
    const nextPinned = !target?.pinned
    // 乐观更新；后端为准，失败则回滚
    setWorkspace((state) => updateConversation(state, threadId, (item) => ({ ...item, pinned: nextPinned })))
    void patchConversation(threadId, { pinned: nextPinned }).then(() => {
      pushToast('success', nextPinned ? '会话已置顶' : '已取消置顶')
    }).catch(() => {
      setWorkspace((state) => updateConversation(state, threadId, (item) => ({ ...item, pinned: !nextPinned })))
      pushToast('error', '置顶状态更新失败，请重试')
    })
  }

  const renameConversation = (threadId: string, restoreFocusTo?: HTMLElement | null) => {
    const target = latestWorkspace.current.conversations.find((item) => item.threadId === threadId)
    if (!target) return
    setDialogError(undefined)
    setDialog({ kind: 'rename', threadId, initialValue: target.title, restoreFocusTo })
  }

  const deleteConversation = (threadId: string, restoreFocusTo?: HTMLElement | null) => {
    const target = latestWorkspace.current.conversations.find((item) => item.threadId === threadId)
    if (!target) return
    setDialogError(undefined)
    setDialog({
      kind: 'delete',
      threadId,
      title: target.title,
      isRunning: isActiveThread(threadId),
      restoreFocusTo,
    })
  }

  const confirmDialog = async (value?: string) => {
    if (!dialog || dialogPending) return
    setDialogPending(true)
    setDialogError(undefined)
    try {
      if (dialog.kind === 'rename') {
        const title = value?.trim()
        if (!title) throw new Error('会话名称不能为空')
        if (title !== dialog.initialValue) {
          await patchConversation(dialog.threadId, { title })
          setWorkspace((state) => updateConversation(state, dialog.threadId, (item) => ({ ...item, title })))
          pushToast('success', '会话已重命名')
        }
      } else if (dialog.kind === 'disable-plan') {
        abandonPlanInteraction(dialog.threadId)
        pushToast('info', '已关闭 Plan，下一条消息将使用 default 模式')
      } else if (dialog.kind === 'delete') {
        if (dialog.isRunning) {
          await cancelActiveRun()
        }
        await deleteConversationApi(dialog.threadId)
        setWorkspace((state) => removeConversation(state, dialog.threadId))
        pushToast('success', '会话已删除')
      } else {
        if (hasActiveStream()) {
          const runningThreadId = getActiveThreadId() ?? conversation.threadId
          detachThreadStream(
            runningThreadId,
            dialog.kind === 'detach-new'
              ? '已新建会话，之前会话的实时输出连接已断开。'
              : '已切换到其他会话，当前会话的实时输出连接已断开。',
          )
        }
        if (dialog.kind === 'detach-new') performNewConversation()
        else performSelectConversation(dialog.threadId)
        pushToast('info', '已断开当前会话的实时输出')
      }
      setDialog(null)
    } catch (error) {
      const message = error instanceof Error && error.message
        ? error.message
        : '操作失败，请稍后重试'
      setDialogError(message)
    } finally {
      setDialogPending(false)
    }
  }

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
    <div ref={appShell} className={`app-shell ${drawerOpen ? 'has-drawer' : ''}`} id="top">
      <Sidebar
        workspace={sidebarWorkspace}
        isOpen={sidebarOpen}
        onClose={() => setSidebarOpen(false)}
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
      />
      <main className="workspace-main">
        <header className="chat-header">
          <div className="header-left">
            <button className="icon-button menu-toggle" aria-label="打开导航" onClick={() => setSidebarOpen(true)}><Menu size={19} /></button>
            <ListboxPicker
              value={modelIds.includes(conversation.model) ? conversation.model : defaultModelId}
              options={modelIds}
              open={isModelPickerOpen}
              onOpenChange={(open) => {
                setModelPickerOpen(open)
                if (open) setAgentPresetPickerOpen(false)
              }}
              onChange={selectModel}
              triggerLabel="选择模型"
              listboxLabel="模型选项"
              rootClassName="model-picker"
              triggerClassName="model-select"
              listboxClassName="model-options"
              optionClassName="overflow-marquee-trigger"
              renderTrigger={(model) => <><span>{modelDisplayName(model) || '加载模型…'}</span><ChevronDown size={14} /></>}
              renderOption={(model, selected) => <ModelOption label={modelDisplayName(model)} selected={selected} />}
            />
          </div>
          <div className="header-actions">
            <ThemePicker />
            <ListboxPicker
              value={conversation.mode}
              options={AGENT_MODES}
              open={isAgentPresetPickerOpen}
              onOpenChange={(open) => {
                setAgentPresetPickerOpen(open)
                if (open) setModelPickerOpen(false)
              }}
              onChange={selectAgentMode}
              disabled={isRunning || conversation.approval != null}
              triggerLabel="当前 Agent 预设"
              listboxLabel="Agent 预设选项"
              rootClassName="agent-preset-picker"
              triggerClassName="agent-preset"
              listboxClassName="agent-preset-options"
              renderTrigger={(mode) => <><GitBranch size={16} /><span className="agent-preset-label">{mode}</span><ChevronDown size={14} /></>}
              renderOption={(mode, selected) => <><span>{mode}</span>{selected && <Check size={14} />}</>}
            />
            <button className={`drawer-toggle ${drawerOpen ? 'is-active' : ''}`} aria-label={drawerOpen ? '关闭任务抽屉' : '打开任务抽屉'} onClick={toggleTaskDrawer}><PanelRight size={17} /><span>任务</span><b>{conversation.todos.length}</b></button>
          </div>
        </header>
        <section ref={conversationPane} onScroll={(event) => {
          handleConversationScroll(event.currentTarget)
        }} onWheel={() => {
          scrollingToBottom.current = false
        }} onTouchStart={() => {
          scrollingToBottom.current = false
        }} className={`conversation-pane ${conversation.messages.length === 0 && !conversation.notice ? 'is-empty' : ''}`} aria-label="对话内容">
          {conversation.messages.length === 0 && !conversation.notice ? (
            <EmptyConversation />
          ) : (
            <div className="message-list">
              {displayMessages.map((entry) => entry.type === 'tools'
                ? <ToolCallBatch key={`batch-${entry.messages[0].id}`} messages={entry.messages} />
                : <MessageBlock
                    key={entry.message.id}
                    message={entry.message}
                    childTools={entry.message.meta?.subRunId
                      ? childToolsByRunId.get(entry.message.meta.subRunId) ?? []
                      : []}
                  />)}
              {conversation.notice && <ConversationNotice notice={conversation.notice} />}
              {isRunning && (
                <p className="message-stream-tail stream-pending-tail">
                  <ActivityDots label="任务仍在继续" />
                </p>
              )}
              {conversation.approval && !conversation.approval.submitted && (
                <ApprovalCard
                  conversation={conversation}
                  onChange={(updater) => changeApproval(conversation.threadId, updater)}
                  onSubmit={submitApproval}
                />
              )}
              {conversation.planInteraction?.kind === 'questions' && !conversation.planInteraction.submitted && (
                <PlanQuestionCard
                  interaction={conversation.planInteraction}
                  onChange={(updater) => changePlanInteraction(
                    conversation.threadId,
                    (current) => current.kind === 'questions'
                      ? updater(current as PlanQuestionState)
                      : current,
                  )}
                  onSubmit={submitPlanInteraction}
                />
              )}
              {conversation.planInteraction?.kind === 'review' && !conversation.planInteraction.submitted && (
                <PlanReviewCard
                  interaction={conversation.planInteraction}
                  onChange={(updater) => changePlanInteraction(
                    conversation.threadId,
                    (current) => current.kind === 'review'
                      ? updater(current as PlanReviewState)
                      : current,
                  )}
                  onSubmit={submitPlanInteraction}
                />
              )}
              <div ref={messageEnd} />
            </div>
          )}
          {showScrollToBottom && (
            <button
              className={`scroll-to-bottom ${fadeScrollToBottom ? 'is-fading' : ''}`}
              onMouseEnter={() => {
                isScrollToBottomHovered.current = true
                clearScrollButtonTimers()
                setFadeScrollToBottom(false)
              }}
              onMouseLeave={() => {
                isScrollToBottomHovered.current = false
                if (!followLatest.current) armScrollButtonFade()
              }}
              onClick={() => {
                followLatest.current = true
                scrollingToBottom.current = true
                clearScrollButtonTimers()
                setShowScrollToBottom(false)
                setFadeScrollToBottom(false)
                isScrollToBottomHovered.current = false
                conversationPane.current?.scrollTo({
                  top: conversationPane.current.scrollHeight,
                  behavior: 'smooth',
                })
              }}
            >
              <ArrowDown size={17} />回到底部
            </button>
          )}
        </section>
        <Composer
          value={draft}
          isRunning={isRunning}
          canStop={Boolean(conversation.threadId)}
          isHydrating={isConversationHydrating}
          onChange={setDraft}
          onSend={send}
          onStop={() => void stop()}
        />
      </main>
      {drawerOpen && <TaskDrawer conversation={conversation} />}
      {dialog?.kind === 'rename' && (
        <ModalDialog
          open
          title="重命名会话"
          description="输入一个便于在历史记录中识别的名称。"
          inputLabel="会话名称"
          initialValue={dialog.initialValue}
          confirmLabel="保存"
          isPending={dialogPending}
          error={dialogError}
          restoreFocusTo={dialog.restoreFocusTo}
          onConfirm={confirmDialog}
          onCancel={closeDialog}
        />
      )}
      {dialog?.kind === 'delete' && (
        <ModalDialog
          open
          title="删除会话"
          description={dialog.isRunning
            ? `“${dialog.title}”仍在接收实时输出。继续会先断开连接，并永久删除全部历史记录。`
            : `将永久删除“${dialog.title}”及其全部历史记录，此操作不可撤销。`}
          confirmLabel="删除"
          tone="danger"
          isPending={dialogPending}
          error={dialogError}
          restoreFocusTo={dialog.restoreFocusTo}
          onConfirm={confirmDialog}
          onCancel={closeDialog}
        />
      )}
      {dialog?.kind === 'disable-plan' && (
        <ModalDialog
          open
          title="关闭当前 Plan？"
          description="当前 Plan 澄清或审阅将被取消，不会执行旧计划。Tool/Filesystem 审批不受影响。"
          confirmLabel="关闭 Plan"
          isPending={dialogPending}
          error={dialogError}
          onConfirm={confirmDialog}
          onCancel={closeDialog}
        />
      )}
      {(dialog?.kind === 'detach-select' || dialog?.kind === 'detach-new') && (
        <ModalDialog
          open
          title="断开实时输出？"
          description={dialog.kind === 'detach-new'
            ? '新建会话会断开当前实时输出，但后端任务可能仍会继续。'
            : '切换会话会断开当前实时输出，但后端任务可能仍会继续。'}
          confirmLabel={dialog.kind === 'detach-new' ? '断开并新建' : '断开并切换'}
          isPending={dialogPending}
          error={dialogError}
          onConfirm={confirmDialog}
          onCancel={closeDialog}
        />
      )}
    </div>
  )
}
