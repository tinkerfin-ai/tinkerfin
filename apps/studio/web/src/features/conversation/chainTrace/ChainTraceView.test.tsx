import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { TraceEntryPage } from '../../../api/conversation/traceEntries'
import { ChainTraceView } from './ChainTraceView'
import chainTraceStyles from './chainTrace.css?raw'

const useChainTrace = vi.hoisted(() => vi.fn())
vi.mock('./useChainTrace', () => ({ useChainTrace }))

const message = (id: string, runId: string, content: string, traceSeq: number) => ({
  id,
  traceSeq,
  sourceId: id,
  namespace: [],
  runId,
  role: 'user' as const,
  content,
  contentOmitted: false,
  status: 'completed' as const,
  createdAt: '2026-08-31T00:00:00.000Z',
})

const page: TraceEntryPage = {
  turns: [
    {
      id: 'turn-1',
      ordinal: 1,
      startedAt: '2026-08-31T00:00:00.000Z',
      userMessage: message('user-1', 'run-1', '第一轮问题', 1),
    },
    {
      id: 'turn-2',
      ordinal: 2,
      startedAt: '2026-08-31T00:01:00.000Z',
      userMessage: message('user-2', 'run-2', '第二轮问题', 9),
    },
  ],
  items: [
    {
      id: 'old-agent',
      turnId: 'turn-1',
      kind: 'agent',
      status: 'succeeded',
      name: 'Old Agent',
      runId: 'run-1',
      namespace: [],
      startedAt: '2026-08-31T00:00:00.000Z',
      completedAt: '2026-08-31T00:00:01.000Z',
      startedSeq: 2,
      updatedSeq: 3,
      requestOmitted: false,
      resultOmitted: false,
      hooks: [],
    },
    {
      id: 'agent-2',
      turnId: 'turn-2',
      kind: 'agent',
      status: 'succeeded',
      name: 'Agent',
      runId: 'run-2',
      namespace: [],
      startedAt: '2026-08-31T00:01:00.000Z',
      completedAt: '2026-08-31T00:01:03.000Z',
      startedSeq: 10,
      updatedSeq: 20,
      requestOmitted: false,
      resultOmitted: false,
      hooks: [],
    },
    {
      id: 'middleware-config',
      turnId: 'turn-2',
      parentId: 'agent-2',
      kind: 'middleware',
      status: 'configured',
      name: 'PromptCacheMiddleware.awrap_model_call',
      runId: 'run-2',
      namespace: [],
      startedAt: '2026-08-31T00:01:00.010Z',
      completedAt: '2026-08-31T00:01:00.010Z',
      startedSeq: 11,
      updatedSeq: 11,
      requestOmitted: false,
      resultOmitted: false,
      className: 'deepagents.middleware.PromptCacheMiddleware',
      hooks: ['awrap_model_call'],
    },
    {
      id: 'model-step',
      turnId: 'turn-2',
      parentId: 'agent-2',
      kind: 'model',
      status: 'succeeded',
      name: 'Model',
      runId: 'run-2',
      namespace: [],
      startedAt: '2026-08-31T00:01:00.100Z',
      completedAt: '2026-08-31T00:01:01.300Z',
      startedSeq: 12,
      updatedSeq: 16,
      requestOmitted: false,
      resultOmitted: false,
      hooks: [],
    },
    {
      id: 'provider-2',
      turnId: 'turn-2',
      parentId: 'model-step',
      kind: 'provider',
      status: 'succeeded',
      name: 'deepseek-chat',
      runId: 'run-2',
      namespace: [],
      provider: 'deepseek',
      model: 'deepseek-chat',
      startedAt: '2026-08-31T00:01:00.200Z',
      firstOutputAt: '2026-08-31T00:01:00.400Z',
      completedAt: '2026-08-31T00:01:01.200Z',
      startedSeq: 13,
      updatedSeq: 15,
      request: {
        messages: [
          {
            messageType: 'system',
            content: [
              { type: 'text', text: '# 最终系统提示词' },
              { type: 'text', text: '- 附加系统规则' },
            ],
          },
          { messageType: 'human', content: '最终用户提示词' },
        ],
        invocation: { model: 'deepseek-chat' },
      },
      requestOmitted: false,
      resultOmitted: false,
      usage: { input_tokens: 10, output_tokens: 2 },
      responseMetadata: { finish_reason: 'stop' },
      hooks: [],
    },
    {
      id: 'tools-step',
      turnId: 'turn-2',
      parentId: 'agent-2',
      kind: 'tools',
      status: 'failed',
      name: 'Tools',
      runId: 'run-2',
      namespace: [],
      startedAt: '2026-08-31T00:01:01.400Z',
      completedAt: '2026-08-31T00:01:02.600Z',
      startedSeq: 17,
      updatedSeq: 19,
      requestOmitted: false,
      resultOmitted: false,
      hooks: [],
    },
    {
      id: 'failed-tool',
      turnId: 'turn-2',
      parentId: 'tools-step',
      proposalId: 'proposal-search',
      kind: 'tool',
      status: 'failed',
      name: 'search',
      runId: 'run-2',
      namespace: [],
      startedAt: '2026-08-31T00:01:01.500Z',
      completedAt: '2026-08-31T00:01:02.500Z',
      startedSeq: 18,
      updatedSeq: 19,
      requestOmitted: false,
      resultOmitted: false,
      failure: {
        errorType: 'builtins.TimeoutError',
        message: '搜索服务超时',
      },
      hooks: [],
    },
  ],
  nextCursor: null,
  asOfSeq: 20,
  facets: {
    kinds: { agent: 2, model: 1, provider: 1, tools: 1, tool: 1, middleware: 1 },
    statuses: { succeeded: 4, failed: 2, configured: 1 },
    agents: {},
    middleware: { PromptCacheMiddleware: 1 },
    skills: {},
    providers: { deepseek: 1 },
    models: { 'deepseek-chat': 1 },
  },
  completeness: { callTrackingMissing: false, executionTreeMissing: false },
}

