import { useMemo, type RefObject } from 'react'

import { Button, ErrorBoundary, FeedbackState, OverlayScrollbar } from '../../../components/ui'
import type {
  Conversation,
  Message,
} from '../../../types'
import { ActivityDots } from '../../conversation/components/ActivityDots'
import { ApprovalStatusRow } from '../../conversation/components/ApprovalCard'
import { ConversationNotice, MessageBlock, ToolCallBatch } from '../../conversation/components/MessageBlock'
import { PlanQuestionStatusRow } from '../../conversation/components/PlanQuestionComposer'
import { PlanReviewStatusRow } from '../../conversation/components/PlanReviewCard'
import { TodoGroupRow } from '../../conversation/todoTrace/components/TodoGroupRow'
import type { ConversationDisplayEntry } from '../../conversation/todoTrace/displayEntries'
import { EmptyConversation } from './EmptyConversation'
import { useI18n } from '../../../i18n'

function collectCopyableAssistantIds(entries: ConversationDisplayEntry[], currentTurnRunning: boolean) {
  const copyableIds = new Set<string>()
  let candidate: Message | null = null
  const finishTurn = () => {
    if (candidate) copyableIds.add(candidate.id)
  }

  for (const entry of entries) {
    if (entry.type === 'tools') {
      candidate = null
      continue
    }
    if (entry.type === 'todo-group') {
      candidate = null
      continue
    }
    if (entry.message.role === 'user') {
      finishTurn()
      candidate = null
      continue
    }
    if (entry.message.role === 'process') continue
    if (entry.message.role === 'assistant') {
      candidate = entry.message.content && entry.message.meta?.status !== 'running'
        ? entry.message
        : null
      continue
    }
    candidate = null
  }

  // 当前轮结束前不暴露阶段性回答操作，历史轮次仍保留各自的最终回答操作
  if (!currentTurnRunning) finishTurn()
  return copyableIds
}

export function ConversationViewport({
  conversation,
  entries,
  hasEarlierMessages,
  childToolsByRunId,
  paneRef,
  messageEndRef,
  historyStatus,
  isHistoryBootstrapped,
  isInitialHistoryUnavailable,
  isHydrating,
  isHydrationFailed,
  isRunning,
  backgroundInert = false,
  onScroll,
  onUserScrollIntent,
  onRetryHistory,
  onRetryHydration,
  onLoadEarlierMessages,
}: {
  conversation: Conversation
  entries: ConversationDisplayEntry[]
  hasEarlierMessages: boolean
  childToolsByRunId: Map<string, Message[]>
  paneRef: RefObject<HTMLElement | null>
  messageEndRef: RefObject<HTMLDivElement | null>
  historyStatus: 'loading' | 'ready' | 'error'
  isHistoryBootstrapped: boolean
  isInitialHistoryUnavailable: boolean
  isHydrating: boolean
  isHydrationFailed: boolean
  isRunning: boolean
  backgroundInert?: boolean
  onScroll: (pane: HTMLElement) => void
  onUserScrollIntent: () => void
  onRetryHistory: () => void
  onRetryHydration: () => void
  onLoadEarlierMessages: (trigger: HTMLButtonElement) => void
}) {
  const { t } = useI18n()
  const isEmpty = conversation.messages.length === 0 && !conversation.notice
  const copyableAssistantIds = useMemo(
    () => collectCopyableAssistantIds(entries, isRunning),
    [entries, isRunning],
  )

  return (
    <ErrorBoundary
      resetKey={conversation.threadId || 'draft'}
      fallback={({ reset }) => (
        <FeedbackState kind="error" title={t('对话区域无法显示')} onRetry={reset} />
      )}
    >
      <div
        id="conversation-panel"
        className="conversation-region"
        role="tabpanel"
        aria-label={t('对话')}
        aria-hidden={backgroundInert || undefined}
        inert={backgroundInert || undefined}
      >
        {/* 命名 section 是主对话滚动区，必须可由键盘直接进入 */}
        {/* eslint-disable jsx-a11y/no-noninteractive-tabindex */}
        <section
          ref={paneRef}
          className={`conversation-pane ui-scrollbar${isEmpty ? ' is-empty' : ''}`}
          aria-label={t('对话内容')}
          tabIndex={0}
          onScroll={(event) => onScroll(event.currentTarget)}
          onWheel={onUserScrollIntent}
          onTouchStart={onUserScrollIntent}
        >
        {!isHistoryBootstrapped || historyStatus === 'loading' ? (
          <FeedbackState kind="loading" title={t('正在加载历史会话')} />
        ) : isInitialHistoryUnavailable ? (
          <FeedbackState kind="error" title={t('历史会话加载失败')} onRetry={onRetryHistory} />
        ) : isHydrating ? (
          <FeedbackState kind="loading" title={t('正在加载会话')} />
        ) : isHydrationFailed ? (
          <FeedbackState kind="error" title={t('会话加载失败')} onRetry={onRetryHydration} />
        ) : isEmpty ? (
          <EmptyConversation />
        ) : (
          <div className="message-list">
            {hasEarlierMessages && (
              <div className="message-history-loader">
                <Button
                  variant="text"
                  onClick={(event) => onLoadEarlierMessages(event.currentTarget)}
                >
                  {t('加载更早消息')}
                </Button>
              </div>
            )}
            {entries.map((entry) => entry.type === 'tools'
              ? <ToolCallBatch key={`batch-${entry.messages[0].id}`} messages={entry.messages} />
              : entry.type === 'todo-group'
                ? <TodoGroupRow key={entry.group.id} group={entry.group} message={entry.message} />
                : <MessageBlock
                  key={entry.message.id}
                  message={entry.message}
                  showActions={copyableAssistantIds.has(entry.message.id)}
                  childTools={entry.message.meta?.subRunId
                    ? childToolsByRunId.get(entry.message.meta.subRunId) ?? []
                    : []}
                />)}
            {conversation.approval && !conversation.approval.submitted && <ApprovalStatusRow />}
            {conversation.planInteraction?.kind === 'questions' && (
              <PlanQuestionStatusRow interaction={conversation.planInteraction} />
            )}
            {conversation.planInteraction?.kind === 'review' && (
              <PlanReviewStatusRow interaction={conversation.planInteraction} />
            )}
            {conversation.notice && <ConversationNotice notice={conversation.notice} />}
            {isRunning && <p className="message-stream-tail stream-pending-tail"><ActivityDots label={t('任务仍在继续')} /></p>}
            <div ref={messageEndRef} />
          </div>
        )}
        </section>
        {/* eslint-enable jsx-a11y/no-noninteractive-tabindex */}
        <OverlayScrollbar
          viewportRef={paneRef}
          visibility="persistent"
          onUserScrollIntent={onUserScrollIntent}
        />
      </div>
    </ErrorBoundary>
  )
}
