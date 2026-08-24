import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Conversation } from '../../../types'
import { TaskDrawer } from './TaskDrawer'

const conversation = (threadId: string): Conversation => ({
  threadId,
  title: '任务抽屉',
  pinned: false,
  updatedAt: '2026-08-17T00:00:00.000Z',
  model: 'GPT-5.5',
  mode: 'default',
  messages: [],
  todos: [{ id: 'todo-1', content: '执行任务', status: 'running' }],
  plan: {
    goal: '完成任务',
    steps: [{ title: '第一步', detail: '执行第一步' }],
  },
  runStatus: 'streaming',
})

describe('TaskDrawer', () => {
  beforeEach(() => {
    window.sessionStorage.clear()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('persists a valid split ratio per thread and restores it after remount', async () => {
    const first = render(<TaskDrawer conversation={conversation('thread-a')} />)
    const drawer = screen.getByLabelText('任务抽屉')
    expect(Array.from(drawer.querySelectorAll('.panel-scroll'))).toHaveLength(2)
    for (const scrollRegion of drawer.querySelectorAll('.panel-scroll')) {
      expect(scrollRegion).toHaveClass('ui-scrollbar')
    }
    expect(drawer.querySelectorAll('.ui-scrollbar-overlay')).toHaveLength(2)
    vi.spyOn(drawer, 'getBoundingClientRect').mockReturnValue({
      top: 0,
      height: 800,
    } as DOMRect)
    const separator = screen.getByRole('separator')

    fireEvent.keyDown(separator, { key: 'ArrowDown' })
    await waitFor(() => expect(Number(window.sessionStorage.getItem(
      'tinkerfin:task-drawer-split:thread-a',
    ))).toBeGreaterThan(50))
    const savedRatio = Number(window.sessionStorage.getItem(
      'tinkerfin:task-drawer-split:thread-a',
    ))
    expect(savedRatio).toBeGreaterThan(50)
    first.unmount()

    render(<TaskDrawer conversation={conversation('thread-a')} />)
    expect(screen.getByRole('separator')).toHaveAttribute(
      'aria-valuenow',
      String(Math.round(savedRatio)),
    )
  })

  it('ignores a stored ratio outside the supported 20 to 80 range', () => {
    window.sessionStorage.setItem('tinkerfin:task-drawer-split:thread-a', '95')

    render(<TaskDrawer conversation={conversation('thread-a')} />)

    expect(screen.getByRole('separator')).toHaveAttribute('aria-valuenow', '50')
  })

  it('measures once and coalesces pointer moves to the latest animation frame', () => {
    let frameCallback: FrameRequestCallback | undefined
    const requestFrame = vi.fn((callback: FrameRequestCallback) => {
      frameCallback = callback
      return 71
    })
    const cancelFrame = vi.fn()
    vi.stubGlobal('requestAnimationFrame', requestFrame)
    vi.stubGlobal('cancelAnimationFrame', cancelFrame)
    const view = render(<TaskDrawer conversation={conversation('thread-a')} />)
    const drawer = screen.getByLabelText('任务抽屉')
    const measure = vi.spyOn(drawer, 'getBoundingClientRect').mockReturnValue({
      top: 100,
      height: 800,
    } as DOMRect)
    const separator = screen.getByRole('separator')
    const dispatchPointer = (type: string, clientY: number) => {
      const event = new Event(type, { bubbles: true, cancelable: true })
      Object.defineProperties(event, {
        clientY: { value: clientY },
        pointerId: { value: 1 },
      })
      fireEvent(separator, event)
    }

    dispatchPointer('pointerdown', 300)
    dispatchPointer('pointermove', 400)
    dispatchPointer('pointermove', 600)

    expect(measure).toHaveBeenCalledOnce()
    expect(requestFrame).toHaveBeenCalledOnce()
    act(() => frameCallback?.(performance.now()))
    expect(separator).toHaveAttribute('aria-valuenow', '63')

    dispatchPointer('pointermove', 500)
    view.unmount()
    expect(cancelFrame).toHaveBeenCalledWith(71)
  })
})
