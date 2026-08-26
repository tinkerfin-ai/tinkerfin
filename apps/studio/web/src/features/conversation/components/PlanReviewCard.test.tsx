import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { PlanReviewState } from '../../../types'
import { planReviewCollapseKey } from '../planQuestionCollapse'
import { PlanReviewCard, PlanReviewStatusRow } from './PlanReviewCard'
import conversationStyles from '../conversation.css?raw'

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
        threadId="thread-a"
        interaction={current}
        onChange={change}
        onSubmit={submit}
      />,
    )

    expect(screen.getByRole('heading', { level: 1, name: '实现模式切换' })).toBeInTheDocument()
    expect(screen.getByText('保持父图稳定')).toBeInTheDocument()
    expect(screen.getByText((content, element) => (
      element?.tagName === 'SMALL' && content.includes('第 3 版')
    ))).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '编辑' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /关闭|放弃/ })).not.toBeInTheDocument()
    expect(screen.getAllByRole('button').filter((button) => (
      ['拒绝', '反馈', '批准'].includes(button.textContent ?? '')
    ))).toHaveLength(3)

    fireEvent.click(screen.getByRole('button', { name: '批准' }))
    expect(current.action).toBe('approve')
    view.rerender(
      <PlanReviewCard
        threadId="thread-a"
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
        threadId="thread-a"
        interaction={current}
        onChange={change}
        onSubmit={vi.fn()}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: '反馈' }))
    view.rerender(
      <PlanReviewCard threadId="thread-a" interaction={current} onChange={change} onSubmit={vi.fn()} />,
    )
    const feedback = screen.getByRole('textbox', { name: '需要调整的内容' })
    fireEvent.change(feedback, { target: { value: '补充移动端验证' } })
    expect(current).toMatchObject({ action: 'respond', message: '补充移动端验证' })

    fireEvent.click(screen.getByRole('button', { name: '拒绝' }))
    view.rerender(
      <PlanReviewCard threadId="thread-a" interaction={current} onChange={change} onSubmit={vi.fn()} />,
    )
    expect(screen.getByRole('textbox', { name: '拒绝原因（可选）' })).toHaveValue('补充移动端验证')
  })

  it('persists collapse per thread without submitting or changing the review', async () => {
    const change = vi.fn()
    const submit = vi.fn()
    const view = render(
      <PlanReviewCard
        threadId="thread-a"
        interaction={interaction()}
        onChange={change}
        onSubmit={submit}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: '收起计划草稿' }))
    expect(screen.queryByRole('region', { name: '计划草稿内容' })).not.toBeInTheDocument()
    expect(screen.getByText('请审阅计划，批准后开始执行')).toBeInTheDocument()
    expect(screen.queryByText('等待审阅')).not.toBeInTheDocument()
    expect(window.sessionStorage.getItem(planReviewCollapseKey('thread-a'))).toBe('collapsed')
    expect(change).not.toHaveBeenCalled()
    expect(submit).not.toHaveBeenCalled()

    view.rerender(
      <PlanReviewCard
        threadId="thread-b"
        interaction={interaction()}
        onChange={change}
        onSubmit={submit}
      />,
    )
    await waitFor(() => expect(screen.getByRole('region', { name: '计划草稿内容' })).toBeVisible())
    view.rerender(
      <PlanReviewCard
        threadId="thread-a"
        interaction={interaction()}
        onChange={change}
        onSubmit={submit}
      />,
    )
    await waitFor(() => expect(screen.queryByRole('region', { name: '计划草稿内容' })).not.toBeInTheDocument())
  })

  it('shows waiting and submitted conversation statuses', () => {
    const view = render(<PlanReviewStatusRow interaction={interaction()} />)
    expect(screen.getByText('等待审阅')).toBeInTheDocument()
    expect(view.container.querySelector('.activity-dots')).toBeInTheDocument()

    view.rerender(<PlanReviewStatusRow interaction={{ ...interaction(), submitted: true }} />)
    expect(screen.getByRole('status')).toHaveTextContent('正在处理决定')
    expect(view.container.querySelector('.activity-dots')).not.toBeInTheDocument()
  })

  it('announces a dynamic submission error', () => {
    render(
      <PlanReviewCard
        threadId="thread-a"
        interaction={{ ...interaction(), error: '计划版本已经更新' }}
        onChange={vi.fn()}
        onSubmit={vi.fn()}
      />,
    )

    expect(screen.getByRole('alert')).toHaveTextContent('计划版本已经更新')
  })

  it('uses the approval palette while preserving the shared interaction shell', () => {
    expect(conversationStyles).toMatch(/\.approval-composer,\s*\.plan-review-composer\s*\{[^}]*border:\s*1px solid var\(--color-warning-panel-border\);[^}]*background:\s*var\(--color-layer-1\);/s)
    expect(conversationStyles).toMatch(/\.approval-composer-head,\s*\.plan-review-composer-head\s*\{[^}]*background:\s*var\(--color-warning-panel-background\);/s)
    expect(conversationStyles).toMatch(/\.plan-review-composer-heading h2 > svg\s*\{[^}]*color:\s*var\(--color-warning-panel-accent\);/s)
    expect(conversationStyles).toMatch(/\.plan-review-composer\.is-minimized \.plan-review-composer-head\s*\{[^}]*min-height:\s*calc\(var\(--layout-composer-surface-height\) - var\(--space-0-5\)\);[^}]*align-items:\s*center;[^}]*padding-top:\s*var\(--space-3\);[^}]*padding-bottom:\s*var\(--space-3\);/s)
    expect(conversationStyles).toMatch(/\.plan-review-composer-body\s*\{[^}]*flex:\s*0 1 auto;[^}]*overflow-y:\s*auto;/s)
    expect(conversationStyles).toMatch(/\.plan-review-composer-footer\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\) auto;[^}]*min-height:\s*var\(--control-xl\);/s)
    expect(conversationStyles).toMatch(/@media \(max-width:\s*440px\)[\s\S]*\.plan-review-actions\s*\{[^}]*grid-template-columns:\s*repeat\(3, minmax\(0, 1fr\)\);/s)
    expect(conversationStyles).toMatch(/@media \(max-width:\s*440px\)[\s\S]*\.plan-review-actions \.ui-button\s*\{[^}]*padding-inline:\s*var\(--space-1-5\);/s)
    expect(conversationStyles).not.toContain('.plan-card')
  })
})
