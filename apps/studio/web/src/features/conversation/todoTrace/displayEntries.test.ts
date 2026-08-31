import { describe, expect, it } from 'vitest'

import type { Message } from '../../../types'
import { buildEmptyConversation } from '../../../lib/workspace'
import { buildConversationDisplayEntries } from './displayEntries'

const tool = (
  id: string,
  toolName: string,
  options: Partial<NonNullable<Message['meta']>> = {},
): Message => ({
  id,
  role: 'tool',
  content: toolName,
  createdAt: '2026-08-30T12:00:00.000Z',
  meta: {
    toolName,
    toolCallId: id,
    runId: 'run-1',
    status: 'completed',
    ...options,
  },
})

describe('buildConversationDisplayEntries', () => {
  it('extracts the selected Todos row without splitting the ordinary batch', () => {
    const selected = tool('tool-selected', 'write_todos', { batchId: 'batch-1' })
    const failed = tool('tool-failed', 'write_todos', {
      batchId: 'batch-1',
      status: 'failed',
    })
    const ordinary = tool('tool-read', 'read_file', { batchId: 'batch-1' })
    const conversation = {
      ...buildEmptyConversation({ now: '2026-08-30T12:00:00.000Z', model: 'main' }),
      messages: [ordinary, selected, failed],
      taskTrace: {
        phase: 'ready' as const,
        snapshot: {
          status: 'ready' as const,
          todoGroups: [{
            id: 'todo-group:run-1',
            userMessageId: 'request-run-1',
            userMessagePreview: '执行任务',
            groupToolCallId: selected.id,
            createdAt: '2026-08-30T12:00:01.000Z',
            status: 'running' as const,
            todos: [],
          }],
        },
      },
    }

    const entries = buildConversationDisplayEntries(conversation)

    expect(entries.map((entry) => entry.type)).toEqual(['tools', 'todo-group'])
    expect(entries[0]?.type === 'tools' && entries[0].messages.map((item) => item.id))
      .toEqual(['tool-read', 'tool-failed'])
    expect(entries[1]?.type === 'todo-group' && entries[1].group.id)
      .toBe('todo-group:run-1')
  })

  it('keeps one unconfirmed row and hides subagent or redundant successful calls', () => {
    const conversation = {
      ...buildEmptyConversation({ now: '2026-08-30T12:00:00.000Z', model: 'main' }),
      messages: [
        tool('first-success', 'write_todos'),
        tool('second-success', 'write_todos'),
        tool('subagent-write', 'write_todos', { sourceAgentName: 'worker' }),
      ],
    }

    const entries = buildConversationDisplayEntries(conversation)

    expect(entries).toHaveLength(1)
    expect(entries[0]?.type === 'tools' && entries[0].messages[0]?.id)
      .toBe('first-success')
  })
})
