import { fireEvent, render, screen } from '@testing-library/react'
import { ListChecks } from 'lucide-react'
import { describe, expect, it, vi } from 'vitest'

import type { Message } from '../../../types'
import { ToolCallRow } from './ToolCallRow'

const message = (toolName: string): Message => ({
  id: `tool-${toolName}`,
  role: 'tool',
  content: toolName,
  createdAt: '2026-08-30T12:00:00.000Z',
  meta: {
    toolName,
    toolCallId: `tool-${toolName}`,
    params: '{"path":"/workspace/report.md"}',
    status: 'completed',
  },
})

describe('ToolCallRow presentation', () => {
  it('renders a controlled Todos presentation without exposing tool arguments', () => {
    render(
      <ToolCallRow
        message={message('write_todos')}
        presentationOverride={{
          title: 'Todos',
          summary: '1/2',
          icon: <ListChecks size={14} />,
        }}
      >
        <div>任务树</div>
      </ToolCallRow>,
    )

    expect(screen.getByText('Todos', { selector: '.tool-row-title' }))
      .toBeInTheDocument()
    expect(screen.getByText('1/2')).toBeInTheDocument()
    expect(screen.queryByText('/workspace/report.md')).not.toBeInTheDocument()
  })

  it('uses Todos for an ordinary write_todos tool without exposing arguments', () => {
    render(
      <ToolCallRow message={message('write_todos')}>
        <div>任务详情</div>
      </ToolCallRow>,
    )

    expect(screen.getByText('Todos', { selector: '.tool-row-title' }))
      .toBeInTheDocument()
    expect(screen.queryByText('/workspace/report.md')).not.toBeInTheDocument()
  })

  it('keeps the ordinary Tool presentation unchanged', () => {
    render(
      <ToolCallRow message={message('read_file')}>
        <div>普通详情</div>
      </ToolCallRow>,
    )

    expect(screen.getByText('Read')).toBeInTheDocument()
    expect(screen.getByText('/workspace/report.md')).toBeInTheDocument()
  })

  it('reveals an opened row with the nearest scroll position and does not scroll on close', () => {
    const onOpenChange = vi.fn()
    const frame = vi.spyOn(window, 'requestAnimationFrame').mockImplementation((callback) => {
      callback(0)
      return 1
    })
    const { container } = render(
      <ToolCallRow message={message('glob')} onOpenChange={onOpenChange}>
        <div>展开详情</div>
      </ToolCallRow>,
    )
    const row = container.querySelector<HTMLDetailsElement>('.tool-row')!
    const scrollIntoView = vi.fn()
    row.scrollIntoView = scrollIntoView

    row.open = true
    fireEvent(row, new Event('toggle'))

    expect(onOpenChange).toHaveBeenLastCalledWith(true)
    expect(scrollIntoView).toHaveBeenCalledOnce()
    expect(scrollIntoView).toHaveBeenCalledWith({ behavior: 'auto', block: 'nearest' })

    row.open = false
    fireEvent(row, new Event('toggle'))

    expect(onOpenChange).toHaveBeenLastCalledWith(false)
    expect(scrollIntoView).toHaveBeenCalledOnce()
    frame.mockRestore()
  })
})
