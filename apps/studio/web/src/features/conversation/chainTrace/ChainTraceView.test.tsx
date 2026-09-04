import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type {
  TraceGraphNode,
  TraceGraphPage,
} from '../../../api/conversation/traceGraph'
import { LocaleProvider } from '../../../i18n'
import { ChainTraceView } from './ChainTraceView'
import chainTraceStyles from './chainTrace.css?raw'

const useChainTrace = vi.hoisted(() => vi.fn())
const queryTraceGraph = vi.hoisted(() => vi.fn())
vi.mock('./useChainTrace', () => ({ useChainTrace }))
vi.mock('../../../api/conversation/traceGraph', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../../api/conversation/traceGraph')>(),
  queryTraceGraph,
}))

const node = (
  id: string,
  kind: TraceGraphNode['kind'],
  parentId: string | null,
  startedSeq: number,
  values: Partial<TraceGraphNode> = {},
): TraceGraphNode => ({
  id,
  turnId: id.startsWith('old-') ? 'turn-1' : 'turn-2',
  parentId,
  structuralParentId: parentId,
  kind,
  status: 'succeeded',
  name: kind === 'human_message'
    ? 'HumanMessage'
    : kind === 'assistant_message' ? 'AssistantMessage' : id,
  runId: id.startsWith('old-') ? 'run-1' : 'run-2',
  namespace: [],
  startedAt: '2026-08-31T00:01:00.000Z',
  completedAt: '2026-08-31T00:01:01.000Z',
  startedSeq,
  updatedSeq: startedSeq,
  contentOmitted: false,
  requestOmitted: false,
  resultOmitted: false,
  hooks: [],
  linkIssues: [],
  ...values,
})

const semanticNodes = [
  node('old-human', 'human_message', null, 1, { content: '第一轮问题' }),
  node('old-agent', 'agent', 'old-human', 2, { name: 'Old Agent' }),
  node('human-2', 'human_message', null, 9, { content: '第二轮问题' }),
  node('agent-2', 'agent', 'human-2', 10, { name: 'Agent' }),
  node('model-2', 'model', 'agent-2', 12, {
    name: 'deepseek-chat',
    provider: 'deepseek',
    model: 'deepseek-chat',
    firstOutputAt: '2026-08-31T00:01:00.200Z',
    request: {
      messages: [{
        messageType: 'system',
        content: [
          { type: 'text', text: '# 最终系统提示词' },
          { type: 'text', text: '- 附加系统规则' },
        ],
      }],
    },
    usage: {
      input_token_details: { cache_read: 4736 },
      input_tokens: 4872,
      output_token_details: { reasoning: 123 },
      output_tokens: 185,
      total_tokens: 5057,
    },
    responseMetadata: { finish_reason: 'stop' },
  }),
  node('assistant-2', 'assistant_message', 'model-2', 13, { content: '完成' }),
  node('failed-tool', 'tool', 'model-2', 14, {
    name: 'search',
    status: 'failed',
    failure: {
      errorType: 'builtins.TimeoutError',
      message: '搜索服务超时',
    },
  }),
]

const technicalNodes = [
  ...semanticNodes.slice(0, 4),
  node('middleware-2', 'middleware', 'agent-2', 11, {
    name: 'PromptCacheMiddleware.awrap_model_call',
    className: 'deepagents.middleware.PromptCacheMiddleware',
    hooks: ['awrap_model_call'],
  }),
  semanticNodes[4],
  node('system-2', 'system_message', 'model-2', 12, {
    content: '# 最终系统提示词',
  }),
  ...semanticNodes.slice(5),
]

