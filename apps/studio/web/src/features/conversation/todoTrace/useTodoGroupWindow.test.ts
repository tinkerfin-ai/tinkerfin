import { describe, expect, it } from 'vitest'

import type { TodoGroup } from '../../../api/conversation/taskTrace'
import { calculateTodoGroupWindow } from './useTodoGroupWindow'

const groups = Array.from({ length: 5_000 }, (_, index): TodoGroup => ({
  id: `todo-group:run-${index}`,
  userMessageId: `message-${index}`,
  userMessagePreview: `任务组 ${index}`,
  groupToolCallId: `tool-${index}`,
  createdAt: new Date(Date.UTC(2026, 7, 30, 12, 0, index)).toISOString(),
  status: 'running',
  todos: Array.from({ length: 10 }, (__, todoIndex) => ({
    id: `todo-${index}-${todoIndex}`,
    content: `任务 ${todoIndex}`,
    status: 'pending',
  })),
}))

describe('calculateTodoGroupWindow', () => {
  it('keeps the complete scroll range while mounting a bounded root window', () => {
    const top = calculateTodoGroupWindow({
      groups,
      scrollTop: 0,
      viewportHeight: 1_440,
    })
    const middle = calculateTodoGroupWindow({
      groups,
      expandedIds: new Set([groups[2_500]!.id]),
      scrollTop: 180_000,
      viewportHeight: 1_440,
    })

    expect(top.totalHeight).toBe((5_000 * 72) + 64)
    expect(top.items.length).toBeLessThanOrEqual(45)
    expect(middle.items.length).toBeLessThanOrEqual(45)
    expect(middle.offsets).toHaveLength(5_001)
    expect(middle.totalHeight).toBeGreaterThan(top.totalHeight)
  })

  it('uses measured expanded height without changing group count or order', () => {
    const measured = new Map([[groups[100]!.id, 900]])
    const result = calculateTodoGroupWindow({
      groups,
      expandedIds: new Set([groups[100]!.id]),
      scrollTop: 6_500,
      viewportHeight: 768,
      measuredHeights: measured,
    })

    expect(result.offsets[101]! - result.offsets[100]!).toBe(900)
    expect(result.items.map((item) => item.group.id))
      .toEqual([...result.items].sort((a, b) => a.index - b.index).map((item) => item.group.id))
  })

  it('does not retain an offscreen group old expanded height after expansion moves', () => {
    const result = calculateTodoGroupWindow({
      groups,
      expandedIds: new Set([groups[200]!.id]),
      scrollTop: 13_500,
      viewportHeight: 768,
      measuredHeights: new Map([[groups[100]!.id, 900]]),
    })

    expect(result.offsets[101]! - result.offsets[100]!).toBe(72)
    expect(result.offsets[201]! - result.offsets[200]!).toBeGreaterThan(72)
  })

  it('reserves independent dynamic height for every expanded group', () => {
    const result = calculateTodoGroupWindow({
      groups,
      expandedIds: new Set([groups[100]!.id, groups[101]!.id]),
      scrollTop: 7_000,
      viewportHeight: 768,
    })

    expect(result.offsets[101]! - result.offsets[100]!).toBeGreaterThan(72)
    expect(result.offsets[102]! - result.offsets[101]!).toBeGreaterThan(72)
  })
})
