import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import type { Dispatch, SetStateAction } from 'react'

import {
  fetchConversationHistoryDetail,
  fetchConversationHistoryGroupConfig,
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
import { useI18n } from '../../i18n'

const HISTORY_PAGE_SIZE = 5
const HISTORY_LOAD_DEBOUNCE_MS = 500
const HISTORY_SEARCH_DEBOUNCE_MS = 300

const sortConversations = (conversations: Conversation[]) =>
  [...conversations].sort((a, b) => Date.parse(b.updatedAt) - Date.parse(a.updatedAt))

const appendUniqueThreadIds = (current: string[], incoming: string[]) => {
  const seen = new Set(current)
  return [
    ...current,
    ...incoming.filter((threadId) => {
      if (seen.has(threadId)) return false
      seen.add(threadId)
      return true
    }),
  ]
}

const prependUniqueThreadIds = (current: string[], incoming: string[]) => {
  const incomingSet = new Set(incoming)
  return [...incoming, ...current.filter((threadId) => !incomingSet.has(threadId))]
}

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
  const { t } = useI18n()
  const [historyCursor, setHistoryCursor] = useState<string | null>(null)
  const [historyThreadIds, setHistoryThreadIds] = useState<string[]>([])
  const [historyDayRanges, setHistoryDayRanges] = useState<number[]>([])
  const [historyQuery, setHistoryQuery] = useState('')
  const [searchThreadIds, setSearchThreadIds] = useState<string[]>([])
  const [searchCursor, setSearchCursor] = useState<string | null>(null)
  const [isHistorySearching, setHistorySearching] = useState(false)
  const [isSearchLoadingMore, setSearchLoadingMore] = useState(false)
  const [searchLoadError, setSearchLoadError] = useState<string | null>(null)
  const [searchRetryVersion, setSearchRetryVersion] = useState(0)
  const [isHistoryLoadingMore, setHistoryLoadingMore] = useState(false)
  const [historyLoadError, setHistoryLoadError] = useState<string | null>(null)
  const [isHistoryBootstrapped, setHistoryBootstrapped] = useState(false)
  const [historyBootstrapStatus, setHistoryBootstrapStatus] = useState<'loading' | 'ready' | 'error'>('loading')
  const [hydrationState, setHydrationState] = useState<{
    threadId: string
    status: 'loading' | 'failed'
  } | null>(null)
  const historyThreadIdsRef = useRef<string[]>([])
  const searchOnlyThreadIds = useRef(new Set<string>())
  const normalizedHistoryQuery = historyQuery.trim()
  const normalizedHistoryQueryRef = useRef(normalizedHistoryQuery)
  normalizedHistoryQueryRef.current = normalizedHistoryQuery
  const hasHistoryBootstrapStarted = useRef(false)
  const historyBootstrapAbortController = useRef<AbortController | null>(null)
  const historyLoadingRef = useRef(false)
  const historyLoadTimer = useRef<number | null>(null)
  const historyInFlightCursor = useRef<string | null>(null)
  const loadedHistoryCursors = useRef(new Set<string>())
  const historyAbortController = useRef<AbortController | null>(null)
  const historySearchDebounceTimer = useRef<number | null>(null)
  const historySearchLoadTimer = useRef<number | null>(null)
  const historySearchAbortController = useRef<AbortController | null>(null)
  const historySearchGeneration = useRef(0)
  const historySearchLoadingRef = useRef(false)
  const historySearchInFlightCursor = useRef<string | null>(null)
  const loadedSearchCursors = useRef(new Set<string>())
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
      const [response, preferredDetail, groupConfig] = await Promise.all([
        fetchConversationHistoryList({
          pageSize: HISTORY_PAGE_SIZE,
          signal: options.signal,
        }),
        preferredThreadId
          ? fetchConversationHistoryDetail(preferredThreadId, {
              signal: options.signal,
            }).catch(() => undefined)
          : Promise.resolve(undefined),
        fetchConversationHistoryGroupConfig({ signal: options.signal }),
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
      const historyItems = preferredDetail && !response.items.some((item) => item.threadId === preferredThreadId)
        ? [...response.items, preferredDetail]
        : response.items
      const nextThreadIds = historyItems.map((item) => item.threadId)
      historyThreadIdsRef.current = nextThreadIds
      setHistoryThreadIds(nextThreadIds)
      for (const threadId of nextThreadIds) searchOnlyThreadIds.current.delete(threadId)
      loadedHistoryCursors.current.clear()
      setHistoryCursor(response.nextCursor ?? null)
      setHistoryDayRanges(groupConfig.dayRanges)
      setWorkspace((state) => {
        const conversations = mergeHistoryConversations(
          state.conversations,
          historyItems,
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

  useEffect(() => {
    const known = new Set(historyThreadIdsRef.current)
    const additions = sortConversations(workspace.conversations)
      .map((conversation) => conversation.threadId)
      .filter((threadId) => (
        threadId
        && !known.has(threadId)
        && !searchOnlyThreadIds.current.has(threadId)
      ))
    if (additions.length === 0) return
    const next = prependUniqueThreadIds(historyThreadIdsRef.current, additions)
    historyThreadIdsRef.current = next
    setHistoryThreadIds(next)
  }, [workspace.conversations])

  useEffect(() => {
    const threadId = workspace.currentThreadId
    if (normalizedHistoryQuery || !threadId || !searchOnlyThreadIds.current.has(threadId)) return
    searchOnlyThreadIds.current.delete(threadId)
    const next = prependUniqueThreadIds(historyThreadIdsRef.current, [threadId])
    historyThreadIdsRef.current = next
    setHistoryThreadIds(next)
  }, [normalizedHistoryQuery, workspace.currentThreadId])

  useEffect(() => {
    historySearchGeneration.current += 1
    const generation = historySearchGeneration.current
    if (historySearchDebounceTimer.current != null) {
      window.clearTimeout(historySearchDebounceTimer.current)
      historySearchDebounceTimer.current = null
    }
    if (historySearchLoadTimer.current != null) {
      window.clearTimeout(historySearchLoadTimer.current)
      historySearchLoadTimer.current = null
    }
    historySearchAbortController.current?.abort()
    historySearchAbortController.current = null
    historySearchLoadingRef.current = false
    historySearchInFlightCursor.current = null
    loadedSearchCursors.current.clear()
    setSearchThreadIds([])
    setSearchCursor(null)
    setSearchLoadError(null)
    setSearchLoadingMore(false)

    if (!normalizedHistoryQuery) {
      setHistorySearching(false)
      return
    }

    setHistorySearching(true)
    historySearchDebounceTimer.current = window.setTimeout(() => {
      historySearchDebounceTimer.current = null
      const controller = new AbortController()
      historySearchAbortController.current = controller
      void fetchConversationHistoryList({
        pageSize: HISTORY_PAGE_SIZE,
        query: normalizedHistoryQuery,
        signal: controller.signal,
        suppressGlobalError: true,
      }).then((response) => {
        if (
          controller.signal.aborted
          || historySearchGeneration.current !== generation
        ) return
        const normalIds = new Set(historyThreadIdsRef.current)
        for (const item of response.items) {
          if (!normalIds.has(item.threadId)) searchOnlyThreadIds.current.add(item.threadId)
        }
        setSearchThreadIds(response.items.map((item) => item.threadId))
        setSearchCursor(response.nextCursor ?? null)
        setWorkspace((state) => ({
          ...state,
          conversations: mergeHistoryConversations(
            state.conversations,
            response.items,
            defaultModelId,
          ),
        }))
      }).catch(() => {
        if (
          !controller.signal.aborted
          && historySearchGeneration.current === generation
        ) setSearchLoadError(t('搜索会话失败'))
      }).finally(() => {
        if (historySearchGeneration.current !== generation) return
        if (historySearchAbortController.current === controller) {
          historySearchAbortController.current = null
        }
        if (!controller.signal.aborted) setHistorySearching(false)
      })
    }, HISTORY_SEARCH_DEBOUNCE_MS)

    return () => {
      if (historySearchDebounceTimer.current != null) {
        window.clearTimeout(historySearchDebounceTimer.current)
        historySearchDebounceTimer.current = null
      }
      historySearchAbortController.current?.abort()
      historySearchAbortController.current = null
    }
  }, [defaultModelId, normalizedHistoryQuery, searchRetryVersion, setWorkspace, t])

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

  const loadMoreNormalHistory = useCallback((isExplicitRetry = false) => {
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
        const incomingIds = response.items.map((item) => item.threadId)
        const nextThreadIds = appendUniqueThreadIds(
          historyThreadIdsRef.current,
          incomingIds,
        )
        historyThreadIdsRef.current = nextThreadIds
        setHistoryThreadIds(nextThreadIds)
        for (const threadId of incomingIds) searchOnlyThreadIds.current.delete(threadId)
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
          setHistoryLoadError(t('历史记录加载失败'))
        }
      }).finally(() => {
        if (historyInFlightCursor.current !== cursor) return
        historyAbortController.current = null
        historyInFlightCursor.current = null
        historyLoadingRef.current = false
        if (!controller.signal.aborted) setHistoryLoadingMore(false)
      })
    }, HISTORY_LOAD_DEBOUNCE_MS)
  }, [defaultModelId, historyCursor, historyLoadError, setWorkspace, t])

  const loadMoreSearchHistory = useCallback((isExplicitRetry = false) => {
    if (historySearchLoadingRef.current || historySearchLoadTimer.current != null) return
    if (searchLoadError && !isExplicitRetry) return
    const cursor = searchCursor
    const query = normalizedHistoryQuery
    if (
      !query
      || !cursor
      || loadedSearchCursors.current.has(cursor)
    ) return
    historySearchLoadingRef.current = true
    historySearchInFlightCursor.current = cursor
    setSearchLoadingMore(true)
    setSearchLoadError(null)
    historySearchLoadTimer.current = window.setTimeout(() => {
      historySearchLoadTimer.current = null
      const controller = new AbortController()
      historySearchAbortController.current = controller
      void fetchConversationHistoryList({
        pageSize: HISTORY_PAGE_SIZE,
        cursor,
        query,
        signal: controller.signal,
        suppressGlobalError: true,
      }).then((response) => {
        if (
          controller.signal.aborted
          || historySearchInFlightCursor.current !== cursor
          || normalizedHistoryQueryRef.current !== query
        ) return
        loadedSearchCursors.current.add(cursor)
        const normalIds = new Set(historyThreadIdsRef.current)
        for (const item of response.items) {
          if (!normalIds.has(item.threadId)) searchOnlyThreadIds.current.add(item.threadId)
        }
        setSearchThreadIds((current) => appendUniqueThreadIds(
          current,
          response.items.map((item) => item.threadId),
        ))
        setSearchCursor(response.nextCursor ?? null)
        setWorkspace((state) => ({
          ...state,
          conversations: mergeHistoryConversations(
            state.conversations,
            response.items,
            defaultModelId,
          ),
        }))
      }).catch(() => {
        if (
          !controller.signal.aborted
          && historySearchInFlightCursor.current === cursor
          && normalizedHistoryQueryRef.current === query
        ) setSearchLoadError(t('搜索会话失败'))
      }).finally(() => {
        if (historySearchInFlightCursor.current !== cursor) return
        historySearchAbortController.current = null
        historySearchInFlightCursor.current = null
        historySearchLoadingRef.current = false
        if (!controller.signal.aborted) setSearchLoadingMore(false)
      })
    }, HISTORY_LOAD_DEBOUNCE_MS)
  }, [defaultModelId, normalizedHistoryQuery, searchCursor, searchLoadError, setWorkspace, t])

  const loadMoreHistory = useCallback((isExplicitRetry = false) => {
    if (normalizedHistoryQuery) loadMoreSearchHistory(isExplicitRetry)
    else loadMoreNormalHistory(isExplicitRetry)
  }, [loadMoreNormalHistory, loadMoreSearchHistory, normalizedHistoryQuery])

  const retryHistoryLoad = useCallback(() => {
    if (
      normalizedHistoryQuery
      && searchLoadError
      && searchCursor == null
    ) {
      setSearchRetryVersion((value) => value + 1)
      return
    }
    loadMoreHistory(true)
  }, [loadMoreHistory, normalizedHistoryQuery, searchCursor, searchLoadError])

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
        onToast('error', t('会话加载失败，请重试'))
      }
    } finally {
      if (hydrationRequests.current.get(threadId) === controller) {
        hydrationRequests.current.delete(threadId)
      }
    }
  }, [catchUpDetachedConversation, onToast, setWorkspace, t])

  const activeHistoryThreadIds = normalizedHistoryQuery
    ? searchThreadIds
    : historyThreadIds
  const historyConversations = useMemo(() => {
    const byId = new Map(workspace.conversations.map((item) => [item.threadId, item]))
    return activeHistoryThreadIds
      .map((threadId) => byId.get(threadId))
      .filter((item): item is Conversation => item != null)
  }, [activeHistoryThreadIds, workspace.conversations])
  const activeHistoryCursor = normalizedHistoryQuery ? searchCursor : historyCursor
  const activeHistoryLoadingMore = normalizedHistoryQuery
    ? isSearchLoadingMore
    : isHistoryLoadingMore
  const activeHistoryLoadError = normalizedHistoryQuery
    ? searchLoadError
    : historyLoadError

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
    if (historySearchDebounceTimer.current != null) window.clearTimeout(historySearchDebounceTimer.current)
    if (historySearchLoadTimer.current != null) window.clearTimeout(historySearchLoadTimer.current)
    historyAbortController.current?.abort()
    historySearchAbortController.current?.abort()
    historyBootstrapAbortController.current?.abort()
    for (const controller of hydrationRequests.current.values()) controller.abort()
    hydrationRequests.current.clear()
  }, [])

  return {
    historyConversations,
    historyDayRanges,
    historyQuery,
    setHistoryQuery,
    isHistorySearchActive: Boolean(normalizedHistoryQuery),
    isHistorySearching,
    historyCursor: activeHistoryCursor,
    isHistoryLoadingMore: activeHistoryLoadingMore,
    historyLoadError: activeHistoryLoadError,
    isHistoryBootstrapped,
    historyBootstrapStatus,
    hydrationState,
    loadMoreHistory,
    retryHistoryLoad,
    retryHistoryBootstrap,
    hydrateConversation,
  }
}
