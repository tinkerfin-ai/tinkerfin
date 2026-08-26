import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { buildEmptyConversation } from '../../../lib/workspace'
import type { ApprovalItem, ApprovalState, Conversation } from '../../../types'
import { ApprovalCard } from './ApprovalCard'

const approvalItem = (
  id: string,
  filePath: string,
  options: Partial<ApprovalItem> = {},
): ApprovalItem => ({
  id,
  interruptId: `interrupt-${id}`,
  toolCallId: `tool-${id}`,
  toolName: 'write_file',
  params: JSON.stringify({ file_path: filePath, content: `content-${id}` }, null, 2),
  input: filePath,
  description: `写入 ${filePath}`,
  originalArgs: { file_path: filePath, content: `content-${id}` },
  allowedDecisions: ['approve', 'reject'],
  ...options,
})

const conversationWithApproval = (
  approval: ApprovalState,
  threadId = 'thread-approval',
): Conversation => ({
  ...buildEmptyConversation({
    threadId,
    now: '2026-08-25T00:00:00.000Z',
    model: 'GPT-5.5',
  }),
  runStatus: 'waiting_approval',
  approval,
})

describe('ApprovalCard', () => {
  beforeEach(() => window.sessionStorage.clear())

  it('advances one independent card at a time and submits the final group once', async () => {
    const user = userEvent.setup()
    const onSubmit = vi.fn()

    function Harness() {
      const [approval, setApproval] = useState<ApprovalState>({
        activeIndex: 0,
        submitted: false,
        mode: 'options',
        items: [
          approvalItem('first', '/first.txt'),
          approvalItem('second', '/second.txt'),
        ],
      })
      return (
        <ApprovalCard
          conversation={conversationWithApproval(approval)}
          onChange={setApproval}
          onSubmit={onSubmit}
        />
      )
    }

    render(<Harness />)

    expect(screen.getByRole('region', { name: '等待审批' })).toHaveTextContent('/first.txt')
    expect(screen.queryByText('1 / 2')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '允许' }))

    expect(screen.getByRole('region', { name: '等待审批' })).toHaveTextContent('/second.txt')
    expect(screen.queryByText('2 / 2')).not.toBeInTheDocument()
    expect(onSubmit).not.toHaveBeenCalled()

    await user.click(screen.getByRole('button', { name: '拒绝' }))
    await user.type(screen.getByLabelText('拒绝原因（可选）'), '文件位置不正确')
    await user.click(screen.getByRole('button', { name: '确认拒绝' }))

    expect(onSubmit).toHaveBeenCalledOnce()
    expect(onSubmit).toHaveBeenCalledWith(
      ['interrupt-first', 'interrupt-second'],
      {
        interruptId: 'interrupt-second',
        decision: 'rejected',
        rejectionReason: '文件位置不正确',
      },
    )
    for (const removedAction of ['编辑', '上一项审批', '下一项审批', '全部允许', '全部拒绝', '批量提交']) {
      expect(screen.queryByRole('button', { name: removedAction })).not.toBeInTheDocument()
    }
  })

  it('updates only approval state and preserves a concurrent conversation message', () => {
    const initial = conversationWithApproval({
      activeIndex: 0,
      submitted: false,
      mode: 'options',
      items: [
        approvalItem('first', '/first.txt'),
        approvalItem('second', '/second.txt'),
      ],
    })
    let authoritative = initial
    render(
      <ApprovalCard
        conversation={initial}
        onChange={(change) => {
          if (!authoritative.approval) return
          authoritative = {
            ...authoritative,
            approval: change(authoritative.approval),
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
        createdAt: '2026-08-25T00:00:01.000Z',
      }],
    }

    fireEvent.click(screen.getByRole('button', { name: '允许' }))

    expect(authoritative.messages.map((message) => message.id)).toEqual(['message-concurrent'])
    expect(authoritative.approval?.items[0]?.decision).toBe('approved')
    expect(authoritative.approval?.activeIndex).toBe(1)
  })

  it('does not apply an old card action after the authoritative approval group changes', () => {
    const initial = conversationWithApproval({
      activeIndex: 0,
      submitted: false,
      mode: 'options',
      items: [
        approvalItem('old', '/old.txt'),
        approvalItem('old-next', '/old-next.txt'),
      ],
    })
    let authoritativeApproval: ApprovalState = {
      activeIndex: 0,
      submitted: false,
      mode: 'options',
      items: [
        approvalItem('new', '/new.txt'),
        approvalItem('new-next', '/new-next.txt'),
      ],
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

  it('keeps historical edit metadata readable without exposing an edit action', () => {
    const conversation = conversationWithApproval({
      activeIndex: 0,
      submitted: false,
      items: [approvalItem('history', '/history-result.txt', {
        allowedDecisions: ['approve', 'edit', 'reject'],
      })],
    })

    render(<ApprovalCard conversation={conversation} onChange={vi.fn()} onSubmit={vi.fn()} />)

    expect(screen.getByText('Write')).toBeInTheDocument()
    expect(screen.getByText('/history-result.txt')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '允许' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '拒绝' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '编辑' })).not.toBeInTheDocument()
  })

  it('remembers collapse per conversation and restores the current approval card', async () => {
    const user = userEvent.setup()
    const approval: ApprovalState = {
      activeIndex: 0,
      submitted: false,
      items: [approvalItem('collapse', '/collapse.txt')],
    }
    const conversation = conversationWithApproval(approval)
    const view = render(<ApprovalCard conversation={conversation} onChange={vi.fn()} onSubmit={vi.fn()} />)

    expect(screen.getAllByText('写入 /collapse.txt')).toHaveLength(1)

    await user.click(screen.getAllByRole('button', { name: '收起审批卡片' }).at(-1)!)
    expect(screen.queryByRole('button', { name: '允许' })).not.toBeInTheDocument()
    expect(screen.getByRole('region', { name: '等待审批' })).not.toHaveTextContent('1 / 1')
    expect(window.sessionStorage.getItem('tinkerfin:approval-collapse:thread-approval'))
      .toBe('collapsed')

    view.rerender(
      <ApprovalCard
        conversation={conversationWithApproval(approval, 'thread-other')}
        onChange={vi.fn()}
        onSubmit={vi.fn()}
      />,
    )
    await waitFor(() => expect(screen.getByRole('button', { name: '允许' })).toBeInTheDocument())

    view.rerender(<ApprovalCard conversation={conversation} onChange={vi.fn()} onSubmit={vi.fn()} />)
    await waitFor(() => expect(screen.queryByRole('button', { name: '允许' })).not.toBeInTheDocument())

    await user.click(screen.getAllByRole('button', { name: '展开审批卡片' }).at(-1)!)
    expect(screen.getByRole('button', { name: '允许' })).toBeInTheDocument()
  })

  it('announces an error and exposes an explicit retry for a completed group', () => {
    const onSubmit = vi.fn()
    const conversation = conversationWithApproval({
      activeIndex: 0,
      submitted: false,
      error: '审批提交失败',
      items: [approvalItem('retry', '/retry.txt', { decision: 'approved' })],
    })

    render(<ApprovalCard conversation={conversation} onChange={vi.fn()} onSubmit={onSubmit} />)

    expect(screen.getByRole('alert')).toHaveTextContent('审批提交失败')
    fireEvent.click(screen.getByRole('button', { name: '重新提交' }))
    expect(onSubmit).toHaveBeenCalledWith(['interrupt-retry'])
  })
})
