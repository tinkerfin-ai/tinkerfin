import {
  Check,
  Circle,
  CircleAlert,
  CirclePause,
  ClipboardList,
  LoaderCircle,
  RadioTower,
  X,
} from 'lucide-react'
import { useEffect, useRef } from 'react'

import { IconButton, OverlayScrollbar } from '../../../components/ui'
import type {
  Conversation,
  ConversationRunStatus,
  TodoStatus,
} from '../../../types'
import { MarkdownContent } from '../../conversation/components/MarkdownContent'
import { useI18n } from '../../../i18n'

type TodoViewStatus = TodoStatus | 'paused' | 'unfinished' | 'background'

const todoViewStatus = (
  status: TodoStatus,
  runStatus: ConversationRunStatus,
): TodoViewStatus => {
  if (status !== 'running') return status
  if (runStatus === 'streaming') return 'running'
  if (runStatus === 'waiting_approval') return 'paused'
  if (runStatus === 'detached') return 'background'
  if (runStatus === 'error') return 'failed'
  return 'unfinished'
}

const todoStatusIcon = (status: TodoViewStatus) => {
  if (status === 'completed') return <Check aria-hidden="true" size={14} />
  if (status === 'running') return <LoaderCircle aria-hidden="true" className="spin" size={14} />
  if (status === 'paused') return <CirclePause aria-hidden="true" size={14} />
  if (status === 'background') return <RadioTower aria-hidden="true" size={14} />
  if (status === 'unfinished') return <CircleAlert aria-hidden="true" size={14} />
  if (status === 'failed' || status === 'cancelled') return <X aria-hidden="true" size={14} />
  return <Circle aria-hidden="true" size={13} />
}

export function TaskDrawer({
  conversation,
  open = true,
  onClose,
  focusOnOpen = false,
}: {
  conversation: Conversation
  open?: boolean
  onClose?: () => void
  focusOnOpen?: boolean
}) {
  const { t } = useI18n()
  const statusLabel: Record<TodoViewStatus, string> = {
    pending: t('待执行'),
    running: t('执行中'),
    completed: t('已完成'),
    failed: t('失败'),
    cancelled: t('已取消'),
    paused: t('等待审批'),
    unfinished: t('未完成'),
    background: t('后台执行中'),
  }
  const closeButtonRef = useRef<HTMLButtonElement>(null)
  const todoScrollRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open || !onClose) return
    const handleEscape = (event: globalThis.KeyboardEvent) => {
      if (event.defaultPrevented || event.key !== 'Escape') return
      event.preventDefault()
      onClose()
    }
    document.addEventListener('keydown', handleEscape)
    return () => document.removeEventListener('keydown', handleEscape)
  }, [onClose, open])

  useEffect(() => {
    if (!open || !focusOnOpen) return
    const frame = window.requestAnimationFrame(() => closeButtonRef.current?.focus())
    return () => window.cancelAnimationFrame(frame)
  }, [focusOnOpen, open])

  // Todo 是持久化 Agent state；run 生命周期只决定其当前展示，不改写原始协议状态
  const todoRows = conversation.todos.map((todo) => ({
    ...todo,
    viewStatus: todoViewStatus(todo.status, conversation.runStatus),
  }))
  const countStatus = (status: TodoViewStatus) => (
    todoRows.filter((todo) => todo.viewStatus === status).length
  )
  const completedCount = countStatus('completed')
  const runningCount = countStatus('running')
  const pausedCount = countStatus('paused')
  const backgroundCount = countStatus('background')
  const partialCount = runningCount + pausedCount + backgroundCount
  const progress = conversation.todos.length
    ? ((completedCount + partialCount * .5) / conversation.todos.length) * 100
    : 0

  return (
    <aside
      data-workspace-layout-target="task-drawer"
      id="task-drawer"
      className={`task-drawer${open ? ' is-open' : ''}`}
      aria-label={t('任务抽屉')}
      aria-hidden={!open || undefined}
      inert={!open || undefined}
    >
      <header className="task-drawer-head">
        <h2>{t('任务详情')}</h2>
        {onClose && <IconButton ref={closeButtonRef} label={t('关闭任务详情')} icon={<X size={18} />} onClick={onClose} />}
      </header>
      <section className="drawer-panel todo-panel">
        <header className="panel-head">
          <div className="panel-title"><ClipboardList size={20} /><h3>{t('待办清单')}</h3></div>
        </header>
        <div
          ref={todoScrollRef}
          className="panel-scroll todo-panel-scroll ui-scrollbar"
          role="region"
          aria-label={t('待办清单')}
          tabIndex={0}
        >
          {conversation.todos.length ? (
            <div className="todo-list">
              <div className="progress-card">
                <div className="progress-card-head"><span>{t('任务进度')}</span><strong>{completedCount}/{conversation.todos.length}</strong></div>
                <div className="progress-track"><span style={{ width: `${progress}%` }} /></div>
              </div>
              {todoRows.map((todo, index) => (
                // 当前协议没有 Todo 到消息的稳定关联，状态项不得伪装成可点击跳转
                <div key={todo.id} className={`todo-item is-${todo.viewStatus}`}>
                  <span className="todo-state">{todoStatusIcon(todo.viewStatus)}</span>
                  <span className="todo-copy">
                    <span
                      className="todo-meta"
                      aria-label={t('步骤 {number} · {status}', { number: index + 1, status: statusLabel[todo.viewStatus] })}
                    >
                      <small>{t('步骤 {number}', { number: index + 1 })}</small>
                      <span className={`todo-status-label is-${todo.viewStatus}`}>{statusLabel[todo.viewStatus]}</span>
                    </span>
                    <strong>{todo.content}</strong>
                    {todo.result && <div className="todo-result"><MarkdownContent content={todo.result} variant="compact" /></div>}
                  </span>
                </div>
              ))}
            </div>
          ) : (
            <div className="panel-empty"><ClipboardList size={20} /><h3>{t('暂无待办')}</h3><p>{t('Agent 生成的执行任务会显示在这里')}</p></div>
          )}
        </div>
        <OverlayScrollbar viewportRef={todoScrollRef} />
      </section>
    </aside>
  )
}
