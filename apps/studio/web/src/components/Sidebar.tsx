import {
  BookOpenCheck,
  Cable,
  ChevronDown,
  ChevronUp,
  CircleEllipsis,
  Ellipsis,
  LogOut,
  PanelsTopLeft,
  Pencil,
  Pin,
  PinOff,
  Search,
  Settings2,
  SquarePen,
  Trash2,
  UserRound,
  Workflow,
  X,
} from 'lucide-react'
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { MouseEvent, Ref } from 'react'

import type { AuthUser } from '../api/auth/types'
import type { Conversation, WorkspaceState } from '../types'
import { BrandMark } from './BrandMark'
import { OverflowMarquee } from './OverflowMarquee'

export function ConversationItem({
  conversation,
  isActive,
  isMenuOpen,
  onSelect,
  onToggleMenu,
}: {
  conversation: Conversation
  isActive: boolean
  isMenuOpen: boolean
  onSelect: () => void
  onToggleMenu?: (event: MouseEvent<HTMLButtonElement>) => void
}) {
  return (
    <div
      className={`conversation-item overflow-marquee-trigger ${conversation.pinned ? 'is-pinned' : 'is-recent'} ${isActive ? 'is-active' : ''}`}
    >
      <button className="conversation-main" aria-label={`打开会话：${conversation.title}`} onClick={onSelect}>
        <OverflowMarquee className="conversation-title-marquee">{`${conversation.title}\u200b`}</OverflowMarquee>
      </button>
      {conversation.pinned && (
        <span className="conversation-pinned-indicator" role="img" aria-label="已置顶">
          <Pin size={15} />
        </span>
      )}
      {onToggleMenu && (
        <button
          className="icon-button conversation-more"
          aria-label={`管理会话：${conversation.title}`}
          aria-expanded={isMenuOpen}
          onClick={onToggleMenu}
        >
          <Ellipsis size={16} />
        </button>
      )}
    </div>
  )
}

