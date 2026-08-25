import { act, fireEvent, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useRef } from 'react'

import {
  OverlayScrollbar,
  type OverlayScrollbarAxis,
  type OverlayScrollbarSize,
  type OverlayScrollbarVisibility,
} from './OverlayScrollbar'

function ScrollbarHarness({
  axis = 'vertical',
  size = 'regular',
  visibility = 'transient',
}: {
  axis?: OverlayScrollbarAxis
  size?: OverlayScrollbarSize
  visibility?: OverlayScrollbarVisibility
}) {
  const viewportRef = useRef<HTMLDivElement>(null)
  return (
    <div className="scrollbar-host">
      <div ref={viewportRef} className="ui-scrollbar"><div /></div>
      <OverlayScrollbar
        viewportRef={viewportRef}
        axis={axis}
        size={size}
        visibility={visibility}
      />
    </div>
  )
}

const installFrames = () => {
  const frames = new Map<number, FrameRequestCallback>()
  let nextId = 1
  vi.stubGlobal('requestAnimationFrame', vi.fn((callback: FrameRequestCallback) => {
    const id = nextId
    nextId += 1
    frames.set(id, callback)
    return id
  }))
  vi.stubGlobal('cancelAnimationFrame', vi.fn((id: number) => frames.delete(id)))
  return () => {
    const callbacks = [...frames.values()]
    frames.clear()
    callbacks.forEach((callback) => callback(0))
  }
}

