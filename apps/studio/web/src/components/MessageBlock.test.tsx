import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import type { Message } from '../types'
import '../styles/global.css'
import { MessageBlock } from './MessageBlock'

const subagentMessage: Message = {
  id: 'subagent-run-researcher-1',
  role: 'subagent',
  content: '',
  createdAt: '2026-08-06T02:30:45.000Z',
  meta: {
    agentName: 'researcher',
    input: '访问两个 URL 并总结业务',
    result: '公司定位：中国最大的搜索引擎和 AI 科技公司。',
    reasoning: '这段内部思考不应出现在子智能体卡片中',
    status: 'completed',
    subRunId: 'sub-run-researcher-1',
    runId: 'sub-run-researcher-1',
    parentRunId: 'main-run-1',
    durationMs: 4210,
  },
}

const childTool: Message = {
  id: 'tool-read-file-1',
  role: 'tool',
  content: '',
  createdAt: '2026-08-06T02:30:46.000Z',
  meta: {
    toolName: 'read_file',
    sourceAgentName: 'researcher',
    params: '{"file_path":"/research/url.json"}',
    result: 'https://www.baidu.com',
    status: 'completed',
    runId: 'sub-run-researcher-1',
    durationMs: 1210,
  },
}

const secondChildTool: Message = {
  ...childTool,
  id: 'tool-web-search-2',
  createdAt: '2026-08-06T02:30:47.000Z',
  meta: {
    ...childTool.meta,
    toolName: 'web_search',
    params: '{"query":"百度主营业务"}',
    result: '百度提供搜索与人工智能服务。',
  },
}

describe('MessageBlock subagent card', () => {
  it('matches the tool-card header height when collapsed and expanded', () => {
    const { container } = render(
      <>
        <MessageBlock message={subagentMessage} childTools={[childTool]} />
        <MessageBlock message={childTool} />
      </>,
    )

    const subagentCard = container.querySelector<HTMLDetailsElement>('.subagent-card')
    const toolCard = container.querySelector<HTMLDetailsElement>('.tool-card')
    const subagentHeader = subagentCard?.querySelector<HTMLElement>(':scope > summary')
    const toolHeader = toolCard?.querySelector<HTMLElement>(':scope > summary')

    expect(getComputedStyle(subagentHeader!).minHeight).toBe('44px')
    expect(getComputedStyle(toolHeader!).minHeight).toBe('44px')

    subagentCard!.open = true
    toolCard!.open = true

    expect(getComputedStyle(subagentHeader!).minHeight).toBe('44px')
    expect(getComputedStyle(toolHeader!).minHeight).toBe('44px')
  })

  it('groups child tools in a collapsed card without exposing reasoning', async () => {
    const user = userEvent.setup()
    const { container } = render(
      <MessageBlock message={subagentMessage} childTools={[childTool]} />,
    )

    const card = container.querySelector<HTMLDetailsElement>('.subagent-card')
    expect(card).not.toBeNull()
    expect(card?.open).toBe(false)
    expect(screen.getByText('researcher')).toBeVisible()
    expect(screen.getByText('1 个工具')).toBeVisible()
    expect(screen.queryByText('已完成')).not.toBeInTheDocument()
    expect(screen.getAllByLabelText('已完成')).toHaveLength(2)
    expect(screen.queryByText('这段内部思考不应出现在子智能体卡片中')).not.toBeInTheDocument()

    await user.click(screen.getByText('researcher'))

    expect(card?.open).toBe(true)
    expect(screen.getByText('访问两个 URL 并总结业务')).toBeVisible()
    expect(screen.getByText('公司定位：中国最大的搜索引擎和 AI 科技公司。')).toBeVisible()
    expect(screen.getByText('read_file')).toBeVisible()
    expect(screen.getAllByRole('heading', { level: 4 }).map((heading) => heading.textContent)).toEqual([
      '输入',
      '工具轨迹 1',
      '输出摘要',
    ])

    const expandAllButton = screen.getByRole('button', { name: '展开全部详情' })
    expect(expandAllButton.querySelector('svg')).toBeNull()
    await user.click(expandAllButton)

    const toolDetails = container.querySelector<HTMLDetailsElement>('.subagent-tool-row')
    expect(toolDetails?.querySelector('summary')).toHaveTextContent('read_file')
    expect(toolDetails?.querySelector('summary')).not.toHaveTextContent('researcher')
    expect(toolDetails?.open).toBe(true)
    expect(screen.getByText('{"file_path":"/research/url.json"}')).toBeVisible()
    expect(screen.getByText('https://www.baidu.com')).toBeVisible()

    fireEvent.click(screen.getByRole('button', { name: '收起全部详情' }))
    expect(toolDetails?.open).toBe(false)
  })

  it('never renders legacy process or assistant reasoning content', () => {
    const processMessage: Message = {
      id: 'legacy-process',
      role: 'process',
      content: '旧版思考过程',
      createdAt: '2026-08-06T02:30:40.000Z',
    }
    const assistantMessage: Message = {
      id: 'assistant-with-reasoning',
      role: 'assistant',
      content: '这是最终回答',
      createdAt: '2026-08-06T02:30:50.000Z',
      meta: { reasoning: '不应展示的模型推理', status: 'completed' },
    }

    const { rerender } = render(<MessageBlock message={processMessage} />)
    expect(screen.queryByText('旧版思考过程')).not.toBeInTheDocument()

    rerender(<MessageBlock message={assistantMessage} />)
    expect(screen.getByText('这是最终回答')).toBeVisible()
    expect(screen.queryByText('不应展示的模型推理')).not.toBeInTheDocument()
    expect(screen.queryByText('思考过程')).not.toBeInTheDocument()
  })

  it('shows only the tool name in a standalone tool header', () => {
    const { container } = render(<MessageBlock message={childTool} />)
    const summary = container.querySelector('.tool-card > summary')

    expect(summary).toHaveTextContent('read_file')
    expect(summary).not.toHaveTextContent('researcher')
  })

  it('explains that a paused tool has not executed yet', () => {
    render(
      <MessageBlock
        message={{
          ...childTool,
          id: 'tool-write-file-paused',
          meta: {
            ...childTool.meta,
            toolName: 'write_file',
            result: '',
            status: 'paused',
          },
        }}
      />,
    )

    expect(screen.getByText('等待审批')).toBeInTheDocument()
    expect(screen.getByText('等待审批后执行')).toBeInTheDocument()
  })

  it('switches to collapse-all after every child tool is opened individually', async () => {
    const user = userEvent.setup()
    const { container } = render(
      <MessageBlock message={subagentMessage} childTools={[childTool, secondChildTool]} />,
    )

    await user.click(screen.getByText('researcher'))
    const toolRows = Array.from(container.querySelectorAll<HTMLDetailsElement>('.subagent-tool-row'))

    await user.click(toolRows[0].querySelector('summary')!)
    expect(screen.getByRole('button', { name: '展开全部详情' })).toHaveAttribute('aria-expanded', 'false')

    await user.click(toolRows[1].querySelector('summary')!)
    expect(screen.getByRole('button', { name: '收起全部详情' })).toHaveAttribute('aria-expanded', 'true')

    await user.click(screen.getByRole('button', { name: '收起全部详情' }))
    expect(toolRows.every((row) => !row.open)).toBe(true)
    expect(screen.getByRole('button', { name: '展开全部详情' })).toHaveAttribute('aria-expanded', 'false')
  })
})
