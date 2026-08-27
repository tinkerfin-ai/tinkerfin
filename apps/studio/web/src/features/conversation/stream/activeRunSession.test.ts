import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ChatRequestPayload } from '../../../api/conversation/types'
import {
  clearActiveRunSession,
  readActiveRunSession,
  writeActiveRunSession,
} from './activeRunSession'

const payload: ChatRequestPayload = {
  threadId: 'thread-active',
  runId: 'run-active',
  state: {},
  messages: [{ id: 'request-run-active', role: 'user', content: '继续输出' }],
  tools: [],
  context: [],
  forwardedProps: { model: 'main', command: { plan: 'off' } },
}

describe('active run session', () => {
  beforeEach(() => window.sessionStorage.clear())
  afterEach(() => vi.restoreAllMocks())

  it('round-trips one active run and only its owner can clear it', () => {
    writeActiveRunSession({
      threadId: 'thread-active',
      payload,
      mode: 'start',
      lastSeq: 41,
    })

    expect(readActiveRunSession()).toEqual({
      threadId: 'thread-active',
      payload,
      mode: 'start',
      lastSeq: 41,
    })
    clearActiveRunSession('other-run')
    expect(readActiveRunSession()?.payload.runId).toBe('run-active')
    clearActiveRunSession('run-active')
    expect(readActiveRunSession()).toBeNull()
  })

  it('rejects a persisted protocol message without its required ID', () => {
    window.sessionStorage.setItem('tinkerfin:active-conversation-run', JSON.stringify({
      threadId: 'thread-active',
      payload: {
        ...payload,
        messages: [{ role: 'user', content: 'invalid' }],
      },
      mode: 'start',
      lastSeq: 1,
    }))

    expect(readActiveRunSession()).toBeNull()
    expect(window.sessionStorage.getItem('tinkerfin:active-conversation-run')).toBeNull()
  })

  it('rejects the removed versioned storage shape', () => {
    window.sessionStorage.setItem('tinkerfin:active-conversation-run', JSON.stringify({
      schemaVersion: 1,
      threadId: 'thread-active',
      payload,
      mode: 'start',
      lastSeq: 1,
    }))

    expect(readActiveRunSession()).toBeNull()
    expect(window.sessionStorage.getItem('tinkerfin:active-conversation-run')).toBeNull()
  })

  it('returns null when session storage access and cleanup are both blocked', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new DOMException('blocked', 'SecurityError')
    })
    vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => {
      throw new DOMException('blocked', 'SecurityError')
    })

    expect(readActiveRunSession()).toBeNull()
  })
})
