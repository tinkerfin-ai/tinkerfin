import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { PlanReviewState } from '../../../types'
import { PlanReviewCard } from './PlanReviewCard'

describe('PlanReviewCard', () => {
  const interaction = (): PlanReviewState => ({
    kind: 'review',
    interruptId: 'plan-review-1',
    revision: 3,
    submitted: false,
    draft: {
      schemaVersion: 1,
      revision: 3,
      contentSchema: {
        id: 'tinkerfin.plan.markdown.v1',
        fingerprint: '0'.repeat(64),
        mediaType: 'text/markdown',
      },
      content: {
        markdown: '# 实现模式切换\n\n- 保持父图稳定',
      },
    },
  })

  it('renders Markdown and records an approval decision', () => {
    let current = interaction()
    const submit = vi.fn()
    const view = render(
      <PlanReviewCard
        interaction={current}
        onChange={(updater) => { current = updater(current) }}
        onSubmit={submit}
      />,
    )

    expect(screen.getByRole('heading', { level: 1, name: '实现模式切换' })).toBeInTheDocument()
    expect(screen.getByText('保持父图稳定')).toBeInTheDocument()
    expect(screen.queryByText(/执行步骤/)).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '批准' }))
    expect(current.action).toBe('approve')
    view.rerender(
      <PlanReviewCard interaction={current} onChange={(updater) => { current = updater(current) }} onSubmit={submit} />,
    )
    fireEvent.click(screen.getByRole('button', { name: '提交决定' }))
    expect(submit).toHaveBeenCalledOnce()
  })

  it('edits the exact Markdown string instead of a JSON envelope', () => {
    let current = interaction()
    const view = render(
      <PlanReviewCard
        interaction={current}
        onChange={(updater) => { current = updater(current) }}
        onSubmit={vi.fn()}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: '编辑' }))
    view.rerender(
      <PlanReviewCard interaction={current} onChange={(updater) => { current = updater(current) }} onSubmit={vi.fn()} />,
    )
    const textarea = screen.getByRole('textbox', { name: '编辑计划（Markdown）' })
    expect(textarea).toHaveValue('# 实现模式切换\n\n- 保持父图稳定')
    fireEvent.change(textarea, { target: { value: '  # 修改后\n\n```ts\nconst ok = true\n```\n' } })
    expect(current.editedMarkdown).toBe('  # 修改后\n\n```ts\nconst ok = true\n```\n')
  })
})
