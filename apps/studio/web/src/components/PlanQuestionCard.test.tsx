import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { PlanQuestionState } from '../types'
import { PlanQuestionCard } from './PlanQuestionCard'

describe('PlanQuestionCard', () => {
  it('records one dynamic option and submits the Plan response', () => {
    let current: PlanQuestionState = {
      kind: 'questions',
      interruptId: 'plan-question-1',
      submitted: false,
      questions: [{
        id: 'environment',
        prompt: '部署到哪个环境？',
        options: [{ id: 'staging', label: '预发布', description: '先验证再上线' }],
        allowCustomAnswer: true,
      }],
    }
    const submit = vi.fn()
    const view = render(
      <PlanQuestionCard
        interaction={current}
        onChange={(updater) => { current = updater(current) }}
        onSubmit={submit}
      />,
    )

    fireEvent.click(screen.getByRole('radio', { name: /预发布/ }))
    expect(current.questions[0]?.selectedOptionId).toBe('staging')
    view.rerender(
      <PlanQuestionCard interaction={current} onChange={(updater) => { current = updater(current) }} onSubmit={submit} />,
    )
    fireEvent.click(screen.getByRole('button', { name: '提交并继续规划' }))
    expect(submit).toHaveBeenCalledOnce()
  })

  it('uses a custom answer instead of a previously selected option', () => {
    let current: PlanQuestionState = {
      kind: 'questions',
      interruptId: 'plan-question-custom',
      submitted: false,
      questions: [{
        id: 'environment',
        prompt: '部署到哪个环境？',
        options: [{ id: 'staging', label: '预发布' }],
        allowCustomAnswer: true,
      }],
    }
    const view = render(
      <PlanQuestionCard
        interaction={current}
        onChange={(updater) => { current = updater(current) }}
        onSubmit={vi.fn()}
      />,
    )

    fireEvent.click(screen.getByRole('radio', { name: /预发布/ }))
    view.rerender(
      <PlanQuestionCard interaction={current} onChange={(updater) => { current = updater(current) }} onSubmit={vi.fn()} />,
    )
    fireEvent.change(screen.getByPlaceholderText('输入会影响方案的具体要求…'), {
      target: { value: '隔离的性能测试环境' },
    })

    expect(current.questions[0]?.selectedOptionId).toBeUndefined()
    expect(current.questions[0]?.customAnswer).toBe('隔离的性能测试环境')
  })
})