const graphPage = (nodes: TraceGraphNode[]): TraceGraphPage => ({
  turns: [
    {
      id: 'turn-1',
      ordinal: 1,
      rootNodeId: 'old-human',
      startedAt: '2026-08-31T00:00:00.000Z',
    },
    {
      id: 'turn-2',
      ordinal: 2,
      rootNodeId: 'human-2',
      startedAt: '2026-08-31T00:01:00.000Z',
    },
  ],
  nodes,
  orderedNodeIds: nodes.map((item) => item.id),
  rootNodeIds: ['old-human', 'human-2'],
  matchedNodeIds: nodes.map((item) => item.id),
  nextCursor: null,
  asOfSeq: 20,
  facets: {
    kinds: {
      human_message: 2,
      assistant_message: 1,
      agent: 2,
      model: 1,
      tool: 1,
      middleware: 1,
      system_message: 1,
    },
    statuses: { succeeded: nodes.length - 1, failed: 1 },
    agents: {},
    middleware: { PromptCacheMiddleware: 1 },
    skills: {},
    providers: { deepseek: 1 },
    models: { 'deepseek-chat': 1 },
  },
  completeness: {
    callTrackingMissing: false,
    relationshipEvidenceMissing: false,
    detailsOmitted: false,
  },
})

const withRequestedMatches = (
  page: TraceGraphPage,
  kinds: TraceGraphNode['kind'][] | undefined,
): TraceGraphPage => ({
  ...page,
  matchedNodeIds: page.nodes
    .filter((item) => !kinds || kinds.includes(item.kind))
    .map((item) => item.id),
})

const semanticPage = {
  ...graphPage(semanticNodes),
  matchedNodeIds: semanticNodes
    .filter((item) => item.kind !== 'agent' && item.kind !== 'model')
    .map((item) => item.id),
}
const technicalPage = graphPage(technicalNodes)
const emptyUsageCases: Array<[string, NonNullable<TraceGraphNode['usage']>]> = [
  ['空对象', {}],
  ['空数组', []],
  ['空嵌套对象', { input_token_details: {} }],
]

const row = (name: string | RegExp) => screen.getByRole('button', { name })

