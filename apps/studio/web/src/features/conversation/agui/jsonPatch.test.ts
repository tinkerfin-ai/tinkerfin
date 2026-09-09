import { describe, expect, it } from 'vitest'

import { applyStateDelta, InvalidStateDeltaError } from './jsonPatch'

describe('strict JSON Patch', () => {
  it('applies legal object and array operations without mutating the input', () => {
    const current = {
      profile: { name: 'before' },
      items: ['first', 'third'],
    }

    const next = applyStateDelta(current, [
      { op: 'replace', path: '/profile/name', value: 'after' },
      { op: 'add', path: '/items/1', value: 'second' },
      { op: 'remove', path: '/items/0' },
    ])

    expect(next).toEqual({
      profile: { name: 'after' },
      items: ['second', 'third'],
    })
    expect(current).toEqual({
      profile: { name: 'before' },
      items: ['first', 'third'],
    })
  })

  it.each([
    ['replace 不存在的目标', [{ op: 'replace', path: '/missing', value: true }]],
    ['remove 不存在的目标', [{ op: 'remove', path: '/missing' }]],
    ['不存在的父节点', [{ op: 'add', path: '/missing/child', value: true }]],
    ['数组前导零索引', [{ op: 'replace', path: '/items/01', value: true }]],
    ['replace 使用追加索引', [{ op: 'replace', path: '/items/-', value: true }]],
    ['超出数组边界', [{ op: 'add', path: '/items/3', value: true }]],
    ['删除根状态', [{ op: 'remove', path: '' }]],
    ['用标量替换根状态', [{ op: 'replace', path: '', value: 'invalid' }]],
  ] as const)('rejects %s', (_name, delta) => {
    expect(() => applyStateDelta({ items: [1] }, delta)).toThrow(InvalidStateDeltaError)
  })

  it('rejects the complete batch without exposing a partial mutation', () => {
    const current = { safe: 'before' }
    const delta = [
      { op: 'replace' as const, path: '/safe', value: 'after' },
      { op: 'replace' as const, path: '/missing', value: true },
    ]

    expect(() => applyStateDelta(current, delta)).toThrow(InvalidStateDeltaError)
    expect(current).toEqual({ safe: 'before' })
  })

  it('treats prototype-like keys as own data without changing object prototypes', () => {
    const next = applyStateDelta({}, [
      { op: 'add', path: '/__proto__', value: { polluted: true } },
      { op: 'add', path: '/constructor', value: 'data' },
    ])

    expect(Object.getPrototypeOf(next)).toBe(Object.prototype)
    expect(Object.hasOwn(next, '__proto__')).toBe(true)
    expect(next.__proto__).toEqual({ polluted: true })
    expect(next.constructor).toBe('data')
    expect(({} as { polluted?: boolean }).polluted).toBeUndefined()
  })
})
