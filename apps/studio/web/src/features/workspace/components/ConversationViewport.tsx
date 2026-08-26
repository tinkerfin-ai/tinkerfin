import { ArrowDown } from 'lucide-react'
import { useMemo, type RefObject } from 'react'

import { Button, ErrorBoundary, OverlayScrollbar } from '../../../components/ui'
import type {
  Conversation,
  Message,
} from '../../../types'
import { ActivityDots } from '../../conversation/components/ActivityDots'
import { ApprovalStatusRow } from '../../conversation/components/ApprovalCard'
import { ConversationNotice, MessageBlock, ToolCallBatch } from '../../conversation/components/MessageBlock'
import { PlanQuestionStatusRow } from '../../conversation/components/PlanQuestionComposer'
import { PlanReviewStatusRow } from '../../conversation/components/PlanReviewCard'
import { EmptyConversation } from './EmptyConversation'
import { WorkspaceStatus } from './WorkspaceStatus'
import { useI18n } from '../../../i18n'

export type ConversationDisplayEntry =
  | { type: 'message'; message: Message }
  | { type: 'tools'; messages: Message[] }

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
  showScrollToBottom,
  fadeScrollToBottom,
  onScroll,
  onUserScrollIntent,
  onRetryHistory,
  onRetryHydration,
  onLoadEarlierMessages,
  onScrollToBottom,
  onScrollToBottomPointerEnter,
  onScrollToBottomPointerLeave,
  onScrollToBottomFocus,
  onScrollToBottomBlur,
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
  showScrollToBottom: boolean
  fadeScrollToBottom: boolean
  onScroll: (pane: HTMLElement) => void
  onUserScrollIntent: () => void
  onRetryHistory: () => void
  onRetryHydration: () => void
  onLoadEarlierMessages: (trigger: HTMLButtonElement) => void
  onScrollToBottom: () => void
  onScrollToBottomPointerEnter: () => void
  onScrollToBottomPointerLeave: () => void
  onScrollToBottomFocus: () => void
  onScrollToBottomBlur: () => void
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
        <WorkspaceStatus kind="error" title={t('对话区域无法显示')} description={t('消息渲染遇到问题，其他工作区功能仍可继续使用')} onRetry={reset} />
      )}
    >
      <div className="conversation-region">
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
          <WorkspaceStatus kind="loading" title={t('正在加载历史会话')} description={t('正在恢复最近的对话和工作区状态')} />
        ) : isInitialHistoryUnavailable ? (
          <WorkspaceStatus kind="error" title={t('历史会话加载失败')} description={t('无法读取历史记录，请重试；现有数据不会被修改')} onRetry={onRetryHistory} />
        ) : isHydrating ? (
          <WorkspaceStatus kind="loading" title={t('正在加载会话')} description={t('正在恢复消息、任务和运行状态')} />
        ) : isHydrationFailed ? (
          <WorkspaceStatus kind="error" title={t('会话加载失败')} description={t('该会话尚未完整恢复，重试前不会发送新消息')} onRetry={onRetryHydration} />
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
        <OverlayScrollbar viewportRef={paneRef} visibility="persistent" />
        <div className="conversation-scroll-action" aria-hidden={!showScrollToBottom || undefined}>
          <button
            type="button"
            className={`scroll-to-bottom${showScrollToBottom ? ' is-visible' : ''}${fadeScrollToBottom ? ' is-fading' : ''}`}
            aria-label={t('回到底部')}
            aria-hidden={!showScrollToBottom || undefined}
            tabIndex={showScrollToBottom ? 0 : -1}
            title={t('回到底部')}
            onPointerEnter={onScrollToBottomPointerEnter}
            onPointerLeave={onScrollToBottomPointerLeave}
            onFocus={onScrollToBottomFocus}
            onBlur={onScrollToBottomBlur}
            onClick={onScrollToBottom}
          >
            <ArrowDown size={16} aria-hidden="true" />
            <span>{t('回到底部')}</span>
          </button>
        </div>
      </div>
    </ErrorBoundary>
  )
}