describe('ChainTraceView', () => {
  it('renders HumanMessage Turns, independently collapses nodes, and opens technical details', async () => {
    useChainTrace.mockReturnValue({ state: { phase: 'ready', page }, retry: vi.fn() })
    const onReturnToConversation = vi.fn()
    const { container } = render(
      <ChainTraceView
        threadId="thread-1"
        active
        headerTarget={document.body}
        onReturnToConversation={onReturnToConversation}
      />,
    )

    expect(screen.getByRole('region', { name: '链路' })).toBeInTheDocument()
    expect(screen.getByText('第 1 轮')).toBeInTheDocument()
    expect(screen.getByText('第 2 轮')).toBeInTheDocument()
    expect(screen.queryByText('Old Agent')).not.toBeInTheDocument()
    expect(screen.getByText('第二轮问题')).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: '链路时间线' })).not.toBeInTheDocument()
    expect(container.querySelector('table')).not.toBeInTheDocument()
    expect(screen.queryByText(/运行 · run-/)).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '返回对话' }))
    expect(onReturnToConversation).toHaveBeenCalledOnce()

    expect(screen.getByRole('button', { name: '收起 Agent' })).toBeEnabled()

    const modelToggle = screen.getByRole('button', { name: '收起 Model' })
    fireEvent.click(modelToggle)
    expect(screen.queryByRole('button', { name: '模型请求，deepseek-chat' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '展开 Model' }))
    const providerButton = screen.getByRole('button', { name: '模型请求，deepseek-chat' })

    expect(screen.queryByText('仅配置')).not.toBeInTheDocument()
    expect(screen.getAllByText('错误')).toHaveLength(1)
    expect(screen.getByText('搜索服务超时')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '工具阶段，Tools' })).not.toBeInTheDocument()
    const modelButton = screen.getByRole('button', { name: '模型，Model' })
    const siblingToolButton = screen.getByRole('button', { name: '工具执行，search' })

    fireEvent.click(providerButton)
    expect(providerButton).toHaveAttribute('aria-expanded', 'true')
    expect(providerButton).toHaveAttribute('aria-controls', 'chain-trace-details')
    expect(providerButton.closest<HTMLElement>('.chain-trace-node-row')?.style.getPropertyValue('--chain-trace-row-inset'))
      .toBe('calc(var(--chain-trace-tree-inline-gutter) + var(--chain-trace-root-indent) + var(--chain-trace-level-indent) + var(--chain-trace-level-indent))')
    expect(providerButton.closest('.chain-trace-node')).toHaveClass(
      'is-on-selected-path',
      'is-path-incoming',
    )
    expect(modelButton.closest('.chain-trace-node'))
      .toHaveClass('is-on-selected-path')
    expect(siblingToolButton.closest('.chain-trace-node'))
      .not.toHaveClass('is-on-selected-path')
    const details = screen.getByRole('complementary', { name: '链路详情' })
    const treeScroll = container.querySelector('.chain-trace-tree-scroll')
    expect(treeScroll).toHaveAttribute('inert')
    expect(within(details).getByRole('button', { name: '关闭链路详情' })).toHaveFocus()
    expect(within(details).getByRole('heading', { name: 'deepseek-chat' })).toBeInTheDocument()
    expect(within(details).getByText('deepseek')).toBeInTheDocument()
    expect(within(details).getAllByText('deepseek-chat')).toHaveLength(2)
    expect(within(details).queryByRole('button', { name: '调整链路详情宽度' }))
      .not.toBeInTheDocument()
    const systemTab = within(details).getByRole('tab', { name: '系统提示词' })
    fireEvent.click(systemTab)
    expect(within(details).getByRole('heading', { name: '最终系统提示词' })).toBeInTheDocument()
    expect(within(details).getByRole('listitem')).toHaveTextContent('附加系统规则')
    fireEvent.keyDown(systemTab, { key: 'ArrowRight' })
    expect(within(details).getByRole('tab', { name: '用量' })).toHaveFocus()
    fireEvent.click(within(details).getByRole('tab', { name: '响应元数据' }))
    expect(within(details).getByText(/finish_reason/)).toBeInTheDocument()
    fireEvent.click(within(details).getByRole('tab', { name: '计时' }))
    expect(within(details).getByText('200 毫秒')).toBeInTheDocument()
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(providerButton).toHaveFocus())
    expect(treeScroll).not.toHaveAttribute('inert')

    fireEvent.click(screen.getByRole('button', { name: '展开第 1 轮' }))
    expect(screen.getByText('Old Agent')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '收起第 1 轮' }))
    expect(screen.queryByText('Old Agent')).not.toBeInTheDocument()
  })

  it('hides middleware with runtime nodes and requests every technical kind only when enabled', () => {
    useChainTrace.mockReturnValue({ state: { phase: 'ready', page }, retry: vi.fn() })
    render(<ChainTraceView threadId="thread-1" active headerTarget={document.body} onReturnToConversation={vi.fn()} />)

    const initialKinds = useChainTrace.mock.calls.at(-1)?.[0].filter.kinds
    const initialFilter = useChainTrace.mock.calls.at(-1)?.[0].filter
    expect(initialKinds).not.toContain('middleware')
    expect(initialKinds).not.toContain('run')
    expect(initialKinds).not.toContain('task')
    expect(initialKinds).not.toContain('tools')
    expect(initialFilter).not.toHaveProperty('query')
    expect(initialFilter).not.toHaveProperty('providers')
    expect(initialFilter).not.toHaveProperty('models')
    expect(screen.queryByRole('searchbox')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^提供方：/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^模型：/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', {
      name: '中间件，PromptCacheMiddleware.awrap_model_call',
    })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '技术节点' }))
    expect(useChainTrace.mock.calls.at(-1)?.[0].filter.kinds).toEqual(
      expect.arrayContaining(['run', 'task', 'tools', 'middleware']),
    )
    expect(screen.getByRole('button', { name: '工具阶段，Tools' })).toBeInTheDocument()
    expect(screen.getByRole('button', {
      name: '中间件，PromptCacheMiddleware.awrap_model_call',
    })).toBeInTheDocument()
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '类型：全部类型' }))
    fireEvent.click(screen.getByRole('option', { name: '运行时任务 · 0' }))
    fireEvent.click(screen.getByRole('button', { name: '技术节点' }))
    expect(screen.getByRole('button', { name: '类型：全部类型' })).toBeInTheDocument()
    expect(useChainTrace.mock.calls.at(-1)?.[0].filter.kinds).not.toContain('task')
    expect(useChainTrace.mock.calls.at(-1)?.[0].filter.kinds).not.toContain('middleware')
  })

  it('temporarily expands filtered results without discarding manual collapse state', () => {
    useChainTrace.mockReturnValue({ state: { phase: 'ready', page }, retry: vi.fn() })
    render(<ChainTraceView threadId="thread-1" active headerTarget={document.body} onReturnToConversation={vi.fn()} />)

    fireEvent.click(screen.getByRole('button', { name: '收起 Model' }))
    expect(screen.queryByRole('button', { name: '模型请求，deepseek-chat' })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '类型：全部类型' }))
    fireEvent.click(screen.getByRole('option', { name: '模型 · 1' }))
    expect(screen.getByRole('button', { name: '模型请求，deepseek-chat' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '收起 Model' }))
    expect(screen.queryByRole('button', { name: '模型请求，deepseek-chat' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '类型：模型 · 1' }))
    fireEvent.click(screen.getByRole('option', { name: '全部类型' }))
    expect(screen.queryByRole('button', { name: '模型请求，deepseek-chat' })).not.toBeInTheDocument()
  })

  it('falls back from a removed detail tab and restores focus when the selected entry disappears', async () => {
    useChainTrace.mockReturnValue({ state: { phase: 'ready', page }, retry: vi.fn() })
    const { container, rerender } = render(
      <ChainTraceView threadId="thread-1" active headerTarget={document.body} onReturnToConversation={vi.fn()} />,
    )
    fireEvent.click(screen.getByRole('button', { name: '模型请求，deepseek-chat' }))
    fireEvent.click(screen.getByRole('tab', { name: '系统提示词' }))

    const withoutRequest = {
      ...page,
      items: page.items.map((entry) => entry.id === 'provider-2'
        ? { ...entry, request: undefined, requestOmitted: false }
        : entry),
    }
    useChainTrace.mockReturnValue({ state: { phase: 'ready', page: withoutRequest }, retry: vi.fn() })
    rerender(<ChainTraceView threadId="thread-1" active headerTarget={document.body} onReturnToConversation={vi.fn()} />)
    expect(screen.queryByRole('tab', { name: '系统提示词' })).not.toBeInTheDocument()
    expect(screen.getByRole('tab', { name: '概述' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('tabpanel')).toHaveAccessibleName('概述')

    const withoutProvider = {
      ...page,
      items: page.items.filter((entry) => entry.id !== 'provider-2'),
    }
    useChainTrace.mockReturnValue({ state: { phase: 'ready', page: withoutProvider }, retry: vi.fn() })
    rerender(<ChainTraceView threadId="thread-1" active headerTarget={document.body} onReturnToConversation={vi.fn()} />)
    await waitFor(() => expect(screen.queryByRole('complementary', { name: '链路详情' })).not.toBeInTheDocument())
    await waitFor(() => expect(container.querySelector('.chain-trace-tree-scroll')).toHaveFocus())
  })

  it('enables narrow-screen detail isolation when the first live node follows an empty snapshot', async () => {
    const emptyPage = {
      ...page,
      turns: [],
      items: [],
      facets: {
        ...page.facets,
        kinds: {},
        statuses: {},
      },
    }
    useChainTrace.mockReturnValue({
      state: { phase: 'ready', page: emptyPage },
      retry: vi.fn(),
    })
    const { container, rerender } = render(
      <ChainTraceView
        threadId="thread-1"
        active
        headerTarget={document.body}
        onReturnToConversation={vi.fn()}
      />,
    )
    expect(container.querySelector('.chain-trace-split')).not.toBeInTheDocument()

    useChainTrace.mockReturnValue({ state: { phase: 'ready', page }, retry: vi.fn() })
    rerender(
      <ChainTraceView
        threadId="thread-1"
        active
        headerTarget={document.body}
        onReturnToConversation={vi.fn()}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: '模型请求，deepseek-chat' }))

    const details = screen.getByRole('complementary', { name: '链路详情' })
    expect(container.querySelector('.chain-trace-tree-scroll')).toHaveAttribute('inert')
    expect(within(details).getByRole('button', { name: '关闭链路详情' })).toHaveFocus()
  })

  it('isolates a failed trace region and exposes retry', () => {
    const retry = vi.fn()
    useChainTrace.mockReturnValue({ state: { phase: 'error' }, retry })
    render(<ChainTraceView threadId="thread-1" active headerTarget={document.body} onReturnToConversation={vi.fn()} />)

    expect(screen.getByRole('alert')).toHaveTextContent('链路加载失败')
    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    expect(retry).toHaveBeenCalledOnce()
  })

  it('keeps measured tree rhythm, touch targets, selected paths, and accessibility modes', () => {
    const headerControlsRule = chainTraceStyles.match(/\.chain-trace-header-controls\s*\{([^}]*)\}/s)?.[1] ?? ''
    const turnRule = chainTraceStyles.match(/\.chain-trace-turn\s*\{([^}]*)\}/s)?.[1] ?? ''
    const toggleRule = chainTraceStyles.match(/\.chain-trace-turn-toggle,\s*\.chain-trace-node-toggle\s*\{([^}]*)\}/s)?.[1] ?? ''
    const selectRule = chainTraceStyles.match(/\.chain-trace-node-select\s*\{([^}]*)\}/s)?.[1] ?? ''
    const iconLayerRule = chainTraceStyles.match(/\.chain-trace-human-icon,\s*\.chain-trace-entry-icon\s*\{\s*position:\s*relative;([^}]*)\}/s)?.[1] ?? ''
    expect(chainTraceStyles).not.toContain('.chain-trace-toolbar')
    expect(headerControlsRule).toContain('align-items: center;')
    expect(headerControlsRule).toContain('height: 100%;')
    expect(headerControlsRule).not.toMatch(/(?:^|\n)\s*border(?:-[\w-]+)?\s*:/)
    expect(chainTraceStyles).not.toContain('.chain-trace-filter')
    expect(chainTraceStyles).not.toContain('.chain-trace-technical')
    expect(toggleRule).toContain('border: 0;')
    expect(toggleRule).toContain('background: transparent;')
    expect(selectRule).toContain('border: 0;')
    expect(selectRule).toContain('background: transparent;')
    expect(iconLayerRule).toContain('z-index: var(--layer-local-raised);')
    expect(chainTraceStyles).toMatch(
      /\.chain-trace-node-list::after\s*\{[^}]*z-index:\s*var\(--layer-local\);/s,
    )
    expect(chainTraceStyles).not.toContain('.chain-trace-detail-resizer')
    expect(chainTraceStyles).not.toContain('.chain-trace-search')
    expect(turnRule).toContain('width: 100%;')
    expect(turnRule).toContain('margin: 0;')
    expect(chainTraceStyles).toContain('--chain-trace-row-height: 40px;')
    expect(chainTraceStyles).toContain('--chain-trace-disclosure-width: 24px;')
    expect(chainTraceStyles).toContain('--chain-trace-level-indent: calc(var(--chain-trace-disclosure-width) + var(--space-3) - var(--chain-trace-rail-x));')
    expect(chainTraceStyles).toContain('--chain-trace-root-indent: calc(var(--chain-trace-level-indent) + var(--space-1));')
    expect(chainTraceStyles).toContain('--chain-trace-rail-x: var(--space-1);')
    expect(chainTraceStyles).toContain('--chain-trace-icon-size: 24px;')
    expect(toggleRule).toContain('place-items: center end;')
    expect(chainTraceStyles).toMatch(/\.chain-trace-human-title strong\s*\{[^}]*font-size:\s*var\(--type-ui-size\);[^}]*line-height:\s*var\(--type-ui-line\);/s)
    expect(chainTraceStyles).toMatch(/\.chain-trace-node-title strong\s*\{[^}]*font-size:\s*var\(--type-ui-size\);[^}]*line-height:\s*var\(--type-ui-line\);/s)
    expect(chainTraceStyles).toMatch(/\.chain-trace-node-row::before\s*\{[^}]*inset:\s*0 calc\(0px - var\(--chain-trace-tree-inline-gutter\)\) 0 calc\(0px - var\(--chain-trace-row-inset\)\);[^}]*background:\s*transparent;/s)
    expect(chainTraceStyles).toMatch(/\.chain-trace-node-row::after\s*\{[^}]*left:\s*calc\(0px - var\(--chain-trace-row-inset\)\);[^}]*width:\s*2px;/s)
    expect(chainTraceStyles).toMatch(/\.chain-trace-node-row\.is-selected::before\s*\{\s*background:\s*var\(--color-brand-soft\);/s)
    expect(chainTraceStyles).not.toMatch(/\.chain-trace-node-row\s*\{[^}]*border-left:/s)
    expect(chainTraceStyles).not.toMatch(/\.chain-trace-node-row\.is-selected\s*\{[^}]*box-shadow/s)
    expect(chainTraceStyles).toMatch(/\.chain-trace-node-list\.is-root\s*\{[^}]*margin-left:\s*var\(--chain-trace-root-indent\);/s)
    expect(chainTraceStyles).toMatch(/\.chain-trace-connector-elbow\s*\{[^}]*width:\s*calc\(var\(--chain-trace-disclosure-width\) - var\(--icon-xs\) - var\(--chain-trace-rail-x\) - var\(--chain-trace-connector-gap\)\);[^}]*border-bottom-left-radius:\s*var\(--radius-lg\);/s)
    expect(chainTraceStyles).toMatch(
      /\.chain-trace-node-list\.has-selected-path::after\s*\{\s*background:\s*var\(--color-brand\);/,
    )
    expect(chainTraceStyles).toMatch(
      /@media \(any-hover: none\), \(any-pointer: coarse\)[\s\S]*--chain-trace-row-height:\s*44px;[\s\S]*--chain-trace-disclosure-width:\s*var\(--control-lg\);/,
    )
    expect(chainTraceStyles).toContain('.chain-trace-node-select:focus-visible')
    expect(chainTraceStyles).toMatch(
      /@media \(prefers-reduced-motion: reduce\)[\s\S]*\.chain-trace-node-toggle svg\s*\{\s*transition:\s*none/,
    )
    expect(chainTraceStyles).toMatch(
      /@media \(prefers-reduced-motion: reduce\)[\s\S]*\.chain-trace-node-row::before\s*\{\s*transition:\s*none;/,
    )
    expect(chainTraceStyles).toMatch(
      /@media \(forced-colors: active\)[\s\S]*\.chain-trace-node-list::before/,
    )
    expect(chainTraceStyles).not.toContain('.chain-trace-timeline')
    expect(chainTraceStyles).not.toContain('.chain-trace-ledger')
  })
})
