import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import type { WorkspaceState } from '../types'
import { Sidebar } from './Sidebar'

const workspace: WorkspaceState = {
  currentThreadId: 'recent',
  conversations: [
    {
      threadId: 'pinned',
      title: '置顶会话',
      pinned: true,
      updatedAt: '2026-08-08T08:00:00Z',
      model: 'GPT-5.5',
      messages: [],
      todos: [],
      plan: null,
      runStatus: 'idle',
    },
    {
      threadId: 'recent',
      title: '最近会话',
      pinned: false,
      updatedAt: '2026-08-08T09:00:00Z',
      model: 'GPT-5.5',
      messages: [],
      todos: [],
      plan: null,
      runStatus: 'idle',
    },
  ],
}

const baseProps = {
  workspace,
  isOpen: true,
  onClose: vi.fn(),
  onNew: vi.fn(),
  onSelect: vi.fn(),
  onPin: vi.fn(),
  onRename: vi.fn(),
  onDelete: vi.fn(),
  hasMore: true,
  onLoadMore: vi.fn(),
  user: {
    user_id: 7,
    username: 'yunsan',
    display_name: '云杉',
    roles: [],
    disabled: false,
  },
  onLogout: vi.fn(),
}

describe('Sidebar', () => {
  it('renders the authenticated display name and performs real logout', () => {
    const onLogout = vi.fn()
    render(<Sidebar {...baseProps} onLogout={onLogout} />)

    expect(screen.getByText('云杉')).toBeInTheDocument()
    expect(screen.queryByText('Yunsan')).not.toBeInTheDocument()
    expect(screen.queryByText('Pro 工作区')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '打开用户菜单' }))
    fireEvent.click(screen.getByRole('button', { name: '退出登录' }))

    expect(onLogout).toHaveBeenCalledOnce()
  })

  it('用户菜单展开时显示向上箭头并同步触发器语义', () => {
    render(<Sidebar {...baseProps} />)

    const trigger = screen.getByRole('button', { name: '打开用户菜单' })
    expect(trigger).toHaveAttribute('aria-expanded', 'false')
    expect(trigger.querySelector('.lucide-chevron-down')).toBeInTheDocument()

    fireEvent.click(trigger)

    const expandedTrigger = screen.getByRole('button', { name: '关闭用户菜单' })
    expect(expandedTrigger).toHaveAttribute('aria-expanded', 'true')
    expect(expandedTrigger.querySelector('.lucide-chevron-up')).toBeInTheDocument()
    expect(expandedTrigger.querySelector('.lucide-chevron-down')).not.toBeInTheDocument()
  })

  it('用户菜单只在容器外部的指针操作后收起', () => {
    render(<Sidebar {...baseProps} />)

    fireEvent.click(screen.getByRole('button', { name: '打开用户菜单' }))
    fireEvent.pointerDown(screen.getByRole('button', { name: '个人设置' }))
    expect(screen.getByRole('button', { name: '退出登录' })).toBeInTheDocument()

    fireEvent.pointerDown(screen.getByRole('navigation', { name: '工作区功能' }))

    expect(screen.queryByRole('button', { name: '退出登录' })).not.toBeInTheDocument()
    const collapsedTrigger = screen.getByRole('button', { name: '打开用户菜单' })
    expect(collapsedTrigger).toHaveAttribute('aria-expanded', 'false')
    expect(collapsedTrigger.querySelector('.lucide-chevron-down')).toBeInTheDocument()
    expect(collapsedTrigger.querySelector('.lucide-chevron-up')).not.toBeInTheDocument()
  })

  it('用户菜单通过 Escape 收起并把焦点还给触发器', () => {
    render(<Sidebar {...baseProps} />)

    fireEvent.click(screen.getByRole('button', { name: '打开用户菜单' }))
    fireEvent.keyDown(document, { key: 'Escape' })

    const trigger = screen.getByRole('button', { name: '打开用户菜单' })
    expect(screen.queryByRole('button', { name: '退出登录' })).not.toBeInTheDocument()
    expect(trigger).toHaveFocus()
  })

  it('falls back to the username when the display name is empty', () => {
    render(<Sidebar {...baseProps} user={{ ...baseProps.user, display_name: '' }} />)

    expect(screen.getByText('yunsan')).toBeInTheDocument()
  })

  it('renders the product navigation labels with matching icons', () => {
    render(<Sidebar {...baseProps} />)

    expect(screen.getByRole('button', { name: '技能库' }).querySelector('.lucide-book-open-check')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '智能体' }).querySelector('.lucide-workflow')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '工作区' }).querySelector('.lucide-panels-top-left')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'MCP管理' }).querySelector('.lucide-cable')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '智能体' })).toHaveAttribute('aria-current', 'page')
    for (const label of ['技能库', '工作区', 'MCP管理', '更多']) {
      expect(screen.getByRole('button', { name: label })).toBeDisabled()
    }

    fireEvent.click(screen.getByRole('button', { name: '打开用户菜单' }))
    expect(screen.getByRole('button', { name: '个人设置' })).toBeDisabled()
  })

  it('renders one fixed history heading and marks pinned conversations inline', () => {
    render(<Sidebar {...baseProps} />)

    const historyList = screen.getByLabelText('历史会话列表')
    const historyHeading = screen.getByText('历史对话').parentElement
    const historyRegion = historyList.parentElement
    const pinnedItem = screen.getByRole('button', { name: '打开会话：置顶会话' }).closest('.conversation-item')
    expect(historyRegion).toHaveClass('conversation-history')
    expect(historyHeading?.parentElement).toBe(historyRegion)
    expect(historyList.previousElementSibling).toBe(historyHeading)
    expect(screen.queryByText('已置顶')).not.toBeInTheDocument()
    expect(screen.queryByText('最近')).not.toBeInTheDocument()
    expect(pinnedItem).toHaveClass('is-pinned')
    expect(pinnedItem?.querySelector('.conversation-pinned-indicator')).toBeInTheDocument()
    const recentButton = screen.getByRole('button', { name: '打开会话：最近会话' })
    expect(recentButton).not.toHaveAttribute('title')
    expect(recentButton.closest('.conversation-item')).toHaveClass('is-recent')
    expect(recentButton.closest('.conversation-item')).toHaveClass('overflow-marquee-trigger')
    expect(recentButton.querySelector('.conversation-title-marquee')).toHaveClass('overflow-marquee')
    expect(historyList.querySelector('.conversation-item')).toBe(pinnedItem)
  })

  it('opens the existing management menu from a pinned conversation', () => {
    render(<Sidebar {...baseProps} />)

    const historyList = screen.getByLabelText('历史会话列表')
    historyList.scrollTop = 48
    fireEvent.click(screen.getByRole('button', { name: '管理会话：置顶会话' }))

    expect(historyList).toHaveClass('is-scroll-locked')
    historyList.scrollTop = 96
    fireEvent.scroll(historyList)
    expect(historyList).toHaveProperty('scrollTop', 48)
    expect(screen.getByRole('button', { name: /取消置顶/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /重命名/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /删除/ })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /取消置顶/ }))
    expect(historyList).not.toHaveClass('is-scroll-locked')
    historyList.scrollTop = 96
    fireEvent.scroll(historyList)
    expect(historyList).toHaveProperty('scrollTop', 96)
  })

  it('opens the conversation menu into the keyboard path and restores focus on Escape', async () => {
    const user = userEvent.setup()
    render(<Sidebar {...baseProps} />)
    const trigger = screen.getByRole('button', { name: '管理会话：置顶会话' })
    trigger.focus()

    await user.keyboard('{Enter}')

    expect(screen.getByRole('button', { name: '取消置顶' })).toHaveFocus()
    await user.tab()
    expect(screen.getByRole('button', { name: '重命名' })).toHaveFocus()
    await user.keyboard('{Escape}')

    expect(screen.queryByRole('button', { name: '重命名' })).not.toBeInTheDocument()
    expect(trigger).toHaveFocus()
  })

  it('renders exactly five loading skeleton rows', () => {
    render(<Sidebar {...baseProps} isLoadingMore />)

    expect(screen.getAllByTestId('history-skeleton')).toHaveLength(5)
  })

  it('renders a retry tail and suppresses scroll retries until explicitly requested', () => {
    const onLoadMore = vi.fn()
    const onRetryLoadMore = vi.fn()
    render(
      <Sidebar
        {...baseProps}
        onLoadMore={onLoadMore}
        loadMoreError="加载历史失败"
        onRetryLoadMore={onRetryLoadMore}
      />,
    )

    const scroll = screen.getByLabelText('历史会话列表')
    Object.defineProperties(scroll, {
      scrollTop: { configurable: true, value: 100 },
      clientHeight: { configurable: true, value: 200 },
      scrollHeight: { configurable: true, value: 300 },
    })
    fireEvent.scroll(scroll)
    expect(onLoadMore).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: '重试加载历史' }))
    expect(onRetryLoadMore).toHaveBeenCalledOnce()
  })
})
