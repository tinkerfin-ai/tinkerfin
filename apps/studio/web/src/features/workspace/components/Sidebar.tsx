import {
  BookOpenCheck,
  Cable,
  ChevronDown,
  ChevronUp,
  CircleEllipsis,
  Ellipsis,
  LogOut,
  PanelLeftClose,
  PanelLeftOpen,
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
import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import type { MouseEvent, Ref } from 'react'

import type { AuthUser } from '../../../api/auth/types'
import { BrandMark } from '../../../components/ui/BrandMark'
import { Button, IconButton } from '../../../components/ui'
import type { Conversation, WorkspaceState } from '../../../types'
import type { SidebarMode } from '../useWorkspaceNavigation'
import { OverflowMarquee } from './OverflowMarquee'

const SCROLLBAR_HIDE_DELAY_MS = 2000

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
      <button type="button" className="conversation-main" aria-label={`打开会话：${conversation.title}`} onClick={onSelect}>
        <OverflowMarquee className="conversation-title-marquee">{`${conversation.title}\u200b`}</OverflowMarquee>
      </button>
      {conversation.pinned && (
        <span className="conversation-pinned-indicator" role="img" aria-label="已置顶">
          <Pin size={15} />
        </span>
      )}
      {onToggleMenu && (
        <IconButton
          className="conversation-more"
          label={`管理会话：${conversation.title}`}
          icon={<Ellipsis size={16} />}
          selected={isMenuOpen}
          aria-expanded={isMenuOpen}
          onClick={onToggleMenu}
        />
      )}
    </div>
  )
}

export interface SidebarProps {
  workspace: WorkspaceState
  mode: SidebarMode
  settledMode: SidebarMode
  overlayOpen: boolean
  wideInteractive: boolean
  railInteractive: boolean
  onToggleMode: () => void
  onRequestExpanded: () => void
  onCloseOverlay: (restoreFocus?: boolean) => void
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
  backgroundInert?: boolean
}

