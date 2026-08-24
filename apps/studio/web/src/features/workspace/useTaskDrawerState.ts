import { useCallback, useEffect, useRef, useState } from 'react'

const TASK_DRAWER_PREFERENCE_KEY_PREFIX = 'tinkerfin:task-drawer:'
const DRAWER_OVERLAY_QUERY = '(max-width: 1280px)'

const taskDrawerPreferenceKey = (threadId: string) => `${TASK_DRAWER_PREFERENCE_KEY_PREFIX}${threadId}`

const readTaskDrawerPreference = (threadId: string): boolean | null => {
  try {
    const storedValue = window.sessionStorage.getItem(taskDrawerPreferenceKey(threadId))
    if (storedValue === 'open') return true
    if (storedValue === 'closed') return false
    return null
  } catch {
    return null
  }
}

const writeTaskDrawerPreference = (threadId: string, isOpen: boolean): void => {
  try {
    window.sessionStorage.setItem(taskDrawerPreferenceKey(threadId), isOpen ? 'open' : 'closed')
  } catch {
    // 浏览器禁用会话存储时保留当前页面内的抽屉行为
  }
}

export function useTaskDrawerState({
  threadId,
  todoCount,
}: {
  threadId: string
  todoCount: number
}) {
  const [open, setOpen] = useState(false)
  const [usesOverlay, setUsesOverlay] = useState(() => window.matchMedia(DRAWER_OVERLAY_QUERY).matches)
  const tracker = useRef({ threadId: '', todoCount: 0 })
  const toggleRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    const media = window.matchMedia(DRAWER_OVERLAY_QUERY)
    const handleChange = (event: MediaQueryListEvent) => setUsesOverlay(event.matches)
    setUsesOverlay(media.matches)
    media.addEventListener('change', handleChange)
    return () => media.removeEventListener('change', handleChange)
  }, [])

  useEffect(() => {
    const switchedConversation = tracker.current.threadId !== threadId
    if (switchedConversation) {
      tracker.current = { threadId, todoCount }
      const preference = threadId ? readTaskDrawerPreference(threadId) : null
      setOpen(preference ?? todoCount > 0)
      return
    }

    if (todoCount > 0 && tracker.current.todoCount === 0) {
      setOpen(readTaskDrawerPreference(threadId) ?? true)
    }
    tracker.current = { threadId, todoCount }
  }, [threadId, todoCount])

  const toggle = useCallback(() => {
    setOpen((current) => {
      const next = !current
      if (threadId) writeTaskDrawerPreference(threadId, next)
      return next
    })
  }, [threadId])

  const close = useCallback(() => {
    if (threadId) writeTaskDrawerPreference(threadId, false)
    setOpen(false)
    window.requestAnimationFrame(() => toggleRef.current?.focus())
  }, [threadId])

  return {
    open,
    usesOverlay,
    modalActive: open && usesOverlay,
    toggleRef,
    toggle,
    close,
  }
}
