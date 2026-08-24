import { CircleAlert, LoaderCircle, RotateCcw } from 'lucide-react'

import { Button, Surface } from '../../../components/ui'
import { useI18n } from '../../../i18n'

export function WorkspaceStatus({
  kind,
  title,
  description,
  onRetry,
  compact = false,
}: {
  kind: 'loading' | 'error'
  title: string
  description: string
  onRetry?: () => void
  compact?: boolean
}) {
  const { t } = useI18n()
  return (
    <Surface
      tone={kind === 'error' ? 'danger' : 'neutral'}
      elevation={compact ? 0 : 1}
      className={`workspace-status is-${kind}${compact ? ' is-compact' : ''}`}
      role={kind === 'error' ? 'alert' : 'status'}
      aria-busy={kind === 'loading' || undefined}
    >
      <span className="workspace-status__icon" aria-hidden="true">
        {kind === 'loading'
          ? <LoaderCircle className="spin" size={20} />
          : <CircleAlert size={20} />}
      </span>
      <div>
        <strong>{title}</strong>
        <p>{description}</p>
      </div>
      {kind === 'error' && onRetry && (
        <Button size="sm" leadingIcon={<RotateCcw size={14} />} onClick={onRetry}>{t('重试')}</Button>
      )}
    </Surface>
  )
}
