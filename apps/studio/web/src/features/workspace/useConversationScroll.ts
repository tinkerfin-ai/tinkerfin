import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from 'react'

import type { Conversation } from '../../types'
import { MOTION_DURATION_MS } from '../../components/ui/motion'

const CONVERSATION_SCROLL_KEY_PREFIX = 'tinkerfin:conversation-scroll:'
const SCROLL_BUTTON_IDLE_MS = 1500
const SCROLL_BUTTON_HIDE_MS = SCROLL_BUTTON_IDLE_MS + MOTION_DURATION_MS.slow
const conversationScrollKey = (threadId: string) => `${CONVERSATION_SCROLL_KEY_PREFIX}${threadId}`

type ScrollButtonPhase = 'hidden' | 'visible' | 'fading'

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

export function useConversationScroll({
  conversation,
  isRunning,
}: {
  conversation: Conversation
  isRunning: boolean
}) {
  const [showScrollToBottom, setShowScrollToBottom] = useState(false)
  const [fadeScrollToBottom, setFadeScrollToBottom] = useState(false)
  const paneRef = useRef<HTMLElement>(null)
  const messageEndRef = useRef<HTMLDivElement>(null)
  const followLatest = useRef(true)
  const pendingImmediateScroll = useRef(false)
  const scrollingToBottom = useRef(false)
  const followScrollFrame = useRef<number | null>(null)
  const scrollMeasureFrame = useRef<number | null>(null)
  const scrollPersistenceFrame = useRef<number | null>(null)
  const pendingScrollPersistence = useRef<{
    threadId: string
    scrollTop: number
  } | null>(null)
  const scrollButtonFadeTimeout = useRef<number | null>(null)
  const scrollButtonHideTimeout = useRef<number | null>(null)
  const scrollButtonHovered = useRef(false)
  const scrollButtonFocused = useRef(false)
  const scrollButtonPhase = useRef<ScrollButtonPhase>('hidden')
  const pendingUserScrollIntent = useRef(false)
  const userHasScrolled = useRef(false)
  const pendingConversationScroll = useRef<{ threadId: string; scrollTop: number | null } | null>(null)
  const lastForcedApprovalIdentity = useRef<string | null>(null)
  const pendingApprovalKey = conversation.approval && !conversation.approval.submitted
    ? conversation.approval.items.map((item) => item.interruptId).join('\u0000')
    : null

  const setScrollButtonPhase = useCallback((phase: ScrollButtonPhase) => {
    scrollButtonPhase.current = phase
    setShowScrollToBottom(phase !== 'hidden')
    setFadeScrollToBottom(phase === 'fading')
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

  const commitPendingScrollPersistence = useCallback(() => {
    const pending = pendingScrollPersistence.current
    pendingScrollPersistence.current = null
    if (pending) writeConversationScrollTop(pending.threadId, pending.scrollTop)
  }, [])

  const flushScrollPersistence = useCallback(() => {
    if (scrollPersistenceFrame.current != null) {
      window.cancelAnimationFrame(scrollPersistenceFrame.current)
      scrollPersistenceFrame.current = null
    }
    commitPendingScrollPersistence()
  }, [commitPendingScrollPersistence])

  const scheduleScrollPersistence = useCallback((threadId: string, scrollTop: number) => {
    pendingScrollPersistence.current = { threadId, scrollTop }
    if (scrollPersistenceFrame.current != null) return
    scrollPersistenceFrame.current = window.requestAnimationFrame(() => {
      scrollPersistenceFrame.current = null
      commitPendingScrollPersistence()
    })
  }, [commitPendingScrollPersistence])

  const armScrollButtonFade = useCallback(() => {
    clearScrollButtonTimers()
    scrollButtonFadeTimeout.current = window.setTimeout(() => {
      setScrollButtonPhase('fading')
    }, SCROLL_BUTTON_IDLE_MS)
    scrollButtonHideTimeout.current = window.setTimeout(() => {
      setScrollButtonPhase('hidden')
    }, SCROLL_BUTTON_HIDE_MS)
  }, [clearScrollButtonTimers, setScrollButtonPhase])

  const handleScroll = useCallback((pane: HTMLElement) => {
    if (conversation.threadId) {
      scheduleScrollPersistence(conversation.threadId, pane.scrollTop)
    }
    if (followScrollFrame.current != null) {
      window.cancelAnimationFrame(followScrollFrame.current)
      followScrollFrame.current = null
    }
    if (scrollMeasureFrame.current != null) return
    scrollMeasureFrame.current = window.requestAnimationFrame(() => {
      scrollMeasureFrame.current = null
      if (pendingUserScrollIntent.current) {
        pendingUserScrollIntent.current = false
        userHasScrolled.current = true
      }
      const isNearBottom = pane.scrollHeight - pane.scrollTop - pane.clientHeight <= 96
      if (isNearBottom) {
        followLatest.current = true
        scrollingToBottom.current = false
        clearScrollButtonTimers()
        setScrollButtonPhase('hidden')
        scrollButtonHovered.current = false
        userHasScrolled.current = false
      } else if (userHasScrolled.current) {
        followLatest.current = false
        if (scrollingToBottom.current) {
          setScrollButtonPhase('hidden')
        } else {
          setScrollButtonPhase('visible')
          if (!scrollButtonHovered.current && !scrollButtonFocused.current) armScrollButtonFade()
        }
      } else if (scrollingToBottom.current) {
        setScrollButtonPhase('hidden')
      } else {
        setScrollButtonPhase('hidden')
      }
    })
  }, [armScrollButtonFade, clearScrollButtonTimers, conversation.threadId, scheduleScrollPersistence, setScrollButtonPhase])

  const scrollToBottomImmediately = useCallback(() => {
    pendingImmediateScroll.current = true
    followLatest.current = true
    scrollingToBottom.current = false
    if (followScrollFrame.current != null) {
      window.cancelAnimationFrame(followScrollFrame.current)
      followScrollFrame.current = null
    }
    clearScrollButtonTimers()
    setScrollButtonPhase('hidden')
    scrollButtonHovered.current = false
    pendingUserScrollIntent.current = false
    userHasScrolled.current = false

    const pane = paneRef.current
    if (pane) {
      pane.scrollTop = pane.scrollHeight
      if (conversation.threadId) {
        scheduleScrollPersistence(conversation.threadId, pane.scrollTop)
      }
    } else {
      messageEndRef.current?.scrollIntoView?.({ behavior: 'auto', block: 'end' })
    }
  }, [clearScrollButtonTimers, conversation.threadId, scheduleScrollPersistence, setScrollButtonPhase])

  const syncToBottomIfFollowing = useCallback(() => {
    if (!followLatest.current) return
    const pane = paneRef.current
    if (!pane) return
    pane.scrollTop = pane.scrollHeight
  }, [])

  const markUserScrollIntent = useCallback(() => {
    scrollingToBottom.current = false
    pendingUserScrollIntent.current = true
  }, [])

  const scrollToBottom = useCallback(() => {
    // 操作完成后按钮会退出可访问树，焦点必须交给仍可继续阅读的对话区域
    paneRef.current?.focus({ preventScroll: true })
    followLatest.current = true
    scrollingToBottom.current = true
    clearScrollButtonTimers()
    setScrollButtonPhase('hidden')
    scrollButtonHovered.current = false
    pendingUserScrollIntent.current = false
    userHasScrolled.current = false
    const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    paneRef.current?.scrollTo({
      top: paneRef.current.scrollHeight,
      behavior: reduceMotion ? 'auto' : 'smooth',
    })
  }, [clearScrollButtonTimers, setScrollButtonPhase])

  const pauseScrollToBottomFade = useCallback(() => {
    scrollButtonHovered.current = true
    clearScrollButtonTimers()
    setScrollButtonPhase('visible')
  }, [clearScrollButtonTimers, setScrollButtonPhase])

  const resumeScrollToBottomFade = useCallback(() => {
    scrollButtonHovered.current = false
    if (!scrollButtonFocused.current && !followLatest.current) armScrollButtonFade()
  }, [armScrollButtonFade])

  const focusScrollToBottom = useCallback(() => {
    scrollButtonFocused.current = true
    clearScrollButtonTimers()
    setScrollButtonPhase('visible')
  }, [clearScrollButtonTimers, setScrollButtonPhase])

  const blurScrollToBottom = useCallback(() => {
    scrollButtonFocused.current = false
    if (!scrollButtonHovered.current && !followLatest.current) armScrollButtonFade()
  }, [armScrollButtonFade])

  const scrollBy = useCallback((deltaY: number) => {
    const pane = paneRef.current
    if (!pane || deltaY === 0) return
    markUserScrollIntent()
    pane.scrollTop += deltaY
    handleScroll(pane)
  }, [handleScroll, markUserScrollIntent])

  useLayoutEffect(() => {
    // 会话切换前先提交旧会话最后一次滚动位置，避免新线程覆盖待写状态
    flushScrollPersistence()
    const savedScrollTop = conversation.threadId
      ? readConversationScrollTop(conversation.threadId)
      : null
    pendingConversationScroll.current = conversation.threadId
      ? { threadId: conversation.threadId, scrollTop: savedScrollTop }
      : null
    lastForcedApprovalIdentity.current = null
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
    pendingUserScrollIntent.current = false
    userHasScrolled.current = false
    clearScrollButtonTimers()
    setScrollButtonPhase('hidden')
    scrollButtonHovered.current = false
    scrollButtonFocused.current = false
  }, [clearScrollButtonTimers, conversation.threadId, flushScrollPersistence, setScrollButtonPhase])

  useLayoutEffect(() => {
    if (!conversation.threadId || !conversation.isHydrated || !pendingApprovalKey) return
    const identity = `${conversation.threadId}:${pendingApprovalKey}`
    if (lastForcedApprovalIdentity.current === identity) return
    lastForcedApprovalIdentity.current = identity
    // 待审批会话需要先让用户看到对话尾部状态，但只能覆盖缓存位置一次
    scrollToBottomImmediately()
  }, [
    conversation.isHydrated,
    conversation.threadId,
    pendingApprovalKey,
    scrollToBottomImmediately,
  ])

  useLayoutEffect(() => {
    const pane = paneRef.current
    if (pendingImmediateScroll.current) {
      pendingImmediateScroll.current = false
      pendingConversationScroll.current = null
      followLatest.current = true
      if (pane) {
        pane.scrollTop = pane.scrollHeight
        if (conversation.threadId) {
          scheduleScrollPersistence(conversation.threadId, pane.scrollTop)
        }
      } else {
        messageEndRef.current?.scrollIntoView?.({ behavior: 'auto', block: 'end' })
      }
      return
    }
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
        handleScroll(pane)
        return
      }
    }
    if (!followLatest.current) return
    if (followScrollFrame.current != null) window.cancelAnimationFrame(followScrollFrame.current)
    followScrollFrame.current = window.requestAnimationFrame(() => {
      followScrollFrame.current = null
      if (!followLatest.current) return
      if (paneRef.current) paneRef.current.scrollTop = paneRef.current.scrollHeight
      else messageEndRef.current?.scrollIntoView?.({ behavior: 'auto', block: 'end' })
      clearScrollButtonTimers()
      setScrollButtonPhase('hidden')
      scrollButtonHovered.current = false
    })
  }, [
    conversation.approval?.submitted,
    conversation.isHydrated,
    conversation.messages,
    conversation.notice,
    conversation.threadId,
    clearScrollButtonTimers,
    handleScroll,
    isRunning,
    setScrollButtonPhase,
    scheduleScrollPersistence,
  ])

  useEffect(() => {
    const measure = () => {
      if (paneRef.current) handleScroll(paneRef.current)
    }
    window.addEventListener('resize', measure)
    return () => window.removeEventListener('resize', measure)
  }, [handleScroll])

  useEffect(() => {
    const handlePageHide = () => flushScrollPersistence()
    window.addEventListener('pagehide', handlePageHide)
    return () => {
      window.removeEventListener('pagehide', handlePageHide)
      flushScrollPersistence()
    }
  }, [flushScrollPersistence])

  useEffect(() => () => {
    clearScrollButtonTimers()
    if (followScrollFrame.current != null) window.cancelAnimationFrame(followScrollFrame.current)
    if (scrollMeasureFrame.current != null) window.cancelAnimationFrame(scrollMeasureFrame.current)
    flushScrollPersistence()
  }, [clearScrollButtonTimers, flushScrollPersistence])

  return {
    paneRef,
    messageEndRef,
    showScrollToBottom,
    fadeScrollToBottom,
    handleScroll,
    scrollToBottomImmediately,
    syncToBottomIfFollowing,
    markUserScrollIntent,
    scrollBy,
    scrollToBottom,
    pauseScrollToBottomFade,
    resumeScrollToBottomFade,
    focusScrollToBottom,
    blurScrollToBottom,
  }
}
