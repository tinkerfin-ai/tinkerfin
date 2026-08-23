import {
  useCallback,
  useEffect,
  useRef,
  useState,
} from 'react'
import type { Dispatch, RefObject, SetStateAction } from 'react'

import {
  fetchConversationHistoryDetail,
  fetchConversationHistoryList,
  type ConversationHistoryDetail,
  type ConversationHistoryListItem,
} from '../../api/conversation/history'
import {
  clearActiveRunSession,
  readActiveRunSession,
} from '../conversation/stream/activeRunSession'
import { restoreConversationFromHistory } from '../conversation/agui'
import { readThreadFromLocation } from '../../lib/threadRoute'
import { upsertConversation } from '../../lib/workspace'
import type { Conversation, WorkspaceState } from '../../types'

const HISTORY_PAGE_SIZE = 5
const HISTORY_LOAD_DEBOUNCE_MS = 300

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
  incoming: ConversationHistoryListItem[],
  fallbackModel: string,
) => {
  const byId = new Map(current.map((item) => [item.threadId, item]))
  for (const item of incoming) {
    const existing = byId.get(item.threadId)
    const summary = conversationFromHistoryItem(item, fallbackModel)
    byId.set(
      item.threadId,
      existing
        ? {
            ...existing,
            title: item.title,
            pinned: item.pinned,
            updatedAt: item.updatedAt,
            model: summary.model,
            activeRunId: existing.runStatus === 'streaming'
              ? existing.activeRunId
              : summary.activeRunId,
            lastSeq: existing.isHydrated ? existing.lastSeq : summary.lastSeq,
            runStatus: existing.runStatus === 'streaming' ? existing.runStatus : summary.runStatus,
          }
        : summary,
    )
  }
  return sortConversations([...byId.values()])
}

export function useWorkspaceHistory({
  workspace,
  setWorkspace,
  defaultModelId,
  modelCatalogStatus,
  catchUpDetachedConversation,
  onToast,
}: {
  workspace: WorkspaceState
  setWorkspace: Dispatch<SetStateAction<WorkspaceState>>
  defaultModelId: string
  modelCatalogStatus: 'loading' | 'ready' | 'error'
  catchUpDetachedConversation: (threadId: string) => void | Promise<void>
  onToast: (kind: 'error', message: string) => void
}) {
  const [historyCursor, setHistoryCursor] = useState<string | null>(null)
  const [isHistoryLoadingMore, setHistoryLoadingMore] = useState(false)
  const [historyLoadError, setHistoryLoadError] = useState<string | null>(null)
  const [isHistoryBootstrapped, setHistoryBootstrapped] = useState(false)
  const [historyBootstrapStatus, setHistoryBootstrapStatus] = useState<'loading' | 'ready' | 'error'>('loading')
  const [hydrationState, setHydrationState] = useState<{
    threadId: string
    status: 'loading' | 'failed'
  } | null>(null)
  const historyLoadSentinel = useRef<HTMLDivElement>(null)
  const hasHistoryBootstrapStarted = useRef(false)
  const historyBootstrapAbortController = useRef<AbortController | null>(null)
  const historyLoadingRef = useRef(false)
  const historyLoadTimer = useRef<number | null>(null)
  const historyInFlightCursor = useRef<string | null>(null)
  const loadedHistoryCursors = useRef(new Set<string>())
  const historyAbortController = useRef<AbortController | null>(null)
  const prefetchedHistoryDetails = useRef(new Map<string, ConversationHistoryDetail>())
  const hydrationRequests = useRef(new Map<string, AbortController>())
  const initialThreadId = useRef(readThreadFromLocation())
  const latestWorkspace = useRef(workspace)
  latestWorkspace.current = workspace

  const refreshHistoryList = useCallback(async (
    options: { preferredThreadId?: string; signal?: AbortSignal } = {},
  ) => {
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
      if (options.signal?.aborted) return false
      const activeSession = readActiveRunSession()
      const preferredExists = preferredThreadId
        ? Boolean(preferredDetail || response.items.some((item) => item.threadId === preferredThreadId))
        : true
      if (
        (response.items.length === 0 && !preferredDetail)
        || (!preferredExists && activeSession?.threadId === preferredThreadId)
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
      return true
    } catch {
      return false
    }
  }, [defaultModelId, setWorkspace])

  const retryHistoryBootstrap = useCallback(() => {
    historyBootstrapAbortController.current?.abort()
    const controller = new AbortController()
    historyBootstrapAbortController.current = controller
    setHistoryBootstrapStatus('loading')
    void refreshHistoryList({
      preferredThreadId: readThreadFromLocation(),
      signal: controller.signal,
    }).then((succeeded) => {
      if (controller.signal.aborted) return
      setHistoryBootstrapStatus(succeeded ? 'ready' : 'error')
      setHistoryBootstrapped(true)
    }).finally(() => {
      if (historyBootstrapAbortController.current === controller) {
        historyBootstrapAbortController.current = null
      }
    })
  }, [refreshHistoryList])

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
  }, [defaultModelId, historyCursor, historyLoadError, setWorkspace])

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
      if (!controller.signal.aborted && hydrationRequests.current.get(threadId) === controller) {
        setHydrationState({ threadId, status: 'failed' })
        onToast('error', '会话加载失败，请重试')
      }
    } finally {
      if (hydrationRequests.current.get(threadId) === controller) {
        hydrationRequests.current.delete(threadId)
      }
    }
  }, [catchUpDetachedConversation, onToast, setWorkspace])

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

  useEffect(() => {
    if (modelCatalogStatus === 'loading' || isHistoryBootstrapped) return
    if (hasHistoryBootstrapStarted.current) return
    hasHistoryBootstrapStarted.current = true
    const controller = new AbortController()
    historyBootstrapAbortController.current = controller
    setHistoryBootstrapStatus('loading')
    void refreshHistoryList({
      preferredThreadId: initialThreadId.current,
      signal: controller.signal,
    }).then((succeeded) => {
      if (!controller.signal.aborted) {
        setHistoryBootstrapStatus(succeeded ? 'ready' : 'error')
        setHistoryBootstrapped(true)
      }
    })
    return () => {
      controller.abort()
      if (historyBootstrapAbortController.current === controller) {
        historyBootstrapAbortController.current = null
      }
      hasHistoryBootstrapStarted.current = false
    }
  }, [isHistoryBootstrapped, modelCatalogStatus, refreshHistoryList])

  const selectedConversation = workspace.conversations.find(
    (item) => item.threadId === workspace.currentThreadId,
  )
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

  useEffect(() => () => {
    if (historyLoadTimer.current != null) window.clearTimeout(historyLoadTimer.current)
    historyAbortController.current?.abort()
    historyBootstrapAbortController.current?.abort()
    for (const controller of hydrationRequests.current.values()) controller.abort()
    hydrationRequests.current.clear()
  }, [])

  return {
    historyCursor,
    isHistoryLoadingMore,
    historyLoadError,
    isHistoryBootstrapped,
    historyBootstrapStatus,
    hydrationState,
    historyLoadSentinel: historyLoadSentinel as RefObject<HTMLDivElement | null>,
    loadMoreHistory,
    retryHistoryBootstrap,
    hydrateConversation,
  }
}
