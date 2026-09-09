import { describe, expect, it } from 'vitest'
import { isTranslationKey } from './messages'
import { translate } from './locale'

describe('动态错误的翻译边界', () => {
  it('字典中的提示可按当前语言翻译', () => {
    const value: string = '上传失败，请重试'
    expect(isTranslationKey(value)).toBe(true)
    if (isTranslationKey(value)) expect(translate('en', value)).not.toBe(value)
  })

  it.each(['服务器返回错误：文件已删除', 'toString', '__proto__', ''])('保留未声明的提示 %s', (value) => {
    expect(isTranslationKey(value)).toBe(false)
  })
})
