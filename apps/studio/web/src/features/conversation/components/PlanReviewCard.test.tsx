import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { PlanReviewState } from '../../../types'
import { PlanReviewCard, PlanReviewStatusRow } from './PlanReviewCard'
import conversationStyles from '../conversation.css?raw'

describe('PlanReviewCard', () => {
  const interaction = (): PlanReviewState => ({
    kind: 'review',
    interruptId: 'plan-review-1',
    revision: 3,
    submitted: false,
    draft: {
      revision: 3,
      contentSchema: {
        fingerprint: '0'.repeat(64),
        mediaType: 'text/markdown',
      },
      content: {
        description: '切换实现模式并保持父图稳定',
        markdown: '# 实现模式切换\n\n- 保持父图稳定',
      },
    },
  })

  beforeEach(() => {
    window.sessionStorage.clear()
  })

  it('renders the fixed three decisions and records an approval', () => {
    let current = interaction()
    const submit = vi.fn()
    const change = (updater: (value: PlanReviewState) => PlanReviewState) => {
      current = updater(current)
    }
    const view = render(
      <PlanReviewCard
        interaction={current}
        onChange={change}
        onSubmit={submit}
      />,
    )

    expect(screen.getByRole('heading', { level: 1, name: '实现模式切换' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 2 }))
      .toHaveTextContent('切换实现模式并保持父图稳定')
    const card = screen.getByRole('region', { name: 'Plan 审阅' })
    expect(card.querySelector('.plan-review-composer-heading p')).not.toBeInTheDocument()
    expect(card.querySelector('.plan-review-composer-heading small')).not.toBeInTheDocument()
    expect(card).not.toHaveTextContent('Plan')
    expect(screen.getByRole('region', { name: '计划草稿内容' }))
      .not.toHaveTextContent('切换实现模式并保持父图稳定')
    expect(card.querySelector('.interaction-card-color-bridge.is-warning')).toBeInTheDocument()
    expect(screen.getByText('保持父图稳定')).toBeInTheDocument()
    expect(card).not.toHaveTextContent('第 3 版')
    expect(screen.queryByRole('button', { name: '编辑' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /关闭|放弃/ })).not.toBeInTheDocument()
    expect(screen.getAllByRole('button').filter((button) => (
      ['拒绝', '反馈', '批准'].includes(button.textContent ?? '')
    ))).toHaveLength(3)

    fireEvent.click(screen.getByRole('button', { name: '批准' }))
    expect(current.action).toBe('approve')
    view.rerender(
      <PlanReviewCard
        interaction={current}
        onChange={change}
        onSubmit={submit}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: '提交决定' }))
    expect(submit).toHaveBeenCalledOnce()
  })

  it('keeps feedback required and rejection reason optional without editing Markdown', () => {
    let current = interaction()
    const change = (updater: (value: PlanReviewState) => PlanReviewState) => {
      current = updater(current)
    }
    const view = render(
      <PlanReviewCard
        interaction={current}
        onChange={change}
        onSubmit={vi.fn()}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: '反馈' }))
    view.rerender(
      <PlanReviewCard interaction={current} onChange={change} onSubmit={vi.fn()} />,
    )
    const feedback = screen.getByRole('textbox', { name: '需要调整的内容' })
    fireEvent.change(feedback, { target: { value: '补充移动端验证' } })
    expect(current).toMatchObject({ action: 'respond', message: '补充移动端验证' })

    fireEvent.click(screen.getByRole('button', { name: '拒绝' }))
    view.rerender(
      <PlanReviewCard interaction={current} onChange={change} onSubmit={vi.fn()} />,
    )
    const rejectionReason = screen.getByRole('textbox', { name: '拒绝原因（可选）' })
    expect(rejectionReason).toHaveValue('补充移动端验证')
    expect(rejectionReason).not.toBeRequired()
  })

  it('focuses required feedback and disables submission until it is non-blank', async () => {
    const user = userEvent.setup()
    const submit = vi.fn()

    function Harness() {
      const [current, setCurrent] = useState(interaction())
      return (
        <PlanReviewCard
          interaction={current}
          onChange={setCurrent}
          onSubmit={submit}
        />
      )
    }

    render(<Harness />)
    await user.click(screen.getByRole('button', { name: '反馈' }))

    const feedback = screen.getByRole('textbox', { name: '需要调整的内容' })
    const submitDecision = screen.getByRole('button', { name: '提交决定' })
    await waitFor(() => expect(feedback).toHaveFocus())
    expect(feedback).toBeRequired()
    expect(submitDecision).toBeDisabled()

    await user.type(feedback, '  ')
    expect(submitDecision).toBeDisabled()
    await user.type(feedback, '补充异常路径')
    expect(submitDecision).toBeEnabled()
    await user.click(submitDecision)
    expect(submit).toHaveBeenCalledOnce()
  })

  it('always stays expanded and ignores the removed per-thread collapse preference', () => {
    const change = vi.fn()
    const submit = vi.fn()
    window.sessionStorage.setItem('tinkerfin:plan-review-collapse:thread-a', 'collapsed')
    render(
      <PlanReviewCard
        interaction={interaction()}
        onChange={change}
        onSubmit={submit}
      />,
    )

    expect(screen.getByRole('separator', { name: '调整交互卡片高度' })).toBeInTheDocument()
    expect(screen.getByRole('region', { name: '计划草稿内容' })).toBeInTheDocument()
    const card = screen.getByRole('region', { name: 'Plan 审阅' })
    expect(card).not.toHaveClass('is-minimized')
    expect(card.querySelector('.plan-review-toggle-surface')).not.toBeInTheDocument()
    expect(card.querySelector('.plan-review-composer-head-button')).not.toBeInTheDocument()
    expect(card.querySelector('.interaction-card-color-bridge')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /展开计划草稿|收起计划草稿/ })).not.toBeInTheDocument()
    expect(change).not.toHaveBeenCalled()
    expect(submit).not.toHaveBeenCalled()
  })

  it('shows waiting and submitted conversation statuses', () => {
    const view = render(<PlanReviewStatusRow interaction={interaction()} />)
    expect(screen.getByText('Plan')).toBeInTheDocument()
    expect(screen.getByText('等待审阅')).toBeInTheDocument()
    expect(view.container.querySelector('.activity-dots')).toBeInTheDocument()

    view.rerender(<PlanReviewStatusRow interaction={{ ...interaction(), submitted: true }} />)
    expect(screen.getByRole('status')).toHaveTextContent('正在处理决定')
    expect(view.container.querySelector('.activity-dots')).not.toBeInTheDocument()
  })

  it('announces a dynamic submission error', () => {
    render(
      <PlanReviewCard
        interaction={{ ...interaction(), error: '计划版本已经更新' }}
        onChange={vi.fn()}
        onSubmit={vi.fn()}
      />,
    )

    expect(screen.getByRole('alert')).toHaveTextContent('计划版本已经更新')
  })

  it('uses the approval palette while preserving the shared interaction shell', () => {
    expect(conversationStyles).toMatch(/\.approval-composer,\s*\.plan-question-composer,\s*\.plan-review-composer\s*\{[^}]*border:\s*0;/s)
    expect(conversationStyles).toMatch(/\.approval-composer,\s*\.plan-review-composer\s*\{[^}]*background:\s*var\(--color-layer-1\);/s)
    expect(conversationStyles).toMatch(/\.approval-composer-head,\s*\.plan-review-composer-head\s*\{[^}]*background:\s*var\(--color-warning-panel-background\);/s)
    expect(conversationStyles).toMatch(/\.approval-composer-head,\s*\.plan-review-composer-head\s*\{[^}]*padding-block:\s*10px;/s)
    expect(conversationStyles).toMatch(/\.plan-review-composer-heading h2 > svg\s*\{[^}]*color:\s*var\(--color-warning-panel-accent\);/s)
    expect(conversationStyles).toMatch(/\.approval-composer-heading h2 > span:not\(\.approval-status-dot\),\s*\.plan-review-composer-heading h2 > span\s*\{[^}]*color:\s*var\(--color-warning-panel-accent\);/s)
    expect(conversationStyles).toMatch(/\.plan-review-composer-heading h2 > span\s*\{[^}]*flex:\s*1 1 auto;/s)
    expect(conversationStyles).toMatch(/\.approval-composer-footer\s*\{[^}]*min-height:\s*var\(--space-16\);[^}]*padding:\s*var\(--space-3\) var\(--space-4\);/s)
    expect(conversationStyles).not.toContain('.plan-review-composer.is-minimized')
    expect(conversationStyles).not.toContain('.plan-review-composer-head-button')
    expect(conversationStyles).toMatch(/\.plan-review-composer-body\s*\{[^}]*flex:\s*1 1 auto;[^}]*overflow-y:\s*auto;/s)
    expect(conversationStyles).toMatch(/\.plan-review-composer-footer\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\) auto;[^}]*min-height:\s*var\(--control-xl\);/s)
    expect(conversationStyles).toMatch(/@media \(max-width:\s*440px\)[\s\S]*\.plan-review-actions\s*\{[^}]*grid-template-columns:\s*repeat\(3, minmax\(0, 1fr\)\);/s)
    expect(conversationStyles).toMatch(/@media \(max-width:\s*440px\)[\s\S]*\.plan-review-actions \.ui-button\s*\{[^}]*padding-inline:\s*var\(--space-1-5\);/s)
    expect(conversationStyles).not.toContain('.plan-card')
  })
})
