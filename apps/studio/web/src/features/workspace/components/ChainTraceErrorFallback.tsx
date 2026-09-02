import { Button } from '../../../components/ui'
import { useI18n } from '../../../i18n'

export function ChainTraceErrorFallback({
  onReturn,
  onRetry,
}: {
  onReturn: () => void
  onRetry: () => void
}) {
  const { t } = useI18n()
  return (
    <div className="chain-trace-state is-error" role="alert">
      <p>{t('链路区域无法显示')}</p>
      <div className="chain-trace-state-actions">
        <Button onClick={onReturn}>{t('返回对话')}</Button>
        <Button onClick={onRetry}>{t('重试')}</Button>
      </div>
    </div>
  )
}
