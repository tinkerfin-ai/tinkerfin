import { useCallback, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'

import {
  login,
  logout as logoutApi,
} from './api/auth/client'
import { AuthScreen } from './features/auth/AuthScreen'
import { LoginTransition } from './features/auth/LoginTransition'
import { useAuthVerification } from './features/auth/useAuthVerification'
import { WorkspaceScreen } from './features/workspace/WorkspaceScreen'
import { ApiError, AuthError, subscribeApiErrors } from './api/shared/http'
import { ToastViewport } from './components/ToastViewport'
import type { ToastItem, ToastKind } from './components/ToastViewport'
import {
  clearAuthSession,
  createAuthSession,
  getAuthSession,
  saveAuthSession,
  startAuthSessionLifecycle,
  subscribeAuthSession,
} from './auth/session'
import { writeThreadToLocation } from './lib/threadRoute'
import { clearActiveRunSession } from './features/conversation/stream/activeRunSession'

type AuthPhase = 'checking' | 'signedOut' | 'transitioning' | 'signedIn'
type AuthEntry = 'restore' | 'manual'
let toastSequence = 0

export default function App() {
  const [phase, setPhase] = useState<AuthPhase>(() => (
    getAuthSession() ? 'checking' : 'signedOut'
  ))
  const [loginError, setLoginError] = useState<string>()
  const [isLoginPending, setLoginPending] = useState(false)
  const [authCheckVersion, setAuthCheckVersion] = useState(0)
  const [toasts, setToasts] = useState<ToastItem[]>([])
  const authEntry = useRef<AuthEntry>('restore')
  const isAuthRetrying = useAuthVerification({
    enabled: phase === 'checking',
    version: authCheckVersion,
    onAuthenticated: () => {
      const entry = authEntry.current
      authEntry.current = 'restore'
      setPhase(
        entry === 'manual'
          && !window.matchMedia('(prefers-reduced-motion: reduce)').matches
          ? 'transitioning'
          : 'signedIn',
      )
    },
    onUnauthenticated: () => {
      clearAuthSession()
      setPhase('signedOut')
    },
  })

  const pushToast = useCallback((kind: ToastKind, message: string) => {
    toastSequence += 1
    setToasts((current) => [
      ...current,
      { id: `toast-${toastSequence}`, kind, message },
    ].slice(-4))
  }, [])

  useEffect(() => subscribeApiErrors((error) => {
    pushToast('error', error.message)
  }), [pushToast])

  useEffect(() => subscribeAuthSession((session) => {
    if (session) return
    authEntry.current = 'restore'
    clearActiveRunSession()
    setToasts([])
    writeThreadToLocation('')
    setPhase('signedOut')
  }), [])

  useEffect(() => startAuthSessionLifecycle({
    onExternalSession: () => {
      authEntry.current = 'restore'
      setPhase((current) => current === 'signedOut' ? current : 'checking')
      setAuthCheckVersion((current) => current + 1)
    },
  }), [])

  useEffect(() => {
    if (phase === 'signedOut') writeThreadToLocation('')
  }, [phase])

  let content: ReactNode

  if (phase === 'checking') {
    content = (
      <main
        className="auth-checking"
        aria-label={isAuthRetrying ? '正在重新验证登录状态' : '正在检查登录状态'}
      >
        <span className="auth-checking__mark" aria-hidden="true" />
      </main>
    )
  } else if (phase === 'signedOut') {
    content = (
      <AuthScreen
        error={loginError}
        pending={isLoginPending}
        onLogin={async (credentials) => {
          setLoginPending(true)
          setLoginError(undefined)
          try {
            const payload = await login(credentials)
            authEntry.current = 'manual'
            saveAuthSession(createAuthSession(payload))
            setPhase('checking')
            setAuthCheckVersion((current) => current + 1)
          } catch (error) {
            if (error instanceof AuthError) {
              setLoginError(error.message)
            } else if (!(error instanceof ApiError)) {
              pushToast('error', '登录失败，请稍后重试')
            }
          } finally {
            setLoginPending(false)
          }
        }}
      />
    )
  } else if (phase === 'transitioning') {
    content = <LoginTransition onComplete={() => setPhase('signedIn')} />
  } else {
    const session = getAuthSession()
    content = session ? (
      <WorkspaceScreen
        user={session.user}
        onToast={pushToast}
        onLogout={() => {
          const logoutRequest = logoutApi()
          clearAuthSession()
          void logoutRequest.catch(() => undefined)
        }}
      />
    ) : null
  }

  return (
    <>
      {content}
      <ToastViewport
        toasts={toasts}
        onDismiss={(id) => setToasts((current) => current.filter((toast) => toast.id !== id))}
      />
    </>
  )
}
