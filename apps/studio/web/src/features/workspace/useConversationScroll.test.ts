import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Conversation } from '../../types'
import { useConversationScroll } from './useConversationScroll'

const conversation: Conversation = {
  threadId: 'thread-scroll',
  title: '滚动测试',
  pinned: false,
  updatedAt: '2026-08-24T00:00:00Z',
  model: 'GPT-5.5',
  mode: 'default',
  messages: [],
  todos: [],
  runStatus: 'idle',
  isHydrated: true,
}

describe('useConversationScroll', () => {
  beforeEach(() => {
    window.sessionStorage.clear()
    vi.useFakeTimers()
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => (
      window.setTimeout(() => callback(performance.now()), 0)
    ))
    vi.stubGlobal('cancelAnimationFrame', (id: number) => window.clearTimeout(id))
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('resets idle timing, fades after 1500ms, hides after 1800ms, and pauses while hovered', () => {
    const { result } = renderHook(() => useConversationScroll({ conversation, isRunning: false }))
    const pane = document.createElement('section')
    Object.defineProperties(pane, {
      clientHeight: { configurable: true, value: 400 },
      scrollHeight: { configurable: true, value: 1200 },
      scrollTop: { configurable: true, writable: true, value: 100 },
    })

    act(() => {
      result.current.markUserScrollIntent()
      result.current.handleScroll(pane)
    })
    act(() => vi.advanceTimersByTime(0))
    expect(result.current.showScrollToBottom).toBe(true)
    expect(result.current.fadeScrollToBottom).toBe(false)

    act(() => vi.advanceTimersByTime(1000))
    act(() => {
      result.current.markUserScrollIntent()
      result.current.handleScroll(pane)
    })
    act(() => vi.advanceTimersByTime(0))
    act(() => vi.advanceTimersByTime(1499))
    expect(result.current.fadeScrollToBottom).toBe(false)
    act(() => vi.advanceTimersByTime(1))
    expect(result.current.fadeScrollToBottom).toBe(true)
    act(() => vi.advanceTimersByTime(300))
    expect(result.current.showScrollToBottom).toBe(false)

    act(() => {
      result.current.markUserScrollIntent()
      result.current.handleScroll(pane)
    })
    act(() => vi.advanceTimersByTime(0))
    act(() => result.current.pauseScrollToBottomFade())
    act(() => vi.advanceTimersByTime(5000))
    expect(result.current.showScrollToBottom).toBe(true)
    expect(result.current.fadeScrollToBottom).toBe(false)

    act(() => result.current.resumeScrollToBottomFade())
    act(() => vi.advanceTimersByTime(1500))
    expect(result.current.fadeScrollToBottom).toBe(true)
  })

  it('does not flash again while rapidly scrolling toward the bottom after the idle hide', () => {
    const { result } = renderHook(() => useConversationScroll({ conversation, isRunning: false }))
    const pane = document.createElement('section')
    Object.defineProperties(pane, {
      clientHeight: { configurable: true, value: 400 },
      scrollHeight: { configurable: true, value: 1200 },
      scrollTop: { configurable: true, writable: true, value: 200 },
    })

    act(() => {
      result.current.markUserScrollIntent()
      result.current.handleScroll(pane)
    })
    act(() => vi.advanceTimersByTime(0))
    expect(result.current.showScrollToBottom).toBe(true)

    act(() => vi.advanceTimersByTime(1800))
    expect(result.current.showScrollToBottom).toBe(false)

    pane.scrollTop = 300
    act(() => {
      result.current.markUserScrollIntent()
      result.current.handleScroll(pane)
    })
    act(() => vi.advanceTimersByTime(0))
    expect(result.current.showScrollToBottom).toBe(false)

    pane.scrollTop = 800
    act(() => {
      result.current.markUserScrollIntent()
      result.current.handleScroll(pane)
    })
    act(() => vi.advanceTimersByTime(0))
    expect(result.current.showScrollToBottom).toBe(false)

    pane.scrollTop = 600
    act(() => {
      result.current.markUserScrollIntent()
      result.current.handleScroll(pane)
    })
    act(() => vi.advanceTimersByTime(0))
    expect(result.current.showScrollToBottom).toBe(true)
  })

  it('keeps the scroll action visible while it owns keyboard focus', () => {
    const { result } = renderHook(() => useConversationScroll({ conversation, isRunning: false }))
    const pane = document.createElement('section')
    Object.defineProperties(pane, {
      clientHeight: { configurable: true, value: 400 },
      scrollHeight: { configurable: true, value: 1200 },
      scrollTop: { configurable: true, writable: true, value: 100 },
    })

    act(() => {
      result.current.markUserScrollIntent()
      result.current.handleScroll(pane)
    })
    act(() => vi.advanceTimersByTime(0))
    act(() => result.current.focusScrollToBottom())
    act(() => vi.advanceTimersByTime(5000))
    expect(result.current.showScrollToBottom).toBe(true)
    expect(result.current.fadeScrollToBottom).toBe(false)

    act(() => result.current.blurScrollToBottom())
    act(() => vi.advanceTimersByTime(1500))
    expect(result.current.fadeScrollToBottom).toBe(true)
  })

  it('把回到底部操作的焦点交给对话区域后再隐藏按钮', () => {
    const { result } = renderHook(() => useConversationScroll({ conversation, isRunning: false }))
    const pane = document.createElement('section')
    pane.tabIndex = 0
    pane.scrollTo = vi.fn()
    Object.defineProperty(pane, 'scrollHeight', { configurable: true, value: 1200 })
    document.body.append(pane)
    const action = document.createElement('button')
    document.body.append(action)
    action.focus()
    result.current.paneRef.current = pane

    act(() => result.current.scrollToBottom())

    expect(pane).toHaveFocus()
    expect(pane.scrollTo).toHaveBeenCalledWith({ top: 1200, behavior: 'smooth' })
    expect(result.current.showScrollToBottom).toBe(false)
    act(() => vi.runOnlyPendingTimers())
    pane.remove()
    action.remove()
  })

  it('每帧合并滚动持久化，并在会话切换和卸载时提交最后位置', () => {
    const secondConversation = { ...conversation, threadId: 'thread-scroll-second' }
    const { result, rerender, unmount } = renderHook(
      ({ currentConversation }) => useConversationScroll({
        conversation: currentConversation,
        isRunning: false,
      }),
      { initialProps: { currentConversation: conversation } },
    )
    const pane = document.createElement('section')
    Object.defineProperties(pane, {
      clientHeight: { configurable: true, value: 400 },
      scrollHeight: { configurable: true, value: 1200 },
      scrollTop: { configurable: true, writable: true, value: 100 },
    })

    act(() => result.current.handleScroll(pane))
    pane.scrollTop = 180
    act(() => result.current.handleScroll(pane))
    expect(window.sessionStorage.getItem('tinkerfin:conversation-scroll:thread-scroll')).toBeNull()

    rerender({ currentConversation: secondConversation })
    expect(window.sessionStorage.getItem('tinkerfin:conversation-scroll:thread-scroll')).toBe('180')

    pane.scrollTop = 260
    act(() => result.current.handleScroll(pane))
    unmount()
    expect(window.sessionStorage.getItem('tinkerfin:conversation-scroll:thread-scroll-second')).toBe('260')
  })

  it('页面进入后台前提交尚未执行的滚动写入', () => {
    const { result } = renderHook(() => useConversationScroll({ conversation, isRunning: false }))
    const pane = document.createElement('section')
    Object.defineProperties(pane, {
      clientHeight: { configurable: true, value: 400 },
      scrollHeight: { configurable: true, value: 1200 },
      scrollTop: { configurable: true, writable: true, value: 320 },
    })

    act(() => result.current.handleScroll(pane))
    act(() => window.dispatchEvent(new Event('pagehide')))

    expect(window.sessionStorage.getItem('tinkerfin:conversation-scroll:thread-scroll')).toBe('320')
    act(() => vi.runOnlyPendingTimers())
  })

  it('restores an off-bottom conversation position without treating it as user scrolling', () => {
    const firstConversation = { ...conversation, threadId: 'thread-first' }
    const secondConversation = { ...conversation, threadId: 'thread-second' }
    window.sessionStorage.setItem('tinkerfin:conversation-scroll:thread-second', '240')
    const { result, rerender } = renderHook(
      ({ currentConversation }) => useConversationScroll({
        conversation: currentConversation,
        isRunning: false,
      }),
      { initialProps: { currentConversation: firstConversation } },
    )
    const pane = document.createElement('section')
    Object.defineProperties(pane, {
      clientHeight: { configurable: true, value: 400 },
      scrollHeight: { configurable: true, value: 1200 },
      scrollTop: { configurable: true, writable: true, value: 0 },
    })
    result.current.paneRef.current = pane

    rerender({ currentConversation: secondConversation })
    act(() => vi.advanceTimersByTime(0))

    expect(pane.scrollTop).toBe(240)
    expect(result.current.showScrollToBottom).toBe(false)

    pane.scrollTop = 120
    act(() => {
      result.current.markUserScrollIntent()
      result.current.handleScroll(pane)
    })
    act(() => vi.advanceTimersByTime(0))
    expect(result.current.showScrollToBottom).toBe(true)
  })

  it('treats wheel input forwarded from the composer as user scrolling', () => {
    const { result } = renderHook(() => useConversationScroll({ conversation, isRunning: false }))
    const pane = document.createElement('section')
    Object.defineProperties(pane, {
      clientHeight: { configurable: true, value: 400 },
      scrollHeight: { configurable: true, value: 1200 },
      scrollTop: { configurable: true, writable: true, value: 300 },
    })
    result.current.paneRef.current = pane

    act(() => result.current.scrollBy(-120))
    act(() => vi.advanceTimersByTime(0))

    expect(pane.scrollTop).toBe(180)
    expect(result.current.showScrollToBottom).toBe(true)
  })
})
