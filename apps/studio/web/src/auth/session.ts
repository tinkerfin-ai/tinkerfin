import type { LoginResponse } from '../api/auth/types'
import type { AuthUser } from '../api/auth/types'

export const AUTH_SESSION_STORAGE_KEY = 'tinkerfin.auth.session.v1'

export interface AuthSession {
  token: string
  tokenType: string
  expiresAt: string | null
  user: AuthUser
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

function canUseStorage() {
  return typeof window !== 'undefined' && typeof window.localStorage !== 'undefined'
}

function isAuthUser(value: unknown): value is AuthUser {
  if (!value || typeof value !== 'object') return false
  const candidate = value as Partial<AuthUser>
  return typeof candidate.user_id === 'number'
    && typeof candidate.username === 'string'
    && typeof candidate.display_name === 'string'
    && Array.isArray(candidate.roles)
    && typeof candidate.disabled === 'boolean'
}

function isAuthSession(value: unknown): value is AuthSession {
  if (!value || typeof value !== 'object') return false
  const candidate = value as Partial<AuthSession>
  return typeof candidate.token === 'string'
    && candidate.token.length > 0
    && typeof candidate.tokenType === 'string'
    && (typeof candidate.expiresAt === 'string' || candidate.expiresAt === null)
    && isAuthUser(candidate.user)
}

function readStoredSession(): AuthSession | null {
  if (!canUseStorage()) return null
  const raw = window.localStorage.getItem(AUTH_SESSION_STORAGE_KEY)
  if (!raw) return null
  try {
    const parsed = JSON.parse(raw) as unknown
    return isAuthSession(parsed) ? parsed : null
  } catch {
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
  return currentSession
}

export function saveAuthSession(session: AuthSession) {
  currentSession = session
  persistSession(session)
  notifySessionListeners(session)
}

export function clearAuthSession() {
  currentSession = null
  persistSession(null)
  notifySessionListeners(null)
}

export function updateAuthUser(user: AuthUser) {
  const session = getAuthSession()
  if (!session) return
  saveAuthSession({ ...session, user })
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
    expiresAt: payload.expires_in > 0
      ? new Date(Date.now() + payload.expires_in * 1000).toISOString()
      : null,
    user: payload.user,
  }
}
