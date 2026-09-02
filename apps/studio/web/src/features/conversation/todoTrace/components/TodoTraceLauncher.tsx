import { ListChecks, LoaderCircle, TriangleAlert } from 'lucide-react'
import { forwardRef } from 'react'

import { useI18n } from '../../../../i18n'
import type { WebTaskTraceViewState } from '../../../../types'

export const TodoTraceLauncher = forwardRef<HTMLButtonElement, {
  taskTrace: WebTaskTraceViewState
  open: boolean
  loadFailed: boolean
  onToggle: () => void
  onRetry: () => void
}>(function TodoTraceLauncher({
  taskTrace,
  open,
  loadFailed,
  onToggle,
  onRetry,
}, ref) {
  const { t } = useI18n()
  if (taskTrace.phase === 'unloaded' && !loadFailed) return null
  if (taskTrace.phase === 'loading') {
    return (
      <button
        ref={ref}
        type="button"
        className="composer-auxiliary-control composer-trace-launcher todo-trace-launcher is-loading"
        aria-label={t('正在加载任务轨迹')}
        disabled
      >
        <LoaderCircle size={16} className="todo-trace-spin" aria-hidden="true" />
        <span>{t('正在加载任务轨迹')}</span>
      </button>
    )
  }
  if (taskTrace.phase === 'unavailable' || loadFailed) {
    return (
      <button
        ref={ref}
        type="button"
        className="composer-auxiliary-control composer-trace-launcher todo-trace-launcher is-error"
        aria-label={t('重试任务轨迹')}
        onClick={onRetry}
      >
        <TriangleAlert size={16} aria-hidden="true" />
        <span>{t('任务轨迹不可用')}</span>
      </button>
    )
  }
  if (taskTrace.phase !== 'ready' || taskTrace.snapshot.todoGroups.length === 0) {
    return null
  }
  const count = taskTrace.snapshot.todoGroups.length
  return (
    <button
      ref={ref}
      type="button"
      className={`composer-auxiliary-control composer-trace-launcher todo-trace-launcher${open ? ' is-selected' : ''}`}
      aria-expanded={open}
      aria-controls="todo-trace-drawer"
      aria-label={t('任务轨迹 {count}', { count })}
      onClick={onToggle}
    >
      <ListChecks size={16} aria-hidden="true" />
      <span>{t('任务轨迹 {count}', { count })}</span>
    </button>
  )
})