export function Sidebar({
  workspace,
  mode,
  settledMode,
  overlayOpen,
  wideInteractive,
  railInteractive,
  onToggleMode,
  onRequestExpanded,
  onCloseOverlay,
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
  backgroundInert = false,
}: SidebarProps) {
  const [query, setQuery] = useState('')
  const [isSearchOpen, setSearchOpen] = useState(false)
  const [userMenu, setUserMenu] = useState(false)
  const [scrollbarVisible, setScrollbarVisible] = useState(false)
  const [openMenu, setOpenMenu] = useState<{
    threadId: string
    top: number
    left: number
    trigger: HTMLButtonElement
  } | null>(null)
  const rootRef = useRef<HTMLElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const userMenuRef = useRef<HTMLDivElement>(null)
  const userMenuButtonRef = useRef<HTMLButtonElement>(null)
  const historyScrollRef = useRef<HTMLDivElement>(null)
  const searchInputRef = useRef<HTMLInputElement>(null)
  const searchTriggerRef = useRef<HTMLButtonElement>(null)
  const overlayCloseButtonRef = useRef<HTMLButtonElement>(null)
  const pendingSearchFocusRef = useRef(false)
  const lockedHistoryScrollTop = useRef(0)
  const scrollbarTimerRef = useRef<number | null>(null)
  const normalizedQuery = query.trim().toLocaleLowerCase('zh-CN')
  const visible = useMemo(() => {
    return workspace.conversations
      .filter((item) => item.title.toLocaleLowerCase('zh-CN').includes(normalizedQuery))
      .sort((a, b) => Date.parse(b.updatedAt) - Date.parse(a.updatedAt))
  }, [normalizedQuery, workspace.conversations])
  const ordered = [
    ...visible.filter((item) => item.pinned),
    ...visible.filter((item) => !item.pinned),
  ]
  const menuConversation = openMenu
    ? workspace.conversations.find((item) => item.threadId === openMenu.threadId)
    : undefined
  const isOverlayHidden = mode === 'overlay' && !overlayOpen

  const clearScrollbarTimer = () => {
    if (scrollbarTimerRef.current == null) return
    window.clearTimeout(scrollbarTimerRef.current)
    scrollbarTimerRef.current = null
  }

  const showScrollbar = () => {
    clearScrollbarTimer()
    setScrollbarVisible(true)
  }

  const delayHideScrollbar = () => {
    clearScrollbarTimer()
    scrollbarTimerRef.current = window.setTimeout(() => {
      setScrollbarVisible(false)
      scrollbarTimerRef.current = null
    }, SCROLLBAR_HIDE_DELAY_MS)
  }

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
        top: Math.min(rect.bottom + 4, window.innerHeight - 164),
        left: Math.max(8, rect.right - 144),
        trigger: anchor,
      }
    })
  }

  const focusSearch = () => {
    window.requestAnimationFrame(() => searchInputRef.current?.focus())
  }

  const openSearch = (fromRail = false) => {
    setSearchOpen(true)
    if (fromRail) {
      pendingSearchFocusRef.current = true
      onRequestExpanded()
    } else {
      focusSearch()
    }
  }

  const clearAndCloseSearch = (restoreFocus = true) => {
    setQuery('')
    setSearchOpen(false)
    pendingSearchFocusRef.current = false
    if (restoreFocus) window.requestAnimationFrame(() => searchTriggerRef.current?.focus())
  }

  useEffect(() => {
    if (
      pendingSearchFocusRef.current
      && isSearchOpen
      && settledMode === 'expanded'
    ) {
      pendingSearchFocusRef.current = false
      focusSearch()
    }
  }, [isSearchOpen, settledMode])

  useEffect(() => {
    if (!isSearchOpen) return
    const handleOutsidePointerDown = (event: PointerEvent) => {
      const target = event.target
      if (target instanceof Node && rootRef.current?.contains(target)) return
      if (query.trim()) {
        searchInputRef.current?.blur()
      } else {
        setSearchOpen(false)
      }
    }
    document.addEventListener('pointerdown', handleOutsidePointerDown)
    return () => document.removeEventListener('pointerdown', handleOutsidePointerDown)
  }, [isSearchOpen, query])

  useEffect(() => {
    if (wideInteractive) return
    setOpenMenu(null)
    setUserMenu(false)
  }, [wideInteractive])

  useEffect(() => {
    if (mode !== 'overlay' || !overlayOpen || !wideInteractive) return
    window.requestAnimationFrame(() => overlayCloseButtonRef.current?.focus())
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      event.preventDefault()
      onCloseOverlay()
    }
    document.addEventListener('keydown', handleEscape)
    return () => document.removeEventListener('keydown', handleEscape)
  }, [mode, onCloseOverlay, overlayOpen, wideInteractive])

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

  useEffect(() => () => clearScrollbarTimer(), [])

  const selectConversation = (threadId: string) => {
    closeMenu()
    onSelect(threadId)
    if (mode === 'overlay') onCloseOverlay(false)
  }

  const renderItems = (items: Conversation[]) => items.map((conversation) => (
    <ConversationItem
      key={conversation.threadId}
      conversation={conversation}
      isActive={conversation.threadId === workspace.currentThreadId}
      isMenuOpen={openMenu?.threadId === conversation.threadId}
      onSelect={() => selectConversation(conversation.threadId)}
      onToggleMenu={conversation.threadId
        ? (event) => toggleMenu(conversation, event.currentTarget)
        : undefined}
    />
  ))

  return (
    <>
      {mode === 'overlay' && overlayOpen && (
        <button type="button" className="sidebar-scrim" aria-label="关闭导航遮罩" onClick={() => onCloseOverlay()} />
      )}
      <aside
        ref={rootRef}
        id="workspace-sidebar"
        className={`workspace-sidebar is-${mode}${overlayOpen ? ' is-overlay-open' : ''}`}
        data-sidebar-mode={mode}
        aria-label="会话导航"
        aria-hidden={isOverlayHidden || backgroundInert || undefined}
        inert={isOverlayHidden || backgroundInert || undefined}
      >
        <div
          className="sidebar-wide"
          aria-hidden={!wideInteractive || undefined}
          inert={!wideInteractive || undefined}
        >
          <div className="sidebar-head">
            <a className="brand" href="#top" aria-label="TinkerFin 首页">
              <BrandMark size={24} />
              <span>TinkerFin <small>Plus</small></span>
            </a>
            <div className="sidebar-head-actions">
              {mode === 'overlay' ? (
                <IconButton ref={overlayCloseButtonRef} label="关闭导航" icon={<X size={18} />} onClick={() => onCloseOverlay()} />
              ) : (
                <IconButton
                  label="收起侧边栏"
                  tooltip="收起侧边栏"
                  icon={<PanelLeftClose size={16} />}
                  aria-controls="workspace-sidebar"
                  aria-expanded="true"
                  onClick={onToggleMode}
                />
              )}
            </div>
          </div>

          <Button className="new-chat" variant="ghost" leadingIcon={<SquarePen size={18} />} onClick={onNew}>新聊天</Button>

          <nav className="primary-nav" aria-label="工作区功能">
            <Button variant="ghost" selected leadingIcon={<Workflow size={18} />} aria-current="page">智能体</Button>
            <Button variant="ghost" leadingIcon={<BookOpenCheck size={18} />} disabled>技能库</Button>
            <Button variant="ghost" leadingIcon={<PanelsTopLeft size={18} />} disabled>工作区</Button>
            <Button variant="ghost" leadingIcon={<Cable size={18} />} disabled>MCP管理</Button>
            <Button variant="ghost" leadingIcon={<CircleEllipsis size={18} />} disabled>更多</Button>
          </nav>

          <div className="conversation-history">
            <div className={`history-toolbar${isSearchOpen ? ' is-search-open' : ''}`}>
              <div id="conversation-history-title" className="group-title"><span>历史对话</span></div>
              <div className="history-toolbar-actions">
                <IconButton
                  ref={searchTriggerRef}
                  label="搜索会话"
                  icon={<Search size={18} />}
                  selected={isSearchOpen || Boolean(query)}
                  aria-expanded={isSearchOpen}
                  aria-controls="sidebar-search"
                  onClick={() => openSearch(false)}
                />
              </div>
              <label id="sidebar-search" className="sidebar-search">
                <Search size={16} aria-hidden="true" />
                <input
                  ref={searchInputRef}
                  aria-label="搜索会话"
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key !== 'Escape') return
                    event.preventDefault()
                    clearAndCloseSearch()
                  }}
                  placeholder="搜索对话"
                />
                <IconButton label="清除搜索" icon={<X size={16} />} onClick={() => clearAndCloseSearch()} />
              </label>
            </div>

            <div
              ref={historyScrollRef}
              className={`conversation-scroll${openMenu ? ' is-scroll-locked' : ''}${scrollbarVisible ? ' is-scrollbar-visible' : ''}`}
              role="region"
              aria-labelledby="conversation-history-title"
              onPointerEnter={showScrollbar}
              onPointerLeave={delayHideScrollbar}
              onFocus={showScrollbar}
              onScroll={(event) => {
                showScrollbar()
                const element = event.currentTarget
                if (openMenu) {
                  element.scrollTop = lockedHistoryScrollTop.current
                  return
                }
                if (!hasMore || isLoadingMore || loadMoreError) return
                if (element.scrollTop + element.clientHeight >= element.scrollHeight - 64) onLoadMore()
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
                        <div key={index} className="history-skeleton" data-testid="history-skeleton" aria-hidden="true"><span /></div>
                      ))}
                    </div>
                  )}
                  {loadMoreError && !isLoadingMore && (
                    <div className="history-load-error" role="alert">
                      <span>{loadMoreError}</span>
                      <Button size="sm" aria-label="重试加载历史" onClick={onRetryLoadMore ?? onLoadMore}>重试</Button>
                    </div>
                  )}
                </div>
              )}
            </div>
          </div>

          <div ref={userMenuRef} className="user-wrap">
            {userMenu && (
              <div className="user-menu">
                <Button variant="ghost" leadingIcon={<Settings2 size={16} />} disabled>个人设置</Button>
                <Button variant="ghost" leadingIcon={<LogOut size={16} />} onClick={onLogout}>退出登录</Button>
              </div>
            )}
            <button
              ref={userMenuButtonRef}
              type="button"
              className="user-card"
              aria-label={userMenu ? '关闭用户菜单' : '打开用户菜单'}
              aria-expanded={userMenu}
              onClick={() => setUserMenu((value) => !value)}
            >
              <span className="avatar"><UserRound size={17} /></span>
              <span className="user-copy"><strong>{user.display_name.trim() || user.username}</strong><small>@{user.username}</small></span>
              {userMenu ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
            </button>
          </div>
        </div>

        <div
          className="sidebar-rail"
          aria-hidden={!railInteractive || undefined}
          inert={!railInteractive || undefined}
        >
          <IconButton
            className="rail-brand-toggle"
            label="打开侧边栏"
            tooltip="打开侧边栏"
            icon={<><BrandMark size={24} className="rail-brand-mark" /><PanelLeftOpen className="rail-panel-icon" size={18} /></>}
            aria-controls="workspace-sidebar"
            aria-expanded="false"
            onClick={onToggleMode}
          />
          <IconButton label="新聊天" tooltip="新聊天" icon={<SquarePen size={18} />} onClick={onNew} />
          <IconButton
            label={query ? `搜索会话，当前查询：${query}` : '搜索会话'}
            tooltip="搜索会话"
            icon={<Search size={18} />}
            selected={isSearchOpen || Boolean(query)}
            aria-expanded={isSearchOpen}
            aria-controls="sidebar-search"
            onClick={() => openSearch(true)}
          />
          <IconButton label="智能体" tooltip="智能体" icon={<Workflow size={18} />} selected />
          <span className="rail-spacer" />
          <IconButton label="打开用户菜单" tooltip="账户" icon={<UserRound size={18} />} onClick={onRequestExpanded} />
        </div>

        {menuConversation && openMenu && wideInteractive && (
          <div ref={menuRef} className="conversation-menu conversation-menu-floating" style={{ top: openMenu.top, left: openMenu.left }}>
            <Button variant="ghost" leadingIcon={menuConversation.pinned ? <PinOff size={15} /> : <Pin size={15} />} onClick={() => { onPin(menuConversation.threadId); closeMenu(true) }}>
              {menuConversation.pinned ? '取消置顶' : '置顶'}
            </Button>
            <Button variant="ghost" leadingIcon={<Pencil size={15} />} onClick={() => { onRename(menuConversation.threadId, openMenu.trigger); closeMenu() }}>重命名</Button>
            <Button variant="ghost" className="conversation-menu-danger" leadingIcon={<Trash2 size={15} />} onClick={() => { onDelete(menuConversation.threadId, openMenu.trigger); closeMenu() }}>删除</Button>
          </div>
        )}
      </aside>
    </>
  )
}
