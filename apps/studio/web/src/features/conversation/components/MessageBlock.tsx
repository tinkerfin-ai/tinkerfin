import {
  Bot,
  Check,
  ChevronDown,
  CircleAlert,
  Copy,
  Pause,
  TriangleAlert,
  X,
} from 'lucide-react'
import { memo, useEffect, useRef, useState } from 'react'

import { IconButton } from '../../../components/ui'
import type { ConversationNotice as ConversationNoticeType, Message } from '../../../types'
import { ActivityDots } from './ActivityDots'
import { MarkdownContent } from './MarkdownContent'
import { ToolCallRow } from './ToolCallRow'
import { COPY_FEEDBACK_DURATION_MS } from './copyFeedback'
import { useI18n } from '../../../i18n'

type MessageStatus = NonNullable<Message['meta']>['status']

function PlaceholderField({ status }: { status?: MessageStatus }) {
  const { t } = useI18n()
  if (status === 'paused') {
    return <span className="tool-field-placeholder">{t('等待审批后执行')}</span>
  }
  return status === 'running'
    ? <span className="tool-skeleton" aria-label={t('工具字段加载中')} aria-busy="true" />
    : <span className="tool-field-placeholder">—</span>
}

function CodeField({ value, status }: { value?: string; status?: MessageStatus }) {
  return value
    ? <pre className="tool-code-field"><code>{value}</code></pre>
    : <PlaceholderField status={status} />
}

function RichField({ value, status, className = 'tool-rich-field' }: { value?: string; status?: MessageStatus; className?: string }) {
  return value
    ? <div className={className}><MarkdownContent content={value} variant="compact" /></div>
    : <PlaceholderField status={status} />
}

function ToolDetails({ message }: { message: Message }) {
  const { t } = useI18n()
  return (
    <div className="tool-detail-card">
      <div className="tool-detail-section tool-detail-section--params">
        <span className="tool-field-label">{t('参数')}</span>
        <CodeField value={message.meta?.params} status={message.meta?.status} />
      </div>
      <span className="tool-detail-divider" aria-hidden="true" />
      <div className="tool-detail-section tool-detail-section--result">
        <span className="tool-field-label">{t('结果')}</span>
        <RichField value={message.meta?.result} status={message.meta?.status} />
      </div>
    </div>
  )
}

function MessageActionRow({ content }: { content: string }) {
  const { t } = useI18n()
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const resetTimer = useRef<number | null>(null)

  useEffect(() => () => {
    if (resetTimer.current != null) window.clearTimeout(resetTimer.current)
  }, [])

  const copyMessage = async () => {
    if (resetTimer.current != null) window.clearTimeout(resetTimer.current)
    try {
      await navigator.clipboard.writeText(content)
      setCopyState('copied')
    } catch {
      setCopyState('failed')
    }
    resetTimer.current = window.setTimeout(() => setCopyState('idle'), COPY_FEEDBACK_DURATION_MS)
  }

  const label = copyState === 'copied'
    ? t('回答已复制')
    : copyState === 'failed'
      ? t('复制回答失败')
      : t('复制回答')

  return (
    <footer className="message-action-row" role="group" aria-label={t('回答操作')}>
      <IconButton
        label={label}
        tooltip={label}
        icon={copyState === 'copied'
          ? <Check size={16} />
          : copyState === 'failed'
            ? <TriangleAlert size={16} />
            : <Copy size={16} />}
        onClick={() => void copyMessage()}
      />
      <span className="message-action-status" aria-live="polite">
        {copyState === 'copied' ? t('已复制') : copyState === 'failed' ? t('复制失败，请重试') : ''}
      </span>
    </footer>
  )
}

