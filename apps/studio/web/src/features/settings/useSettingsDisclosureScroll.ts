import { useCallback, useEffect, useRef } from 'react'

/** 展开后露出新增内容；延迟内容就绪前跟随高度变化，用户继续操作后交还滚动 */
export function useSettingsDisclosureScroll() {
  const cleanup = useRef<(() => void) | null>(null)
  const stop = useCallback(() => {
    cleanup.current?.()
    cleanup.current = null
  }, [])

  useEffect(() => stop, [stop])

  return useCallback((section: HTMLDetailsElement) => {
    stop()
    if (!section.open) return
    let frame: number | null = null
    const schedule = () => {
      if (frame !== null) return
      frame = requestAnimationFrame(() => {
        frame = null
        if (!section.isConnected || !section.open) {
          stop()
          return
        }
        section.scrollIntoView({ block: 'nearest', behavior: 'instant' })
      })
    }
    const observer = new ResizeObserver(schedule)
    observer.observe(section)
    // 用户开始滚动、选择或输入后，异步内容不再改变其阅读位置
    const intents = ['wheel', 'touchstart', 'pointerdown', 'keydown'] as const
    for (const intent of intents) document.addEventListener(intent, stop, { capture: true, passive: true })
    cleanup.current = () => {
      observer.disconnect()
      if (frame !== null) cancelAnimationFrame(frame)
      for (const intent of intents) document.removeEventListener(intent, stop, true)
    }
    schedule()
  }, [stop])
}
