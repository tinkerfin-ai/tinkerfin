import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { LoginResponse } from '../api/auth/types'
import {
  AUTH_SESSION_STORAGE_KEY,
  clearAuthSession,
  createAuthSession,
  getAuthorizationHeader,
  getAuthSession,
  saveAuthSession,
  startAuthSessionLifecycle,
} from './session'

const user = {
  user_id: 7,
  username: 'yunsan',
  display_name: '云杉',
  avatar_url: null,
  roles: [],
  disabled: false,
}

function loginPayload(expiresAt: string): LoginResponse {
  return {
    access_token: 'token-123',
    token_type: 'Bearer',
    expires_at: expiresAt,
    user,
  }
}

describe('auth session lifecycle', () => {
  beforeEach(() => {
    window.localStorage.clear()
    clearAuthSession()
  })

  afterEach(() => {
    clearAuthSession()
    vi.restoreAllMocks()
    vi.useRealTimers()
  })

  it('stores the exact absolute deadline returned by the backend', () => {
    const session = createAuthSession(loginPayload('2026-08-23T10:00:00Z'))

    expect(session.expiresAt).toBe('2026-08-23T10:00:00.000Z')
  })

  it('normalizes a stored v1 session without avatar_url instead of signing out', () => {
    const legacy = createAuthSession(loginPayload('2099-01-01T00:00:00Z'))
    const legacyUser = { ...legacy.user } as Partial<typeof legacy.user>
    delete legacyUser.avatar_url
    const serialized = JSON.stringify({
      ...legacy,
      user: legacyUser,
    })
    const stop = startAuthSessionLifecycle()
    window.localStorage.setItem(AUTH_SESSION_STORAGE_KEY, serialized)
    window.dispatchEvent(new StorageEvent('storage', {
      key: AUTH_SESSION_STORAGE_KEY,
      newValue: serialized,
    }))

    expect(getAuthSession()?.user.avatar_url).toBeNull()
    expect(window.localStorage.getItem(AUTH_SESSION_STORAGE_KEY)).not.toBeNull()
    stop()
  })

  it('rejects a login response without a valid absolute deadline', () => {
    expect(() => createAuthSession(loginPayload('not-a-date'))).toThrow(
      '登录接口返回的固定到期时间无效',
    )
  })

  it('clears the session exactly when its fixed deadline is reached', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-08-22T10:00:00Z'))
    saveAuthSession(createAuthSession(loginPayload('2026-08-22T10:00:01Z')))
    const stop = startAuthSessionLifecycle()

    await vi.advanceTimersByTimeAsync(999)
    expect(getAuthSession()).not.toBeNull()

    await vi.advanceTimersByTimeAsync(1)
    expect(getAuthSession()).toBeNull()
    expect(window.localStorage.getItem(AUTH_SESSION_STORAGE_KEY)).toBeNull()
    stop()
  })

  it('rechecks the deadline before sending an authenticated request', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-08-22T10:00:00Z'))
    saveAuthSession(createAuthSession(loginPayload('2026-08-22T10:00:01Z')))

    vi.setSystemTime(new Date('2026-08-22T10:00:02Z'))

    expect(getAuthorizationHeader()).toBeNull()
    expect(window.localStorage.getItem(AUTH_SESSION_STORAGE_KEY)).toBeNull()
  })

  it('rechecks an overdue session when the page regains focus', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-08-22T10:00:00Z'))
    saveAuthSession(createAuthSession(loginPayload('2026-08-22T10:00:01Z')))
    const stop = startAuthSessionLifecycle()

    vi.setSystemTime(new Date('2026-08-22T10:00:02Z'))
    window.dispatchEvent(new Event('focus'))

    expect(getAuthSession()).toBeNull()
    stop()
  })

  it('rechecks an overdue session when a background page becomes visible', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-08-22T10:00:00Z'))
    saveAuthSession(createAuthSession(loginPayload('2026-08-22T10:00:01Z')))
    const stop = startAuthSessionLifecycle()
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')

    vi.setSystemTime(new Date('2026-08-22T10:00:02Z'))
    document.dispatchEvent(new Event('visibilitychange'))

    expect(getAuthSession()).toBeNull()
    stop()
  })

  it('synchronizes external replacement internally and external removal as logout', () => {
    saveAuthSession(createAuthSession(loginPayload('2099-01-01T00:00:00Z')))
    const onExternalSession = vi.fn()
    const stop = startAuthSessionLifecycle({ onExternalSession })
    const replacement = {
      ...createAuthSession(loginPayload('2099-02-01T00:00:00Z')),
      token: 'new-token',
    }
    window.localStorage.setItem(AUTH_SESSION_STORAGE_KEY, JSON.stringify(replacement))

    window.dispatchEvent(new StorageEvent('storage', {
      key: AUTH_SESSION_STORAGE_KEY,
      newValue: JSON.stringify(replacement),
    }))

    expect(getAuthSession()?.token).toBe('new-token')
    expect(onExternalSession).toHaveBeenCalledWith(replacement)

    window.localStorage.removeItem(AUTH_SESSION_STORAGE_KEY)
    window.dispatchEvent(new StorageEvent('storage', {
      key: AUTH_SESSION_STORAGE_KEY,
      newValue: null,
    }))

    expect(getAuthSession()).toBeNull()
    stop()
  })
})
