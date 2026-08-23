import { useRef, useState } from 'react'
import type { Dispatch, SetStateAction } from 'react'

import {
  deleteConversation as deleteConversationApi,
  patchConversation,
} from '../../api/conversation/history'
import type { ToastKind } from '../../components/ui/ToastViewport'
import type { Conversation, WorkspaceState } from '../../types'
import {
  createNewConversation,
  removeConversation,
  updateConversation,
} from '../../lib/workspace'
import type { WorkspaceDialog } from './components/WorkspaceDialogs'

export function useConversationManagement({
  workspace,
  conversation,
  setWorkspace,
  setDraft,
  setDraftConversation,
  setDraftModel,
  catchUpDetachedConversation,
  abandonPlanInteraction,
  cancelActiveRun,
  detachThreadStream,
  getActiveThreadId,
  hasActiveStream,
  isActiveThread,
  onToast,
}: {
  workspace: WorkspaceState
  conversation: Conversation
  setWorkspace: Dispatch<SetStateAction<WorkspaceState>>
  setDraft: Dispatch<SetStateAction<string>>
  setDraftConversation: Dispatch<SetStateAction<Conversation | null>>
  setDraftModel: Dispatch<SetStateAction<string>>
  catchUpDetachedConversation: (threadId: string) => Promise<void>
  abandonPlanInteraction: (threadId: string) => void
  cancelActiveRun: () => Promise<boolean>
  detachThreadStream: (threadId: string, reason: string) => void
  getActiveThreadId: () => string | null
  hasActiveStream: () => boolean
  isActiveThread: (threadId: string) => boolean
  onToast: (kind: ToastKind, message: string) => void
}) {
  const [dialog, setDialog] = useState<WorkspaceDialog | null>(null)
  const [dialogPending, setDialogPending] = useState(false)
  const [dialogError, setDialogError] = useState<string>()
  const latest = useRef({ workspace, conversation })
  latest.current = { workspace, conversation }

  const findConversation = (threadId: string) => (
    latest.current.workspace.conversations.find((item) => item.threadId === threadId)
  )

  const performSelectConversation = (threadId: string) => {
    setDraft('')
    setDraftConversation(null)
    setWorkspace((state) => ({ ...state, currentThreadId: threadId }))
    if (findConversation(threadId)?.isHydrated) void catchUpDetachedConversation(threadId)
  }

  const performNewConversation = () => {
    setDraft('')
    setDraftConversation(null)
    setDraftModel(latest.current.conversation.model)
    setWorkspace((state) => createNewConversation(state))
  }

  const openDialog = (nextDialog: WorkspaceDialog) => {
    setDialogError(undefined)
    setDialog(nextDialog)
  }

  const closeDialog = () => {
    if (dialogPending) return
    setDialog(null)
    setDialogError(undefined)
  }

  const selectConversation = (threadId: string) => {
    if (hasActiveStream() && getActiveThreadId() !== threadId) {
      openDialog({ kind: 'detach-select', threadId })
      return
    }
    performSelectConversation(threadId)
  }

  const newConversation = () => {
    if (hasActiveStream()) {
      openDialog({ kind: 'detach-new' })
      return
    }
    performNewConversation()
  }

  const pinConversation = (threadId: string) => {
    const target = findConversation(threadId)
    const nextPinned = !target?.pinned
    setWorkspace((state) => updateConversation(
      state,
      threadId,
      (item) => ({ ...item, pinned: nextPinned }),
    ))
    void patchConversation(threadId, { pinned: nextPinned }).then(() => {
      onToast('success', nextPinned ? '会话已置顶' : '已取消置顶')
    }).catch(() => {
      setWorkspace((state) => updateConversation(
        state,
        threadId,
        (item) => ({ ...item, pinned: !nextPinned }),
      ))
      onToast('error', '置顶状态更新失败，请重试')
    })
  }

  const renameConversation = (threadId: string, restoreFocusTo?: HTMLElement | null) => {
    const target = findConversation(threadId)
    if (!target) return
    openDialog({ kind: 'rename', threadId, initialValue: target.title, restoreFocusTo })
  }

  const deleteConversation = (threadId: string, restoreFocusTo?: HTMLElement | null) => {
    const target = findConversation(threadId)
    if (!target) return
    openDialog({
      kind: 'delete',
      threadId,
      title: target.title,
      isRunning: isActiveThread(threadId),
      restoreFocusTo,
    })
  }

  const requestDisablePlan = (threadId: string) => {
    openDialog({ kind: 'disable-plan', threadId })
  }

  const confirmDialog = async (value?: string) => {
    if (!dialog || dialogPending) return
    setDialogPending(true)
    setDialogError(undefined)
    try {
      if (dialog.kind === 'rename') {
        const title = value?.trim()
        if (!title) throw new Error('会话名称不能为空')
        if (title !== dialog.initialValue) {
          await patchConversation(dialog.threadId, { title })
          setWorkspace((state) => updateConversation(
            state,
            dialog.threadId,
            (item) => ({ ...item, title }),
          ))
          onToast('success', '会话已重命名')
        }
      } else if (dialog.kind === 'disable-plan') {
        abandonPlanInteraction(dialog.threadId)
        onToast('info', '已关闭 Plan，下一条消息将使用 default 模式')
      } else if (dialog.kind === 'delete') {
        if (dialog.isRunning) await cancelActiveRun()
        await deleteConversationApi(dialog.threadId)
        setWorkspace((state) => removeConversation(state, dialog.threadId))
        onToast('success', '会话已删除')
      } else {
        if (hasActiveStream()) {
          const runningThreadId = getActiveThreadId()
            ?? latest.current.conversation.threadId
          detachThreadStream(
            runningThreadId,
            dialog.kind === 'detach-new'
              ? '已新建会话，之前会话的实时输出连接已断开。'
              : '已切换到其他会话，当前会话的实时输出连接已断开。',
          )
        }
        if (dialog.kind === 'detach-new') performNewConversation()
        else performSelectConversation(dialog.threadId)
        onToast('info', '已断开当前会话的实时输出')
      }
      setDialog(null)
    } catch (error) {
      setDialogError(error instanceof Error && error.message
        ? error.message
        : '操作失败，请稍后重试')
    } finally {
      setDialogPending(false)
    }
  }

  return {
    dialog,
    dialogPending,
    dialogError,
    closeDialog,
    confirmDialog,
    selectConversation,
    newConversation,
    pinConversation,
    renameConversation,
    deleteConversation,
    requestDisablePlan,
  }
}
