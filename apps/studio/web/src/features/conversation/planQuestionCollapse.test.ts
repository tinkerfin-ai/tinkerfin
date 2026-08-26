import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  approvalCollapseKey,
  clearInteractionCardCollapsed,
  planQuestionCollapseKey,
  planReviewCollapseKey,
  readApprovalCollapsed,
  readPlanQuestionCollapsed,
  readPlanReviewCollapsed,
  writeApprovalCollapsed,
  writePlanQuestionCollapsed,
  writePlanReviewCollapsed,
} from './planQuestionCollapse'

describe('接管交互卡片收起状态缓存', () => {
  beforeEach(() => {
    window.sessionStorage.clear()
    vi.restoreAllMocks()
  })

  it('按会话和卡片类型独立保存、读取并统一清理状态', () => {
    writeApprovalCollapsed('thread-a', true)
    writeApprovalCollapsed('thread-b', false)
    writePlanQuestionCollapsed('thread-a', true)
    writePlanQuestionCollapsed('thread-b', false)
    writePlanReviewCollapsed('thread-a', false)
    writePlanReviewCollapsed('thread-b', true)

    expect(readApprovalCollapsed('thread-a')).toBe(true)
    expect(readApprovalCollapsed('thread-b')).toBe(false)
    expect(readPlanQuestionCollapsed('thread-a')).toBe(true)
    expect(readPlanQuestionCollapsed('thread-b')).toBe(false)
    expect(readPlanReviewCollapsed('thread-a')).toBe(false)
    expect(readPlanReviewCollapsed('thread-b')).toBe(true)
    expect(window.sessionStorage.getItem(approvalCollapseKey('thread-a'))).toBe('collapsed')
    expect(window.sessionStorage.getItem(planQuestionCollapseKey('thread-a'))).toBe('collapsed')
    expect(window.sessionStorage.getItem(planReviewCollapseKey('thread-b'))).toBe('collapsed')

    clearInteractionCardCollapsed('thread-a')
    expect(window.sessionStorage.getItem(approvalCollapseKey('thread-a'))).toBeNull()
    expect(window.sessionStorage.getItem(planQuestionCollapseKey('thread-a'))).toBeNull()
    expect(window.sessionStorage.getItem(planReviewCollapseKey('thread-a'))).toBeNull()
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

    expect(readApprovalCollapsed('thread-a')).toBe(false)
    expect(readPlanQuestionCollapsed('thread-a')).toBe(false)
    expect(readPlanReviewCollapsed('thread-a')).toBe(false)
    expect(() => writeApprovalCollapsed('thread-a', true)).not.toThrow()
    expect(() => writePlanQuestionCollapsed('thread-a', true)).not.toThrow()
    expect(() => writePlanReviewCollapsed('thread-a', true)).not.toThrow()
    expect(() => clearInteractionCardCollapsed('thread-a')).not.toThrow()
  })
})
