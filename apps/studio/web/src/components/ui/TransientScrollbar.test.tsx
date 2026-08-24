import { act, fireEvent, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useRef } from 'react'

import { TransientScrollbar } from './TransientScrollbar'

function ScrollbarHarness() {
  const viewportRef = useRef<HTMLDivElement>(null)
  return (
    <div className="scrollbar-host">
      <div ref={viewportRef} className="ui-scrollbar" />
      <TransientScrollbar viewportRef={viewportRef} />
    </div>
  )
}

describe('TransientScrollbar', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('measures, resets the idle timer, fades while hovered, and supports thumb dragging', () => {
    vi.useFakeTimers()
    const { container } = render(<ScrollbarHarness />)
    const viewport = container.querySelector<HTMLElement>('.ui-scrollbar')!
    const host = container.querySelector<HTMLElement>('.scrollbar-host')!
    const overlay = container.querySelector<HTMLElement>('.ui-scrollbar-overlay')!
    const thumb = container.querySelector<HTMLElement>('.ui-scrollbar-overlay__thumb')!

    Object.defineProperties(viewport, {
      clientHeight: { configurable: true, value: 200 },
      scrollHeight: { configurable: true, value: 800 },
      scrollTop: { configurable: true, writable: true, value: 300 },
    })
    vi.spyOn(viewport, 'getBoundingClientRect').mockReturnValue({ top: 40 } as DOMRect)
    vi.spyOn(host, 'getBoundingClientRect').mockReturnValue({ top: 10 } as DOMRect)

    fireEvent.pointerEnter(viewport)

    expect(overlay).toHaveAttribute('data-scrollable', 'true')
    expect(overlay).toHaveClass('is-visible')
    expect(overlay.style.top).toBe('30px')
    expect(overlay.style.height).toBe('200px')
    expect(thumb.style.height).toBe('50px')
    expect(thumb.style.transform).toBe('translateY(75px)')

    act(() => vi.advanceTimersByTime(400))
    viewport.scrollTop = 400
    fireEvent.scroll(viewport)
    act(() => vi.advanceTimersByTime(400))
    expect(overlay).toHaveClass('is-visible')
    act(() => vi.advanceTimersByTime(100))
    expect(overlay).not.toHaveClass('is-visible')

    Object.defineProperty(overlay, 'clientHeight', { configurable: true, value: 200 })
    vi.spyOn(thumb, 'getBoundingClientRect').mockReturnValue({ height: 50 } as DOMRect)
    const dispatchPointer = (type: string, clientY: number) => {
      const event = new Event(type, { bubbles: true, cancelable: true })
      Object.defineProperties(event, {
        button: { value: 0 },
        clientY: { value: clientY },
        pointerId: { value: 7 },
      })
      fireEvent(thumb, event)
    }
    fireEvent.pointerEnter(viewport)
    dispatchPointer('pointerdown', 100)
    dispatchPointer('pointermove', 150)
    expect(viewport.scrollTop).toBe(600)
    dispatchPointer('pointerup', 150)
  })
})
