import { useCallback, useEffect, useRef, useState } from 'react'

import type { WebTaskTraceViewState } from '../../../types'

const PREFERENCE_PREFIX = 'tinkerfin:todo-trace-drawer:'
const OVERLAY_QUERY = '(max-width: 1280px)'
const FOCUSABLE = [
  'button:not([disabled])',
  '[href]',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

const preferenceKey = (threadId: string) => `${PREFERENCE_PREFIX}${threadId}`

const readPreference = (threadId: string) => {
  try {
    return window.sessionStorage.getItem(preferenceKey(threadId)) === 'open'
  } catch {
    return false
  }
}

const writePreference = (threadId: string, open: boolean) => {
  try {
    window.sessionStorage.setItem(preferenceKey(threadId), open ? 'open' : 'closed')
  } catch {
    // 禁用会话存储时仍保留当前页面内的用户选择
  }
}

export function useTodoTraceDrawer({
  threadId,
  taskTrace,
  blocked,
}: {
  threadId: string
  taskTrace: WebTaskTraceViewState
  blocked: boolean
}) {
  const [desiredOpen, setDesiredOpen] = useState(false)
  const [usesOverlay, setUsesOverlay] = useState(
    () => window.matchMedia(OVERLAY_QUERY).matches,
  )
  const [openEpoch, setOpenEpoch] = useState(0)
  const launcherRef = useRef<HTMLButtonElement>(null)
  const drawerRef = useRef<HTMLElement>(null)
  const hasGroups = taskTrace.phase === 'ready' && taskTrace.snapshot.todoGroups.length > 0
  const open = desiredOpen && !blocked && hasGroups
  const modalActive = open && usesOverlay

  useEffect(() => {
    setDesiredOpen(threadId ? readPreference(threadId) : false)
  }, [threadId])

  useEffect(() => {
    const media = window.matchMedia(OVERLAY_QUERY)
    const update = (event: MediaQueryListEvent) => setUsesOverlay(event.matches)
    setUsesOverlay(media.matches)
    media.addEventListener('change', update)
    return () => media.removeEventListener('change', update)
  }, [])

  const toggle = useCallback(() => {
    setDesiredOpen((current) => {
      const next = !current
      if (threadId) writePreference(threadId, next)
      if (next) setOpenEpoch((value) => value + 1)
      return next
    })
  }, [threadId])

  const close = useCallback((restoreFocus = true) => {
    if (threadId) writePreference(threadId, false)
    setDesiredOpen(false)
    if (restoreFocus) {
      window.requestAnimationFrame(() => launcherRef.current?.focus())
    }
  }, [threadId])

  useEffect(() => {
    if (!open) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        close(true)
        return
      }
      if (!modalActive || event.key !== 'Tab') return
      const controls = [
        launcherRef.current,
        ...Array.from(drawerRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? []),
      ].filter((item): item is HTMLElement => item != null && !item.hasAttribute('disabled'))
      if (controls.length === 0) return
      const current = controls.indexOf(document.activeElement as HTMLElement)
      const next = event.shiftKey
        ? (current <= 0 ? controls.length - 1 : current - 1)
        : (current < 0 || current === controls.length - 1 ? 0 : current + 1)
      event.preventDefault()
      controls[next]?.focus()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [close, modalActive, open])

  return {
    open,
    desiredOpen,
    usesOverlay,
    modalActive,
    openEpoch,
    launcherRef,
    drawerRef,
    toggle,
    close,
  }
}
