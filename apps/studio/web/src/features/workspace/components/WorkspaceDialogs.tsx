import { ModalDialog } from './ModalDialog'
import { useI18n } from '../../../i18n'

export type WorkspaceDialog =
  | { kind: 'rename'; threadId: string; initialValue: string; restoreFocusTo?: HTMLElement | null }
  | { kind: 'delete'; threadId: string; title: string; isRunning: boolean; restoreFocusTo?: HTMLElement | null }
  | { kind: 'detach-select'; threadId: string }
  | { kind: 'detach-new' }
  | { kind: 'disable-plan'; threadId: string }

export function WorkspaceDialogs({
  dialog,
  pending,
  error,
  onConfirm,
  onCancel,
}: {
  dialog: WorkspaceDialog | null
  pending: boolean
  error?: string
  onConfirm: (value?: string) => void | Promise<void>
  onCancel: () => void
}) {
  const { t } = useI18n()
  if (!dialog) return null

  if (dialog.kind === 'rename') {
    return (
      <ModalDialog
        open
        title={t('重命名会话')}
        inputLabel={t('会话名称')}
        initialValue={dialog.initialValue}
        confirmLabel={t('保存')}
        isPending={pending}
        error={error}
        restoreFocusTo={dialog.restoreFocusTo}
        onConfirm={onConfirm}
        onCancel={onCancel}
      />
    )
  }

  if (dialog.kind === 'delete') {
    return (
      <ModalDialog
        open
        title={t('删除会话')}
        description={dialog.isRunning
          ? t('“{title}”仍在接收实时输出。继续会先断开连接，并永久删除全部历史记录', { title: dialog.title })
          : t('将永久删除“{title}”及其全部历史记录，此操作不可撤销', { title: dialog.title })}
        confirmLabel={t('删除')}
        tone="danger"
        isPending={pending}
        error={error}
        restoreFocusTo={dialog.restoreFocusTo}
        onConfirm={onConfirm}
        onCancel={onCancel}
      />
    )
  }

  if (dialog.kind === 'disable-plan') {
    return (
      <ModalDialog
        open
        title={t('关闭当前 Plan？')}
        description={t('当前 Plan 澄清或审阅将被取消，不会执行旧计划。Tool/Filesystem 审批不受影响')}
        confirmLabel={t('关闭 Plan')}
        isPending={pending}
        error={error}
        onConfirm={onConfirm}
        onCancel={onCancel}
      />
    )
  }

  return (
    <ModalDialog
      open
      title={t('断开实时输出？')}
      description={dialog.kind === 'detach-new'
        ? t('新建会话会断开当前实时输出，但后端任务可能仍会继续')
        : t('切换会话会断开当前实时输出，但后端任务可能仍会继续')}
      confirmLabel={dialog.kind === 'detach-new' ? t('断开并新建') : t('断开并切换')}
      isPending={pending}
      error={error}
      onConfirm={onConfirm}
      onCancel={onCancel}
    />
  )
}
