import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  clearPlanQuestionCollapsed,
  planQuestionCollapseKey,
  readPlanQuestionCollapsed,
  writePlanQuestionCollapsed,
} from './planQuestionCollapse'

describe('Plan 澄清卡片收起状态缓存', () => {
  beforeEach(() => {
    window.sessionStorage.clear()
    vi.restoreAllMocks()
  })

  it('按会话独立保存、读取并清理状态', () => {
    writePlanQuestionCollapsed('thread-a', true)
    writePlanQuestionCollapsed('thread-b', false)

    expect(readPlanQuestionCollapsed('thread-a')).toBe(true)
    expect(readPlanQuestionCollapsed('thread-b')).toBe(false)
    expect(window.sessionStorage.getItem(planQuestionCollapseKey('thread-a'))).toBe('collapsed')

    clearPlanQuestionCollapsed('thread-a')
    expect(window.sessionStorage.getItem(planQuestionCollapseKey('thread-a'))).toBeNull()
  })

  it('存储不可用时回退为展开且不抛出异常', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new DOMException('blocked')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('blocked')
    })
    vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => {
      throw new DOMException('blocked')
    })

    expect(readPlanQuestionCollapsed('thread-a')).toBe(false)
    expect(() => writePlanQuestionCollapsed('thread-a', true)).not.toThrow()
    expect(() => clearPlanQuestionCollapsed('thread-a')).not.toThrow()
  })
})
