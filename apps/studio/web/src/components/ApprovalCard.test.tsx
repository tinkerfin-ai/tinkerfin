import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { buildEmptyConversation } from '../lib/workspace'
import type { ApprovalState, Conversation } from '../types'
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
})
