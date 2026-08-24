import { act, renderHook } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { useTaskDrawerState } from './useTaskDrawerState'

type MediaController = {
  mediaQuery: MediaQueryList
  setMatches: (matches: boolean) => void
}

const createMediaController = (initialMatches: boolean): MediaController => {
  let matches = initialMatches
  const listeners = new Set<(event: MediaQueryListEvent) => void>()
  const mediaQuery = {
    get matches() { return matches },
    media: '(max-width: 1280px)',
    onchange: null,
    addEventListener: vi.fn((_type: string, listener: (event: MediaQueryListEvent) => void) => listeners.add(listener)),
    removeEventListener: vi.fn((_type: string, listener: (event: MediaQueryListEvent) => void) => listeners.delete(listener)),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(() => true),
  } as unknown as MediaQueryList

  return {
    mediaQuery,
    setMatches(nextMatches) {
      matches = nextMatches
      const event = { matches, media: mediaQuery.media } as MediaQueryListEvent
      listeners.forEach((listener) => listener(event))
    },
  }
}

describe('useTaskDrawerState', () => {
  beforeEach(() => {
    window.sessionStorage.clear()
  })

  it('makes every automatic or restored overlay opening modal and keeps layout openings non-modal', () => {
    const media = createMediaController(true)
    vi.stubGlobal('matchMedia', vi.fn(() => media.mediaQuery))
    const { result, rerender } = renderHook(
      ({ threadId, todoCount }) => useTaskDrawerState({ threadId, todoCount }),
      { initialProps: { threadId: 'thread-a', todoCount: 0 } },
    )

    expect(result.current.open).toBe(false)
    rerender({ threadId: 'thread-a', todoCount: 2 })
    expect(result.current.open).toBe(true)
    expect(result.current.modalActive).toBe(true)

    act(() => media.setMatches(false))
    expect(result.current.open).toBe(true)
    expect(result.current.modalActive).toBe(false)

    act(() => media.setMatches(true))
    expect(result.current.modalActive).toBe(true)
  })

  it('persists explicit close and open choices without losing the overlay invariant', () => {
    const media = createMediaController(true)
    vi.stubGlobal('matchMedia', vi.fn(() => media.mediaQuery))
    const first = renderHook(() => useTaskDrawerState({ threadId: 'thread-a', todoCount: 2 }))

    expect(first.result.current.modalActive).toBe(true)
    act(() => first.result.current.close())
    expect(first.result.current.open).toBe(false)
    expect(window.sessionStorage.getItem('tinkerfin:task-drawer:thread-a')).toBe('closed')
    first.unmount()

    const second = renderHook(() => useTaskDrawerState({ threadId: 'thread-a', todoCount: 2 }))
    expect(second.result.current.open).toBe(false)
    act(() => second.result.current.toggle())
    expect(second.result.current.modalActive).toBe(true)
    expect(window.sessionStorage.getItem('tinkerfin:task-drawer:thread-a')).toBe('open')
  })
})
