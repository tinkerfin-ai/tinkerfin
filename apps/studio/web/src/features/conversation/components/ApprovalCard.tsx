import { ChevronDown, ChevronUp } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'

import { Button, IconButton, OverlayScrollbar } from '../../../components/ui'
import type {
  ApprovalDecision,
  ApprovalState,
  Conversation,
  Message,
} from '../../../types'
import { useI18n } from '../../../i18n'
import {
  readApprovalCollapsed,
  writeApprovalCollapsed,
} from '../planQuestionCollapse'
import { MarkdownContent } from './MarkdownContent'
import { ToolCallCard } from './MessageBlock'
import { ActivityDots } from './ActivityDots'

export interface ApprovalSubmissionDecision {
  interruptId: string
  decision: ApprovalDecision
  rejectionReason?: string
}

export function ApprovalStatusRow() {
  const { t } = useI18n()
  return (
    <div className="approval-wait-state">
      <ActivityDots label={t('等待处理')} />
    </div>
  )
}

const descriptionParts = (description: string, fallback: string) => {
  const normalized = description.trim()
  const [title, ...rest] = normalized.split(/\n\s*\n/).filter(Boolean)
  return {
    title: title || fallback,
    detail: rest.join('\n\n'),
  }
}

const matchesApprovalGroup = (
  approval: ApprovalState,
  interruptIds: readonly string[],
) => approval.items.length === interruptIds.length
  && approval.items.every(
    (item, index) => item.interruptId === interruptIds[index],
  )

const decideItem = (
  approval: ApprovalState,
  decision: ApprovalSubmissionDecision,
) => {
  const activeIndex = approval.items.findIndex(
    (item) => item.interruptId === decision.interruptId,
  )
  if (activeIndex < 0) return approval
  const items = approval.items.map((item, index) => index === activeIndex
    ? {
        ...item,
        decision: decision.decision,
        rejectionReason: decision.decision === 'rejected'
          ? decision.rejectionReason
          : undefined,
      }
    : item)
  const nextUndecided = items.findIndex((item) => !item.decision)
  return {
    ...approval,
    items,
    activeIndex: nextUndecided < 0 ? activeIndex : nextUndecided,
    mode: 'options' as const,
    error: undefined,
  }
}

