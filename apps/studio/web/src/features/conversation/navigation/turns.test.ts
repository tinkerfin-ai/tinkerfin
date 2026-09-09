import { describe, expect, it } from 'vitest'
import type { Message } from '../../../types'
import { conversationTurns } from './turns'

const message = (id: string, role: Message['role'], content: string): Message => ({ id, role, content, createdAt: '' })

describe('对话目录', () => {
  it('以提问 ID 区分相同内容，排除孤立回答、工具和子智能体内容', () => {
    expect(conversationTurns([
      message('orphan', 'assistant', '未加载提问'),
      message('a', 'user', '同一提问'),
      message('stage', 'assistant', '阶段回复'),
      message('tool', 'tool', '参数'),
      { ...message('child', 'assistant', '子智能体回复'), meta: { sourceAgentName: 'worker' } },
      message('final', 'assistant', '公开回答'),
      message('b', 'user', '同一提问'),
    ])).toEqual([
      { messageId: 'a', prompt: '同一提问', response: '公开回答' },
      { messageId: 'b', prompt: '同一提问', response: '' },
    ])
  })
  it('空历史不生成入口，摘要压缩空白并限制长度，不读取思考', () => {
    expect(conversationTurns([])).toEqual([])
    const turns = conversationTurns([
      message('a', 'user', ' 问题\n  详情 '),
      { ...message('b', 'assistant', '答'.repeat(200)), meta: { reasoning: '私有思考' } },
    ])
    expect(turns[0]).toEqual({ messageId: 'a', prompt: '问题 详情', response: '答'.repeat(160) })
  })
})
