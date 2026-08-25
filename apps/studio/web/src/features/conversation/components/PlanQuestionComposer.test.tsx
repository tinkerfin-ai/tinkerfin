import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { PlanQuestionState } from '../../../types'
import { PlanQuestionComposer, PlanQuestionStatusRow } from './PlanQuestionComposer'
import conversationStyles from '../conversation.css?raw'

const interaction = (): PlanQuestionState => ({
  kind: 'questions',
  interruptId: 'plan-question-1',
  title: '确认部署约束',
  description: '这些答案会影响计划范围与验证方式',
  activeQuestionIndex: 0,
  form: { schemaVersion: 2, questions: [] },
  submitted: false,
  questions: [
    {
      id: 'environment',
      prompt: '部署到哪个环境？',
      required: true,
      options: [
        { id: 'staging', label: '预发布', description: '先验证再上线', recommended: true },
        { id: 'production', label: '生产', recommended: false },
      ],
      allowFreeText: true,
    },
    {
      id: 'deadline',
      prompt: '交付时间有什么偏好？',
      required: false,
      options: [{ id: 'week', label: '一周内', recommended: true }],
      allowFreeText: true,
    },
    {
      id: 'notes',
      prompt: '还有其他补充吗？',
      required: false,
      options: [],
      allowFreeText: true,
    },
  ],
})

