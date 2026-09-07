import { CheckCircle2, CircleAlert, Clock3, XCircle } from 'lucide-react'
import { Button } from '../../components/ui'
import { isTranslationKey, useI18n, type TranslationKey } from '../../i18n'
import type { ModelTestKind, ModelTestResult } from './useModelTest'

const resultMessages: Record<string, TranslationKey> = {
  model_listed: '基础检查通过，服务已列出该模型',
  models_unavailable: '还不能确认这个模型能否使用',
  model_not_listed: '还不能确认这个模型能否使用',
  text_received: '已收到文字回复',
  vision_response_received: '已收到看图回复，请核对识别内容',
  image_received: '测试图片已生成',
  empty_response: '服务没有返回可展示的内容',
  invalid_response: '服务返回格式不符合当前接口要求',
  response_too_large: '测试结果超过大小限制',
  timeout: '测试超时，请检查服务后重试',
  authentication_failed: '服务拒绝了认证，请检查密钥及接口权限',
  rate_limited: '服务请求受限，请稍后重试',
  service_error: '模型服务返回错误，请检查配置和服务状态',
  network_error: '无法连接模型服务，请检查地址和网络',
  endpoint_not_allowed: '该地址不在管理员允许的访问范围内',
  invalid_configuration: '配置不完整或参数不合法，请检查后重试',
}

export interface ModelTestPanelProps {
  purpose: 'chat' | 'image'
  disabled: boolean
  running?: ModelTestKind
  result?: ModelTestResult
  error?: string
  stale: boolean
  cancelled: boolean
  onRun: (kind: ModelTestKind) => void
  onCancel: () => void
}

/** 将基础连通性与真实模型能力分开展示，不把接口返回等同于识别正确 */
export function ModelTestPanel({ purpose, disabled, running, result, error, stale, cancelled, onRun, onCancel }: ModelTestPanelProps) {
  const { t } = useI18n()
  const Status = result?.outcome === 'success' ? CheckCircle2 : result?.outcome === 'failed' ? XCircle : CircleAlert
  return <section className="settings-models__test" aria-label={t('模型测试')}>
    <div className="settings-models__test-row">
      <div><strong>{t('基础检查')}</strong><p>{t('检查服务地址、密钥和模型名称')}</p></div>
      <Button type="button" size="xs" disabled={disabled || Boolean(running)} loading={running === 'basic'} onClick={() => onRun('basic')}>{t('检查连接')}</Button>
    </div>
    <div className="settings-models__test-row">
      <div><strong>{t('能力测试')}</strong><p>{t('实际发送一次请求，可能产生费用')}</p></div>
      <div className="settings-models__test-buttons">
        {purpose === 'chat' ? <>
          <Button type="button" size="xs" disabled={disabled || Boolean(running)} loading={running === 'text'} onClick={() => onRun('text')}>{t('文字回复')}</Button>
          <Button type="button" size="xs" disabled={disabled || Boolean(running)} loading={running === 'vision'} onClick={() => onRun('vision')}>{t('图片理解')}</Button>
        </> : <Button type="button" size="xs" disabled={disabled || Boolean(running)} loading={running === 'image'} onClick={() => onRun('image')}>{t('生成测试图片')}</Button>}
      </div>
    </div>
    {running && <div className="settings-models__test-pending" role="status"><span>{t('测试进行中…')}</span><Button type="button" variant="text" onClick={onCancel}>{t('取消等待')}</Button></div>}
    {cancelled && <p className="settings-models__hint" role="status">{t('已取消等待，服务商可能仍在处理并计费')}</p>}
    {error && <p className="settings-models__error" role="alert">{isTranslationKey(error) ? t(error) : error}</p>}
    {result && <div className={`settings-models__test-result${stale ? ' is-stale' : ''}`} role="status" data-outcome={result.outcome}>
      <div className="settings-models__result-heading"><Status size={16} aria-hidden="true" /><strong>{t(resultMessages[result.code] ?? '测试已完成，请查看结果')}</strong><span><Clock3 size={12} aria-hidden="true" />{t('{seconds} 秒', { seconds: (result.elapsed_ms / 1000).toFixed(1) })}</span></div>
      {['models_unavailable', 'model_not_listed'].includes(result.code) && <p className="settings-models__hint">{t(purpose === 'image' ? '请点击“生成测试图片”，看看能否成功生成' : '请点击“文字回复”，看看能否收到回答')}</p>}
      {stale && <p>{t('配置已修改，此结果已过期，请重新测试')}</p>}
      {result.image && ['image/png', 'image/jpeg', 'image/webp'].includes(result.image.mime_type) && <img className="settings-models__test-image" src={`data:${result.image.mime_type};base64,${result.image.data_base64}`} alt={t(result.kind === 'vision' ? '图片理解测试图' : '模型生成的测试图片')} />}
      {result.kind === 'vision' && <p className="settings-models__hint">{t('预期内容：左侧红色方块，右侧蓝色方块')}</p>}
      {result.text && <p className="settings-models__test-reply">{result.text}</p>}
    </div>}
  </section>
}
