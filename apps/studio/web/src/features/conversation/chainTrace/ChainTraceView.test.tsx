import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type {
  TraceGraphNode,
  TraceGraphPage,
} from '../../../api/conversation/traceGraph'
import { ChainTraceView } from './ChainTraceView'
import chainTraceStyles from './chainTrace.css?raw'

const useChainTrace = vi.hoisted(() => vi.fn())
vi.mock('./useChainTrace', () => ({ useChainTrace }))

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
    usage: { input_tokens: 10, output_tokens: 2 },
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

const semanticPage = graphPage(semanticNodes)
const technicalPage = graphPage(technicalNodes)

describe('ChainTraceView', () => {
  it('renders the authoritative Human-first tree, collapses nodes, and opens details', async () => {
    useChainTrace.mockReturnValue({
      state: { phase: 'ready', page: semanticPage },
      retry: vi.fn(),
    })
    const onReturnToConversation = vi.fn()
    render(
      <ChainTraceView
        threadId="thread-1"
        active
        headerTarget={document.body}
        onReturnToConversation={onReturnToConversation}
      />,
    )

    const treeButtons = screen.getAllByRole('button').filter((button) => (
      button.hasAttribute('data-trace-node-id')
    ))
    expect(treeButtons[0]).toHaveAccessibleName('用户消息，HumanMessage')
    expect(screen.queryByText('Old Agent')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '智能体调用，主智能体' })).toBeVisible()
    expect(screen.queryByText('LangGraph')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '模型调用，deepseek-chat' })).toBeVisible()
    expect(screen.getByRole('button', { name: '工具，ToolMessage' })).toBeVisible()
    expect(screen.getByText('搜索服务超时')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: '返回对话' }))
    expect(onReturnToConversation).toHaveBeenCalledOnce()

    fireEvent.click(screen.getByRole('button', { name: '收起 deepseek-chat' }))
    expect(screen.queryByRole('button', { name: '助手消息，AssistantMessage' }))
      .not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '展开 deepseek-chat' }))

    const modelButton = screen.getByRole('button', { name: '模型调用，deepseek-chat' })
    fireEvent.click(modelButton)
    const details = screen.getByRole('complementary', { name: '链路详情' })
    expect(within(details).getByText('deepseek')).toBeVisible()
    fireEvent.click(within(details).getByRole('tab', { name: '响应' }))
    expect(within(details).getByText(/"content": "完成"/)).toBeVisible()
    expect(within(details).getByText(/"name": "search"/)).toBeVisible()
    expect(within(details).getByText(/"input_tokens": 10/)).toBeVisible()
    expect(within(details).getByText(/"finish_reason": "stop"/)).toBeVisible()
    expect(within(details).queryByText(/"result":/)).not.toBeInTheDocument()
    expect(within(details).queryByText(/"error":/)).not.toBeInTheDocument()
    expect(within(details).queryByRole('tab', { name: '响应元数据' }))
      .not.toBeInTheDocument()
    fireEvent.click(within(details).getByRole('tab', { name: '系统提示词' }))
    expect(within(details).getByRole('heading', { name: '最终系统提示词' })).toBeVisible()
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(modelButton).toHaveFocus())
  })

  it('requests technical nodes from the server and renders them without local promotion', () => {
    useChainTrace.mockImplementation(({ filter }) => ({
      state: {
        phase: 'ready',
        page: filter.includeTechnicalNodes ? technicalPage : semanticPage,
      },
      retry: vi.fn(),
    }))
    render(
      <ChainTraceView
        threadId="thread-1"
        active
        headerTarget={document.body}
        onReturnToConversation={vi.fn()}
      />,
    )

    expect(useChainTrace.mock.calls.at(-1)?.[0].filter).toMatchObject({
      includeTechnicalNodes: false,
      includeAncestorNodes: true,
    })
    expect(screen.queryByRole('button', {
      name: '中间件，PromptCacheMiddleware.awrap_model_call',
    })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '技术节点' }))
    expect(useChainTrace.mock.calls.at(-1)?.[0].filter.includeTechnicalNodes).toBe(true)
    expect(screen.getByRole('button', {
      name: '中间件，PromptCacheMiddleware.awrap_model_call',
    })).toBeVisible()
    expect(screen.getByRole('button', { name: '系统消息，system-2' })).toBeVisible()
  })

  it('opens older fixed pages and can return to the live page', () => {
    useChainTrace.mockImplementation(({ cursor }) => ({
      state: {
        phase: 'ready',
        page: cursor
          ? semanticPage
          : { ...semanticPage, nextCursor: 'older-page' },
      },
      retry: vi.fn(),
    }))
    render(
      <ChainTraceView
        threadId="thread-1"
        active
        headerTarget={document.body}
        onReturnToConversation={vi.fn()}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: '查看较早节点' }))
    expect(useChainTrace.mock.calls.at(-1)?.[0].cursor).toBe('older-page')
    fireEvent.click(screen.getByRole('button', { name: '返回最新节点' }))
    expect(useChainTrace.mock.calls.at(-1)?.[0].cursor).toBeNull()
  })

  it('isolates a failed trace region and exposes retry', () => {
    const retry = vi.fn()
    useChainTrace.mockReturnValue({ state: { phase: 'error' }, retry })
    render(
      <ChainTraceView
        threadId="thread-1"
        active
        headerTarget={document.body}
        onReturnToConversation={vi.fn()}
      />,
    )
    expect(screen.getByRole('alert')).toHaveTextContent('链路加载失败')
    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    expect(retry).toHaveBeenCalledOnce()
  })

  it('keeps the shared tree controls borderless and accessible', () => {
    const toggleRule = chainTraceStyles.match(/\.chain-trace-node-toggle\s*\{([^}]*)\}/s)?.[1] ?? ''
    const selectRule = chainTraceStyles.match(/\.chain-trace-node-select\s*\{([^}]*)\}/s)?.[1] ?? ''
    expect(chainTraceStyles).not.toContain('.chain-trace-toolbar')
    expect(toggleRule).toContain('border: 0;')
    expect(toggleRule).toContain('background: transparent;')
    expect(selectRule).toContain('border: 0;')
    expect(selectRule).toContain('background: transparent;')
    expect(chainTraceStyles).toContain('--chain-trace-row-height: 40px;')
    expect(chainTraceStyles).toContain('--chain-trace-icon-size: 24px;')
    expect(chainTraceStyles).toContain('--chain-trace-root-indent: 0px;')
    expect(chainTraceStyles).not.toMatch(
      /@media \(any-hover: none\), \(any-pointer: coarse\)[\s\S]*--chain-trace-root-indent:/,
    )
    expect(chainTraceStyles).toContain('.chain-trace-node-select:focus-visible')
    expect(chainTraceStyles).toMatch(
      /@media \(prefers-reduced-motion: reduce\)[\s\S]*\.chain-trace-node-toggle svg\s*\{\s*transition:\s*none/,
    )
    expect(chainTraceStyles).toMatch(
      /@media \(any-hover: none\), \(any-pointer: coarse\)[\s\S]*--chain-trace-row-height:\s*44px;/,
    )
  })
})
