import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  normalizeAppLocation,
  readThreadFromLocation,
  writeThreadToLocation,
} from './threadRoute'

describe('threadRoute', () => {
  beforeEach(() => {
    window.sessionStorage.clear()
    window.history.replaceState(null, '', '/')
  })

  afterEach(() => {
    window.history.replaceState(null, '', '/')
  })

  it('reads the thread id from ?thread=', () => {
    window.history.replaceState(null, '', '/?thread=abc-123')
    expect(readThreadFromLocation()).toBe('abc-123')
  })

  it('returns an empty string when ?thread is absent', () => {
    window.history.replaceState(null, '', '/')
    expect(readThreadFromLocation()).toBe('')
  })

  it('writes the thread id into the URL', () => {
    writeThreadToLocation('thread-xyz')
    expect(window.location.search).toContain('thread=thread-xyz')
    expect(readThreadFromLocation()).toBe('thread-xyz')
  })

  it('removes the param when given an empty thread id', () => {
    window.history.replaceState(null, '', '/?thread=stale')
    writeThreadToLocation('')
    expect(window.location.search).toBe('')
    expect(readThreadFromLocation()).toBe('')
  })

  it('does not call replaceState when the URL already matches', () => {
    window.history.replaceState(null, '', '/?thread=already')
    const replaceState = vi.spyOn(window.history, 'replaceState')
    writeThreadToLocation('already')
    expect(replaceState).not.toHaveBeenCalled()
  })

  it('restores the last valid thread URL when an unsupported path is opened', () => {
    window.history.replaceState(null, '', '/?thread=remembered-thread')
    normalizeAppLocation()
    window.history.replaceState(null, '', '/sssssssssssssssd#top')

    normalizeAppLocation()

    expect(window.location.pathname).toBe('/')
    expect(window.location.search).toBe('?thread=remembered-thread')
    expect(window.location.hash).toBe('')
  })

  it('falls back to the app root when no valid location was recorded', () => {
    window.history.replaceState(null, '', '/not-a-route?thread=unknown#top')

    normalizeAppLocation()

    expect(window.location.pathname).toBe('/')
    expect(window.location.search).toBe('')
    expect(window.location.hash).toBe('')
  })

  it('keeps conversation scroll state while correcting an unsupported path', () => {
    window.history.replaceState(null, '', '/?thread=scroll-thread')
    normalizeAppLocation()
    window.sessionStorage.setItem('tinkerfin:conversation-scroll:scroll-thread', '240')
    window.history.replaceState(null, '', '/wrong-address')

    normalizeAppLocation()

    expect(readThreadFromLocation()).toBe('scroll-thread')
    expect(window.sessionStorage.getItem('tinkerfin:conversation-scroll:scroll-thread')).toBe('240')
  })
})
