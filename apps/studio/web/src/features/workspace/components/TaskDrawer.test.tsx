import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { Conversation, ConversationRunStatus } from '../../../types'
import { TaskDrawer } from './TaskDrawer'

const conversation = (
  todos: Conversation['todos'],
  runStatus: ConversationRunStatus = 'streaming',
): Conversation => ({
  threadId: 'thread-a',
  title: '任务抽屉',
  pinned: false,
  updatedAt: '2026-08-17T00:00:00.000Z',
  model: 'GPT-5.5',
  mode: 'default',
  messages: [],
  todos,
  taskTrace: { phase: 'unloaded' },
  runStatus,
})

describe('TaskDrawer', () => {
  it('renders the production Todo contract without dead Plan or splitter UI', () => {
    const { container } = render(<TaskDrawer conversation={conversation([
      { id: 'todo-1', content: '执行任务', status: 'running' },
    ])} />)

    expect(screen.getByRole('complementary', { name: '任务抽屉' })).toBeInTheDocument()
    expect(screen.getByRole('region', { name: '待办清单' })).toHaveAttribute('tabindex', '0')
    expect(container.querySelectorAll('.panel-scroll')).toHaveLength(1)
    expect(container.querySelector('.ui-overlay-scrollbar')).toHaveAttribute('data-visibility', 'transient')
    expect(container.querySelector('.plan-panel')).toBeNull()
    expect(screen.queryByRole('separator')).not.toBeInTheDocument()
  })

  it('renders Todo state as non-interactive content', () => {
    const { container } = render(<TaskDrawer conversation={conversation([
      { id: 'todo-1', content: '等待执行', status: 'pending' },
      { id: 'todo-2', content: '用户已取消', status: 'cancelled' },
    ])} />)

    expect(container.querySelectorAll('.todo-item')).toHaveLength(2)
    expect(container.querySelectorAll('.todo-item button')).toHaveLength(0)
    expect(screen.getByLabelText('步骤 1 · 待执行')).toBeInTheDocument()
    expect(screen.getByLabelText('步骤 2 · 已取消')).toBeInTheDocument()
  })

  it('renders an in-progress Todo as unfinished after a successful run ends', () => {
    const todos: Conversation['todos'] = [
      { id: 'todo-1', content: '检查需求', status: 'running' },
      { id: 'todo-2', content: '执行验证', status: 'pending' },
    ]
    const { container } = render(
      <TaskDrawer conversation={conversation(todos, 'idle')} />,
    )

    expect(screen.getByText('0/2')).toBeInTheDocument()
    expect(screen.getByLabelText('步骤 1 · 未完成')).toBeInTheDocument()
    expect(container.querySelector('.todo-item.is-unfinished')).toBeInTheDocument()
    expect(container.querySelector('.todo-state .spin')).toBeNull()
    expect(container.querySelector('.progress-track span')).toHaveStyle({ width: '0%' })
    expect(todos[0]?.status).toBe('running')
  })

  it.each([
    ['waiting_approval', 'paused', '等待审批'],
    ['detached', 'background', '后台执行中'],
    ['error', 'failed', '失败'],
  ] as const)(
    'derives %s Todo presentation without mutating persisted state',
    (runStatus, viewStatus, label) => {
      const todo = { id: 'todo-1', content: '执行任务', status: 'running' as const }
      const { container } = render(
        <TaskDrawer conversation={conversation([todo], runStatus)} />,
      )

      expect(screen.getByLabelText(`步骤 1 · ${label}`)).toBeInTheDocument()
      expect(container.querySelector(`.todo-item.is-${viewStatus}`)).toBeInTheDocument()
      expect(todo.status).toBe('running')
    },
  )

  it('keeps active Todo progress animated only while the run is streaming', () => {
    const { container } = render(<TaskDrawer conversation={conversation([
      { id: 'todo-1', content: '已完成', status: 'completed' },
      { id: 'todo-2', content: '执行任务', status: 'running' },
    ])} />)

    expect(container.querySelector('.todo-state .spin')).toBeInTheDocument()
    expect(container.querySelector('.progress-track span')).toHaveStyle({ width: '75%' })
  })

  it('does not close from an Escape already handled by a higher overlay', () => {
    const onClose = vi.fn()
    render(<TaskDrawer conversation={conversation([])} onClose={onClose} />)
    const event = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })
    event.preventDefault()

    fireEvent(document, event)

    expect(onClose).not.toHaveBeenCalled()
  })
})
