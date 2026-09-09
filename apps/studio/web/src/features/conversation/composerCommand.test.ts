import { describe, expect, it } from 'vitest'

import { parseComposerSubmission } from './composerCommand'

describe('parseComposerSubmission', () => {
  it.each([
    ['/plan', { kind: 'plan-enable' }],
    ['  /plan   ', { kind: 'plan-enable' }],
    ['/plan 制定发布方案', { kind: 'plan-message', content: '制定发布方案' }],
    ['/plan   多空格正文  ', { kind: 'plan-message', content: '多空格正文' }],
    ['/plan off', { kind: 'plan-off-unsupported' }],
    ['/planner', { kind: 'message', content: '/planner' }],
    ['/PLAN', { kind: 'message', content: '/PLAN' }],
    ['普通消息', { kind: 'message', content: '普通消息' }],
  ] as const)('parses %s without leaking command syntax', (input, expected) => {
    expect(parseComposerSubmission(input)).toEqual(expected)
  })
})
