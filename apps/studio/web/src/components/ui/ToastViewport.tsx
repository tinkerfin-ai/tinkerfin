import { useGSAP } from '@gsap/react'
import gsap from 'gsap'
import { CheckCircle2, CircleAlert, Info, X } from 'lucide-react'
import { useCallback, useEffect, useRef } from 'react'

import { MOTION_DURATION_SECONDS } from './motion'
import { useI18n } from '../../i18n'

gsap.registerPlugin(useGSAP)

export type ToastKind = 'success' | 'info' | 'error'

export interface ToastItem {
  id: string
  kind: ToastKind
  message: string
}

const TOAST_DURATION_MS: Record<ToastItem['kind'], number> = {
  success: 3000,
  info: 4000,
  error: 6000,
}

const TOAST_ICON = {
  success: CheckCircle2,
  info: Info,
  error: CircleAlert,
}

function ToastCard({
  toast,
  onDismiss,
}: {
  toast: ToastItem
  onDismiss: (id: string) => void
}) {
  const { t } = useI18n()
  const duration = TOAST_DURATION_MS[toast.kind]
  const cardRef = useRef<HTMLLIElement>(null)
  const timerRef = useRef<number | null>(null)
  const startedAtRef = useRef(0)
  const remainingRef = useRef(duration)
  const isDismissingRef = useRef(false)
  const requestDismissRef = useRef<() => void>(() => undefined)
  const dismissRef = useRef(onDismiss)
  dismissRef.current = onDismiss

  const clearTimer = useCallback(() => {
    if (timerRef.current === null) return
    window.clearTimeout(timerRef.current)
    timerRef.current = null
  }, [])

  const { contextSafe } = useGSAP(() => {
    const card = cardRef.current
    if (!card) return

    const media = gsap.matchMedia()
    media.add({
      allowMotion: '(prefers-reduced-motion: no-preference)',
      reduceMotion: '(prefers-reduced-motion: reduce)',
    }, (context) => {
      if (context.conditions?.reduceMotion) {
        gsap.set(card, { autoAlpha: 1, y: 0, scale: 1 })
        return
      }

      gsap.fromTo(card, {
        autoAlpha: 0,
        y: -8,
        scale: 0.985,
      }, {
        autoAlpha: 1,
        y: 0,
        scale: 1,
        duration: MOTION_DURATION_SECONDS.normal,
        ease: 'power3.out',
        clearProps: 'transform,opacity,visibility',
      })
    })

    return () => media.revert()
  }, { scope: cardRef })

  const requestDismiss = contextSafe(() => {
    if (isDismissingRef.current) return
    isDismissingRef.current = true
    clearTimer()

    const finishDismiss = () => dismissRef.current(toast.id)
    const card = cardRef.current
    if (!card || window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      finishDismiss()
      return
    }

    gsap.to(card, {
      autoAlpha: 0,
      y: -6,
      scale: 0.985,
      duration: MOTION_DURATION_SECONDS.fast,
      ease: 'power2.in',
      overwrite: 'auto',
      onComplete: finishDismiss,
    })
  })
  requestDismissRef.current = requestDismiss

  const scheduleDismiss = useCallback(() => {
    if (remainingRef.current <= 0) {
      requestDismissRef.current()
      return
    }
    startedAtRef.current = Date.now()
    timerRef.current = window.setTimeout(() => {
      timerRef.current = null
      remainingRef.current = 0
      requestDismissRef.current()
    }, remainingRef.current)
  }, [])

  useEffect(() => {
    isDismissingRef.current = false
    remainingRef.current = duration
    scheduleDismiss()
    return clearTimer
  }, [clearTimer, duration, scheduleDismiss])

  const Icon = TOAST_ICON[toast.kind]
  const message = toast.message.replace(/。$/, '')

  return (
    <li
      ref={cardRef}
      className={`toast-card is-${toast.kind}`}
      onMouseEnter={() => {
        if (timerRef.current === null) return
        remainingRef.current = Math.max(0, remainingRef.current - (Date.now() - startedAtRef.current))
        clearTimer()
      }}
      onMouseLeave={() => {
        if (timerRef.current === null) scheduleDismiss()
      }}
    >
      <span className="toast-icon" aria-hidden="true"><Icon size={16} /></span>
      <p role={toast.kind === 'error' ? 'alert' : 'status'}>{message}</p>
      <button type="button" aria-label={t('关闭提示：{message}', { message })} onClick={() => requestDismissRef.current()}>
        <X size={13} />
      </button>
    </li>
  )
}

export function ToastViewport({
  toasts,
  onDismiss,
}: {
  toasts: ToastItem[]
  onDismiss: (id: string) => void
}) {
  const { t } = useI18n()
  if (toasts.length === 0) return null

  return (
    <ol className="toast-viewport" aria-live="polite" aria-label={t('系统提示')}>
      {toasts.map((toast) => <ToastCard key={toast.id} toast={toast} onDismiss={onDismiss} />)}
    </ol>
  )
}
