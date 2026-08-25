import { fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'

import { buildEmptyConversation } from '../../../lib/workspace'
import type { ApprovalState, Conversation } from '../../../types'
import { ApprovalCard } from './ApprovalCard'

describe('ApprovalCard', () => {
  it('updates only approval state and preserves a concurrent conversation message', () => {
    const initial: Conversation = {
      ...buildEmptyConversation({
        threadId: 'thread-approval-concurrency',
        now: '2026-08-17T00:00:00.000Z',
        model: 'GPT-5.5',
      }),
      runStatus: 'waiting_approval',
      approval: {
        activeIndex: 0,
        submitted: false,
        mode: 'options',
        items: [{
          id: 'approval-1',
          interruptId: 'interrupt-1',
          toolName: 'write_file',
          params: '{}',
          input: '{}',
          description: '写入文件',
          originalArgs: {},
          allowedDecisions: ['approve', 'reject'],
        }],
      },
    }
    let authoritative = initial
    render(
      <ApprovalCard
        conversation={initial}
        onChange={(change) => {
          const maybeUpdater = change as unknown
          if (typeof maybeUpdater === 'function' && authoritative.approval) {
            authoritative = {
              ...authoritative,
              approval: (maybeUpdater as (current: ApprovalState) => ApprovalState)(
                authoritative.approval,
              ),
            }
          } else {
            authoritative = change as unknown as Conversation
          }
        }}
        onSubmit={vi.fn()}
      />,
    )
    authoritative = {
      ...authoritative,
      messages: [{
        id: 'message-concurrent',
        role: 'assistant',
        content: '审批期间到达的消息',
        createdAt: '2026-08-17T00:00:01.000Z',
      }],
    }

    fireEvent.click(screen.getByRole('button', { name: '允许' }))

    expect(authoritative.messages.map((message) => message.id)).toEqual(['message-concurrent'])
    expect(authoritative.approval?.items[0]?.decision).toBe('approved')
  })

  it('does not apply an old card action after the authoritative approval group changes', () => {
    const initial: Conversation = {
      ...buildEmptyConversation({
        threadId: 'thread-approval-replaced',
        now: '2026-08-17T00:00:00.000Z',
        model: 'GPT-5.5',
      }),
      runStatus: 'waiting_approval',
      approval: {
        activeIndex: 0,
        submitted: false,
        mode: 'options',
        items: [{
          id: 'approval-old',
          interruptId: 'interrupt-old',
          toolName: 'write_file',
          params: '{}',
          input: '{}',
          description: '旧审批',
          originalArgs: {},
          allowedDecisions: ['approve', 'reject'],
        }],
      },
    }
    let authoritativeApproval: ApprovalState = {
      ...initial.approval!,
      items: [{
        ...initial.approval!.items[0],
        id: 'approval-new',
        interruptId: 'interrupt-new',
        description: '新审批',
      }],
    }
    render(
      <ApprovalCard
        conversation={initial}
        onChange={(change) => {
          authoritativeApproval = change(authoritativeApproval)
        }}
        onSubmit={vi.fn()}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: '允许' }))

    expect(authoritativeApproval.items[0]?.interruptId).toBe('interrupt-new')
    expect(authoritativeApproval.items[0]?.decision).toBeUndefined()
  })

  it('renders restored arguments and every allowed Tool decision', () => {
    const originalArgs = {
      file_path: '/history-result.txt',
      content: 'HISTORY_APPROVAL_OK',
    }
    const conversation: Conversation = {
      ...buildEmptyConversation({
        threadId: 'thread-history-approval',
        now: '2026-08-17T00:00:00.000Z',
        model: 'GPT-5.5',
      }),
      runStatus: 'waiting_approval',
      approval: {
        activeIndex: 0,
        submitted: false,
        items: [{
          id: 'history-approval',
          interruptId: 'history-interrupt#0',
          toolCallId: 'history-tool-call',
          toolName: 'write_file',
          params: JSON.stringify(originalArgs, null, 2),
          input: JSON.stringify(originalArgs, null, 2),
          description: '确认历史写入',
          originalArgs,
          allowedDecisions: ['approve', 'edit', 'reject'],
        }],
      },
    }
    render(
      <ApprovalCard
        conversation={conversation}
        onChange={vi.fn()}
        onSubmit={vi.fn()}
      />,
    )

    expect(screen.getByText('/history-result.txt')).toBeInTheDocument()
    expect(screen.getByText('HISTORY_APPROVAL_OK')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '允许' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '编辑' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '拒绝' })).toBeInTheDocument()
  })

  it('keeps edit drafts bound to each interrupt while paging', () => {
    const firstArgs = { file_path: '/first.txt' }
    const secondArgs = { file_path: '/second.txt' }
    const initial: ApprovalState = {
      activeIndex: 0,
      submitted: false,
      mode: 'options',
      items: [
        {
          id: 'approval-first',
          interruptId: 'interrupt-first',
          toolName: 'write_file',
          params: JSON.stringify(firstArgs),
          input: JSON.stringify(firstArgs),
          description: '写入第一份文件',
          originalArgs: firstArgs,
          allowedDecisions: ['approve', 'edit', 'reject'],
        },
        {
          id: 'approval-second',
          interruptId: 'interrupt-second',
          toolName: 'write_file',
          params: JSON.stringify(secondArgs),
          input: JSON.stringify(secondArgs),
          description: '写入第二份文件',
          originalArgs: secondArgs,
          allowedDecisions: ['approve', 'edit', 'reject'],
        },
      ],
    }

    function Harness() {
      const [approval, setApproval] = useState(initial)
      const conversation: Conversation = {
        ...buildEmptyConversation({
          threadId: 'thread-drafts',
          now: '2026-08-25T00:00:00.000Z',
          model: 'GPT-5.5',
        }),
        runStatus: 'waiting_approval',
        approval,
      }
      return <ApprovalCard conversation={conversation} onChange={setApproval} onSubmit={vi.fn()} />
    }

    render(<Harness />)
    fireEvent.click(screen.getByRole('button', { name: '编辑' }))
    const editor = screen.getByLabelText('编辑参数（JSON 对象）')
    fireEvent.change(editor, { target: { value: '{"file_path":"/edited-first.txt"}' } })

    fireEvent.click(screen.getByRole('button', { name: '下一项审批' }))
    expect(screen.getByLabelText('编辑参数（JSON 对象）')).toHaveValue(
      JSON.stringify(secondArgs),
    )
    fireEvent.change(screen.getByLabelText('编辑参数（JSON 对象）'), {
      target: { value: '{"file_path":"/edited-second.txt"}' },
    })
    fireEvent.click(screen.getByRole('button', { name: '保存并允许' }))

    expect(screen.getByText('写入第一份文件')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '编辑' }))
    expect(screen.getByLabelText('编辑参数（JSON 对象）')).toHaveValue(
      '{"file_path":"/edited-first.txt"}',
    )
  })

  it('announces a dynamic approval error', () => {
    const conversation: Conversation = {
      ...buildEmptyConversation({
        threadId: 'thread-error',
        now: '2026-08-25T00:00:00.000Z',
        model: 'GPT-5.5',
      }),
      runStatus: 'waiting_approval',
      approval: {
        activeIndex: 0,
        submitted: false,
        error: '审批状态已经更新',
        items: [{
          id: 'approval-error',
          interruptId: 'interrupt-error',
          toolName: 'write_file',
          params: '{}',
          input: '{}',
          description: '确认操作',
          originalArgs: {},
          allowedDecisions: ['approve'],
        }],
      },
    }

    render(<ApprovalCard conversation={conversation} onChange={vi.fn()} onSubmit={vi.fn()} />)

    expect(screen.getByRole('alert')).toHaveTextContent('审批状态已经更新')
  })
})
