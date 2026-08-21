import { Check, MessageSquareText, PencilLine, Route, X } from 'lucide-react'

import type { JsonObject, PlanReviewState } from '../types'
import { TypographyReveal } from './TypographyReveal'

const textValue = (value: unknown, fallback: string) =>
  typeof value === 'string' && value.trim() ? value : fallback

const planSteps = (draft: JsonObject) => Array.isArray(draft.steps)
  ? draft.steps.flatMap((rawStep, index) => {
      if (!rawStep || typeof rawStep !== 'object' || Array.isArray(rawStep)) return []
      const step = rawStep as JsonObject
      return [{
        id: textValue(step.id, `step-${index + 1}`),
        title: textValue(step.title, `步骤 ${index + 1}`),
        description: textValue(step.description, ''),
      }]
    })
  : []

export function PlanReviewCard({
  interaction,
  onChange,
  onSubmit,
}: {
  interaction: PlanReviewState
  onChange: (updater: (current: PlanReviewState) => PlanReviewState) => void
  onSubmit: () => void
}) {
  const steps = planSteps(interaction.draft)
  const update = (patch: Partial<PlanReviewState>) => onChange((current) => ({
    ...current,
    ...patch,
    error: undefined,
  }))

  return (
    <section className="plan-card plan-review-card" aria-label="Plan 审阅">
      <header className="plan-card-head">
        <span className="eyebrow"><Route size={14} />计划草稿 · revision {interaction.revision}</span>
        <TypographyReveal as="h3" variant="state" revealKey={`${interaction.interruptId}:${interaction.revision}`}>
          {textValue(interaction.draft.goal, '请审阅执行计划')}
        </TypographyReveal>
        <p>确认后，Deep Agent 将以此计划作为执行合同。</p>
      </header>
      <ol className="plan-step-list">
        {steps.map((step, index) => (
          <li key={step.id}>
            <span>{String(index + 1).padStart(2, '0')}</span>
            <div><strong>{step.title}</strong>{step.description && <p>{step.description}</p>}</div>
          </li>
        ))}
      </ol>
      <div className="plan-review-actions" role="group" aria-label="Plan 处理方式">
        <button className={interaction.action === 'reject' ? 'is-selected' : ''} onClick={() => update({ action: 'reject' })}><X size={14} />拒绝</button>
        <button className={interaction.action === 'respond' ? 'is-selected' : ''} onClick={() => update({ action: 'respond' })}><MessageSquareText size={14} />反馈</button>
        <button className={interaction.action === 'edit' ? 'is-selected' : ''} onClick={() => update({ action: 'edit', editedDraft: interaction.editedDraft ?? JSON.stringify(interaction.draft, null, 2) })}><PencilLine size={14} />编辑</button>
        <button className={interaction.action === 'approve' ? 'is-selected primary' : 'primary'} onClick={() => update({ action: 'approve' })}><Check size={14} />批准</button>
      </div>
      {interaction.action === 'edit' && (
        <label className="plan-review-input">
          <span>编辑完整 Plan（JSON）</span>
          <textarea rows={10} value={interaction.editedDraft ?? JSON.stringify(interaction.draft, null, 2)} onChange={(event) => update({ editedDraft: event.currentTarget.value })} />
        </label>
      )}
      {(interaction.action === 'respond' || interaction.action === 'reject') && (
        <label className="plan-review-input">
          <span>{interaction.action === 'respond' ? '需要调整的内容' : '拒绝原因（可选）'}</span>
          <textarea rows={3} value={interaction.message ?? ''} placeholder={interaction.action === 'respond' ? '说明需要修改的范围和原因…' : '说明为什么不执行这份计划…'} onChange={(event) => update({ message: event.currentTarget.value })} />
        </label>
      )}
      {interaction.error && <p className="plan-card-error">{interaction.error}</p>}
      <footer className="plan-card-footer">
        <span>{steps.length} 个执行步骤</span>
        <button className="primary" disabled={!interaction.action || interaction.submitted} onClick={onSubmit}>
          提交决定
        </button>
      </footer>
    </section>
  )
}