function SubagentToolTraceRow({
  message,
  open,
  onOpenChange,
}: {
  message: Message
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  return (
    <ToolCallRow
      message={message}
      className="subagent-tool-row"
      open={open}
      onOpenChange={onOpenChange}
    >
      <ToolDetails message={message} />
    </ToolCallRow>
  )
}

function SubagentOutputNode({ message }: { message: Message }) {
  const { t } = useI18n()
  const status = message.meta?.status ?? 'completed'
  const result = message.meta?.result
  const label = status === 'running'
    ? t('执行中')
    : status === 'failed'
      ? t('执行失败')
      : status === 'cancelled'
        ? t('已取消')
      : status === 'paused'
        ? t('等待审批')
        : t('已完成')

  return (
    <li className={`subagent-trace-node subagent-output-node is-${status}`}>
      <span className="subagent-trace-junction" aria-hidden="true" />
      <span className="subagent-output-icon" aria-hidden="true">
        {status === 'completed'
          ? <Check size={12} strokeWidth={2.5} />
          : status === 'failed' || status === 'cancelled'
            ? <X size={12} strokeWidth={2.5} />
            : status === 'paused'
              ? <Pause size={11} strokeWidth={2.5} />
              : <span className="subagent-output-pulse" />}
      </span>
      <div className="subagent-output-copy">
        <strong>{label}</strong>
        {result
          ? <RichField value={result} status={status} className="subagent-trace-output" />
          : status === 'running'
            ? <ActivityDots label={t('正在运行')} />
            : null}
      </div>
    </li>
  )
}

function SubagentCard({ message, childTools }: { message: Message; childTools: Message[] }) {
  const { t } = useI18n()
  const [openToolIds, setOpenToolIds] = useState<Set<string>>(() => new Set())
  const status = message.meta?.status ?? 'completed'
  const input = message.meta?.input
  const agentName = message.meta?.agentName ?? 'subagent'
  const statusLabel = status === 'running'
    ? t('正在运行')
    : status === 'failed'
      ? t('执行失败')
      : status === 'cancelled'
        ? t('已取消')
      : status === 'paused'
        ? t('等待审批')
        : t('已完成')

  const setToolOpen = (toolId: string, open: boolean) => {
    setOpenToolIds((current) => {
      if (current.has(toolId) === open) return current
      const next = new Set(current)
      if (open) next.add(toolId)
      else next.delete(toolId)
      return next
    })
  }

  return (
    <details id={message.id} className={`subagent-card ${status}`}>
      <summary className="subagent-card-head">
        <span className="tool-row-leading" aria-hidden="true">
          <span className="tool-row-icon">
            {status === 'failed' || status === 'paused' || status === 'cancelled'
              ? <span className={`tool-row-state-dot is-${status}`} />
              : <Bot size={14} strokeWidth={2} />}
          </span>
          <ChevronDown className="tool-row-chevron" size={14} strokeWidth={2} />
        </span>
        <span className="tool-row-title">Task</span>
        <span className="tool-row-separator" aria-hidden="true" />
        <span className="tool-row-summary">SubAgent</span>
        <span className="subagent-card-meta">
          <span className="subagent-tool-count">{t('{count} 个工具', { count: childTools.length })}</span>
        </span>
        <span className="subagent-visually-hidden">{agentName}，{statusLabel}</span>
      </summary>
      <div className="subagent-card-body">
        {input && (
          <div className="subagent-task-line">
            <span>{agentName}</span>
            <p>{input}</p>
          </div>
        )}
        <ol
          className={`subagent-trace-list${childTools.length === 0 ? ' is-tool-empty' : ''}`}
          aria-label={`${agentName} ${t('工具轨迹')}`}
        >
          {childTools.map((tool) => (
            <li key={tool.id} className="subagent-trace-node subagent-tool-node">
              <span className="subagent-trace-junction" aria-hidden="true" />
              <SubagentToolTraceRow
                message={tool}
                open={openToolIds.has(tool.id)}
                onOpenChange={(open) => setToolOpen(tool.id, open)}
              />
            </li>
          ))}
          <SubagentOutputNode message={message} />
        </ol>
      </div>
    </details>
  )
}

function MessageBlockView({
  message,
  childTools = [],
  showActions = true,
}: {
  message: Message
  childTools?: Message[]
  showActions?: boolean
}) {
  const { t } = useI18n()
  if (message.role === 'user') {
    return <article id={message.id} className="message user-message"><MarkdownContent content={message.content} className="message-markdown" /></article>
  }
  if (message.role === 'process') {
    return null
  }
  if (message.role === 'subagent') {
    return <SubagentCard message={message} childTools={childTools} />
  }
  if (message.role === 'tool') {
    return <ToolCallCard message={message} />
  }
  if (message.role === 'error') {
    return <article id={message.id} className="error-message"><CircleAlert size={17} /><div><strong>{t('任务遇到问题')}</strong><MarkdownContent content={message.content} className="error-markdown" variant="compact" /></div></article>
  }
  return (
    <article id={message.id} className="message assistant-message">
      {message.content
        ? <>
            <MarkdownContent content={message.content} className="message-markdown" />
            {showActions && message.meta?.status !== 'running' && <MessageActionRow content={message.content} />}
          </>
        : message.meta?.status === 'running'
          ? null
          : <p className="streaming-indicator"><ActivityDots label={t('正在回复')} /></p>}
    </article>
  )
}

const sameMessageReferences = (left: Message[], right: Message[]) =>
  left.length === right.length && left.every((message, index) => message === right[index])

export const MessageBlock = memo(
  MessageBlockView,
  (previous, next) => previous.message === next.message
    && sameMessageReferences(previous.childTools ?? [], next.childTools ?? [])
    && (previous.showActions ?? true) === (next.showActions ?? true),
)

export function ConversationNotice({ notice }: { notice: ConversationNoticeType }) {
  const { t } = useI18n()
  return (
    <article className={`error-message conversation-notice is-${notice.kind}`} role={notice.kind === 'error' ? 'alert' : 'status'}>
      <CircleAlert size={17} />
      <div>
        <strong>{notice.kind === 'error' ? t('任务遇到问题') : t('连接状态')}</strong>
        <MarkdownContent content={notice.content} className="error-markdown" />
      </div>
    </article>
  )
}

export function ToolCallCard({ message }: { message: Message }) {
  return (
    <ToolCallRow message={message} className="tool-card">
      <ToolDetails message={message} />
    </ToolCallRow>
  )
}

function ToolCallBatchView({ messages }: { messages: Message[] }) {
  const { t } = useI18n()
  if (messages.length === 1) return <ToolCallCard message={messages[0]} />
  return (
    <section className="tool-batch" aria-label={t('工具调用批次')}>
      {messages.map((message) => <ToolCallCard key={message.id} message={message} />)}
    </section>
  )
}

export const ToolCallBatch = memo(
  ToolCallBatchView,
  (previous, next) => sameMessageReferences(previous.messages, next.messages),
)
