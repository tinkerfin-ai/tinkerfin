import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { PlanQuestionState } from '../../../types'
import { PlanQuestionCard } from './PlanQuestionCard'

describe('PlanQuestionCard', () => {
  it('records one dynamic option and submits the Plan response', () => {
    let current: PlanQuestionState = {
      kind: 'questions',
      interruptId: 'plan-question-1',
      form: { schemaVersion: 1, questions: [] },
      submitted: false,
      questions: [{
        id: 'environment',
        prompt: '部署到哪个环境？',
        options: [{ id: 'staging', label: '预发布', description: '先验证再上线' }],
        allowFreeText: true,
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
      form: { schemaVersion: 1, questions: [] },
      submitted: false,
      questions: [{
        id: 'environment',
        prompt: '部署到哪个环境？',
        options: [{ id: 'staging', label: '预发布' }],
        allowFreeText: true,
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

  it('keeps mixed answers independent and submits the complete question list once', () => {
    let current: PlanQuestionState = {
      kind: 'questions',
      interruptId: 'plan-question-list',
      form: { schemaVersion: 1, questions: [] },
      submitted: false,
      questions: [
        {
          id: 'environment',
          prompt: '部署到哪个环境？',
          options: [{ id: 'staging', label: '预发布' }],
          allowFreeText: true,
        },
        {
          id: 'deadline',
          prompt: '交付时间有什么约束？',
          options: [{ id: 'week', label: '一周内' }],
          allowFreeText: true,
        },
      ],
    }
    const submit = vi.fn()
    const change = (updater: (value: PlanQuestionState) => PlanQuestionState) => {
      current = updater(current)
    }
    const view = render(
      <PlanQuestionCard interaction={current} onChange={change} onSubmit={submit} />,
    )

    fireEvent.click(screen.getByRole('radio', { name: '预发布' }))
    view.rerender(
      <PlanQuestionCard interaction={current} onChange={change} onSubmit={submit} />,
    )
    fireEvent.change(screen.getAllByPlaceholderText('输入会影响方案的具体要求…')[1], {
      target: { value: '下周三前完成' },
    })
    view.rerender(
      <PlanQuestionCard interaction={current} onChange={change} onSubmit={submit} />,
    )

    expect(current.questions[0]).toMatchObject({
      id: 'environment',
      selectedOptionId: 'staging',
      customAnswer: '',
    })
    expect(current.questions[1]).toMatchObject({
      id: 'deadline',
      selectedOptionId: undefined,
      customAnswer: '下周三前完成',
    })
    fireEvent.click(screen.getByRole('button', { name: '提交并继续规划' }))
    expect(submit).toHaveBeenCalledOnce()
  })

  it('renders and independently edits every question in a larger form', () => {
    const questions = Array.from({ length: 4 }, (_, index) => ({
      id: `question-${index}`,
      prompt: `第 ${index + 1} 个问题？`,
      options: [],
      allowFreeText: true,
    }))
    let current: PlanQuestionState = {
      kind: 'questions',
      interruptId: 'plan-question-large',
      form: { schemaVersion: 1, questions },
      submitted: false,
      questions,
    }
    const submit = vi.fn()
    const change = (updater: (value: PlanQuestionState) => PlanQuestionState) => {
      current = updater(current)
    }
    const view = render(
      <PlanQuestionCard interaction={current} onChange={change} onSubmit={submit} />,
    )

    for (let index = 0; index < questions.length; index += 1) {
      fireEvent.change(screen.getAllByPlaceholderText('输入会影响方案的具体要求…')[index], {
        target: { value: `答案 ${index + 1}` },
      })
      view.rerender(
        <PlanQuestionCard interaction={current} onChange={change} onSubmit={submit} />,
      )
    }

    expect(screen.getByText('4 个待确认问题')).toBeInTheDocument()
    expect(current.questions.map((question) => question.customAnswer)).toEqual([
      '答案 1',
      '答案 2',
      '答案 3',
      '答案 4',
    ])
    fireEvent.click(screen.getByRole('button', { name: '提交并继续规划' }))
    expect(submit).toHaveBeenCalledOnce()
  })
})
