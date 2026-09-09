import { describe, expect, it } from 'vitest'
import { combineModelOptions, parseModelOptions, splitModelOptions } from './modelOptions'

describe('模型高级参数契约', () => {
  it.each(['[]', 'null', '"text"', '2'])('拒绝非对象 %s', (text) => expect(parseModelOptions(text).issues[0]?.code).toBe('object_required'))
  it.each(['{', '{"x":1,}', '{/* note */"x":1}'])('严格检查 JSON 语法 %s', (text) => expect(parseModelOptions(text).issues[0]?.code).toBe('syntax'))
  it('定位嵌套重复键', () => {
    const text = '{"x":{"a":1,"a":2}}'
    const issue = parseModelOptions(text).issues[0]
    expect(issue?.code).toBe('duplicate')
    expect(text.slice(issue?.from, issue?.to)).toBe('"a"')
  })
  it.each(['model', 'prompt', 'n', 'api_key', 'Authorization'])('禁止受控键 %s', (key) => expect(parseModelOptions(JSON.stringify({[key]: 'x'})).issues[0]?.code).toBe('reserved'))
  it.each(['size', 'output_format'])('拒绝与常用控件冲突 %s', (key) => expect(parseModelOptions(JSON.stringify({[key]: 'x'})).issues[0]?.code).toBe('common'))
  it('拒绝无穷大', () => expect(parseModelOptions('{"value":1e999}').issues[0]?.code).toBe('nonfinite'))
  it('按 UTF-8 字节限制编辑器内容', () => expect(parseModelOptions(JSON.stringify({notes: '汉'.repeat(23_000)})).issues[0]?.code).toBe('too_large'))
  it('Seedream 参数经常用项和高级编辑无损往返', () => {
    const original = {size: '2K', output_format: 'png', watermark: false, response_format: 'url', sequential_image_generation: 'disabled', extra: {quality: [1, 'x']}}
    const split = splitModelOptions(original)
    const parsed = parseModelOptions(split.advanced)
    expect(parsed.issues).toEqual([])
    expect(combineModelOptions(parsed.value ?? {}, split.size, split.format)).toEqual(original)
  })
  it('服务默认不添加参数', () => expect(combineModelOptions({}, '', '')).toEqual({}))
})

it('过深输入在解析前得到可恢复错误', () => {
  expect(parseModelOptions('{"x":'.repeat(65) + '1' + '}'.repeat(65)).issues[0]?.code).toBe('too_deep')
  expect(parseModelOptions('{"x":'.repeat(64) + '1' + '}'.repeat(64)).issues).toEqual([])
})
