import { CircleHelp, ListChecks } from 'lucide-react'

import type { PlanQuestionState } from '../types'
import { TypographyReveal } from './TypographyReveal'

export function PlanQuestionCard({
  interaction,
  onChange,
  onSubmit,
}: {
  interaction: PlanQuestionState
  onChange: (updater: (current: PlanQuestionState) => PlanQuestionState) => void
  onSubmit: () => void
}) {
  return (
    <section className="plan-card" aria-label="Plan 澄清问题">
      <header className="plan-card-head">
        <span className="eyebrow"><CircleHelp size={14} />规划前需要确认</span>
        <TypographyReveal as="h3" variant="state" revealKey={interaction.interruptId}>
          补充关键选择
        </TypographyReveal>
        <p>这些答案会成为本次计划的明确约束。</p>
      </header>
      <div className="plan-question-list">
        {interaction.questions.map((question, questionIndex) => (
          <fieldset key={question.id} className="plan-question-fieldset">
            <legend><span>{questionIndex + 1}</span>{question.prompt}</legend>
            {question.options.length > 0 && (
              <div className="plan-option-list">
                {question.options.map((option) => (
                  <label key={option.id} className="plan-option">
                    <input
                      type="radio"
                      name={`plan-question-${question.id}`}
                      checked={question.selectedOptionId === option.id}
                      disabled={interaction.submitted}
                      onChange={() => onChange((current) => ({
                        ...current,
                        error: undefined,
                        questions: current.questions.map((item) => item.id === question.id
                          ? { ...item, selectedOptionId: option.id, customAnswer: '' }
                          : item),
                      }))}
                    />
                    <span><strong>{option.label}</strong>{option.description && <small>{option.description}</small>}</span>
                  </label>
                ))}
              </div>
            )}
            {question.allowCustomAnswer && (
              <label className="plan-custom-answer">
                <span>{question.options.length ? '或填写其他答案' : '你的答案'}</span>
                <textarea
                  rows={2}
                  value={question.customAnswer ?? ''}
                  disabled={interaction.submitted}
                  placeholder="输入会影响方案的具体要求…"
                  onChange={(event) => {
                    const value = event.currentTarget.value
                    onChange((current) => ({
                      ...current,
                      error: undefined,
                      questions: current.questions.map((item) => item.id === question.id
                        ? { ...item, customAnswer: value, selectedOptionId: undefined }
                        : item),
                    }))
                  }}
                />
              </label>
            )}
          </fieldset>
        ))}
      </div>
      {interaction.error && <p className="plan-card-error">{interaction.error}</p>}
      <footer className="plan-card-footer">
        <span><ListChecks size={14} />{interaction.questions.length} 个待确认问题</span>
        <button className="primary" disabled={interaction.submitted} onClick={onSubmit}>
          提交并继续规划
        </button>
      </footer>
    </section>
  )
}
