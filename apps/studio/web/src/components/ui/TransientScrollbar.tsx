import { useCallback, useLayoutEffect, useRef, useState } from 'react'
import type { PointerEvent as ReactPointerEvent, RefObject } from 'react'

const HIDE_DELAY_MS = 500
const MIN_THUMB_SIZE_PX = 32

export interface TransientScrollbarProps {
  viewportRef: RefObject<HTMLElement | null>
}

export function TransientScrollbar({ viewportRef }: TransientScrollbarProps) {
  const overlayRef = useRef<HTMLDivElement>(null)
  const thumbRef = useRef<HTMLSpanElement>(null)
  const hideTimerRef = useRef<number | null>(null)
  const scrollableRef = useRef(false)
  const dragRef = useRef<{
    pointerId: number
    startY: number
    startScrollTop: number
    maxScroll: number
    travel: number
  } | null>(null)
  const [visible, setVisible] = useState(false)

  const clearHideTimer = useCallback(() => {
    if (hideTimerRef.current == null) return
    window.clearTimeout(hideTimerRef.current)
    hideTimerRef.current = null
  }, [])

  const measure = useCallback(() => {
    const viewport = viewportRef.current
    const overlay = overlayRef.current
    const thumb = thumbRef.current
    const host = overlay?.parentElement
    if (!viewport || !overlay || !thumb || !host) return

    const viewportBounds = viewport.getBoundingClientRect()
    const hostBounds = host.getBoundingClientRect()
    const trackHeight = viewport.clientHeight
    const maxScroll = Math.max(0, viewport.scrollHeight - viewport.clientHeight)
    const scrollable = trackHeight > 0 && maxScroll > 1
    scrollableRef.current = scrollable
    overlay.dataset.scrollable = String(scrollable)
    overlay.style.top = `${Math.max(0, viewportBounds.top - hostBounds.top)}px`
    overlay.style.height = `${trackHeight}px`

    if (!scrollable) {
      setVisible(false)
      return
    }

    const thumbHeight = Math.max(
      MIN_THUMB_SIZE_PX,
      Math.min(trackHeight, trackHeight * viewport.clientHeight / viewport.scrollHeight),
    )
    const travel = Math.max(0, trackHeight - thumbHeight)
    const thumbTop = maxScroll > 0 ? travel * viewport.scrollTop / maxScroll : 0
    thumb.style.height = `${thumbHeight}px`
    thumb.style.transform = `translateY(${thumbTop}px)`
  }, [viewportRef])

  const reveal = useCallback(() => {
    clearHideTimer()
    measure()
    if (scrollableRef.current) setVisible(true)
  }, [clearHideTimer, measure])

  const scheduleHide = useCallback(() => {
    clearHideTimer()
    hideTimerRef.current = window.setTimeout(() => {
      setVisible(false)
      hideTimerRef.current = null
    }, HIDE_DELAY_MS)
  }, [clearHideTimer])

  const revealTemporarily = useCallback(() => {
    reveal()
    if (scrollableRef.current) scheduleHide()
  }, [reveal, scheduleHide])

  const handleThumbPointerDown = (event: ReactPointerEvent<HTMLSpanElement>) => {
    if (event.button !== 0) return
    const viewport = viewportRef.current
    const thumb = thumbRef.current
    const overlay = overlayRef.current
    if (!viewport || !thumb || !overlay) return
    const maxScroll = Math.max(0, viewport.scrollHeight - viewport.clientHeight)
    const travel = Math.max(0, overlay.clientHeight - thumb.getBoundingClientRect().height)
    if (maxScroll <= 0 || travel <= 0) return

    event.preventDefault()
    event.currentTarget.setPointerCapture?.(event.pointerId)
    dragRef.current = {
      pointerId: event.pointerId,
      startY: event.clientY,
      startScrollTop: viewport.scrollTop,
      maxScroll,
      travel,
    }
    reveal()
  }

  const handleThumbPointerMove = (event: ReactPointerEvent<HTMLSpanElement>) => {
    const drag = dragRef.current
    const viewport = viewportRef.current
    if (!drag || !viewport || drag.pointerId !== event.pointerId) return
    const nextScrollTop = drag.startScrollTop
      + (event.clientY - drag.startY) * drag.maxScroll / drag.travel
    viewport.scrollTop = Math.max(0, Math.min(drag.maxScroll, nextScrollTop))
    measure()
  }

  const finishThumbDrag = (event: ReactPointerEvent<HTMLSpanElement>) => {
    if (dragRef.current?.pointerId !== event.pointerId) return
    event.currentTarget.releasePointerCapture?.(event.pointerId)
    dragRef.current = null
    scheduleHide()
  }

  useLayoutEffect(() => {
    const viewport = viewportRef.current
    if (!viewport) return undefined

    const handlePointerEnter = () => revealTemporarily()
    const handlePointerLeave = () => {
      if (!dragRef.current) scheduleHide()
    }
    const handleFocusIn = () => revealTemporarily()
    const handleFocusOut = (event: FocusEvent) => {
      if (event.relatedTarget instanceof Node && viewport.contains(event.relatedTarget)) return
      if (!dragRef.current) scheduleHide()
    }
    const handleScroll = () => {
      reveal()
      if (!dragRef.current) scheduleHide()
    }

    viewport.addEventListener('pointerenter', handlePointerEnter)
    viewport.addEventListener('pointerleave', handlePointerLeave)
    viewport.addEventListener('focusin', handleFocusIn)
    viewport.addEventListener('focusout', handleFocusOut)
    viewport.addEventListener('scroll', handleScroll, { passive: true })

    const resizeObserver = typeof ResizeObserver === 'undefined'
      ? undefined
      : new ResizeObserver(measure)
    resizeObserver?.observe(viewport)
    for (const child of viewport.children) resizeObserver?.observe(child)

    const mutationObserver = typeof MutationObserver === 'undefined'
      ? undefined
      : new MutationObserver(() => {
        for (const child of viewport.children) resizeObserver?.observe(child)
        measure()
      })
    mutationObserver?.observe(viewport, { childList: true })
    measure()

    return () => {
      clearHideTimer()
      resizeObserver?.disconnect()
      mutationObserver?.disconnect()
      viewport.removeEventListener('pointerenter', handlePointerEnter)
      viewport.removeEventListener('pointerleave', handlePointerLeave)
      viewport.removeEventListener('focusin', handleFocusIn)
      viewport.removeEventListener('focusout', handleFocusOut)
      viewport.removeEventListener('scroll', handleScroll)
    }
  }, [clearHideTimer, measure, reveal, revealTemporarily, scheduleHide, viewportRef])

  return (
    <div
      ref={overlayRef}
      className={`ui-scrollbar-overlay${visible ? ' is-visible' : ''}`}
      data-scrollable="false"
      aria-hidden="true"
    >
      <span
        ref={thumbRef}
        className="ui-scrollbar-overlay__thumb"
        onPointerDown={handleThumbPointerDown}
        onPointerMove={handleThumbPointerMove}
        onPointerUp={finishThumbDrag}
        onPointerCancel={finishThumbDrag}
      />
    </div>
  )
}
