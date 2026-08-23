import {
  Bot,
  CheckCircle2,
  Check,
  ChevronDown,
  CircleAlert,
  Copy,
  FileText,
  TriangleAlert,
  Zap,
} from 'lucide-react'
import { memo, useEffect, useRef, useState } from 'react'

import { IconButton } from '../../../components/ui'
import type { ConversationNotice as ConversationNoticeType, Message } from '../../../types'
import { normalizeEscapedText } from '../../../lib/text'
import { ActivityDots } from './ActivityDots'
import { MarkdownContent } from './MarkdownContent'
import { COPY_FEEDBACK_DURATION_MS } from './copyFeedback'

type MessageStatus = NonNullable<Message['meta']>['status']

const loadingField = <span className="tool-skeleton" aria-label="工具字段加载中" aria-busy="true" />

function PlaceholderField({ status }: { status?: MessageStatus }) {
  if (status === 'paused') {
    return <span className="tool-field-placeholder">等待审批后执行</span>
  }
  return status === 'running' ? loadingField : <span className="tool-field-placeholder">—</span>
}

function CodeField({ value, status }: { value?: string; status?: MessageStatus }) {
  return value
    ? <pre className="tool-code-field"><code>{normalizeEscapedText(value)}</code></pre>
    : <PlaceholderField status={status} />
}

function RichField({ value, status, className = 'tool-rich-field' }: { value?: string; status?: MessageStatus; className?: string }) {
  return value
    ? <div className={className}><MarkdownContent content={normalizeEscapedText(value)} variant="compact" /></div>
    : <PlaceholderField status={status} />
}

function MessageActionRow({ content }: { content: string }) {
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
    ? '回答已复制'
    : copyState === 'failed'
      ? '复制回答失败'
      : '复制回答'

  return (
    <footer className="message-action-row" role="group" aria-label="回答操作">
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
        {copyState === 'copied' ? '已复制' : copyState === 'failed' ? '复制失败，请重试' : ''}
      </span>
    </footer>
  )
}

const statusLabel = (status?: MessageStatus) => {
  if (status === 'failed') return '执行失败'
  if (status === 'paused') return '等待审批'
  if (status === 'running') return <ActivityDots label="正在运行" />
  return <CheckCircle2 className="completed-status-icon" size={14} aria-label="已完成" />
}

const formatClock = (iso: string) => new Date(iso).toLocaleTimeString('zh-CN', {
  hour12: false,
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
})

const formatDuration = (durationMs: number | undefined) => {
  if (durationMs == null) return '—'
  if (durationMs < 1000) return `${Math.round(durationMs)}ms`
  return `${(durationMs / 1000).toFixed(durationMs < 10000 ? 2 : 1)}s`
}

