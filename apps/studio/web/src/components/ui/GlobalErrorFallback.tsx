import { useI18n } from '../../i18n'
import { Button } from './Button'

export function GlobalErrorFallback() {
  const { t } = useI18n()
  return (
    <main id="main-content" className="app-error-fallback" role="alert">
      <div>
        <h1>{t('页面暂时无法显示')}</h1>
        <p>{t('界面渲染遇到问题，请重新加载后再试。')}</p>
        <Button variant="primary" size="md" onClick={() => window.location.reload()}>{t('重新加载页面')}</Button>
      </div>
    </main>
  )
}
