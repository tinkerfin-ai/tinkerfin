import { Check, MessageSquareText, PencilLine, Route, X } from 'lucide-react'

import { Button } from '../../../components/ui'
import type { PlanReviewState } from '../../../types'
import { useI18n } from '../../../i18n'
import { MarkdownContent } from './MarkdownContent'

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
  const update = (patch: Partial<PlanReviewState>) => onChange((current) => ({
    ...current,
    ...patch,
    error: undefined,
  }))

  return (
    <section className="plan-card plan-review-card" aria-label={t('Plan 审阅')}>
      <header className="plan-card-head">
        <span className="eyebrow"><Route size={14} />{t('计划草稿 · revision {revision}', { revision: interaction.revision })}</span>
      </header>
      <div className="plan-review-content" key={`${interaction.interruptId}:${interaction.revision}`}>
        <MarkdownContent content={interaction.draft.content.markdown} />
      </div>
      <div className="plan-review-actions" role="group" aria-label={t('Plan 处理方式')}>
        <Button selected={interaction.action === 'reject'} leadingIcon={<X size={14} />} onClick={() => update({ action: 'reject' })}>{t('拒绝')}</Button>
        <Button selected={interaction.action === 'respond'} leadingIcon={<MessageSquareText size={14} />} onClick={() => update({ action: 'respond' })}>{t('反馈')}</Button>
        <Button selected={interaction.action === 'edit'} leadingIcon={<PencilLine size={14} />} onClick={() => update({ action: 'edit', editedMarkdown: interaction.editedMarkdown ?? interaction.draft.content.markdown })}>{t('编辑')}</Button>
        <Button variant="primary" selected={interaction.action === 'approve'} leadingIcon={<Check size={14} />} onClick={() => update({ action: 'approve' })}>{t('批准')}</Button>
      </div>
      {interaction.action === 'edit' && (
        <label className="plan-review-input">
          <span>{t('编辑计划（Markdown）')}</span>
          <textarea rows={10} value={interaction.editedMarkdown ?? interaction.draft.content.markdown} onChange={(event) => update({ editedMarkdown: event.currentTarget.value })} />
        </label>
      )}
      {(interaction.action === 'respond' || interaction.action === 'reject') && (
        <label className="plan-review-input">
          <span>{interaction.action === 'respond' ? t('需要调整的内容') : t('拒绝原因（可选）')}</span>
          <textarea rows={3} value={interaction.message ?? ''} placeholder={interaction.action === 'respond' ? t('说明需要修改的范围和原因…') : t('说明为什么不执行这份计划…')} onChange={(event) => update({ message: event.currentTarget.value })} />
        </label>
      )}
      {interaction.error && <p className="plan-card-error">{interaction.error}</p>}
      <footer className="plan-card-footer">
        <span>Markdown</span>
        <Button variant="primary" disabled={!interaction.action || interaction.submitted} onClick={onSubmit}>
          {t('提交决定')}
        </Button>
      </footer>
    </section>
  )
}