const defineVerticalGeometry = (viewport: HTMLElement) => {
  Object.defineProperties(viewport, {
    clientHeight: { configurable: true, value: 200 },
    clientWidth: { configurable: true, value: 200 },
    scrollHeight: { configurable: true, value: 800 },
    scrollWidth: { configurable: true, value: 200 },
    scrollTop: { configurable: true, writable: true, value: 300 },
  })
  vi.spyOn(viewport, 'getBoundingClientRect').mockReturnValue({
    top: 40,
    right: 220,
    bottom: 240,
    left: 20,
    width: 200,
    height: 200,
  } as DOMRect)
}

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('OverlayScrollbar', () => {
  it('measures a persistent vertical thumb with the global proportional contract', () => {
    const flushFrames = installFrames()
    const { container } = render(<ScrollbarHarness visibility="persistent" />)
    const host = container.querySelector<HTMLElement>('.scrollbar-host')!
    const viewport = container.querySelector<HTMLElement>('.ui-scrollbar')!
    const overlay = container.querySelector<HTMLElement>('.ui-overlay-scrollbar')!
    const thumb = container.querySelector<HTMLElement>('.ui-overlay-scrollbar__thumb')!
    defineVerticalGeometry(viewport)
    vi.spyOn(host, 'getBoundingClientRect').mockReturnValue({ top: 10, left: 10 } as DOMRect)

    fireEvent.scroll(viewport)
    act(() => flushFrames())

    expect(overlay).toHaveAttribute('data-scrollable', 'true')
    expect(overlay).toHaveAttribute('data-visibility', 'persistent')
    expect(overlay).toHaveClass('is-visible', 'ui-overlay-scrollbar--regular')
    expect(Number.parseFloat(overlay.style.top)).toBeCloseTo(33)
    expect(Number.parseFloat(overlay.style.left)).toBeCloseTo(199)
    expect(Number.parseFloat(overlay.style.height)).toBeCloseTo(194)
    expect(Number.parseFloat(thumb.style.height)).toBeCloseTo(48.5)
    expect(thumb.style.transform).toBe('translate3d(0, 72.75px, 0)')
  })

  it('stays visible while hovered and hides one second after leaving', () => {
    vi.useFakeTimers()
    const flushFrames = installFrames()
    const { container } = render(<ScrollbarHarness />)
    const host = container.querySelector<HTMLElement>('.scrollbar-host')!
    const viewport = container.querySelector<HTMLElement>('.ui-scrollbar')!
    const overlay = container.querySelector<HTMLElement>('.ui-overlay-scrollbar')!
    defineVerticalGeometry(viewport)
    vi.spyOn(host, 'getBoundingClientRect').mockReturnValue({ top: 10, left: 10 } as DOMRect)

    fireEvent.pointerEnter(viewport, { pointerType: 'mouse' })
    act(() => flushFrames())
    expect(overlay).toHaveClass('is-visible')
    act(() => vi.advanceTimersByTime(2_000))
    expect(overlay).toHaveClass('is-visible')

    fireEvent.pointerLeave(viewport, { pointerType: 'mouse' })
    act(() => vi.advanceTimersByTime(999))
    expect(overlay).toHaveClass('is-visible')
    act(() => vi.advanceTimersByTime(1))
    expect(overlay).not.toHaveClass('is-visible')
  })

  it('maps thumb dragging directly to the viewport scroll position', () => {
    const flushFrames = installFrames()
    const { container } = render(<ScrollbarHarness visibility="persistent" />)
    const host = container.querySelector<HTMLElement>('.scrollbar-host')!
    const viewport = container.querySelector<HTMLElement>('.ui-scrollbar')!
    const thumb = container.querySelector<HTMLElement>('.ui-overlay-scrollbar__thumb')!
    defineVerticalGeometry(viewport)
    vi.spyOn(host, 'getBoundingClientRect').mockReturnValue({ top: 10, left: 10 } as DOMRect)
    fireEvent.scroll(viewport)
    act(() => flushFrames())

    const dispatchPointer = (type: string, clientY: number) => {
      const event = new Event(type, { bubbles: true, cancelable: true })
      Object.defineProperties(event, {
        button: { value: 0 },
        clientX: { value: 0 },
        clientY: { value: clientY },
        pointerId: { value: 7 },
        pointerType: { value: 'mouse' },
      })
      fireEvent(thumb, event)
    }
    dispatchPointer('pointerdown', 100)
    dispatchPointer('pointermove', 150)
    expect(viewport.scrollTop).toBeCloseTo(506.19, 1)
    dispatchPointer('pointerup', 150)
  })

  it('supports a compact horizontal variant and hides when no overflow exists', () => {
    const flushFrames = installFrames()
    const { container, rerender } = render(
      <ScrollbarHarness axis="horizontal" size="compact" visibility="persistent" />,
    )
    const host = container.querySelector<HTMLElement>('.scrollbar-host')!
    const viewport = container.querySelector<HTMLElement>('.ui-scrollbar')!
    const overlay = container.querySelector<HTMLElement>('.ui-overlay-scrollbar')!
    const thumb = container.querySelector<HTMLElement>('.ui-overlay-scrollbar__thumb')!
    Object.defineProperties(viewport, {
      clientHeight: { configurable: true, value: 100 },
      clientWidth: { configurable: true, value: 300 },
      scrollHeight: { configurable: true, value: 100 },
      scrollWidth: { configurable: true, value: 1200 },
      scrollLeft: { configurable: true, writable: true, value: 450 },
    })
    vi.spyOn(viewport, 'getBoundingClientRect').mockReturnValue({
      top: 20,
      right: 320,
      bottom: 120,
      left: 20,
      width: 300,
      height: 100,
    } as DOMRect)
    vi.spyOn(host, 'getBoundingClientRect').mockReturnValue({ top: 10, left: 10 } as DOMRect)
    fireEvent.scroll(viewport)
    act(() => flushFrames())

    expect(overlay).toHaveClass('ui-overlay-scrollbar--horizontal', 'ui-overlay-scrollbar--compact')
    expect(Number.parseFloat(overlay.style.width)).toBeCloseTo(296)
    expect(Number.parseFloat(thumb.style.width)).toBeCloseTo(74)
    expect(thumb.style.transform).toBe('translate3d(111px, 0, 0)')

    Object.defineProperty(viewport, 'scrollWidth', { configurable: true, value: 300 })
    rerender(<ScrollbarHarness axis="horizontal" size="compact" visibility="persistent" />)
    fireEvent.scroll(viewport)
    act(() => flushFrames())
    expect(overlay).toHaveAttribute('data-scrollable', 'false')
    expect(overlay).not.toHaveClass('is-visible')
  })

  it('coalesces content geometry changes into one immediate frame', () => {
    let resizeCallback: ResizeObserverCallback | undefined
    vi.stubGlobal('ResizeObserver', class {
      constructor(callback: ResizeObserverCallback) { resizeCallback = callback }
      observe() {}
      unobserve() {}
      disconnect() {}
    })
    vi.stubGlobal('MutationObserver', class {
      constructor() {}
      observe() {}
      disconnect() {}
      takeRecords() { return [] }
    })
    const flushFrames = installFrames()
    const { container } = render(<ScrollbarHarness visibility="persistent" />)
    const host = container.querySelector<HTMLElement>('.scrollbar-host')!
    const viewport = container.querySelector<HTMLElement>('.ui-scrollbar')!
    const overlay = container.querySelector<HTMLElement>('.ui-overlay-scrollbar')!
    defineVerticalGeometry(viewport)
    vi.spyOn(host, 'getBoundingClientRect').mockReturnValue({ top: 10, left: 10 } as DOMRect)
    fireEvent.scroll(viewport)
    act(() => flushFrames())
    const thumb = container.querySelector<HTMLElement>('.ui-overlay-scrollbar__thumb')!
    expect(Number.parseFloat(thumb.style.height)).toBeCloseTo(48.5)

    vi.mocked(requestAnimationFrame).mockClear()
    Object.defineProperty(viewport, 'scrollHeight', { configurable: true, value: 1_000 })
    act(() => resizeCallback?.([], {} as ResizeObserver))
    act(() => resizeCallback?.([], {} as ResizeObserver))
    expect(requestAnimationFrame).toHaveBeenCalledOnce()
    act(() => flushFrames())
    expect(overlay).not.toHaveClass('is-geometry-transitioning')
    expect(Number.parseFloat(thumb.style.height)).toBeCloseTo(38.8)
  })
})