export function ApprovalCard({
  conversation,
  onChange,
  onSubmit,
}: {
  conversation: Conversation
  onChange: (updater: (approval: ApprovalState) => ApprovalState) => void
  onSubmit: (
    interruptIds: readonly string[],
    finalDecision?: ApprovalSubmissionDecision,
  ) => void
}) {
  const { t } = useI18n()
  const approval = conversation.approval
  const bodyRef = useRef<HTMLDivElement>(null)
  const [minimized, setMinimized] = useState(() => readApprovalCollapsed(conversation.threadId))
  const [rejectionDrafts, setRejectionDrafts] = useState<Record<string, string>>({})

  useEffect(() => {
    setMinimized(readApprovalCollapsed(conversation.threadId))
  }, [conversation.threadId])

  if (!approval || approval.items.length === 0) return null

  const activeIndex = Math.max(0, Math.min(
    approval.activeIndex,
    approval.items.length - 1,
  ))
  const active = approval.items[activeIndex]
  if (!active) return null
  const interruptIds = approval.items.map((item) => item.interruptId)
  const description = descriptionParts(active.description, t('请确认本次操作'))
  const canApprove = active.allowedDecisions.includes('approve')
  const canReject = active.allowedDecisions.includes('reject')
  const allDecided = approval.items.every((item) => Boolean(item.decision))
  const rejectionReason = rejectionDrafts[active.interruptId]
    ?? active.rejectionReason
    ?? ''
  const rejectionFormId = `approval-rejection-${active.id}`
  const toolMessage: Message = {
    id: `approval-tool-${active.interruptId}`,
    role: 'tool',
    content: active.toolName,
    createdAt: conversation.updatedAt,
    meta: {
      toolName: active.toolName,
      params: active.params,
      status: 'paused',
      toolCallId: active.toolCallId,
      interruptId: active.interruptId,
    },
  }

  const updateApproval = (updater: (current: ApprovalState) => ApprovalState) => {
    onChange((current) => matchesApprovalGroup(current, interruptIds)
      ? updater(current)
      : current)
  }

  const recordDecision = (decision: ApprovalSubmissionDecision) => {
    const completesGroup = approval.items.every((item) => (
      Boolean(item.decision) || item.interruptId === decision.interruptId
    ))
    if (completesGroup) {
      // 最后一项由提交所有者合并到权威会话，避免先 setState 再读取造成遗漏
      onSubmit(interruptIds, decision)
      return
    }
    updateApproval((current) => decideItem(current, decision))
  }

  const confirmRejection = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    recordDecision({
      interruptId: active.interruptId,
      decision: 'rejected',
      rejectionReason,
    })
  }

  const setMode = (mode: NonNullable<ApprovalState['mode']>) => {
    updateApproval((current) => ({ ...current, mode, error: undefined }))
  }

  const toggleMinimized = () => {
    // 收起只保存当前会话的展示偏好，不改变审批状态或触发恢复
    setMinimized((current) => {
      const next = !current
      writeApprovalCollapsed(conversation.threadId, next)
      return next
    })
  }

  return (
    <section
      className={`approval-composer${minimized ? ' is-minimized' : ''}`}
      aria-label={t('等待审批')}
      onWheel={(event) => {
        const body = bodyRef.current
        if (!body || body.scrollHeight <= body.clientHeight) {
          event.preventDefault()
          event.stopPropagation()
          return
        }
        if (!body.contains(event.target as Node)) {
          event.preventDefault()
          event.stopPropagation()
          body.scrollTop += event.deltaY
        }
      }}
    >
      <header className="approval-composer-head">
        <button
          type="button"
          className="approval-toggle-surface"
          aria-label={minimized ? t('展开审批卡片') : t('收起审批卡片')}
          onClick={toggleMinimized}
        />
        <div className="approval-composer-heading">
          <h2>
            <span className="approval-status-dot" aria-hidden="true" />
            <span>{t('等待审批')}</span>
          </h2>
          <p>{description.title}</p>
        </div>
        <div className="approval-composer-head-actions">
          <IconButton
            size="sm"
            className="approval-composer-head-button"
            label={minimized ? t('展开审批卡片') : t('收起审批卡片')}
            tooltip={minimized ? t('展开审批卡片') : t('收起审批卡片')}
            icon={minimized ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
            aria-expanded={!minimized}
            onClick={toggleMinimized}
          />
        </div>
      </header>

      {!minimized && (
        <>
          <div ref={bodyRef} className="approval-composer-body ui-scrollbar">
            {description.detail && (
              <div className="approval-composer-detail">
                <MarkdownContent content={description.detail} variant="compact" />
              </div>
            )}
            <ToolCallCard message={toolMessage} className="approval-tool-card" />
            {approval.mode === 'reject' && (
              <form
                id={rejectionFormId}
                key={active.interruptId}
                className="approval-rejection-form"
                onSubmit={confirmRejection}
              >
                <label htmlFor={`approval-reason-${active.id}`}>
                  {t('拒绝原因（可选）')}
                </label>
                <textarea
                  id={`approval-reason-${active.id}`}
                  name="reason"
                  value={rejectionReason}
                  rows={3}
                  placeholder={t('说明拒绝此操作的原因…')}
                  onChange={(event) => {
                    const value = event.currentTarget.value
                    setRejectionDrafts((current) => ({
                      ...current,
                      [active.interruptId]: value,
                    }))
                  }}
                />
              </form>
            )}
          </div>
          <OverlayScrollbar viewportRef={bodyRef} />
          <footer className="approval-composer-footer">
            <p className="approval-composer-feedback" role="alert">
              {approval.error ?? ''}
            </p>
            <div className="approval-composer-actions">
              {approval.mode === 'reject' ? (
                <>
                  <Button size="sm" onClick={() => setMode('options')}>{t('取消')}</Button>
                  <Button size="sm" type="submit" form={rejectionFormId} variant="danger">
                    {t('确认拒绝')}
                  </Button>
                </>
              ) : allDecided ? (
                <Button
                  size="sm"
                  className="approval-allow-button"
                  onClick={() => onSubmit(interruptIds)}
                >
                  {t('重新提交')}
                </Button>
              ) : (
                <>
                  {canReject && (
                    <Button size="sm" className="approval-reject-button" onClick={() => setMode('reject')}>
                      {t('拒绝')}
                    </Button>
                  )}
                  {canApprove && (
                    <Button
                      size="sm"
                      className="approval-allow-button"
                      onClick={() => recordDecision({
                        interruptId: active.interruptId,
                        decision: 'approved',
                      })}
                    >
                      {t('允许')}
                    </Button>
                  )}
                </>
              )}
            </div>
          </footer>
        </>
      )}
    </section>
  )
}
