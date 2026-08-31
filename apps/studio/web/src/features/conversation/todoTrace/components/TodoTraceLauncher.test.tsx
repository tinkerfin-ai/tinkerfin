import { fireEvent, render, screen } from '@testing-library/react'
import { createRef } from 'react'
import { describe, expect, it, vi } from 'vitest'

import type { WebTaskTraceViewState } from '../../../../types'
import { TodoTraceLauncher } from './TodoTraceLauncher'
import todoTraceStyles from '../todoTrace.css?raw'

const ready = (count: number): WebTaskTraceViewState => ({
  phase: 'ready',
  snapshot: {
    status: 'ready',
    todoGroups: Array.from({ length: count }, (_, index) => ({
      id: `todo-group:run-${index}`,
      userMessageId: `message-${index}`,
      userMessagePreview: `任务 ${index}`,
      groupToolCallId: `tool-${index}`,
      createdAt: `2026-08-31T00:00:0${index}.000Z`,
      status: 'running',
      todos: [],
    })),
  },
})

describe('TodoTraceLauncher', () => {
  it('keeps the launcher borderless in every visual state', () => {
    expect(todoTraceStyles).toMatch(
      /\.composer-auxiliary-control\.todo-trace-launcher\s*{[^}]*border:\s*0;/s,
    )
    expect(todoTraceStyles).toMatch(
      /\.composer-auxiliary-control\.todo-trace-launcher\.is-selected\s*{[^}]*box-shadow:\s*var\(--shadow-1\);/s,
    )
  })

  it('stays hidden before loading and when no group is confirmed', () => {
    const { rerender } = render(
      <TodoTraceLauncher
        ref={createRef()}
        taskTrace={{ phase: 'unloaded' }}
        open={false}
        loadFailed={false}
        onToggle={vi.fn()}
        onRetry={vi.fn()}
      />,
    )
    expect(screen.queryByRole('button')).not.toBeInTheDocument()

    rerender(
      <TodoTraceLauncher
        ref={createRef()}
        taskTrace={ready(0)}
        open={false}
        loadFailed={false}
        onToggle={vi.fn()}
        onRetry={vi.fn()}
      />,
    )
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })

  it('shows loading and retry as distinct accessible states', () => {
    const retry = vi.fn()
    const { rerender } = render(
      <TodoTraceLauncher
        ref={createRef()}
        taskTrace={{ phase: 'loading' }}
        open={false}
        loadFailed={false}
        onToggle={vi.fn()}
        onRetry={retry}
      />,
    )
    expect(screen.getByRole('button', { name: '正在加载任务轨迹' })).toBeDisabled()

    rerender(
      <TodoTraceLauncher
        ref={createRef()}
        taskTrace={{ phase: 'unloaded' }}
        open={false}
        loadFailed
        onToggle={vi.fn()}
        onRetry={retry}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: '重试任务轨迹' }))
    expect(retry).toHaveBeenCalledOnce()
  })

  it('counts groups and keeps the selected launcher available', () => {
    const toggle = vi.fn()
    render(
      <TodoTraceLauncher
        ref={createRef()}
        taskTrace={ready(2)}
        open
        loadFailed={false}
        onToggle={toggle}
        onRetry={vi.fn()}
      />,
    )
    const launcher = screen.getByRole('button', { name: '任务轨迹 2' })
    expect(launcher).toHaveAttribute('aria-expanded', 'true')
    expect(launcher).toHaveClass('is-selected')
    fireEvent.click(launcher)
    expect(toggle).toHaveBeenCalledOnce()
  })
})
