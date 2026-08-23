import { ArrowDown } from 'lucide-react'
import type { RefObject } from 'react'

import { ErrorBoundary } from '../../../components/ui'
import type {
  ApprovalState,
  Conversation,
  Message,
  PlanInteraction,
  PlanQuestionState,
  PlanReviewState,
} from '../../../types'
import { ActivityDots } from '../../conversation/components/ActivityDots'
import { ApprovalCard } from '../../conversation/components/ApprovalCard'
import { ConversationNotice, MessageBlock, ToolCallBatch } from '../../conversation/components/MessageBlock'
import { PlanQuestionCard } from '../../conversation/components/PlanQuestionCard'
import { PlanReviewCard } from '../../conversation/components/PlanReviewCard'
import { EmptyConversation } from './EmptyConversation'
import { WorkspaceStatus } from './WorkspaceStatus'

export type ConversationDisplayEntry =
  | { type: 'message'; message: Message }
  | { type: 'tools'; messages: Message[] }

export function ConversationViewport({
  conversation,
  entries,
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
  onScroll,
  onUserScrollIntent,
  onRetryHistory,
  onRetryHydration,
  onChangeApproval,
  onSubmitApproval,
  onChangePlan,
  onSubmitPlan,
  onScrollToBottom,
}: {
  conversation: Conversation
  entries: ConversationDisplayEntry[]
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
  onScroll: (pane: HTMLElement) => void
  onUserScrollIntent: () => void
  onRetryHistory: () => void
  onRetryHydration: () => void
  onChangeApproval: (updater: (approval: ApprovalState) => ApprovalState) => void
  onSubmitApproval: (expectedInterruptIds: readonly string[]) => void
  onChangePlan: (updater: (interaction: PlanInteraction) => PlanInteraction) => void
  onSubmitPlan: () => void
  onScrollToBottom: () => void
}) {
  const isEmpty = conversation.messages.length === 0 && !conversation.notice

  return (
    <ErrorBoundary
      resetKey={conversation.threadId || 'draft'}
      fallback={({ reset }) => (
        <WorkspaceStatus kind="error" title="对话区域无法显示" description="消息渲染遇到问题，其他工作区功能仍可继续使用。" onRetry={reset} />
      )}
    >
      <div className="conversation-region">
        <section
          ref={paneRef}
          className={`conversation-pane${isEmpty ? ' is-empty' : ''}`}
          aria-label="对话内容"
          onScroll={(event) => onScroll(event.currentTarget)}
          onWheel={onUserScrollIntent}
          onTouchStart={onUserScrollIntent}
        >
        {!isHistoryBootstrapped || historyStatus === 'loading' ? (
          <WorkspaceStatus kind="loading" title="正在加载历史会话" description="正在恢复最近的对话和工作区状态。" />
        ) : isInitialHistoryUnavailable ? (
          <WorkspaceStatus kind="error" title="历史会话加载失败" description="无法读取历史记录，请重试；现有数据不会被修改。" onRetry={onRetryHistory} />
        ) : isHydrating ? (
          <WorkspaceStatus kind="loading" title="正在加载会话" description="正在恢复消息、任务和运行状态。" />
        ) : isHydrationFailed ? (
          <WorkspaceStatus kind="error" title="会话加载失败" description="该会话尚未完整恢复，重试前不会发送新消息。" onRetry={onRetryHydration} />
        ) : isEmpty ? (
          <EmptyConversation />
        ) : (
          <div className="message-list">
            {entries.map((entry) => entry.type === 'tools'
              ? <ToolCallBatch key={`batch-${entry.messages[0].id}`} messages={entry.messages} />
              : <MessageBlock
                  key={entry.message.id}
                  message={entry.message}
                  childTools={entry.message.meta?.subRunId
                    ? childToolsByRunId.get(entry.message.meta.subRunId) ?? []
                    : []}
                />)}
            {conversation.notice && <ConversationNotice notice={conversation.notice} />}
            {isRunning && <p className="message-stream-tail stream-pending-tail"><ActivityDots label="任务仍在继续" /></p>}
            {conversation.approval && !conversation.approval.submitted && (
              <ApprovalCard
                conversation={conversation}
                onChange={onChangeApproval}
                onSubmit={onSubmitApproval}
              />
            )}
            {conversation.planInteraction?.kind === 'questions' && !conversation.planInteraction.submitted && (
              <PlanQuestionCard
                interaction={conversation.planInteraction}
                onChange={(updater) => onChangePlan((current) => current.kind === 'questions'
                  ? updater(current as PlanQuestionState)
                  : current)}
                onSubmit={onSubmitPlan}
              />
            )}
            {conversation.planInteraction?.kind === 'review' && !conversation.planInteraction.submitted && (
              <PlanReviewCard
                interaction={conversation.planInteraction}
                onChange={(updater) => onChangePlan((current) => current.kind === 'review'
                  ? updater(current as PlanReviewState)
                  : current)}
                onSubmit={onSubmitPlan}
              />
            )}
            <div ref={messageEndRef} />
          </div>
        )}
        </section>
        <div className="conversation-scroll-action" aria-hidden={!showScrollToBottom || undefined}>
          <button
            type="button"
            className={`scroll-to-bottom${showScrollToBottom ? ' is-visible' : ''}`}
            aria-label="回到底部"
            aria-hidden={!showScrollToBottom || undefined}
            tabIndex={showScrollToBottom ? 0 : -1}
            title="回到底部"
            onClick={onScrollToBottom}
          >
            <ArrowDown size={18} aria-hidden="true" />
          </button>
        </div>
      </div>
    </ErrorBoundary>
  )
}
