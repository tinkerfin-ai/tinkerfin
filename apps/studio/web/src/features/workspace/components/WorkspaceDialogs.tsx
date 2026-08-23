import { ModalDialog } from './ModalDialog'

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
  if (!dialog) return null

  if (dialog.kind === 'rename') {
    return (
      <ModalDialog
        open
        title="重命名会话"
        description="输入一个便于在历史记录中识别的名称。"
        inputLabel="会话名称"
        initialValue={dialog.initialValue}
        confirmLabel="保存"
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
        title="删除会话"
        description={dialog.isRunning
          ? `“${dialog.title}”仍在接收实时输出。继续会先断开连接，并永久删除全部历史记录。`
          : `将永久删除“${dialog.title}”及其全部历史记录，此操作不可撤销。`}
        confirmLabel="删除"
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
        title="关闭当前 Plan？"
        description="当前 Plan 澄清或审阅将被取消，不会执行旧计划。Tool/Filesystem 审批不受影响。"
        confirmLabel="关闭 Plan"
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
      title="断开实时输出？"
      description={dialog.kind === 'detach-new'
        ? '新建会话会断开当前实时输出，但后端任务可能仍会继续。'
        : '切换会话会断开当前实时输出，但后端任务可能仍会继续。'}
      confirmLabel={dialog.kind === 'detach-new' ? '断开并新建' : '断开并切换'}
      isPending={pending}
      error={error}
      onConfirm={onConfirm}
      onCancel={onCancel}
    />
  )
}
