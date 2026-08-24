import type { AuthSessionResponse, AuthUser, LoginResponse } from '../api/auth/types'

export const AUTH_SESSION_STORAGE_KEY = 'tinkerfin.auth.session.v1'

export interface AuthSession {
  token: string
  tokenType: string
  expiresAt: string
  user: AuthUser
}

export interface AuthSessionLifecycleOptions {
  onExternalSession?: (session: AuthSession) => void
}

export interface AuthFailureEvent {
  code: number
  message: string
}

type SessionListener = (session: AuthSession | null) => void
type FailureListener = (event: AuthFailureEvent) => void

const sessionListeners = new Set<SessionListener>()
const failureListeners = new Set<FailureListener>()

let currentSession: AuthSession | null | undefined

function normalizeExpiresAt(value: string): string {
  const timestamp = Date.parse(value)
  if (!Number.isFinite(timestamp)) throw new TypeError('登录接口返回的固定到期时间无效')
  return new Date(timestamp).toISOString()
}

function sessionsEqual(first: AuthSession | null | undefined, second: AuthSession | null) {
  if (first == null || second == null) return first == null && second == null
  return first.token === second.token
    && first.tokenType === second.tokenType
    && first.expiresAt === second.expiresAt
    && first.user.user_id === second.user.user_id
    && first.user.username === second.user.username
    && first.user.display_name === second.user.display_name
    && first.user.avatar_url === second.user.avatar_url
    && first.user.disabled === second.user.disabled
    && first.user.roles.length === second.user.roles.length
    && first.user.roles.every((role, index) => role === second.user.roles[index])
}

function canUseStorage() {
  return typeof window !== 'undefined' && typeof window.localStorage !== 'undefined'
}

function normalizeAuthUser(value: unknown): AuthUser | null {
  if (!value || typeof value !== 'object') return null
  const candidate = value as Partial<AuthUser>
  if (!(typeof candidate.user_id === 'number'
    && typeof candidate.username === 'string'
    && typeof candidate.display_name === 'string'
    && (candidate.avatar_url == null || typeof candidate.avatar_url === 'string')
    && Array.isArray(candidate.roles)
    && candidate.roles.every((role) => typeof role === 'string')
    && typeof candidate.disabled === 'boolean')) return null
  return {
    user_id: candidate.user_id,
    username: candidate.username,
    display_name: candidate.display_name,
    avatar_url: candidate.avatar_url ?? null,
    roles: candidate.roles,
    disabled: candidate.disabled,
  }
}

function normalizeAuthSession(value: unknown): AuthSession | null {
  if (!value || typeof value !== 'object') return null
  const candidate = value as Partial<AuthSession>
  const user = normalizeAuthUser(candidate.user)
  if (!(typeof candidate.token === 'string'
    && candidate.token.length > 0
    && typeof candidate.tokenType === 'string'
    && typeof candidate.expiresAt === 'string'
    && Number.isFinite(Date.parse(candidate.expiresAt))
    && user != null)) return null
  return {
    token: candidate.token,
    tokenType: candidate.tokenType,
    expiresAt: candidate.expiresAt,
    user,
  }
}

export function isAuthSessionExpired(session: AuthSession, now = Date.now()): boolean {
  return now >= Date.parse(session.expiresAt)
}

function readStoredSession(): AuthSession | null {
  if (!canUseStorage()) return null
  const raw = window.localStorage.getItem(AUTH_SESSION_STORAGE_KEY)
  if (!raw) return null
  try {
    const parsed = JSON.parse(raw) as unknown
    const normalized = normalizeAuthSession(parsed)
    if (!normalized || isAuthSessionExpired(normalized)) {
      window.localStorage.removeItem(AUTH_SESSION_STORAGE_KEY)
      return null
    }
    return {
      ...normalized,
      expiresAt: normalizeExpiresAt(normalized.expiresAt),
    }
  } catch {
    window.localStorage.removeItem(AUTH_SESSION_STORAGE_KEY)
    return null
  }
}

