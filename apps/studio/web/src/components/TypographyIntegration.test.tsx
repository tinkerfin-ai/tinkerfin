import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { Conversation } from '../types'
import { ApprovalCard } from './ApprovalCard'
import { EmptyConversation } from './EmptyConversation'
import { ModalDialog } from './ModalDialog'

const conversationWithApproval = (activeIndex: number): Conversation => ({
  threadId: 'thread-typography',
  title: '字体测试',
  pinned: false,
  updatedAt: '2026-08-09T00:00:00.000Z',
  model: 'GPT-5.5',
  mode: 'default',
  messages: [],
  todos: [],
  plan: null,
  runStatus: 'waiting_approval',
  approval: {
    activeIndex,
    submitted: false,
    mode: 'options',
    items: [
      {
        id: 'approval-1',
        interruptId: 'interrupt-1',
        toolName: 'read_file',
        params: '{}',
        input: '{}',
        description: '读取文件',
        originalArgs: {},
        allowedDecisions: ['approve'],
      },
      {
        id: 'approval-2',
        interruptId: 'interrupt-2',
        toolName: 'write_file',
        params: '{}',
        input: '{}',
        description: '写入文件',
        originalArgs: {},
        allowedDecisions: ['approve'],
      },
    ],
  },
})

describe('typography reveal integration', () => {
  it('uses the display variant for the empty-state heading', () => {
    render(<EmptyConversation />)
    expect(screen.getByRole('heading', { level: 2, name: '暂无消息' }))
      .toHaveClass('typography-reveal--display')
  })

  it('uses state reveals for modal and active approval titles', () => {
    const { unmount } = render(
      <ModalDialog
        open
        title="重命名会话"
        confirmLabel="保存"
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
      />,
    )
    expect(screen.getByRole('heading', { level: 2, name: '重命名会话' }))
      .toHaveClass('typography-reveal--state')
    unmount()

    const { rerender } = render(
      <ApprovalCard
        conversation={conversationWithApproval(0)}
        onChange={vi.fn()}
        onSubmit={vi.fn()}
      />,
    )
    expect(screen.getByRole('heading', { level: 3, name: '读取文件' }))
      .toHaveClass('typography-reveal--state')

    rerender(
      <ApprovalCard
        conversation={conversationWithApproval(1)}
        onChange={vi.fn()}
        onSubmit={vi.fn()}
      />,
    )
    expect(screen.getByRole('heading', { level: 3, name: '写入文件' }))
      .toHaveClass('typography-reveal--state')
  })
})
