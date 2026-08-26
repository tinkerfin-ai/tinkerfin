import { Check, MessageSquareText, Route, X } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'

import { Button, OverlayScrollbar } from '../../../components/ui'
import type { PlanReviewState } from '../../../types'
import { useI18n } from '../../../i18n'
import {
  readPlanReviewCollapsed,
  writePlanReviewCollapsed,
} from '../planQuestionCollapse'
import { MarkdownContent } from './MarkdownContent'
import { PlanInteractionCard, PlanInteractionStatusRow } from './PlanInteractionCard'

export function PlanReviewStatusRow({ interaction }: { interaction: PlanReviewState }) {
  const { t } = useI18n()
  return (
    <PlanInteractionStatusRow
      kind="review"
      icon={<Route size={14} aria-hidden="true" />}
      label={t('计划草稿')}
      pendingStatus={t('等待审阅')}
      submittedStatus={t('正在处理决定')}
      submitted={interaction.submitted}
    />
  )
}

export function PlanReviewCard({
  threadId,
  interaction,
  onChange,
  onSubmit,
}: {
  threadId: string
  interaction: PlanReviewState
  onChange: (updater: (current: PlanReviewState) => PlanReviewState) => void
  onSubmit: () => void
}) {
  const { t } = useI18n()
  const bodyRef = useRef<HTMLDivElement | null>(null)
  const [minimized, setMinimized] = useState(() => readPlanReviewCollapsed(threadId))

  useEffect(() => {
    setMinimized(readPlanReviewCollapsed(threadId))
  }, [threadId])

  const update = (patch: Partial<PlanReviewState>) => onChange((current) => ({
    ...current,
    ...patch,
    error: undefined,
  }))

  const toggleMinimized = () => {
    // 收起只保存当前会话的展示偏好，不改变待审阅状态或触发恢复
    setMinimized((current) => {
      const next = !current
      writePlanReviewCollapsed(threadId, next)
      return next
    })
  }

  return (
    <PlanInteractionCard
      kind="review"
      ariaLabel={t('Plan 审阅')}
      minimized={minimized}
      icon={<Route size={16} aria-hidden="true" />}
      title={t('计划草稿')}
      titleMeta={<>· {t('第 {revision} 版', { revision: interaction.revision })}</>}
      description={t('请审阅计划，批准后开始执行')}
      toggleSurfaceLabel={minimized
        ? t('点击标题区域展开计划草稿')
        : t('点击标题区域收起计划草稿')}
      toggleLabel={minimized ? t('展开计划草稿') : t('收起计划草稿')}
      onToggle={toggleMinimized}
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
                rows={3}
                value={interaction.message ?? ''}
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
            disabled={!interaction.action || interaction.submitted}
            onClick={onSubmit}
          >
            {t('提交决定')}
          </Button>
        </footer>
      </>
    </PlanInteractionCard>
  )
}
