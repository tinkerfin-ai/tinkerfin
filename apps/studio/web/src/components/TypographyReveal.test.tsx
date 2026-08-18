import { render, screen, waitFor } from '@testing-library/react'
import { useEffect } from 'react'
import type { DependencyList, EffectCallback } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const animationMocks = vi.hoisted(() => ({
  isMotionAllowed: true,
  hookConfigs: [] as Array<{ dependencies?: DependencyList; revertOnUpdate?: boolean; scope?: unknown }>,
  matchMediaAdd: vi.fn(),
  matchMediaRevert: vi.fn(),
  matchMediaInstances: [] as Array<{ revert: ReturnType<typeof vi.fn> }>,
  registerPlugin: vi.fn(),
  splitCreate: vi.fn(),
  tweenFrom: vi.fn(() => ({ kill: vi.fn() })),
}))

vi.mock('@gsap/react', () => ({
  useGSAP: (
    callback: EffectCallback,
    config: { dependencies?: DependencyList; revertOnUpdate?: boolean; scope?: unknown } = {},
  ) => {
    animationMocks.hookConfigs.push(config)
    useEffect(() => callback(), [callback, config])
  },
}))

vi.mock('gsap', () => ({
  default: {
    from: animationMocks.tweenFrom,
    matchMedia: () => {
      const instance = { revert: vi.fn() }
      animationMocks.matchMediaInstances.push(instance)
      return {
        add: (query: string, callback: () => void) => {
          animationMocks.matchMediaAdd(query)
          if (animationMocks.isMotionAllowed) callback()
        },
        revert: () => {
          instance.revert()
          animationMocks.matchMediaRevert()
        },
      }
    },
    registerPlugin: animationMocks.registerPlugin,
  },
}))

vi.mock('gsap/SplitText', () => ({
  SplitText: {
    create: (target: HTMLElement, options: {
      onSplit?: (split: { lines: HTMLElement[] }) => unknown
    }) => {
      animationMocks.splitCreate(target, options)
      options.onSplit?.({ lines: [target] })
      return { revert: vi.fn() }
    },
  },
}))

import { TypographyReveal } from './TypographyReveal'

describe('TypographyReveal', () => {
  beforeEach(() => {
    animationMocks.isMotionAllowed = true
    animationMocks.hookConfigs.length = 0
    animationMocks.matchMediaInstances.length = 0
    animationMocks.matchMediaAdd.mockClear()
    animationMocks.matchMediaRevert.mockClear()
    animationMocks.splitCreate.mockClear()
    animationMocks.tweenFrom.mockClear()
  })

  it('preserves heading semantics and applies the requested typography variant', async () => {
    render(
      <TypographyReveal as="h2" variant="state" id="dialog-title" className="custom-title">
        重命名会话
      </TypographyReveal>,
    )

    const heading = screen.getByRole('heading', { level: 2, name: '重命名会话' })
    expect(heading).toHaveAttribute('id', 'dialog-title')
    expect(heading).toHaveClass('custom-title', 'typography-heading', 'typography-reveal--state')
    await waitFor(() => expect(animationMocks.splitCreate).toHaveBeenCalledWith(
      heading,
      expect.objectContaining({
        type: 'lines',
        mask: 'lines',
        autoSplit: true,
        aria: 'auto',
      }),
    ))
    expect(animationMocks.matchMediaAdd).toHaveBeenCalledWith('(prefers-reduced-motion: no-preference)')
    expect(animationMocks.tweenFrom).toHaveBeenCalledWith([heading], {
      yPercent: 105,
      autoAlpha: 0,
      duration: 0.32,
      ease: 'power3.out',
      stagger: 0.04,
      clearProps: 'transform,opacity,visibility',
    })
    expect(animationMocks.hookConfigs[0]).toMatchObject({
      dependencies: ['state', undefined],
      revertOnUpdate: true,
    })
  })

  it('uses the display timing without animating when reduced motion is requested', async () => {
    animationMocks.isMotionAllowed = false
    render(<TypographyReveal as="h1" variant="display">智能投研工作台</TypographyReveal>)

    const heading = screen.getByRole('heading', { level: 1, name: '智能投研工作台' })
    expect(heading).toHaveClass('typography-reveal--display')
    await waitFor(() => expect(animationMocks.matchMediaAdd).toHaveBeenCalled())
    expect(animationMocks.splitCreate).not.toHaveBeenCalled()
    expect(animationMocks.tweenFrom).not.toHaveBeenCalled()
    expect(heading).not.toHaveAttribute('style')
  })

  it('reverts the previous media context on reveal key updates and unmount', async () => {
    const { rerender, unmount } = render(
      <TypographyReveal as="h3" variant="state" revealKey="interrupt-1">读取文件</TypographyReveal>,
    )
    await waitFor(() => expect(animationMocks.splitCreate).toHaveBeenCalledTimes(1))

    rerender(
      <TypographyReveal as="h3" variant="state" revealKey="interrupt-2">写入文件</TypographyReveal>,
    )
    await waitFor(() => expect(animationMocks.splitCreate).toHaveBeenCalledTimes(2))
    expect(animationMocks.matchMediaInstances[0].revert).toHaveBeenCalledTimes(1)

    unmount()
    expect(animationMocks.matchMediaInstances[1].revert).toHaveBeenCalledTimes(1)
  })
})