export function Sidebar({
  workspace,
  isOpen,
  onClose,
  onNew,
  onSelect,
  onPin,
  onRename,
  onDelete,
  hasMore,
  onLoadMore,
  isLoadingMore = false,
  loadMoreError,
  onRetryLoadMore,
  loadMoreSentinelRef,
  user,
  onLogout,
}: {
  workspace: WorkspaceState
  isOpen: boolean
  onClose: () => void
  onNew: () => void
  onSelect: (threadId: string) => void
  onPin: (threadId: string) => void
  onRename: (threadId: string, restoreFocusTo?: HTMLElement | null) => void
  onDelete: (threadId: string, restoreFocusTo?: HTMLElement | null) => void
  hasMore: boolean
  onLoadMore: () => void
  isLoadingMore?: boolean
  loadMoreError?: string
  onRetryLoadMore?: () => void
  loadMoreSentinelRef?: Ref<HTMLDivElement>
  user: AuthUser
  onLogout: () => void
}) {
  const [query, setQuery] = useState('')
  const [isSearchOpen, setSearchOpen] = useState(false)
  const [userMenu, setUserMenu] = useState(false)
  const [openMenu, setOpenMenu] = useState<{
    threadId: string
    top: number
    left: number
    trigger: HTMLButtonElement
  } | null>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const userMenuRef = useRef<HTMLDivElement>(null)
  const userMenuButtonRef = useRef<HTMLButtonElement>(null)
  const historyScrollRef = useRef<HTMLDivElement>(null)
  const lockedHistoryScrollTop = useRef(0)
  const visible = useMemo(() => {
    return workspace.conversations
      .filter((item) => item.title.toLowerCase().includes(query.toLowerCase()))
      .sort((a, b) => Date.parse(b.updatedAt) - Date.parse(a.updatedAt))
  }, [query, workspace.conversations])
  const pinned = visible.filter((item) => item.pinned)
  const recent = visible.filter((item) => !item.pinned)
  const ordered = [...pinned, ...recent]
  const menuConversation = openMenu ? workspace.conversations.find((item) => item.threadId === openMenu.threadId) : undefined

  const closeMenu = (restoreFocus = false) => {
    if (restoreFocus) openMenu?.trigger.focus()
    setOpenMenu(null)
  }
  const toggleMenu = (conversation: Conversation, anchor: HTMLButtonElement) => {
    const rect = anchor.getBoundingClientRect()
    setOpenMenu((current) => {
      if (current?.threadId === conversation.threadId) return null
      lockedHistoryScrollTop.current = historyScrollRef.current?.scrollTop ?? 0
      return {
        threadId: conversation.threadId,
        top: Math.min(rect.bottom + 4, window.innerHeight - 150),
        left: Math.max(8, rect.right - 132),
        trigger: anchor,
      }
    })
  }

  useEffect(() => {
    if (!openMenu) return
    const handleOutsidePointerDown = (event: PointerEvent) => {
      const target = event.target
      if (target instanceof Element && (menuRef.current?.contains(target) || target.closest('.conversation-more'))) return
      setOpenMenu(null)
    }
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      event.preventDefault()
      openMenu.trigger.focus()
      setOpenMenu(null)
    }
    document.addEventListener('pointerdown', handleOutsidePointerDown)
    document.addEventListener('keydown', handleKeyDown)
    return () => {
      document.removeEventListener('pointerdown', handleOutsidePointerDown)
      document.removeEventListener('keydown', handleKeyDown)
    }
  }, [openMenu])

  useLayoutEffect(() => {
    if (!openMenu) return
    menuRef.current?.querySelector<HTMLButtonElement>('button:not([disabled])')?.focus()
  }, [openMenu])

  useEffect(() => {
    if (!userMenu) return

    const handleOutsidePointerDown = (event: PointerEvent) => {
      const target = event.target
      if (target instanceof Node && userMenuRef.current?.contains(target)) return
      setUserMenu(false)
    }
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      event.preventDefault()
      setUserMenu(false)
      userMenuButtonRef.current?.focus()
    }

    document.addEventListener('pointerdown', handleOutsidePointerDown)
    document.addEventListener('keydown', handleKeyDown)
    return () => {
      document.removeEventListener('pointerdown', handleOutsidePointerDown)
      document.removeEventListener('keydown', handleKeyDown)
    }
  }, [userMenu])

  const renderItems = (items: Conversation[]) =>
    items.map((conversation) => (
      <ConversationItem
        key={conversation.threadId}
        conversation={conversation}
        isActive={conversation.threadId === workspace.currentThreadId}
        isMenuOpen={openMenu?.threadId === conversation.threadId}
        onSelect={() => {
          closeMenu()
          if (conversation.threadId) onSelect(conversation.threadId)
          onClose()
        }}
        onToggleMenu={conversation.threadId
          ? (event) => toggleMenu(conversation, event.currentTarget)
          : undefined}
      />
    ))

  return (
    <>
      {isOpen && <button className="sidebar-scrim" aria-label="关闭导航" onClick={onClose} />}
      <aside className={`sidebar ${isOpen ? 'is-open' : ''}`} aria-label="会话导航">
        <div className="sidebar-head">
          <a className="brand" href="#top" aria-label="TinkerFin 首页">
            <BrandMark />
            <span>TinkerFin  <small> Plus</small></span>
          </a>
          <div className="sidebar-head-actions">
            <button className="icon-button" aria-label="搜索会话" onClick={() => setSearchOpen((value) => !value)}><Search size={18} /></button>
            <button className="icon-button mobile-only" aria-label="关闭导航" onClick={onClose}><X size={18} /></button>
          </div>
        </div>

        <button className="new-chat" aria-label="新聊天" onClick={onNew}>
          <SquarePen size={19} />
          <span>新聊天</span>
        </button>

        {isSearchOpen && (
          <label className="search-box">
            <Search size={16} />
            <input aria-label="搜索会话输入" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索对话" autoFocus />
          </label>
        )}

        <nav className="primary-nav" aria-label="工作区功能">
          <button aria-current="page"><Workflow size={19} />智能体</button>
          <button disabled><BookOpenCheck size={19} />技能库</button>
          <button disabled><PanelsTopLeft size={19} />工作区</button>
          <button disabled><Cable size={19} />MCP管理</button>
          <button disabled><CircleEllipsis size={19} />更多</button>
        </nav>

        <div className="conversation-history">
          <div id="conversation-history-title" className="group-title"><span>历史对话</span></div>
          <div
            ref={historyScrollRef}
            className={`conversation-scroll ${openMenu ? 'is-scroll-locked' : ''}`}
            aria-label="历史会话列表"
            aria-labelledby="conversation-history-title"
            onScroll={(event) => {
              const el = event.currentTarget
              if (openMenu) {
                el.scrollTop = lockedHistoryScrollTop.current
                return
              }
              if (!hasMore || isLoadingMore || loadMoreError) return
              if (el.scrollTop + el.clientHeight >= el.scrollHeight - 64) onLoadMore()
            }}
          >
            <section className="conversation-group">
              {renderItems(ordered)}
              {visible.length === 0 && <p className="no-search-result">{query ? '没有匹配的对话' : '暂无历史对话'}</p>}
            </section>
            {(hasMore || isLoadingMore || loadMoreError) && (
              <div ref={loadMoreSentinelRef} className="history-load-more-tail">
                {isLoadingMore && (
                  <div className="history-skeleton-list" aria-label="正在加载更多历史会话">
                    {Array.from({ length: 5 }, (_, index) => (
                      <div key={index} className="history-skeleton" data-testid="history-skeleton" aria-hidden="true">
                        <span />
                      </div>
                    ))}
                  </div>
                )}
                {loadMoreError && !isLoadingMore && (
                  <div className="history-load-error" role="alert">
                    <span>{loadMoreError}</span>
                    <button type="button" aria-label="重试加载历史" onClick={onRetryLoadMore ?? onLoadMore}>重试</button>
                  </div>
                )}
              </div>
            )}
          </div>
        </div>

        <div ref={userMenuRef} className="user-wrap">
          {userMenu && (
            <div className="user-menu">
              <button disabled><Settings2 size={16} />个人设置</button>
              <button onClick={onLogout}><LogOut size={16} />退出登录</button>
            </div>
          )}
          <button ref={userMenuButtonRef} className="user-card" aria-label={userMenu ? '关闭用户菜单' : '打开用户菜单'} aria-expanded={userMenu} onClick={() => setUserMenu((value) => !value)}>
            <span className="avatar"><UserRound size={17} /></span>
            <span className="user-copy"><strong>{user.display_name.trim() || user.username}</strong><small>@{user.username}</small></span>
            {userMenu ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
          </button>
        </div>
        {menuConversation && openMenu && (
          <div ref={menuRef} className="conversation-menu conversation-menu-floating" style={{ top: openMenu.top, left: openMenu.left }}>
            <button onClick={() => { onPin(menuConversation.threadId); closeMenu(true) }}>
              {menuConversation.pinned ? <PinOff size={15} /> : <Pin size={15} />}
              {menuConversation.pinned ? '取消置顶' : '置顶'}
            </button>
            <button onClick={() => { onRename(menuConversation.threadId, openMenu.trigger); closeMenu() }}><Pencil size={15} />重命名</button>
            <button className="danger" onClick={() => { onDelete(menuConversation.threadId, openMenu.trigger); closeMenu() }}><Trash2 size={15} />删除</button>
          </div>
        )}
      </aside>
    </>
  )
}
