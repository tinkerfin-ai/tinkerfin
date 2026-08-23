import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from 'react'

import type { Conversation } from '../../types'

const CONVERSATION_SCROLL_KEY_PREFIX = 'tinkerfin:conversation-scroll:'
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

export function useConversationScroll({
  conversation,
  isRunning,
}: {
  conversation: Conversation
  isRunning: boolean
}) {
  const [showScrollToBottom, setShowScrollToBottom] = useState(false)
  const paneRef = useRef<HTMLElement>(null)
  const messageEndRef = useRef<HTMLDivElement>(null)
  const followLatest = useRef(true)
  const pendingImmediateScroll = useRef(false)
  const scrollingToBottom = useRef(false)
  const followScrollFrame = useRef<number | null>(null)
  const scrollMeasureFrame = useRef<number | null>(null)
  const pendingConversationScroll = useRef<{ threadId: string; scrollTop: number | null } | null>(null)

  const handleScroll = useCallback((pane: HTMLElement) => {
    if (conversation.threadId) writeConversationScrollTop(conversation.threadId, pane.scrollTop)
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
        setShowScrollToBottom(false)
      } else if (scrollingToBottom.current) {
        setShowScrollToBottom(false)
      } else {
        setShowScrollToBottom(true)
      }
    })
  }, [conversation.threadId])

  const scrollToBottomImmediately = useCallback(() => {
    pendingImmediateScroll.current = true
    followLatest.current = true
    scrollingToBottom.current = false
    if (followScrollFrame.current != null) {
      window.cancelAnimationFrame(followScrollFrame.current)
      followScrollFrame.current = null
    }
    setShowScrollToBottom(false)

    const pane = paneRef.current
    if (pane) {
      pane.scrollTop = pane.scrollHeight
      if (conversation.threadId) writeConversationScrollTop(conversation.threadId, pane.scrollTop)
    } else {
      messageEndRef.current?.scrollIntoView?.({ behavior: 'auto', block: 'end' })
    }
  }, [conversation.threadId])

  const markUserScrollIntent = useCallback(() => {
    scrollingToBottom.current = false
  }, [])

  const scrollToBottom = useCallback(() => {
    followLatest.current = true
    scrollingToBottom.current = true
    setShowScrollToBottom(false)
    const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    paneRef.current?.scrollTo({
      top: paneRef.current.scrollHeight,
      behavior: reduceMotion ? 'auto' : 'smooth',
    })
  }, [])

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
    setShowScrollToBottom(false)
  }, [conversation.threadId])

  useLayoutEffect(() => {
    const pane = paneRef.current
    if (pendingImmediateScroll.current) {
      pendingImmediateScroll.current = false
      pendingConversationScroll.current = null
      followLatest.current = true
      if (pane) {
        pane.scrollTop = pane.scrollHeight
        if (conversation.threadId) writeConversationScrollTop(conversation.threadId, pane.scrollTop)
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
      setShowScrollToBottom(false)
    })
  }, [
    conversation.approval?.submitted,
    conversation.isHydrated,
    conversation.messages,
    conversation.notice,
    conversation.threadId,
    handleScroll,
    isRunning,
  ])

  useEffect(() => {
    const measure = () => {
      if (paneRef.current) handleScroll(paneRef.current)
    }
    window.addEventListener('resize', measure)
    return () => window.removeEventListener('resize', measure)
  }, [handleScroll])

  useEffect(() => () => {
    if (followScrollFrame.current != null) window.cancelAnimationFrame(followScrollFrame.current)
    if (scrollMeasureFrame.current != null) window.cancelAnimationFrame(scrollMeasureFrame.current)
  }, [])

  return {
    paneRef,
    messageEndRef,
    showScrollToBottom,
    handleScroll,
    scrollToBottomImmediately,
    markUserScrollIntent,
    scrollToBottom,
  }
}