describe('ChainTraceView', () => {
  beforeEach(() => {
    useChainTrace.mockReset()
    queryTraceGraph.mockReset()
    window.localStorage.clear()
  })

  it('starts with the timeline, selects the latest error, and keeps tree interaction complete', async () => {
    useChainTrace.mockImplementation(({ filter }) => ({
      state: {
        phase: 'ready',
        page: filter.includeTechnicalNodes ? technicalPage : semanticPage,
      },
      retry: vi.fn(),
    }))
    render(<ChainTraceView threadId="thread-1" active />)

    expect(screen.getByRole('tab', { name: '时间线' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('region', { name: '调用时间线' })).toBeVisible()
    const userRows = screen.getAllByRole('button', {
      name: /^用户，.*，查看详情$/,
    })
    expect(userRows).toHaveLength(2)
    expect(within(userRows[0]!).getByText('用户')).toBeVisible()
    expect(screen.queryByRole('button', {
      name: '技术，主智能体，查看详情',
    })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', {
      name: '技术，deepseek-chat，查看详情',
    })).not.toBeInTheDocument()
    expect(row('工具，search，查看详情')).toBeVisible()
    expect(within(row('工具，search，查看详情')).queryByText('ToolMessage'))
      .not.toBeInTheDocument()
    expect(screen.queryByText('AssistantMessage')).not.toBeInTheDocument()
    expect(document.querySelector('.chain-trace-ledger-row .chain-trace-entry-icon'))
      .not.toBeInTheDocument()
    const searchTrigger = screen.getByRole('button', { name: '搜索链路节点' })
    expect(screen.queryByRole('searchbox', { name: '搜索链路节点' }))
      .not.toBeInTheDocument()
    fireEvent.click(searchTrigger)
    const searchbox = screen.getByRole('searchbox', { name: '搜索链路节点' })
    expect(searchbox).toHaveAttribute('placeholder', '搜索节点、内容')
    expect(searchbox).toHaveAttribute('type', 'text')
    expect(screen.getAllByRole('button', { name: '清除链路搜索' })).toHaveLength(1)
    fireEvent.keyDown(searchbox, { key: 'Escape' })
    await waitFor(() => expect(searchTrigger).toHaveFocus())
    expect(screen.queryByRole('searchbox', { name: '搜索链路节点' }))
      .not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /状态：/ })).not.toBeInTheDocument()

    const details = await screen.findByRole('complementary', { name: '链路详情' })
    expect(within(details).getByText('搜索服务超时')).toBeVisible()
    fireEvent.click(within(details).getByRole('tab', { name: '计时' }))
    expect(within(details).getByText('总时长')).toBeVisible()
    fireEvent.click(within(details).getByRole('tab', { name: '结果' }))
    expect(within(details).getByText(/builtins.TimeoutError/)).toBeVisible()
    fireEvent.click(within(details).getByRole('tab', { name: '概述' }))
    fireEvent.click(screen.getByRole('button', { name: '技术' }))
    const agentType = within(row('技术，主智能体，查看详情')).getByText('技术')
    const modelType = within(row('技术，deepseek-chat，查看详情')).getByText('技术')
    expect(agentType).toHaveClass('is-category-technical')
    expect(modelType).toHaveClass('is-category-technical')
    fireEvent.click(row('技术，deepseek-chat，查看详情'))
    const modelDetails = screen.getByRole('complementary', { name: '链路详情' })
    expect(within(modelDetails).getByRole('heading', {
      name: '技术 第 2 轮 · 步骤 4',
    })).toBeVisible()
    fireEvent.click(within(modelDetails).getByRole('tab', { name: '响应' }))
    expect(within(modelDetails).getByText('完成')).toBeVisible()
    expect(within(modelDetails).getByText('完成').closest('.markdown-content'))
      .toHaveClass('markdown-content--compact')
    expect(within(modelDetails).getByText(/"id": "assistant-2"/)).toBeVisible()
    expect(within(modelDetails).getByText(/"name": "search"/)).toBeVisible()
    expect(within(modelDetails).getByText(/"input_tokens": 4872/)).toBeVisible()
    expect(within(modelDetails).getByText(/"finish_reason": "stop"/)).toBeVisible()
    expect(within(modelDetails).queryByText(/"result":/)).not.toBeInTheDocument()
    expect(within(modelDetails).queryByText(/"error":/)).not.toBeInTheDocument()
    expect(within(modelDetails).queryByRole('tab', { name: '响应元数据' }))
      .not.toBeInTheDocument()
    fireEvent.click(within(modelDetails).getByRole('tab', { name: '请求' }))
    expect(within(modelDetails).getByText(/"messageType": "system"/)).toBeVisible()
    fireEvent.click(within(modelDetails).getByRole('tab', { name: '用量' }))
    expect(within(modelDetails).getByRole('heading', { name: 'Token 用量' })).toBeVisible()
    expect(within(modelDetails).getByText('输入 Tokens')).toBeVisible()
    expect(within(modelDetails).getByText('4,872')).toBeVisible()
    expect(within(modelDetails).getByText('缓存读取 Tokens')).toBeVisible()
    expect(within(modelDetails).getByText('4,736')).toBeVisible()
    expect(within(modelDetails).getByText('推理 Tokens')).toBeVisible()
    expect(within(modelDetails).getByText('123')).toBeVisible()
    expect(within(modelDetails).queryByText('input_token_details')).not.toBeInTheDocument()
    expect(within(modelDetails).queryByText(/cache_read/)).not.toBeInTheDocument()
    fireEvent.click(within(modelDetails).getByRole('tab', { name: '计时' }))
    expect(within(modelDetails).getByText('首 token 延迟')).toBeVisible()
    fireEvent.click(within(modelDetails).getByRole('tab', { name: '系统提示词' }))
    expect(within(modelDetails).getByRole('heading', { name: '最终系统提示词' })).toBeVisible()

    const modelButton = row('技术，deepseek-chat，查看详情')
    fireEvent.click(within(modelDetails).getByRole('button', { name: '关闭链路详情' }))
    await waitFor(() => expect(screen.queryByRole('complementary', { name: '链路详情' }))
      .not.toBeInTheDocument())
    await waitFor(() => expect(modelButton).toHaveFocus())
    fireEvent.click(modelButton)

    fireEvent.click(screen.getByRole('tab', { name: '树形' }))
    expect(window.localStorage.getItem('tinkerfin.chain-trace.view')).toBe('tree')
    expect(screen.queryByText('Old Agent')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '收起 技术，deepseek-chat' }))
    expect(screen.queryByRole('button', {
      name: '助手，完成，查看详情',
    })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '展开 技术，deepseek-chat' }))
    const treeModelButton = row('技术，deepseek-chat，查看详情')
    fireEvent.click(treeModelButton)
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(treeModelButton).toHaveFocus())
    expect(screen.queryByRole('button', { name: '返回对话' })).not.toBeInTheDocument()
  })

  it('requests technical nodes from the server and renders only returned facts', () => {
    useChainTrace.mockImplementation(({ filter }) => ({
      state: {
        phase: 'ready',
        page: filter.includeTechnicalNodes ? technicalPage : semanticPage,
      },
      retry: vi.fn(),
    }))
    render(<ChainTraceView threadId="thread-1" active />)

    expect(useChainTrace.mock.calls.at(-1)?.[0]).toMatchObject({
      limit: 1000,
      filter: {
        kinds: [
          'human_message',
          'interaction',
          'assistant_message',
          'subagent',
          'tool',
          'skill',
          'memory',
          'guardrail',
          'retrieval',
          'custom',
          'plan',
        ],
        includeTechnicalNodes: false,
        includeAncestorNodes: true,
      },
    })
    expect(screen.queryByRole('button', {
      name: '技术，PromptCacheMiddleware.awrap_model_call，查看详情',
    })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '技术' }))
    expect(useChainTrace.mock.calls.at(-1)?.[0].filter.includeTechnicalNodes).toBe(true)
    expect(row('技术，PromptCacheMiddleware.awrap_model_call，查看详情')).toBeVisible()
    expect(row('技术，# 最终系统提示词，查看详情')).toBeVisible()
    expect(within(row('技术，# 最终系统提示词，查看详情')).getByText('技术'))
      .toHaveClass('is-category-technical')
    fireEvent.click(screen.getByRole('button', { name: '用户' }))
    fireEvent.click(screen.getByRole('button', { name: '助手' }))
    fireEvent.click(screen.getByRole('button', { name: '工具' }))
    expect(useChainTrace.mock.calls.at(-1)?.[0].filter.kinds).toEqual([
      'skill',
      'memory',
      'guardrail',
      'retrieval',
      'custom',
      'plan',
      'agent',
      'model',
      'system_message',
      'middleware',
      'run',
      'runtime_task',
    ])
    const context = screen.getByRole('button', { name: '上下文' })
    expect(context).toHaveAttribute('aria-pressed', 'true')
    expect(context).toBeDisabled()
  })

  it('restores focus to the same node after its original view has unmounted', async () => {
    useChainTrace.mockImplementation(({ filter }) => ({
      state: {
        phase: 'ready',
        page: filter.includeTechnicalNodes ? technicalPage : semanticPage,
      },
      retry: vi.fn(),
    }))
    render(<ChainTraceView threadId="thread-1" active />)

    fireEvent.click(screen.getByRole('button', { name: '技术' }))
    fireEvent.click(row('技术，deepseek-chat，查看详情'))
    fireEvent.click(screen.getByRole('tab', { name: '树形' }))
    const treeModel = row('技术，deepseek-chat，查看详情')
    fireEvent.click(screen.getByRole('button', { name: '关闭链路详情' }))
    await waitFor(() => expect(treeModel).toHaveFocus())

    fireEvent.click(treeModel)
    fireEvent.click(screen.getByRole('tab', { name: '时间线' }))
    const ledgerModel = row('技术，deepseek-chat，查看详情')
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(ledgerModel).toHaveFocus())
  })

  it('keeps same-category path ancestors out of result counts and the ledger', () => {
    const resultNodes = [
      node('human-2', 'human_message', null, 9, { content: '搜索起点' }),
      node('context-parent', 'custom', 'human-2', 10, { name: '同类祖先' }),
      node('context-match', 'custom', 'context-parent', 11, { name: '直接命中' }),
    ]
    useChainTrace.mockReturnValue({
      state: {
        phase: 'ready',
        page: {
          ...graphPage(resultNodes),
          matchedNodeIds: ['context-match'],
        },
      },
      retry: vi.fn(),
    })

    const { container } = render(<ChainTraceView threadId="thread-1" active />)

    expect(container.querySelectorAll('.chain-trace-ledger-row')).toHaveLength(1)
    expect(row('上下文，直接命中，查看详情')).toBeVisible()
    expect(screen.queryByText('同类祖先')).not.toBeInTheDocument()
    expect(screen.getByText((_, element) => element?.textContent === '1 节点')).toBeVisible()
  })

  it('uses hidden ancestors for tree structure without exposing them as public nodes', () => {
    window.localStorage.setItem('tinkerfin.chain-trace.view', 'tree')
    useChainTrace.mockImplementation(({ filter }) => ({
      state: {
        phase: 'ready',
        page: filter.includeTechnicalNodes ? technicalPage : semanticPage,
      },
      retry: vi.fn(),
    }))
    render(<ChainTraceView threadId="thread-1" active />)

    expect(screen.queryByRole('button', {
      name: '技术，主智能体，查看详情',
    })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', {
      name: '技术，deepseek-chat，查看详情',
    })).not.toBeInTheDocument()
    const collapse = screen.getByRole('button', { name: '收起 用户，第二轮问题' })
    expect(screen.getByRole('button', {
      name: '助手，完成，查看详情',
    })).toBeVisible()
    fireEvent.click(collapse)
    expect(screen.queryByRole('button', {
      name: '助手，完成，查看详情',
    })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '展开 用户，第二轮问题' }))
    expect(screen.getByRole('button', {
      name: '工具，search，查看详情',
    })).toBeVisible()
  })

  it('reuses live Model children when the main query still includes both response kinds', () => {
    useChainTrace.mockImplementation(({ filter }) => ({
      state: {
        phase: 'ready',
        page: filter.includeTechnicalNodes ? technicalPage : semanticPage,
      },
      retry: vi.fn(),
    }))
    render(<ChainTraceView threadId="thread-1" active />)

    fireEvent.click(screen.getByRole('button', { name: '技术' }))
    fireEvent.click(screen.getByRole('button', { name: '上下文' }))
    fireEvent.click(row('技术，deepseek-chat，查看详情'))

    expect(useChainTrace.mock.calls.at(-1)?.[0].filter.kinds).toEqual([
      'human_message',
      'interaction',
      'assistant_message',
      'subagent',
      'tool',
      'agent',
      'model',
      'system_message',
      'middleware',
      'run',
      'runtime_task',
    ])

    expect(queryTraceGraph).not.toHaveBeenCalled()
    const details = screen.getByRole('complementary', { name: '链路详情' })
    fireEvent.click(within(details).getByRole('tab', { name: '响应' }))
    expect(within(details).getByText('完成')).toBeVisible()
    expect(within(details).getByText(/"name": "search"/)).toBeVisible()
  })

  it('refreshes a direct Model response when a hidden child advances the Graph revision', async () => {
    let currentPage = semanticPage
    const responseNodes = semanticNodes.slice(5)
    queryTraceGraph
      .mockResolvedValueOnce({
        ...graphPage([]),
        turns: [],
        rootNodeIds: [],
      })
      .mockResolvedValueOnce({
        ...graphPage(responseNodes),
        turns: [semanticPage.turns[1]],
        rootNodeIds: responseNodes.map((item) => item.id),
      })
    useChainTrace.mockImplementation(({ filter }) => ({
      state: {
        phase: 'ready',
        page: withRequestedMatches(currentPage, filter.kinds),
      },
      retry: vi.fn(),
    }))
    const rendered = render(<ChainTraceView threadId="thread-1" active />)

    fireEvent.click(screen.getByRole('button', { name: '技术' }))
    fireEvent.click(screen.getByRole('button', { name: '助手' }))
    fireEvent.click(row('技术，deepseek-chat，查看详情'))
    await waitFor(() => expect(queryTraceGraph).toHaveBeenCalledTimes(1))

    currentPage = { ...semanticPage, asOfSeq: semanticPage.asOfSeq + 20 }
    rendered.rerender(<ChainTraceView threadId="thread-1" active />)

    await waitFor(() => expect(queryTraceGraph).toHaveBeenCalledTimes(2))
    const details = screen.getByRole('complementary', { name: '链路详情' })
    fireEvent.click(within(details).getByRole('tab', { name: '响应' }))
    expect(await within(details).findByText('完成')).toBeVisible()
  })

  it('re-queries Model details when the main graph omitted details', async () => {
    queryTraceGraph.mockResolvedValue(graphPage(semanticNodes.slice(5)))
    useChainTrace.mockImplementation(({ filter }) => ({
      state: {
        phase: 'ready',
        page: withRequestedMatches({
          ...semanticPage,
          completeness: {
            ...semanticPage.completeness,
            detailsOmitted: true,
          },
        }, filter.kinds),
      },
      retry: vi.fn(),
    }))
    render(<ChainTraceView threadId="thread-1" active />)

    fireEvent.click(screen.getByRole('button', { name: '技术' }))
    fireEvent.click(row('技术，deepseek-chat，查看详情'))
    const details = screen.getByRole('complementary', { name: '链路详情' })
    fireEvent.click(within(details).getByRole('tab', { name: '响应' }))

    expect(await within(details).findByText('完成')).toBeVisible()
    expect(queryTraceGraph).toHaveBeenCalledWith(
      'thread-1',
      expect.objectContaining({ parentId: 'model-2' }),
      expect.objectContaining({ limit: 1000, signal: expect.any(AbortSignal) }),
    )
  })

  it.each(emptyUsageCases)('does not expose a usage tab for %s without scalar values', (_, usage) => {
    useChainTrace.mockImplementation(({ filter }) => ({
      state: {
        phase: 'ready',
        page: withRequestedMatches(
          graphPage(semanticNodes.map((item) => (
            item.id === 'model-2' ? { ...item, usage } : item
          ))),
          filter.kinds,
        ),
      },
      retry: vi.fn(),
    }))
    render(<ChainTraceView threadId="thread-1" active />)

    fireEvent.click(screen.getByRole('button', { name: '技术' }))
    fireEvent.click(row('技术，deepseek-chat，查看详情'))
    const details = screen.getByRole('complementary', { name: '链路详情' })
    expect(within(details).queryByRole('tab', { name: '用量' })).not.toBeInTheDocument()
    expect(within(details).queryByText('input_token_details')).not.toBeInTheDocument()
  })

  it('uses the correct English turn unit for singular and plural ranges', () => {
    window.localStorage.setItem('tinkerfin:language', 'en')
    useChainTrace.mockReturnValue({
      state: {
        phase: 'ready',
        page: {
          ...graphPage(semanticNodes.slice(2)),
          turns: [semanticPage.turns[1]],
          rootNodeIds: ['human-2'],
        },
      },
      retry: vi.fn(),
    })
    const rendered = render(
      <LocaleProvider><ChainTraceView threadId="thread-1" active /></LocaleProvider>,
    )

    expect(screen.getByText((_, element) => element?.textContent === '1 turn')).toBeVisible()
    rendered.unmount()
    useChainTrace.mockReturnValue({
      state: { phase: 'ready', page: semanticPage },
      retry: vi.fn(),
    })
    render(<LocaleProvider><ChainTraceView threadId="thread-1" active /></LocaleProvider>)

    expect(screen.getByText((_, element) => element?.textContent === '2 turns')).toBeVisible()
  })

  it('does not present a partial page as a complete timeline', () => {
    useChainTrace.mockReturnValue({
      state: { phase: 'ready', page: { ...semanticPage, nextCursor: 'older-page' } },
      retry: vi.fn(),
    })
    render(<ChainTraceView threadId="thread-1" active />)

    expect(screen.getByRole('status')).toHaveTextContent(
      '链路超过完整视图上限，请使用筛选或搜索缩小范围',
    )
    expect(screen.queryByRole('region', { name: '调用时间线' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '搜索链路节点' })).toBeVisible()
  })

  it('keeps the filter toolbar available during loading and with no context matches', () => {
    useChainTrace.mockReturnValue({ state: { phase: 'loading' }, retry: vi.fn() })
    const rendered = render(<ChainTraceView threadId="thread-1" active />)

    expect(screen.getByLabelText('链路筛选')).toBeVisible()
    expect(screen.getByRole('status')).toHaveTextContent('正在加载链路')
    for (const label of ['用户', '助手', '工具', '上下文']) {
      expect(screen.getByRole('button', { name: label })).toHaveAttribute('aria-pressed', 'true')
    }
    expect(screen.queryByRole('button', { name: /全部类型/ })).not.toBeInTheDocument()

    rendered.unmount()
    useChainTrace.mockImplementation(({ filter }) => ({
      state: {
        phase: 'ready',
        page: filter.kinds?.length === 6
          ? graphPage([])
          : semanticPage,
      },
      retry: vi.fn(),
    }))
    render(<ChainTraceView threadId="thread-1" active />)
    fireEvent.click(screen.getByRole('button', { name: '用户' }))
    fireEvent.click(screen.getByRole('button', { name: '助手' }))
    fireEvent.click(screen.getByRole('button', { name: '工具' }))

    expect(screen.getByLabelText('链路筛选')).toBeVisible()
    expect(screen.getByText('没有匹配的链路节点')).toBeVisible()
    expect(screen.getByRole('button', { name: '上下文' })).toBeDisabled()
    expect(useChainTrace.mock.calls.at(-1)?.[0].filter.kinds).toEqual([
      'skill',
      'memory',
      'guardrail',
      'retrieval',
      'custom',
      'plan',
    ])
  })

  it('renders the complete one-thousand-node boundary without changing the query limit', () => {
    const nodes = Array.from({ length: 1000 }, (_, index) => node(
      `boundary-${index}`,
      index === 0 ? 'human_message' : 'assistant_message',
      index === 0 ? null : 'boundary-0',
      index + 1,
      {
        turnId: 'turn-boundary',
        runId: 'run-boundary',
        startedAt: new Date(Date.parse('2026-08-31T00:00:00.000Z') + index).toISOString(),
        completedAt: new Date(Date.parse('2026-08-31T00:00:00.001Z') + index).toISOString(),
      },
    ))
    const boundaryPage: TraceGraphPage = {
      ...graphPage(nodes),
      turns: [{
        id: 'turn-boundary',
        ordinal: 1,
        rootNodeId: 'boundary-0',
        startedAt: nodes[0]!.startedAt,
      }],
      rootNodeIds: ['boundary-0'],
      nextCursor: null,
    }
    useChainTrace.mockReturnValue({
      state: { phase: 'ready', page: boundaryPage },
      retry: vi.fn(),
    })

    const { container } = render(<ChainTraceView threadId="thread-boundary" active />)

    expect(container.querySelectorAll('.chain-trace-ledger-row')).toHaveLength(1000)
    expect(useChainTrace.mock.calls.at(-1)?.[0].limit).toBe(1000)
  })

  it('isolates a failed trace region and exposes retry', () => {
    const retry = vi.fn()
    useChainTrace.mockReturnValue({ state: { phase: 'error' }, retry })
    render(<ChainTraceView threadId="thread-1" active />)

    expect(screen.getByRole('alert')).toHaveTextContent('链路加载失败')
    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    expect(retry).toHaveBeenCalledOnce()
  })

  it('keeps trace controls borderless while providing owned layout and scroll states', () => {
    const toggleRule = chainTraceStyles.match(/\.chain-trace-node-toggle\s*\{([^}]*)\}/s)?.[1] ?? ''
    expect(chainTraceStyles).toContain('.chain-trace-toolbar')
    expect(chainTraceStyles).not.toContain('.chain-trace-back')
    expect(chainTraceStyles).not.toContain('.chain-trace-split')
    expect(toggleRule).toContain('border: 0;')
    expect(toggleRule).toContain('background: transparent;')
    expect(toggleRule).toContain('left: calc(0px - var(--chain-trace-row-inset));')
    expect(chainTraceStyles).toMatch(/\.chain-trace-node-select\s*\{[^}]*border:\s*0;[^}]*background:\s*transparent;/s)
    expect(chainTraceStyles).toContain('--chain-trace-row-height: var(--control-lg);')
    expect(chainTraceStyles).toContain('--chain-trace-ledger-header-height: var(--control-composer);')
    expect(chainTraceStyles).toContain('--chain-trace-turn-height: calc(var(--control-xs) - var(--space-1));')
    expect(chainTraceStyles).not.toContain('--chain-trace-detail-width')
    expect(chainTraceStyles).toContain('--chain-trace-root-indent: 0px;')
    expect(chainTraceStyles).toContain('--chain-trace-tree-inline-gutter: var(--space-1);')
    expect(chainTraceStyles).toContain('padding: 0 calc(var(--space-6) + var(--space-1));')
    expect(chainTraceStyles).toMatch(/\.chain-trace-node-select\s*\{[^}]*padding:\s*0 var\(--space-6\) 0 0;/s)
    expect(chainTraceStyles).toContain('grid-template-columns: 36px 68px minmax(280px, 1fr) 74px;')
    expect(chainTraceStyles).toMatch(/\.chain-trace-ledger-header > :last-child\s*\{[^}]*justify-self:\s*end;[^}]*text-align:\s*right;/s)
    expect(chainTraceStyles).toMatch(/\.chain-trace-search-control > \.ui-icon-button-wrap\s*\{[^}]*position:\s*absolute;[^}]*inset:\s*0;/s)
    expect(chainTraceStyles).toMatch(/\.chain-trace-search\s*\{[^}]*overflow:\s*hidden;/s)
    expect(chainTraceStyles).toContain('grid-template-columns: minmax(0, 1fr) var(--layout-drawer-width);')
    expect(chainTraceStyles).toContain('.chain-trace-category-filters')
    expect(chainTraceStyles).not.toContain('.chain-trace-ledger-row::after')
    expect(chainTraceStyles).not.toContain('.chain-trace-node-row::after')
    expect(chainTraceStyles).toContain('.chain-trace-node-row.is-selected::before { background: var(--color-brand-soft); }')
    expect(chainTraceStyles).toContain('.chain-trace-tree-host')
    expect(chainTraceStyles).toContain('.chain-trace-content-grid.has-details:not(.uses-overlay)')
    expect(chainTraceStyles).toMatch(
      /@media \(prefers-reduced-motion: reduce\)[\s\S]*\.chain-trace-node-toggle svg[^}]*transition: none;/,
    )
    expect(chainTraceStyles).toMatch(
      /@media \(any-hover: none\), \(any-pointer: coarse\)[\s\S]*\.chain-trace-timeline\s*\{ display: none;/,
    )
  })
})
