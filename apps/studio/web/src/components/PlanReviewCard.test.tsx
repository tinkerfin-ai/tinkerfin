import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { PlanReviewState } from '../types'
import { PlanReviewCard } from './PlanReviewCard'

describe('PlanReviewCard', () => {
  it('shows the structured steps and records an approval decision', () => {
    let current: PlanReviewState = {
      kind: 'review',
      interruptId: 'plan-review-1',
      revision: 3,
      submitted: false,
      draft: {
        goal: '实现模式切换',
        steps: [{ id: 'step-1', title: '实现路由', description: '保持父图稳定' }],
      },
    }
    const submit = vi.fn()
    const view = render(
      <PlanReviewCard
        interaction={current}
        onChange={(updater) => { current = updater(current) }}
        onSubmit={submit}
      />,
    )

    expect(screen.getByText('实现路由')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '批准' }))
    expect(current.action).toBe('approve')
    view.rerender(
      <PlanReviewCard interaction={current} onChange={(updater) => { current = updater(current) }} onSubmit={submit} />,
    )
    fireEvent.click(screen.getByRole('button', { name: '提交决定' }))
    expect(submit).toHaveBeenCalledOnce()
  })
})
