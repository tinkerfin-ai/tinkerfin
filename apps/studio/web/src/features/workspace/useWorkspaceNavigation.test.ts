import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useWorkspaceNavigation } from './useWorkspaceNavigation'

interface MutableMediaQueryList extends MediaQueryList {
  setMatches: (matches: boolean) => void
}

const mediaQueries = new Map<string, MutableMediaQueryList>()

const createMediaQuery = (query: string): MutableMediaQueryList => {
  const listeners = new Set<(event: MediaQueryListEvent) => void>()
  let currentMatches = false
  const media = {
    get matches() { return currentMatches },
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn((_type: string, listener: EventListenerOrEventListenerObject) => {
      if (typeof listener === 'function') listeners.add(listener as (event: MediaQueryListEvent) => void)
    }),
    removeEventListener: vi.fn((_type: string, listener: EventListenerOrEventListenerObject) => {
      if (typeof listener === 'function') listeners.delete(listener as (event: MediaQueryListEvent) => void)
    }),
    dispatchEvent: vi.fn(() => true),
    setMatches(matches: boolean) {
      currentMatches = matches
      const event = { matches, media: query } as MediaQueryListEvent
      listeners.forEach((listener) => listener(event))
    },
  } as MutableMediaQueryList
  return media
}

const query = (value: string) => {
  if (!mediaQueries.has(value)) mediaQueries.set(value, createMediaQuery(value))
  return mediaQueries.get(value)!
}

const setViewport = ({ desktop, tablet }: { desktop: boolean; tablet: boolean }) => {
  query('(min-width: 1024px)').setMatches(desktop)
  query('(min-width: 768px)').setMatches(tablet)
}

describe('useWorkspaceNavigation', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    mediaQueries.clear()
    Object.defineProperty(window, 'matchMedia', {
      configurable: true,
      value: (value: string) => query(value),
    })
    setViewport({ desktop: true, tablet: true })
    query('(prefers-reduced-motion: reduce)').setMatches(false)
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('uses expanded desktop by default and settles a collapse in 100/300ms phases', () => {
    const { result } = renderHook(() => useWorkspaceNavigation())
    expect(result.current.mode).toBe('expanded')
    expect(result.current.wideInteractive).toBe(true)

    act(() => result.current.toggleDesktopMode())
    expect(result.current.mode).toBe('rail')
    expect(result.current.wideInteractive).toBe(true)

    act(() => vi.advanceTimersByTime(100))
    expect(result.current.wideInteractive).toBe(false)
    expect(result.current.railInteractive).toBe(true)
    act(() => vi.advanceTimersByTime(250))
    expect(result.current.settledMode).toBe('rail')
  })

  it('cancels stale settle work when the direction reverses quickly', () => {
    const { result } = renderHook(() => useWorkspaceNavigation())
    act(() => result.current.toggleDesktopMode())
    act(() => vi.advanceTimersByTime(50))
    act(() => result.current.toggleDesktopMode())
    act(() => vi.advanceTimersByTime(400))

    expect(result.current.mode).toBe('expanded')
    expect(result.current.settledMode).toBe('expanded')
    expect(result.current.wideInteractive).toBe(true)
    expect(result.current.railInteractive).toBe(false)
  })

  it('settles immediately without JavaScript delay when reduced motion is active', () => {
    query('(prefers-reduced-motion: reduce)').setMatches(true)
    const { result } = renderHook(() => useWorkspaceNavigation())
    act(() => result.current.toggleDesktopMode())

    expect(result.current.mode).toBe('rail')
    expect(result.current.settledMode).toBe('rail')
    expect(result.current.wideInteractive).toBe(false)
    expect(result.current.railInteractive).toBe(true)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('defaults tablet to rail and clears that override on crossing 1024px', () => {
    setViewport({ desktop: false, tablet: true })
    const { result } = renderHook(() => useWorkspaceNavigation())
    expect(result.current.mode).toBe('rail')

    act(() => setViewport({ desktop: true, tablet: true }))
    expect(result.current.mode).toBe('expanded')
  })
})
