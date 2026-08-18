import type { ApiError } from '../shared/http'
import { AuthError, requestJson } from '../shared/http'
import type { LoginRequest, LoginResponse } from './types'
import type { AuthSession } from '../../auth/session'
import { getAuthorizationHeader, getAuthSession, updateAuthUser } from '../../auth/session'
import type { AuthUser } from './types'

export interface BootstrapAuthResult {
  status: 'authenticated' | 'unauthenticated' | 'stale'
  session: AuthSession | null
  error?: ApiError
}

let bootstrapPromise: Promise<BootstrapAuthResult> | null = null
let bootstrapToken: string | null = null

export function login(input: LoginRequest, signal?: AbortSignal) {
  return requestJson<LoginResponse>('/api/auth/login', {
    method: 'POST',
    body: input,
    signal,
    requiresAuth: false,
    suppressAuthFailure: true,
  })
}

export function getCurrentUser(signal?: AbortSignal, options?: { suppressAuthFailure?: boolean }) {
  return requestJson<AuthUser>('/api/auth/me', {
    signal,
    suppressGlobalError: true,
    suppressAuthFailure: options?.suppressAuthFailure ?? false,
  })
}

export function logout(signal?: AbortSignal) {
  const authorization = getAuthorizationHeader()
  return requestJson<null>('/api/auth/logout', {
    method: 'POST',
    headers: authorization ? { Authorization: authorization } : undefined,
    signal,
    suppressGlobalError: true,
    suppressAuthFailure: true,
  })
}

export function bootstrapAuthSession(): Promise<BootstrapAuthResult> {
  const session = getAuthSession()
  if (!session) {
    return Promise.resolve({ status: 'unauthenticated', session: null })
  }

  if (bootstrapPromise && bootstrapToken === session.token) return bootstrapPromise
  bootstrapToken = session.token

  bootstrapPromise = getCurrentUser(undefined, { suppressAuthFailure: true })
    .then((user) => {
      updateAuthUser(user)
      return {
        status: 'authenticated' as const,
        session: getAuthSession(),
      }
    })
    .catch((error) => {
      if (error instanceof AuthError) {
        return {
          status: 'unauthenticated' as const,
          session: null,
        }
      }

      return {
        status: 'stale' as const,
        session,
        error: error as ApiError,
      }
    })
    .finally(() => {
      queueMicrotask(() => {
        bootstrapPromise = null
        bootstrapToken = null
      })
    })

  return bootstrapPromise
}
