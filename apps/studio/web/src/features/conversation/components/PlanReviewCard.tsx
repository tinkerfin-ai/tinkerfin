import { Check, MessageSquareText, Route, X } from 'lucide-react'
import { useEffect, useRef } from 'react'

import { Button, OverlayScrollbar } from '../../../components/ui'
import type { PlanReviewState } from '../../../types'
import { useI18n } from '../../../i18n'
import { MarkdownContent } from './MarkdownContent'
import { PlanInteractionCard, PlanInteractionStatusRow } from './PlanInteractionCard'

export function PlanReviewStatusRow({ interaction }: { interaction: PlanReviewState }) {
  const { t } = useI18n()
  return (
    <PlanInteractionStatusRow
      kind="review"
      icon={<Route size={14} aria-hidden="true" />}
      label="Plan"
      pendingStatus={t('等待审阅')}
      submittedStatus={t('正在处理决定')}
      submitted={interaction.submitted}
    />
  )
}

export function PlanReviewCard({
  interaction,
  onChange,
  onSubmit,
}: {
  interaction: PlanReviewState
  onChange: (updater: (current: PlanReviewState) => PlanReviewState) => void
  onSubmit: () => void
}) {
  const { t } = useI18n()
  const bodyRef = useRef<HTMLDivElement | null>(null)
  const messageRef = useRef<HTMLTextAreaElement | null>(null)
  useEffect(() => {
    if (interaction.action !== 'respond' && interaction.action !== 'reject') return
    const frame = window.requestAnimationFrame(() => messageRef.current?.focus())
    return () => window.cancelAnimationFrame(frame)
  }, [interaction.action, interaction.interruptId])

  const update = (patch: Partial<PlanReviewState>) => onChange((current) => ({
    ...current,
    ...patch,
    error: undefined,
  }))

  const canSubmit = Boolean(interaction.action)
    && !interaction.submitted
    && (interaction.action !== 'respond' || Boolean(interaction.message?.trim()))

  return (
    <PlanInteractionCard
      kind="review"
      ariaLabel={t('Plan 审阅')}
      minimized={false}
      collapsible={false}
      icon={<Route size={16} aria-hidden="true" />}
      title={interaction.draft.content.description}
      bodyRef={bodyRef}
    >
      <>
        <div
          ref={bodyRef}
          className="plan-review-composer-body ui-scrollbar"
          role="region"
          aria-label={t('计划草稿内容')}
        >
          <div
            className="plan-review-content"
            key={`${interaction.interruptId}:${interaction.revision}`}
          >
            <MarkdownContent content={interaction.draft.content.markdown} />
          </div>
          <div className="plan-review-actions" role="group" aria-label={t('Plan 处理方式')}>
            <Button
              size="sm"
              className="plan-review-reject-button"
              selected={interaction.action === 'reject'}
              leadingIcon={<X size={14} />}
              onClick={() => update({ action: 'reject' })}
            >
              {t('拒绝')}
            </Button>
            <Button
              size="sm"
              selected={interaction.action === 'respond'}
              leadingIcon={<MessageSquareText size={14} />}
              onClick={() => update({ action: 'respond' })}
            >
              {t('反馈')}
            </Button>
            <Button
              size="sm"
              variant="primary"
              selected={interaction.action === 'approve'}
              leadingIcon={<Check size={14} />}
              onClick={() => update({ action: 'approve' })}
            >
              {t('批准')}
            </Button>
          </div>
          {(interaction.action === 'respond' || interaction.action === 'reject') && (
            <label className="plan-review-input">
              <span>
                {interaction.action === 'respond'
                  ? t('需要调整的内容')
                  : t('拒绝原因（可选）')}
              </span>
              <textarea
                ref={messageRef}
                rows={3}
                value={interaction.message ?? ''}
                required={interaction.action === 'respond'}
                placeholder={interaction.action === 'respond'
                  ? t('说明需要修改的范围和原因…')
                  : t('说明为什么不执行这份计划…')}
                onChange={(event) => update({ message: event.currentTarget.value })}
              />
            </label>
          )}
        </div>
        <OverlayScrollbar viewportRef={bodyRef} />
        <footer className="plan-review-composer-footer">
          <p className="plan-review-composer-feedback" role="alert">
            {interaction.error ?? ''}
          </p>
          <Button
            size="sm"
            variant="primary"
            disabled={!canSubmit}
            onClick={onSubmit}
          >
            {t('提交决定')}
          </Button>
        </footer>
      </>
    </PlanInteractionCard>
  )
}