describe('PlanQuestionComposer', () => {
  beforeEach(() => window.sessionStorage.clear())

  it('takes one question at a time and auto-advances after a selection', () => {
    let current = interaction()
    const change = (updater: (value: PlanQuestionState) => PlanQuestionState) => {
      current = updater(current)
    }
    const view = render(
      <PlanQuestionComposer threadId="thread-a" interaction={current} onChange={change} onSubmit={vi.fn()} onAbandon={vi.fn()} />,
    )

    expect(screen.getByRole('heading', { name: '确认部署约束' })).toBeInTheDocument()
    expect(screen.queryByText('规划前需要确认')).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '部署到哪个环境？' })).toBeInTheDocument()
    expect(screen.queryByText('交付时间有什么偏好？')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '浏览上一题' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '浏览下一题' })).toBeEnabled()
    expect(document.querySelector('.plan-question-composer-pager .ui-tooltip')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('radio', { name: /预发布/ }))
    expect(current.questions[0]).toMatchObject({ selectedOptionId: 'staging', skipped: false })
    expect(current.activeQuestionIndex).toBe(1)

    view.rerender(
      <PlanQuestionComposer threadId="thread-a" interaction={current} onChange={change} onSubmit={vi.fn()} onAbandon={vi.fn()} />,
    )
    expect(screen.getByRole('heading', { name: /交付时间有什么偏好/ })).toBeInTheDocument()
    expect(screen.getByText('可选')).toBeInTheDocument()
    expect(screen.queryByRole('navigation', { name: '问题进度' })).not.toBeInTheDocument()
  })

  it('uses keyboard confirmation, supports optional skip and reports status', () => {
    let current = interaction()
    const change = (updater: (value: PlanQuestionState) => PlanQuestionState) => {
      current = updater(current)
    }
    const view = render(
      <PlanQuestionComposer threadId="thread-a" interaction={current} onChange={change} onSubmit={vi.fn()} onAbandon={vi.fn()} />,
    )

    const first = screen.getByRole('radio', { name: /预发布/ })
    fireEvent.keyDown(first, { key: 'ArrowDown' })
    expect(screen.getByRole('radio', { name: '生产' })).toHaveFocus()
    fireEvent.keyDown(screen.getByRole('radio', { name: '生产' }), { key: 'Enter' })
    view.rerender(
      <PlanQuestionComposer threadId="thread-a" interaction={current} onChange={change} onSubmit={vi.fn()} onAbandon={vi.fn()} />,
    )
    fireEvent.click(screen.getByRole('button', { name: '跳过本题' }))
    expect(current.questions[1]).toMatchObject({ skipped: true })

    const statusView = render(<PlanQuestionStatusRow interaction={current} />)
    expect(screen.getByText('等待回答')).toBeInTheDocument()
    expect(screen.queryByText('3 / 3')).not.toBeInTheDocument()
    expect(statusView.container.querySelectorAll('.activity-dots i')).toHaveLength(3)

    statusView.rerender(<PlanQuestionStatusRow interaction={{ ...current, submitted: true }} />)
    expect(statusView.container.querySelector('.activity-dots')).not.toBeInTheDocument()
  })

  it('collapses to the title, current prompt and progress without losing drafts', () => {
    let current = {
      ...interaction(),
      questions: interaction().questions.map((question, index) => index === 0
        ? { ...question, customAnswer: '保留这个草稿' }
        : question),
    }
    const change = (updater: (value: PlanQuestionState) => PlanQuestionState) => {
      current = updater(current)
    }
    const view = render(
      <PlanQuestionComposer threadId="thread-a" interaction={current} onChange={change} onSubmit={vi.fn()} onAbandon={vi.fn()} />,
    )

    fireEvent.click(screen.getByRole('button', { name: '点击标题区域收起问题卡片' }))
    expect(screen.getByRole('button', { name: '展开问题卡片' })).toHaveAttribute('aria-expanded', 'false')
    expect(screen.getByRole('button', { name: '点击标题区域展开问题卡片' })).toBeInTheDocument()
    expect(screen.getByText('部署到哪个环境？')).toBeInTheDocument()
    expect(screen.queryByRole('radiogroup')).not.toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /查看第/ })).toHaveLength(3)
    fireEvent.click(screen.getByRole('button', { name: /查看第 3 题/ }))
    expect(current.activeQuestionIndex).toBe(2)
    expect(current.questions[0]?.customAnswer).toBe('保留这个草稿')
    view.rerender(
      <PlanQuestionComposer threadId="thread-a" interaction={current} onChange={change} onSubmit={vi.fn()} onAbandon={vi.fn()} />,
    )
    fireEvent.click(screen.getByRole('button', { name: '点击标题区域展开问题卡片' }))
    expect(screen.queryByRole('radiogroup')).not.toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: '自定义回答：还有其他补充吗？' })).toBeInTheDocument()
  })

  it('restores collapse state after remount and isolates it by conversation', async () => {
    const props = {
      interaction: interaction(),
      onChange: vi.fn(),
      onSubmit: vi.fn(),
      onAbandon: vi.fn(),
    }
    const firstView = render(<PlanQuestionComposer threadId="thread-a" {...props} />)
    fireEvent.click(screen.getByRole('button', { name: '点击标题区域收起问题卡片' }))
    expect(window.sessionStorage.getItem('tinkerfin:plan-question-collapse:thread-a'))
      .toBe('collapsed')
    firstView.unmount()

    const restoredView = render(<PlanQuestionComposer threadId="thread-a" {...props} />)
    expect(screen.queryByRole('radiogroup')).not.toBeInTheDocument()

    restoredView.rerender(<PlanQuestionComposer threadId="thread-b" {...props} />)
    await waitFor(() => expect(screen.getByRole('radiogroup')).toBeInTheDocument())

    restoredView.rerender(<PlanQuestionComposer threadId="thread-a" {...props} />)
    await waitFor(() => expect(screen.queryByRole('radiogroup')).not.toBeInTheDocument())
  })

  it('blocks wheel propagation while the natural-height body does not overflow', () => {
    const outerWheel = vi.fn()
    const { container } = render(
      <div onWheel={outerWheel}>
        <PlanQuestionComposer
          threadId="thread-a"
          interaction={interaction()}
          onChange={vi.fn()}
          onSubmit={vi.fn()}
          onAbandon={vi.fn()}
        />
      </div>,
    )
    const body = container.querySelector<HTMLElement>('.plan-question-composer-body')!
    expect(container.querySelector('.ui-overlay-scrollbar')).toHaveAttribute('data-visibility', 'transient')
    Object.defineProperties(body, {
      clientHeight: { configurable: true, value: 220 },
      scrollHeight: { configurable: true, value: 220 },
    })

    fireEvent.wheel(body, { deltaY: 48 })

    expect(outerWheel).not.toHaveBeenCalled()
  })

  it('submits all-optional batches and returns to the first missing required question', () => {
    let current = { ...interaction(), activeQuestionIndex: 2 }
    const submit = vi.fn()
    const change = (updater: (value: PlanQuestionState) => PlanQuestionState) => {
      current = updater(current)
    }
    const view = render(
      <PlanQuestionComposer threadId="thread-a" interaction={current} onChange={change} onSubmit={submit} onAbandon={vi.fn()} />,
    )
    fireEvent.click(screen.getByRole('button', { name: '提交' }))
    expect(submit).not.toHaveBeenCalled()
    expect(current.activeQuestionIndex).toBe(0)
    expect(current.error).toBe('请回答所有必填的 Plan 澄清问题')

    current = {
      ...interaction(),
      activeQuestionIndex: 2,
      questions: interaction().questions.map((question) => ({ ...question, required: false })),
    }
    view.rerender(
      <PlanQuestionComposer threadId="thread-a" interaction={current} onChange={change} onSubmit={submit} onAbandon={vi.fn()} />,
    )
    fireEvent.click(screen.getByRole('button', { name: '提交' }))
    expect(submit).toHaveBeenCalledOnce()
  })

  it('keeps every option row borderless and on one shared grid', () => {
    expect(conversationStyles).toMatch(/\.plan-question-option,\s*\.plan-question-custom\s*{[^}]*grid-template-columns:\s*20px minmax\(0, 1fr\);[^}]*width:\s*100%;[^}]*min-height:\s*var\(--control-md\);[^}]*border:\s*0;/s)
    expect(conversationStyles).not.toMatch(/\.plan-question-option\s*{[^}]*border:\s*1px/s)
    expect(conversationStyles).toMatch(/\.plan-question-composer-body\s*{[^}]*flex:\s*0 1 auto;/s)
    expect(conversationStyles).toMatch(/\.composer-dock\s*{[^}]*min-width:\s*0;/s)
    expect(conversationStyles).toMatch(/\.plan-question-composer\s*{[^}]*font-family:\s*var\(--font-ui\);/s)
    expect(conversationStyles).toMatch(/\.plan-question-composer-heading h2\s*{[^}]*font-size:\s*var\(--type-title-size\);[^}]*line-height:\s*var\(--type-title-line\);/s)
    expect(conversationStyles).toMatch(/\.plan-question-composer-body > h3\s*{[^}]*align-items:\s*flex-start;[^}]*font-size:\s*var\(--type-ui-size\);[^}]*line-height:\s*var\(--type-title-line\);/s)
    expect(conversationStyles).toMatch(/\.plan-question-composer-body > h3 small\s*{[^}]*min-height:\s*var\(--type-title-line\);[^}]*align-items:\s*center;[^}]*line-height:\s*var\(--type-title-line\);/s)
    expect(conversationStyles).toMatch(/\.plan-question-option-copy strong\s*{[^}]*font-size:\s*var\(--type-ui-size\);/s)
    expect(conversationStyles).toMatch(/\.plan-question-option-copy small\s*{[^}]*font-size:\s*var\(--type-ui-size\);/s)
    expect(conversationStyles).toMatch(/\.plan-question-option-copy\s*{[^}]*grid-template-columns:\s*auto minmax\(0, 1fr\) auto;[^}]*align-items:\s*baseline;/s)
    expect(conversationStyles).toMatch(/\.plan-question-option-recommended\s*{[^}]*min-height:\s*var\(--type-title-line\);[^}]*grid-column:\s*3;[^}]*line-height:\s*var\(--type-title-line\);/s)
    expect(conversationStyles).not.toContain('--plan-question-option-inline-padding')
    expect(conversationStyles).not.toMatch(/\.plan-question-(?:option-recommended|composer-body > h3 small)[^{]*{[^}]*translateY/s)
    expect(conversationStyles).toMatch(/\.plan-question-custom textarea\s*{[^}]*height:\s*var\(--type-title-line\);[^}]*max-height:\s*calc\(var\(--type-title-line\) \* 3\);[^}]*overflow-y:\s*hidden;[^}]*font-size:\s*var\(--type-ui-size\);/s)
    expect(conversationStyles).not.toMatch(/\.plan-question-(?:option:focus-visible|custom:focus-within) \.plan-question-option-index/)
    expect(conversationStyles).toMatch(/\.plan-question-option:focus-visible,[\s\S]*\.plan-question-custom:focus-within\s*{[^}]*outline:\s*0;[^}]*background:\s*var\(--color-hover\);/s)
    expect(conversationStyles).toMatch(/@media \(forced-colors: active\)[\s\S]*\.plan-question-option:focus-visible,[\s\S]*outline:\s*2px solid Highlight;/s)
    expect(conversationStyles).toMatch(/\.plan-question-composer-pager > span\s*{[^}]*font-size:\s*var\(--type-ui-size\);/s)
    expect(conversationStyles).toMatch(/\.plan-question-pager-button\s*{[^}]*width:\s*var\(--control-plan-chip\);[^}]*height:\s*var\(--control-plan-chip\);/s)
    expect(conversationStyles).toMatch(/@media \(any-hover: none\), \(any-pointer: coarse\)[\s\S]*\.plan-question-pager-button\s*{[^}]*min-width:\s*var\(--control-lg\);[^}]*min-height:\s*var\(--control-lg\);/s)
    expect(conversationStyles).toMatch(/@media \(prefers-reduced-motion: reduce\)[\s\S]*\.plan-question-pager-button \.ui-icon-button__icon\s*{\s*transition:\s*none;/s)
  })

  it('uses a distinct answer icon instead of an ambiguous plus sign', () => {
    const { container } = render(
      <PlanQuestionComposer
        threadId="thread-a"
        interaction={interaction()}
        onChange={vi.fn()}
        onSubmit={vi.fn()}
        onAbandon={vi.fn()}
      />,
    )
    const customIcon = container.querySelector('.plan-question-custom .plan-question-option-index')
    expect(customIcon?.textContent).toBe('')
    expect(customIcon?.querySelector('svg')).toBeInTheDocument()
  })

  it('grows the custom answer from one line to three lines before scrolling', () => {
    const { container } = render(
      <PlanQuestionComposer
        threadId="thread-a"
        interaction={interaction()}
        onChange={vi.fn()}
        onSubmit={vi.fn()}
        onAbandon={vi.fn()}
      />,
    )
    const textarea = container.querySelector<HTMLTextAreaElement>('.plan-question-custom textarea')!
    textarea.style.maxHeight = '72px'
    Object.defineProperty(textarea, 'scrollHeight', { configurable: true, value: 48 })

    fireEvent.change(textarea, { target: { value: '第一行\n第二行' } })
    expect(textarea.style.height).toBe('48px')
    expect(textarea.style.overflowY).toBe('hidden')

    Object.defineProperty(textarea, 'scrollHeight', { configurable: true, value: 96 })
    fireEvent.change(textarea, { target: { value: '第一行\n第二行\n第三行\n第四行' } })
    expect(textarea.style.height).toBe('72px')
    expect(textarea.style.overflowY).toBe('auto')
  })

  it('focuses the named free-text answer when a question has no options', async () => {
    render(
      <PlanQuestionComposer
        threadId="thread-a"
        interaction={{ ...interaction(), activeQuestionIndex: 2 }}
        onChange={vi.fn()}
        onSubmit={vi.fn()}
        onAbandon={vi.fn()}
      />,
    )

    const answer = screen.getByRole('textbox', { name: '自定义回答：还有其他补充吗？' })
    await waitFor(() => expect(answer).toHaveFocus())
    expect(screen.queryByRole('radiogroup')).not.toBeInTheDocument()
  })
})
