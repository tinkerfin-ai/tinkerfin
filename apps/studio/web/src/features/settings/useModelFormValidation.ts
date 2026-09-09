import { useState } from 'react'
import { useI18n } from '../../i18n'
import type { ModelSettings } from './useModelSettings'

type ModelField = 'display_name' | 'model_name' | 'base_url' | 'api_key'
type FieldErrors = Partial<Record<ModelField, string>>

/** 保存和测试共用字段反馈，修正输入后清除对应错误，不调用浏览器校验气泡 */
export function useModelFormValidation(model: ModelSettings | undefined, key: string, canReuseKey: boolean) {
  const { t } = useI18n()
  const [attempt, setAttempt] = useState(0)
  const collect = (): FieldErrors => {
    if (!model) return {}
    const next: FieldErrors = {}
    if (!model.display_name.trim()) next.display_name = t('请输入显示名称')
    if (!model.model_name.trim()) next.model_name = t('请填写服务商提供的 Model ID')
    try {
      const url = new URL(model.base_url)
      if (url.protocol !== 'http:' && url.protocol !== 'https:') throw new Error('protocol')
    } catch {
      next.base_url = t('请输入有效的 HTTP 或 HTTPS 接口地址')
    }
    if (!canReuseKey && !key.trim()) next.api_key = t('请输入 API Key')
    return next
  }

  return {
    errors: attempt > 0 ? collect() : {},
    attempt,
    validate: () => {
      const next = collect()
      setAttempt(value => value + 1)
      return Object.keys(next).length === 0
    },
    reset: () => setAttempt(0),
  }
}
