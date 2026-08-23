import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import type { WorkspaceState } from '../../../types'
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
      mode: 'default',
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
      mode: 'default',
      messages: [],
      todos: [],
      plan: null,
      runStatus: 'idle',
    },
  ],
}

const baseProps = {
  workspace,
  mode: 'expanded' as const,
  settledMode: 'expanded' as const,
  overlayOpen: false,
  wideInteractive: true,
  railInteractive: false,
  onToggleMode: vi.fn(),
  onRequestExpanded: vi.fn(),
  onCloseOverlay: vi.fn(),
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

    const historyList = screen.getByRole('region', { name: '历史对话' })
    const historyHeading = screen.getByText('历史对话').parentElement
    const historyRegion = historyList.parentElement
    const historyToolbar = historyHeading?.parentElement
    const pinnedItem = screen.getByRole('button', { name: '打开会话：置顶会话' }).closest('.conversation-item')
    expect(historyRegion).toHaveClass('conversation-history')
    expect(historyToolbar).toHaveClass('history-toolbar')
    expect(historyToolbar?.parentElement).toBe(historyRegion)
    expect(historyList.previousElementSibling).toBe(historyToolbar)
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

    const historyList = screen.getByRole('region', { name: '历史对话' })
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
    act(() => trigger.focus())

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

    const scroll = screen.getByRole('region', { name: '历史对话' })
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

describe('Sidebar rail and inline search', () => {
  it('removes a closed mobile overlay from the accessibility tree and focus path', () => {
    render(
      <Sidebar
        {...baseProps}
        mode="overlay"
        settledMode="overlay"
        overlayOpen={false}
        wideInteractive={false}
        railInteractive={false}
      />,
    )

    const sidebar = screen.getByLabelText('会话导航', { selector: 'aside' })
    expect(sidebar).toHaveAttribute('aria-hidden', 'true')
    expect(sidebar).toHaveAttribute('inert')
    expect(screen.queryByRole('button', { name: '新聊天' })).not.toBeInTheDocument()
  })

  it('moves focus into an opened mobile overlay and Escape restores the opener path', async () => {
    const onCloseOverlay = vi.fn()
    render(
      <Sidebar
        {...baseProps}
        mode="overlay"
        settledMode="overlay"
        overlayOpen
        wideInteractive
        railInteractive={false}
        onCloseOverlay={onCloseOverlay}
      />,
    )

    await waitFor(() => expect(screen.getByRole('button', { name: '关闭导航' })).toHaveFocus())
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onCloseOverlay).toHaveBeenCalledOnce()
  })

  it('keeps only rail controls accessible and expands search with one action', async () => {
    const user = userEvent.setup()
    const onRequestExpanded = vi.fn()
    render(
      <Sidebar
        {...baseProps}
        mode="rail"
        settledMode="rail"
        wideInteractive={false}
        railInteractive
        onRequestExpanded={onRequestExpanded}
      />,
    )

    expect(screen.getByRole('button', { name: '打开侧边栏' })).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByRole('link', { name: 'TinkerFin 首页' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '搜索会话' }))
    expect(onRequestExpanded).toHaveBeenCalledOnce()
  })

  it('filters loaded titles and lets Escape clear, close and restore focus', async () => {
    const user = userEvent.setup()
    render(<Sidebar {...baseProps} />)

    const trigger = screen.getByRole('button', { name: '搜索会话' })
    await user.click(trigger)
    const input = screen.getByRole('textbox', { name: '搜索会话' })
    expect(trigger).toHaveAttribute('aria-expanded', 'true')

    fireEvent.pointerDown(document.body)
    expect(trigger).toHaveAttribute('aria-expanded', 'false')
    await user.click(trigger)
    await user.type(input, '置顶')
    expect(screen.getByRole('button', { name: '打开会话：置顶会话' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '打开会话：最近会话' })).not.toBeInTheDocument()

    fireEvent.pointerDown(document.body)
    expect(trigger).toHaveAttribute('aria-expanded', 'true')
    expect(input).not.toHaveFocus()
    await user.click(input)

    await user.keyboard('{Escape}')
    expect(trigger).toHaveAttribute('aria-expanded', 'false')
    await waitFor(() => expect(trigger).toHaveFocus())
    expect(screen.getByRole('button', { name: '打开会话：最近会话' })).toBeInTheDocument()

    await user.click(trigger)
    await user.type(input, '置顶')
    await user.click(screen.getByRole('button', { name: '清除搜索' }))
    expect(trigger).toHaveAttribute('aria-expanded', 'false')
    expect(input).toHaveValue('')
  })

  it('preserves a non-empty query across rail and expanded presentation changes', async () => {
    const user = userEvent.setup()
    const { rerender } = render(<Sidebar {...baseProps} />)
    await user.click(screen.getByRole('button', { name: '搜索会话' }))
    await user.type(screen.getByRole('textbox', { name: '搜索会话' }), '置顶')

    rerender(
      <Sidebar {...baseProps} mode="rail" settledMode="rail" wideInteractive={false} railInteractive />,
    )
    expect(screen.getByRole('button', { name: '搜索会话，当前查询：置顶' })).toBeInTheDocument()

    rerender(<Sidebar {...baseProps} />)
    expect(screen.getByRole('textbox', { name: '搜索会话' })).toHaveValue('置顶')
    expect(screen.queryByRole('button', { name: '打开会话：最近会话' })).not.toBeInTheDocument()
  })

  it('focuses the input only after a rail expansion settles', async () => {
    const user = userEvent.setup()
    const { rerender } = render(
      <Sidebar {...baseProps} mode="rail" settledMode="rail" wideInteractive={false} railInteractive />,
    )
    await user.click(screen.getByRole('button', { name: '搜索会话' }))

    rerender(
      <Sidebar {...baseProps} mode="expanded" settledMode="rail" wideInteractive railInteractive={false} />,
    )
    const input = screen.getByRole('textbox', { name: '搜索会话' })
    expect(input).not.toHaveFocus()

    rerender(<Sidebar {...baseProps} />)
    await waitFor(() => expect(input).toHaveFocus())
  })
})