function persistSession(session: AuthSession | null) {
  if (!canUseStorage()) return
  if (session) {
    window.localStorage.setItem(AUTH_SESSION_STORAGE_KEY, JSON.stringify(session))
  } else {
    window.localStorage.removeItem(AUTH_SESSION_STORAGE_KEY)
  }
}

function notifySessionListeners(session: AuthSession | null) {
  for (const listener of sessionListeners) listener(session)
}

export function getAuthSession(): AuthSession | null {
  if (currentSession === undefined) currentSession = readStoredSession()
  if (currentSession && isAuthSessionExpired(currentSession)) {
    clearAuthSession()
    return null
  }
  return currentSession
}

export function saveAuthSession(session: AuthSession) {
  const normalized = {
    ...session,
    expiresAt: normalizeExpiresAt(session.expiresAt),
  }
  if (isAuthSessionExpired(normalized)) {
    clearAuthSession()
    return
  }
  currentSession = normalized
  persistSession(normalized)
  notifySessionListeners(normalized)
}

export function clearAuthSession() {
  const hadSession = currentSession != null
    || (canUseStorage() && window.localStorage.getItem(AUTH_SESSION_STORAGE_KEY) != null)
  currentSession = null
  persistSession(null)
  if (hadSession) notifySessionListeners(null)
}

export function updateAuthSession(payload: AuthSessionResponse): AuthSession | null {
  const session = getAuthSession()
  if (!session) return null
  saveAuthSession({
    ...session,
    expiresAt: normalizeExpiresAt(payload.expires_at),
    user: payload.user,
  })
  return getAuthSession()
}

export function subscribeAuthSession(listener: SessionListener) {
  sessionListeners.add(listener)
  return () => {
    sessionListeners.delete(listener)
  }
}

export function subscribeAuthFailure(listener: FailureListener) {
  failureListeners.add(listener)
  return () => {
    failureListeners.delete(listener)
  }
}

export function notifyAuthFailure(event: AuthFailureEvent) {
  for (const listener of failureListeners) listener(event)
}

export function getAuthorizationHeader(): string | null {
  const session = getAuthSession()
  if (!session) return null
  return `${session.tokenType || 'Bearer'} ${session.token}`
}

export function createAuthSession(payload: LoginResponse): AuthSession {
  return {
    token: payload.access_token,
    tokenType: payload.token_type || 'Bearer',
    expiresAt: normalizeExpiresAt(payload.expires_at),
    user: payload.user,
  }
}

export function startAuthSessionLifecycle(
  options: AuthSessionLifecycleOptions = {},
): () => void {
  let expiryTimer: number | null = null

  const clearExpiryTimer = () => {
    if (expiryTimer == null) return
    window.clearTimeout(expiryTimer)
    expiryTimer = null
  }

  const scheduleExpiry = (session: AuthSession | null) => {
    clearExpiryTimer()
    if (!session) return
    const delay = Date.parse(session.expiresAt) - Date.now()
    if (delay <= 0) {
      clearAuthSession()
      return
    }
    expiryTimer = window.setTimeout(() => {
      expiryTimer = null
      const current = getAuthSession()
      if (current) scheduleExpiry(current)
    }, Math.min(delay, 2_147_483_647))
  }

  const recheckExpiry = () => scheduleExpiry(getAuthSession())
  const handleVisibilityChange = () => {
    if (document.visibilityState === 'visible') recheckExpiry()
  }
  const handleStorage = (event: StorageEvent) => {
    if (event.key !== AUTH_SESSION_STORAGE_KEY) return
    const previous = currentSession
    const next = readStoredSession()
    if (sessionsEqual(previous, next)) return
    currentSession = next
    notifySessionListeners(next)
    if (next) options.onExternalSession?.(next)
  }

  const unsubscribe = subscribeAuthSession(scheduleExpiry)
  window.addEventListener('focus', recheckExpiry)
  window.addEventListener('storage', handleStorage)
  document.addEventListener('visibilitychange', handleVisibilityChange)
  scheduleExpiry(getAuthSession())

  return () => {
    clearExpiryTimer()
    unsubscribe()
    window.removeEventListener('focus', recheckExpiry)
    window.removeEventListener('storage', handleStorage)
    document.removeEventListener('visibilitychange', handleVisibilityChange)
  }
}
