import { useCallback, useEffect, useState } from 'react'
import type { ReactNode } from 'react'

import {
  bootstrapAuthSession,
  login,
  logout as logoutApi,
} from './api/auth/client'
import { AuthScreen } from './features/auth/AuthScreen'
import { LoginTransition } from './features/auth/LoginTransition'
import { WorkspaceScreen } from './features/workspace/WorkspaceScreen'
import { ApiError, AuthError, subscribeApiErrors } from './api/shared/http'
import { ToastViewport } from './components/ToastViewport'
import type { ToastItem, ToastKind } from './components/ToastViewport'
import {
  clearAuthSession,
  createAuthSession,
  getAuthSession,
  saveAuthSession,
  subscribeAuthSession,
} from './auth/session'
import { writeThreadToLocation } from './lib/threadRoute'
import { clearActiveRunSession } from './features/conversation/stream/activeRunSession'

type AuthPhase = 'checking' | 'signedOut' | 'transitioning' | 'signedIn'
let toastSequence = 0

export default function App() {
  const [phase, setPhase] = useState<AuthPhase>(() => (
    getAuthSession() ? 'checking' : 'signedOut'
  ))
  const [loginError, setLoginError] = useState<string>()
  const [isLoginPending, setLoginPending] = useState(false)
  const [toasts, setToasts] = useState<ToastItem[]>([])

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

  useEffect(() => {
    if (phase !== 'checking') return
    let isActive = true
    void bootstrapAuthSession().then((result) => {
      if (!isActive) return
      if (result.status === 'authenticated') {
        setPhase('signedIn')
        return
      }
      clearAuthSession()
      writeThreadToLocation('')
      if (result.error) pushToast('error', result.error.message)
      setPhase('signedOut')
    })
    return () => {
      isActive = false
    }
  }, [phase, pushToast])

  useEffect(() => subscribeAuthSession((session) => {
    if (session) return
    clearActiveRunSession()
    setToasts([])
    writeThreadToLocation('')
    setPhase('signedOut')
  }), [])

  useEffect(() => {
    if (phase === 'signedOut') writeThreadToLocation('')
  }, [phase])

  let content: ReactNode

  if (phase === 'checking') {
    content = (
      <main className="auth-checking" aria-label="正在检查登录状态">
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
            saveAuthSession(createAuthSession(payload))
            const verification = await bootstrapAuthSession()
            if (verification.status !== 'authenticated') {
              clearAuthSession()
              if (verification.error) pushToast('error', verification.error.message)
              return
            }
            setPhase(window.matchMedia('(prefers-reduced-motion: reduce)').matches
              ? 'signedIn'
              : 'transitioning')
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