function SubagentToolTraceRow({
  message,
  index,
  open,
  onOpenChange,
}: {
  message: Message
  index: number
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  return (
    <div className="subagent-trace-item">
      <span className="subagent-trace-marker" aria-hidden="true">{index + 1}</span>
      <details
        className={`subagent-tool-row ${message.meta?.status ?? ''}`}
        open={open}
        onToggle={(event) => onOpenChange(event.currentTarget.open)}
      >
        <summary>
          <span className="subagent-tool-title">
            <span className="subagent-tool-icon"><FileText size={14} /></span>
            <strong>{message.meta?.toolName ?? 'tool'}</strong>
          </span>
          <span className="subagent-tool-meta">
            <time dateTime={message.createdAt}>{formatClock(message.createdAt)}</time>
            {message.meta?.status !== 'running' && <span>{formatDuration(message.meta?.durationMs)}</span>}
            <span className="tool-status">{statusLabel(message.meta?.status)}</span>
            <ChevronDown size={14} />
          </span>
        </summary>
        <div className="tool-grid subagent-tool-detail">
          <span className="tool-field-label">参数</span><CodeField value={message.meta?.params} status={message.meta?.status} />
          <span className="tool-field-label">结果</span><RichField value={message.meta?.result} status={message.meta?.status} />
        </div>
      </details>
    </div>
  )
}

function SubagentCard({ message, childTools }: { message: Message; childTools: Message[] }) {
  const [openToolIds, setOpenToolIds] = useState<Set<string>>(() => new Set())
  const allToolsExpanded = childTools.length > 0
    && childTools.every((tool) => openToolIds.has(tool.id))

  const setToolOpen = (toolId: string, open: boolean) => {
    setOpenToolIds((current) => {
      if (current.has(toolId) === open) return current
      const next = new Set(current)
      if (open) next.add(toolId)
      else next.delete(toolId)
      return next
    })
  }

  const toggleAllTools = () => {
    setOpenToolIds(allToolsExpanded
      ? new Set()
      : new Set(childTools.map((tool) => tool.id)))
  }

  return (
    <details id={message.id} className={`subagent-card ${message.meta?.status ?? ''}`}>
      <summary className="subagent-card-head">
        <span className="subagent-identity">
          <span className="tool-kind-badge">子智能体</span>
          <span className="subagent-avatar"><Bot size={15} /></span>
          <strong>{message.meta?.agentName ?? 'subagent'}</strong>
        </span>
        <span className="subagent-card-meta">
          <span className="subagent-tool-count">{childTools.length} 个工具</span>
          <span className="subagent-status">{statusLabel(message.meta?.status)}</span>
          <ChevronDown className="subagent-card-chevron" size={16} />
        </span>
      </summary>
      <div className="subagent-card-body">
        <section className="subagent-section">
          <h4><span aria-hidden="true" />输入</h4>
          <RichField value={message.meta?.input} status={message.meta?.status} className="subagent-rich-field" />
        </section>
        <section className="subagent-section subagent-trace-section">
          <div className="subagent-trace-head">
            <h4><span aria-hidden="true" />工具轨迹 <b>{childTools.length}</b></h4>
            {childTools.length > 0 && (
              <button type="button" aria-expanded={allToolsExpanded} onClick={toggleAllTools}>
                {allToolsExpanded ? '收起全部详情' : '展开全部详情'}
              </button>
            )}
          </div>
          {childTools.length > 0
            ? <div className="subagent-trace-list">{childTools.map((tool, index) => (
                <SubagentToolTraceRow
                  key={tool.id}
                  message={tool}
                  index={index}
                  open={openToolIds.has(tool.id)}
                  onOpenChange={(open) => setToolOpen(tool.id, open)}
                />
              ))}</div>
            : <p className="subagent-empty-trace">暂未调用工具</p>}
        </section>
        <section className="subagent-section">
          <h4><span aria-hidden="true" />输出摘要</h4>
          <RichField value={message.meta?.result} status={message.meta?.status} className="subagent-rich-field subagent-output" />
        </section>
      </div>
    </details>
  )
}

function MessageBlockView({
  message,
  childTools = [],
}: {
  message: Message
  childTools?: Message[]
}) {
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
    return <article id={message.id} className="error-message"><CircleAlert size={17} /><div><strong>任务遇到问题</strong><MarkdownContent content={message.content} className="error-markdown" variant="compact" /></div></article>
  }
  return (
    <article id={message.id} className="message assistant-message">
      {message.content
        ? <>
            <MarkdownContent content={message.content} className="message-markdown" />
            {message.meta?.status !== 'running' && <MessageActionRow content={message.content} />}
          </>
        : message.meta?.status === 'running'
          ? null
          : <p className="streaming-indicator"><ActivityDots label="正在回复" /></p>}
    </article>
  )
}

const sameMessageReferences = (left: Message[], right: Message[]) =>
  left.length === right.length && left.every((message, index) => message === right[index])

export const MessageBlock = memo(
  MessageBlockView,
  (previous, next) => previous.message === next.message
    && sameMessageReferences(previous.childTools ?? [], next.childTools ?? []),
)

export function ConversationNotice({ notice }: { notice: ConversationNoticeType }) {
  return (
    <article className={`error-message conversation-notice is-${notice.kind}`} role={notice.kind === 'error' ? 'alert' : 'status'}>
      <CircleAlert size={17} />
      <div>
        <strong>{notice.kind === 'error' ? '任务遇到问题' : '连接状态'}</strong>
        <MarkdownContent content={notice.content} className="error-markdown" />
      </div>
    </article>
  )
}

export function ToolCallCard({ message }: { message: Message }) {
  return (
    <details id={message.id} className={`tool-card ${message.meta?.status ?? ''}`}>
      <summary>
        <span><span className="tool-kind-badge">工具</span><Zap size={16} />{message.meta?.toolName}</span>
        <span className="tool-card-meta"><span className="tool-status">{statusLabel(message.meta?.status)}</span><ChevronDown size={15} /></span>
      </summary>
      <div className="tool-grid">
        <span className="tool-field-label">参数</span><CodeField value={message.meta?.params} status={message.meta?.status} />
        <span className="tool-field-label">结果</span><RichField value={message.meta?.result} status={message.meta?.status} />
      </div>
    </details>
  )
}

function ToolCallBatchView({ messages }: { messages: Message[] }) {
  if (messages.length === 1) return <ToolCallCard message={messages[0]} />
  return (
    <section className="tool-batch" aria-label="工具调用批次">
      {messages.map((message) => <ToolCallCard key={message.id} message={message} />)}
    </section>
  )
}

export const ToolCallBatch = memo(
  ToolCallBatchView,
  (previous, next) => sameMessageReferences(previous.messages, next.messages),
)
