import { describe, expect, it } from 'vitest'
import { isConversationTitle } from './titles'

describe('会话标题边界', () => {
  it.each(['中', 'a', '😀'])('按Unicode字符限制32个字符：%s', (character) => {
    const snapshot = { threadId: 'thread', title: character.repeat(32), titleSource: 'generated', titleGenerationStatus: 'succeeded', titleSeq: 2 }
    expect(isConversationTitle(snapshot)).toBe(true)
    expect(isConversationTitle({ ...snapshot, title: character.repeat(33) })).toBe(false)
    expect(isConversationTitle({ ...snapshot, title: '' })).toBe(false)
  })
})


it('标题来源和生成状态必须是字符串', () => {
  expect(isConversationTitle({ threadId: 'thread', title: '标题', titleSource: ['user'], titleGenerationStatus: ['skipped'], titleSeq: 2 })).toBe(false)
})
