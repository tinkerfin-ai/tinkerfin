import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { CSSProperties } from 'react'

const ENDPOINT_PAUSE_MS = 800
const TRAVEL_SPEED_PX_PER_SECOND = 36

const marqueeTimeline = (scrollDistance: number) => {
  // 固定时长会让长标题显著加速；按距离计算时长，保证模型与历史标题的像素速度一致
  const travelDurationMs = Math.round(scrollDistance * 1000 / TRAVEL_SPEED_PX_PER_SECOND)
  const cycleDurationMs = (ENDPOINT_PAUSE_MS + travelDurationMs) * 2
  const startTravelOffset = ENDPOINT_PAUSE_MS / cycleDurationMs
  const endTravelOffset = (ENDPOINT_PAUSE_MS + travelDurationMs) / cycleDurationMs
  const startReturnOffset = (ENDPOINT_PAUSE_MS * 2 + travelDurationMs) / cycleDurationMs
  const endTransform = `translateX(-${scrollDistance}px)`

  return {
    cycleDurationMs,
    travelDurationMs,
    keyframes: [
      { transform: 'translateX(0)', offset: 0 },
      { transform: 'translateX(0)', offset: startTravelOffset, easing: 'ease-in-out' },
      { transform: endTransform, offset: endTravelOffset },
      { transform: endTransform, offset: startReturnOffset, easing: 'ease-in-out' },
      { transform: 'translateX(0)', offset: 1 },
    ] satisfies Keyframe[],
  }
}

export function OverflowMarquee({
  children,
  className,
}: {
  children: string
  className?: string
}) {
  const viewportRef = useRef<HTMLSpanElement>(null)
  const contentRef = useRef<HTMLSpanElement>(null)
  const [scrollDistance, setScrollDistance] = useState(0)

  useLayoutEffect(() => {
    const measure = () => {
      const viewport = viewportRef.current
      const content = contentRef.current
      if (!viewport || !content) return
      const nextScrollDistance = Math.max(0, content.scrollWidth - viewport.clientWidth)
      setScrollDistance((current) => current === nextScrollDistance ? current : nextScrollDistance)
    }

    measure()
    if (typeof ResizeObserver === 'undefined') return undefined
    const observer = new ResizeObserver(measure)
    if (viewportRef.current) observer.observe(viewportRef.current)
    if (contentRef.current) observer.observe(contentRef.current)
    return () => observer.disconnect()
  }, [children])

  const timeline = useMemo(() => marqueeTimeline(scrollDistance), [scrollDistance])

  useEffect(() => {
    const viewport = viewportRef.current
    const content = contentRef.current
    if (!viewport || !content || scrollDistance === 0) return undefined
    const trigger = viewport.closest<HTMLElement>('.overflow-marquee-trigger') ?? viewport
    let animation: Animation | null = null

    const stop = () => {
      animation?.cancel()
      animation = null
      content.style.removeProperty('transform')
    }
    const start = () => {
      stop()
      const prefersReducedMotion = typeof window.matchMedia === 'function'
        && window.matchMedia('(prefers-reduced-motion: reduce)').matches
      if (prefersReducedMotion) {
        content.style.transform = `translateX(-${scrollDistance}px)`
        return
      }
      if (typeof content.animate !== 'function') return
      animation = content.animate(timeline.keyframes, {
        duration: timeline.cycleDurationMs,
        iterations: Infinity,
      })
    }

    trigger.addEventListener('mouseenter', start)
    trigger.addEventListener('mouseleave', stop)
    if (trigger.matches(':hover')) start()
    return () => {
      trigger.removeEventListener('mouseenter', start)
      trigger.removeEventListener('mouseleave', stop)
      stop()
    }
  }, [scrollDistance, timeline])

  const marqueeStyle = {
    '--overflow-marquee-distance': `${scrollDistance}px`,
    '--overflow-marquee-pause-duration': `${ENDPOINT_PAUSE_MS}ms`,
    '--overflow-marquee-travel-duration': `${timeline.travelDurationMs}ms`,
  } as CSSProperties

  return (
    <span ref={viewportRef} className={['overflow-marquee', className].filter(Boolean).join(' ')}>
      <span
        ref={contentRef}
        className={`overflow-marquee-content ${scrollDistance > 0 ? 'is-overflowing' : ''}`}
        style={marqueeStyle}
      >
        {children}
      </span>
    </span>
  )
}
