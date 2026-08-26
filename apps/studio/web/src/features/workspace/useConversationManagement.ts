import { useEffect, useRef, useState } from 'react'
import type { Dispatch, SetStateAction } from 'react'

import {
  deleteConversation as deleteConversationApi,
  patchConversation,
} from '../../api/conversation/history'
import type { ToastKind } from '../../components/ui/ToastViewport'
import type { Conversation, WorkspaceState } from '../../types'
import { clearInteractionCardCollapsed } from '../conversation/planQuestionCollapse'
import {
  createNewConversation,
  removeConversation,
  updateConversation,
} from '../../lib/workspace'
import type { WorkspaceDialog } from './components/WorkspaceDialogs'
import { useI18n } from '../../i18n'

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
  onConversationBoundary,
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
  onConversationBoundary: () => void
}) {
  const { t } = useI18n()
  const [dialog, setDialog] = useState<WorkspaceDialog | null>(null)
  const [dialogPending, setDialogPending] = useState(false)
  const [dialogError, setDialogError] = useState<string>()
  const [pinPendingThreadIds, setPinPendingThreadIds] = useState<ReadonlySet<string>>(
    () => new Set(),
  )
  const pinRequests = useRef(new Set<string>())
  const isMounted = useRef(true)
  const latest = useRef({ workspace, conversation })
  latest.current = { workspace, conversation }

  useEffect(() => () => {
    isMounted.current = false
  }, [])

  const findConversation = (threadId: string) => (
    latest.current.workspace.conversations.find((item) => item.threadId === threadId)
  )

  const performSelectConversation = (threadId: string) => {
    if (threadId !== latest.current.workspace.currentThreadId) onConversationBoundary()
    setDraft('')
    setDraftConversation(null)
    setWorkspace((state) => ({ ...state, currentThreadId: threadId }))
    if (findConversation(threadId)?.isHydrated) void catchUpDetachedConversation(threadId)
  }

  const performNewConversation = () => {
    onConversationBoundary()
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
    if (!target || pinRequests.current.has(threadId)) return
    pinRequests.current.add(threadId)
    setPinPendingThreadIds((current) => new Set(current).add(threadId))
    const previousPinned = target.pinned
    const nextPinned = !target?.pinned
    setWorkspace((state) => updateConversation(
      state,
      threadId,
      (item) => ({ ...item, pinned: nextPinned }),
    ))
    // 同一会话只允许一个置顶 mutation 在途，响应才能安全提交或回滚其乐观值
    void patchConversation(threadId, { pinned: nextPinned }).then((summary) => {
      if (!isMounted.current) return
      setWorkspace((state) => updateConversation(
        state,
        threadId,
        (item) => item.pinned === nextPinned
          ? { ...item, pinned: summary.pinned }
          : item,
      ))
      onToast('success', nextPinned ? t('会话已置顶') : t('已取消置顶'))
    }).catch(() => {
      if (!isMounted.current) return
      setWorkspace((state) => updateConversation(
        state,
        threadId,
        (item) => item.pinned === nextPinned
          ? { ...item, pinned: previousPinned }
          : item,
      ))
      onToast('error', t('置顶状态更新失败，请重试'))
    }).finally(() => {
      pinRequests.current.delete(threadId)
      if (!isMounted.current) return
      setPinPendingThreadIds((current) => {
        const next = new Set(current)
        next.delete(threadId)
        return next
      })
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
        if (!title) throw new Error(t('会话名称不能为空'))
        if (title !== dialog.initialValue) {
          await patchConversation(dialog.threadId, { title })
          setWorkspace((state) => updateConversation(
            state,
            dialog.threadId,
            (item) => ({ ...item, title }),
          ))
          onToast('success', t('会话已重命名'))
        }
      } else if (dialog.kind === 'disable-plan') {
        abandonPlanInteraction(dialog.threadId)
        onToast('info', t('已关闭 Plan，下一条消息将使用 default 模式'))
      } else if (dialog.kind === 'delete') {
        if (dialog.isRunning) await cancelActiveRun()
        await deleteConversationApi(dialog.threadId)
        clearInteractionCardCollapsed(dialog.threadId)
        if (dialog.threadId === latest.current.workspace.currentThreadId) {
          onConversationBoundary()
        }
        setWorkspace((state) => removeConversation(state, dialog.threadId))
        onToast('success', t('会话已删除'))
      } else {
        if (hasActiveStream()) {
          const runningThreadId = getActiveThreadId()
            ?? latest.current.conversation.threadId
          detachThreadStream(
            runningThreadId,
            dialog.kind === 'detach-new'
              ? t('已新建会话，之前会话的实时输出连接已断开')
              : t('已切换到其他会话，当前会话的实时输出连接已断开'),
          )
        }
        if (dialog.kind === 'detach-new') performNewConversation()
        else performSelectConversation(dialog.threadId)
        onToast('info', t('已断开当前会话的实时输出'))
      }
      setDialog(null)
    } catch (error) {
      setDialogError(error instanceof Error && error.message
        ? error.message
        : t('操作失败，请稍后重试'))
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
    pinPendingThreadIds,
    renameConversation,
    deleteConversation,
    requestDisablePlan,
  }
}
