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

  it('uses one linear travel speed while ignoring imperceptible overflow', () => {
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
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
      callback(0)
      return 1
    })
    vi.stubGlobal('cancelAnimationFrame', vi.fn())
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
    Object.defineProperty(content, 'scrollWidth', { configurable: true, value: 68 })
    act(() => resizeCallbacks.forEach((resize) => resize()))

    expect(content).not.toHaveClass('is-overflowing')
    expect(viewport).not.toHaveClass('is-overflowing')
    fireEvent.mouseEnter(trigger!)
    expect(animate).not.toHaveBeenCalled()

    Object.defineProperty(content, 'scrollWidth', { configurable: true, value: 84 })
    act(() => resizeCallbacks.forEach((resize) => resize()))

    expect(content).toHaveClass('is-overflowing')
    expect(viewport).toHaveClass('is-overflowing')
    expect(content.style.getPropertyValue('--overflow-marquee-distance')).toBe('24px')
    expect(content.style.getPropertyValue('--overflow-marquee-travel-duration')).toBe('750ms')
    fireEvent.mouseEnter(trigger!)

    const [shortKeyframes, shortOptions] = animate.mock.calls.at(-1) as unknown as [Keyframe[], KeyframeAnimationOptions]
    expect(shortOptions.easing).toBe('linear')
    const shortDurationMs = Number(shortOptions.duration)
    const shortTravelMs = (Number(shortKeyframes[1].offset) - Number(shortKeyframes[0].offset)) * shortDurationMs
    expect(shortTravelMs).toBeCloseTo(750)
    const shortReturnMs = (Number(shortKeyframes[2].offset) - Number(shortKeyframes[1].offset)) * shortDurationMs
    expect(shortReturnMs).toBeCloseTo(750)
    fireEvent.mouseLeave(trigger!)
    expect(cancel).toHaveBeenCalled()

    Object.defineProperty(content, 'scrollWidth', { configurable: true, value: 240 })
    act(() => resizeCallbacks.forEach((resize) => resize()))

    expect(content.style.getPropertyValue('--overflow-marquee-distance')).toBe('180px')
    expect(content.style.getPropertyValue('--overflow-marquee-travel-duration')).toBe('5625ms')
    fireEvent.mouseEnter(trigger!)

    const [longKeyframes, longOptions] = animate.mock.calls.at(-1) as unknown as [Keyframe[], KeyframeAnimationOptions]
    const longDurationMs = Number(longOptions.duration)
    const longTravelMs = (Number(longKeyframes[1].offset) - Number(longKeyframes[0].offset)) * longDurationMs
    expect(longTravelMs).toBeCloseTo(5625)
    const longReturnMs = (Number(longKeyframes[2].offset) - Number(longKeyframes[1].offset)) * longDurationMs
    expect(longReturnMs).toBeCloseTo(5625)
    expect(24 / (shortTravelMs / 1000)).toBeCloseTo(180 / (longTravelMs / 1000), 1)
    expect(180 / (longTravelMs / 1000)).toBeCloseTo(32, 1)
  })

  it('adds the title reveal inset before reversing even for micro-overflow', () => {
    const resizeCallbacks: Array<() => void> = []
    vi.stubGlobal('ResizeObserver', class {
      constructor(callback: ResizeObserverCallback) {
        resizeCallbacks.push(() => callback([], {} as ResizeObserver))
      }
      observe = vi.fn()
      disconnect = vi.fn()
    })
    const { container } = render(
      <OverflowMarquee endRevealInset={12}>短标题</OverflowMarquee>,
    )
    const viewport = container.querySelector<HTMLElement>('.overflow-marquee')
    const content = screen.getByText('短标题')

    Object.defineProperty(viewport, 'clientWidth', { configurable: true, value: 60 })
    Object.defineProperty(content, 'scrollWidth', { configurable: true, value: 62 })
    act(() => resizeCallbacks.forEach((resize) => resize()))

    expect(viewport).toHaveClass('is-overflowing')
    expect(content.style.getPropertyValue('--overflow-marquee-distance')).toBe('14px')
    expect(content.style.getPropertyValue('--overflow-marquee-travel-duration')).toBe('438ms')
  })
})
