import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { OverflowMarquee } from './OverflowMarquee'

const originalAnimate = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'animate')

describe('OverflowMarquee', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    if (originalAnimate) Object.defineProperty(HTMLElement.prototype, 'animate', originalAnimate)
    else Reflect.deleteProperty(HTMLElement.prototype, 'animate')
  })

  it('uses one travel speed while preserving the endpoint pauses for every overflow distance', () => {
    const resizeCallbacks: Array<() => void> = []
    vi.stubGlobal('ResizeObserver', class {
      constructor(callback: ResizeObserverCallback) {
        resizeCallbacks.push(() => callback([], {} as ResizeObserver))
      }
      observe = vi.fn()
      disconnect = vi.fn()
    })
    const cancel = vi.fn()
    const animate = vi.fn(() => ({ cancel }) as unknown as Animation)
    Object.defineProperty(HTMLElement.prototype, 'animate', { configurable: true, value: animate })
    const { container } = render(
      <div className="overflow-marquee-trigger">
        <OverflowMarquee>DeepSeek-V4-Pro</OverflowMarquee>
      </div>,
    )
    const trigger = container.querySelector<HTMLElement>('.overflow-marquee-trigger')
    const viewport = container.querySelector<HTMLElement>('.overflow-marquee')
    const content = screen.getByText('DeepSeek-V4-Pro')

    Object.defineProperty(viewport, 'clientWidth', { configurable: true, value: 60 })
    Object.defineProperty(content, 'scrollWidth', { configurable: true, value: 84 })
    act(() => resizeCallbacks.forEach((resize) => resize()))

    expect(content).toHaveClass('is-overflowing')
    expect(content.style.getPropertyValue('--overflow-marquee-distance')).toBe('24px')
    expect(content.style.getPropertyValue('--overflow-marquee-travel-duration')).toBe('667ms')
    fireEvent.mouseEnter(trigger!)

    const [shortKeyframes, shortOptions] = animate.mock.calls.at(-1) as unknown as [Keyframe[], KeyframeAnimationOptions]
    const shortDurationMs = Number(shortOptions.duration)
    expect((Number(shortKeyframes[1].offset) - Number(shortKeyframes[0].offset)) * shortDurationMs).toBeCloseTo(800)
    const shortTravelMs = (Number(shortKeyframes[2].offset) - Number(shortKeyframes[1].offset)) * shortDurationMs
    expect(shortTravelMs).toBeCloseTo(667)
    expect((Number(shortKeyframes[3].offset) - Number(shortKeyframes[2].offset)) * shortDurationMs).toBeCloseTo(800)
    fireEvent.mouseLeave(trigger!)
    expect(cancel).toHaveBeenCalled()

    Object.defineProperty(content, 'scrollWidth', { configurable: true, value: 240 })
    act(() => resizeCallbacks.forEach((resize) => resize()))

    expect(content.style.getPropertyValue('--overflow-marquee-distance')).toBe('180px')
    expect(content.style.getPropertyValue('--overflow-marquee-travel-duration')).toBe('5000ms')
    fireEvent.mouseEnter(trigger!)

    const [longKeyframes, longOptions] = animate.mock.calls.at(-1) as unknown as [Keyframe[], KeyframeAnimationOptions]
    const longDurationMs = Number(longOptions.duration)
    expect((Number(longKeyframes[1].offset) - Number(longKeyframes[0].offset)) * longDurationMs).toBeCloseTo(800)
    const longTravelMs = (Number(longKeyframes[2].offset) - Number(longKeyframes[1].offset)) * longDurationMs
    expect(longTravelMs).toBeCloseTo(5000)
    expect((Number(longKeyframes[3].offset) - Number(longKeyframes[2].offset)) * longDurationMs).toBeCloseTo(800)
    expect(24 / (shortTravelMs / 1000)).toBeCloseTo(180 / (longTravelMs / 1000), 1)
    expect(180 / (longTravelMs / 1000)).toBeCloseTo(36, 1)
  })
})
