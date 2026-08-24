import {
  BookOpenCheck,
  Cable,
  CircleEllipsis,
  Ellipsis,
  LogOut,
  PanelRight,
  Pencil,
  Pin,
  PinOff,
  Search,
  Settings2,
  SquarePen,
  Trash2,
  Workflow,
  X,
} from 'lucide-react'
import {
  Fragment,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import type { MouseEvent } from 'react'

import type { AuthUser } from '../../../api/auth/types'
import { BrandMark } from '../../../components/ui/BrandMark'
import { Button, IconButton, UserAvatar } from '../../../components/ui'
import { TransientScrollbar } from '../../../components/ui/TransientScrollbar'
import { TRANSIENT_THREAD_ID } from '../../../lib/workspace'
import { useI18n } from '../../../i18n'
import type { Conversation, WorkspaceState } from '../../../types'
import { groupConversationHistory } from '../historyGroups'
import type { SidebarMode } from '../useWorkspaceNavigation'
import { OverflowMarquee } from './OverflowMarquee'

const HISTORY_OBSERVER_MARGIN_PX = 80
const HISTORY_SCROLL_BURST_IDLE_MS = 600

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
  const { t } = useI18n()
  return (
    <div
      className={`conversation-item overflow-marquee-trigger ${conversation.pinned ? 'is-pinned' : 'is-recent'} ${isActive ? 'is-active' : ''}`}
      data-history-thread-id={conversation.threadId}
    >
      <button type="button" className="conversation-main" aria-label={t('打开会话：{title}', { title: conversation.title })} onClick={onSelect}>
        <OverflowMarquee className="conversation-title-marquee" endRevealInset={12}>{`${conversation.title}\u200b`}</OverflowMarquee>
      </button>
      {onToggleMenu && (
        <IconButton
          className="conversation-more"
          size="sm"
          label={t('管理会话：{title}', { title: conversation.title })}
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
  historyConversations: Conversation[]
  historyDayRanges: number[]
  historyQuery: string
  onHistoryQueryChange: (query: string) => void
  isHistorySearchActive: boolean
  isHistorySearching: boolean
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
  user: AuthUser
  onOpenSettings: (restoreFocusTo?: HTMLElement | null) => void
  onLogout: () => void
  backgroundInert?: boolean
}

export function Sidebar({
  workspace,
  historyConversations,
  historyDayRanges,
  historyQuery,
  onHistoryQueryChange,
  isHistorySearchActive,
  isHistorySearching,
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
  user,
  onOpenSettings,
  onLogout,
  backgroundInert = false,
}: SidebarProps) {
  const { locale, t } = useI18n()
  const [isSearchOpen, setSearchOpen] = useState(false)
  const [userMenu, setUserMenu] = useState(false)
  const [stickyHistoryTitle, setStickyHistoryTitle] = useState('')
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
  const historyLoadSentinelRef = useRef<HTMLDivElement>(null)
  const paginationArmedRef = useRef(true)
  const paginationRequestedInScrollBurstRef = useRef(false)
  const paginationScrollBurstTimerRef = useRef<number | null>(null)
  const paginationAnchorRef = useRef<{
    threadId?: string
    offset: number
    scrollTop: number
  } | null>(null)
  const paginationStateRef = useRef({
    hasMore,
    isLoadingMore,
    loadMoreError,
    onLoadMore,
    onRetryLoadMore,
  })
  paginationStateRef.current = {
    hasMore,
    isLoadingMore,
    loadMoreError,
    onLoadMore,
    onRetryLoadMore,
  }
  const groups = useMemo(
    () => groupConversationHistory(historyConversations, historyDayRanges, new Date(), locale),
    [historyConversations, historyDayRanges, locale],
  )
  const menuConversation = openMenu
    ? workspace.conversations.find((item) => item.threadId === openMenu.threadId)
    : undefined
  const isOverlayHidden = mode === 'overlay' && !overlayOpen
  const isNewConversation = workspace.currentThreadId === TRANSIENT_THREAD_ID

  const capturePaginationAnchor = useCallback(() => {
    const root = historyScrollRef.current
    if (!root) return
    const rootBounds = root.getBoundingClientRect()
    const firstVisible = Array.from(
      root.querySelectorAll<HTMLElement>('[data-history-thread-id]'),
    ).find((item) => {
      const bounds = item.getBoundingClientRect()
      return bounds.bottom > rootBounds.top && bounds.top < rootBounds.bottom
    })
    paginationAnchorRef.current = {
      threadId: firstVisible?.dataset.historyThreadId,
      offset: firstVisible
        ? firstVisible.getBoundingClientRect().top - rootBounds.top
        : 0,
      scrollTop: root.scrollTop,
    }
  }, [])

  const requestHistoryPage = useCallback((allowRetry = false) => {
    const state = paginationStateRef.current
    if (
      !paginationArmedRef.current
      || !state.hasMore
      || state.isLoadingMore
      || (state.loadMoreError && !allowRetry)
    ) return
    paginationArmedRef.current = false
    capturePaginationAnchor()
    const load = state.loadMoreError
      ? state.onRetryLoadMore ?? state.onLoadMore
      : state.onLoadMore
    load()
  }, [capturePaginationAnchor])

  const updateStickyHistoryTitle = useCallback((scrollElement: HTMLElement) => {
    const groupsElement = scrollElement.querySelector<HTMLElement>('.conversation-groups')
    const groupsOffset = groupsElement?.offsetTop ?? 0
    const scrollPosition = scrollElement.scrollTop + 1
    let activeTitle = ''
    for (const heading of scrollElement.querySelectorAll<HTMLElement>('[data-history-group-label]')) {
      if (groupsOffset + heading.offsetTop > scrollPosition) break
      activeTitle = heading.dataset.historyGroupLabel ?? ''
    }
    setStickyHistoryTitle((current) => current === activeTitle ? current : activeTitle)
  }, [])

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
    onHistoryQueryChange('')
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
      if (historyQuery.trim()) {
        searchInputRef.current?.blur()
      } else {
        setSearchOpen(false)
      }
    }
    document.addEventListener('pointerdown', handleOutsidePointerDown)
    return () => document.removeEventListener('pointerdown', handleOutsidePointerDown)
  }, [historyQuery, isSearchOpen])

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

  useLayoutEffect(() => {
    const scrollElement = historyScrollRef.current
    if (scrollElement) updateStickyHistoryTitle(scrollElement)
  }, [groups, updateStickyHistoryTitle])

  useLayoutEffect(() => {
    const anchor = paginationAnchorRef.current
    const root = historyScrollRef.current
    if (!anchor || !root) return
    const anchoredItem = anchor.threadId
      ? Array.from(root.querySelectorAll<HTMLElement>('[data-history-thread-id]'))
          .find((item) => item.dataset.historyThreadId === anchor.threadId)
      : undefined
    if (anchoredItem) {
      const nextOffset = anchoredItem.getBoundingClientRect().top - root.getBoundingClientRect().top
      root.scrollTop += nextOffset - anchor.offset
    } else {
      root.scrollTop = anchor.scrollTop
    }
    paginationAnchorRef.current = null
    updateStickyHistoryTitle(root)
  }, [groups, updateStickyHistoryTitle])

  useEffect(() => {
    paginationArmedRef.current = true
    paginationAnchorRef.current = null
    const sentinel = historyLoadSentinelRef.current
    const root = historyScrollRef.current
    if (!sentinel || !root || typeof IntersectionObserver === 'undefined') return
    const observer = new IntersectionObserver((entries) => {
      const visible = entries.some((entry) => entry.isIntersecting)
      if (!visible) {
        paginationArmedRef.current = true
        return
      }
      requestHistoryPage()
    }, {
      root,
      rootMargin: `0px 0px ${HISTORY_OBSERVER_MARGIN_PX}px`,
      threshold: 0.01,
    })
    observer.observe(sentinel)
    return () => observer.disconnect()
  }, [historyQuery, requestHistoryPage])

  useLayoutEffect(() => {
    const root = historyScrollRef.current
    if (
      !root
      || root.clientHeight <= 0
      || isLoadingMore
      || loadMoreError
      || !hasMore
      || root.scrollHeight > root.clientHeight + HISTORY_OBSERVER_MARGIN_PX
    ) return
    paginationArmedRef.current = true
    const frame = window.requestAnimationFrame(() => requestHistoryPage())
    return () => window.cancelAnimationFrame(frame)
  }, [groups, hasMore, isLoadingMore, loadMoreError, requestHistoryPage])

  useEffect(() => {
    if (loadMoreError) paginationAnchorRef.current = null
  }, [loadMoreError])

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

  useEffect(() => () => {
    if (paginationScrollBurstTimerRef.current != null) {
      window.clearTimeout(paginationScrollBurstTimerRef.current)
    }
  }, [])

  useEffect(() => {
    const handleNewConversationShortcut = (event: KeyboardEvent) => {
      if (!event.metaKey || event.key.toLocaleLowerCase('en-US') !== 'k') return
      event.preventDefault()
      onNew()
    }
    document.addEventListener('keydown', handleNewConversationShortcut)
    return () => document.removeEventListener('keydown', handleNewConversationShortcut)
  }, [onNew])

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
        <button type="button" className="sidebar-scrim" aria-label={t('关闭导航遮罩')} onClick={() => onCloseOverlay()} />
      )}
      <aside
        ref={rootRef}
        id="workspace-sidebar"
        className={`workspace-sidebar is-${mode}${overlayOpen ? ' is-overlay-open' : ''}`}
        data-sidebar-mode={mode}
        aria-label={t('会话导航')}
        aria-hidden={isOverlayHidden || backgroundInert || undefined}
        inert={isOverlayHidden || backgroundInert || undefined}
      >
        <div
          className="sidebar-wide"
          aria-hidden={!wideInteractive || undefined}
          inert={!wideInteractive || undefined}
        >
          <div className={`sidebar-head${isSearchOpen ? ' is-search-open' : ''}`}>
            <a className="brand" href="#top" aria-label={t('TinkerFin 首页')}>
              <BrandMark size={30} />
              <span className="brand-name">TinkerFin</span>
              <small className="brand-plus">Plus</small>
            </a>
            <div className="sidebar-head-actions">
              <IconButton
                ref={searchTriggerRef}
                size="sm"
                label={t('搜索会话')}
                tooltip={t('搜索会话')}
                icon={<Search size={17} />}
                selected={isSearchOpen || isHistorySearchActive}
                aria-expanded={isSearchOpen}
                aria-controls="sidebar-search"
                onClick={() => openSearch(false)}
              />
              {mode === 'overlay' ? (
                <IconButton ref={overlayCloseButtonRef} label={t('关闭导航')} icon={<X size={18} />} onClick={() => onCloseOverlay()} />
              ) : (
                <IconButton
                  size="sm"
                  label={t('收起侧边栏')}
                  tooltip={t('收起侧边栏')}
                  icon={<PanelRight size={17} />}
                  aria-controls="workspace-sidebar"
                  aria-expanded="true"
                  onClick={onToggleMode}
                />
              )}
            </div>
            <label id="sidebar-search" className="sidebar-search">
              <Search size={16} aria-hidden="true" />
              <input
                ref={searchInputRef}
                aria-label={t('搜索会话')}
                value={historyQuery}
                onChange={(event) => onHistoryQueryChange(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key !== 'Escape') return
                  event.preventDefault()
                  clearAndCloseSearch()
                }}
                placeholder={t('搜索对话')}
              />
              <IconButton label={t('清除搜索')} icon={<X size={16} />} onClick={() => clearAndCloseSearch()} />
            </label>
          </div>

          <div className="new-chat-wrap">
            <Button
              className="new-chat"
              size="lg"
              variant="ghost"
              selected={isNewConversation}
              leadingIcon={<SquarePen size={18} />}
              trailingIcon={<kbd className="new-chat-shortcut">⌘ K</kbd>}
              aria-keyshortcuts="Meta+K"
              title={t('Command + K 开启新会话')}
              onClick={onNew}
            >
              {t('新会话')}
            </Button>
          </div>

          <div className="conversation-history">
            <div
              className={`conversation-sticky-title${stickyHistoryTitle ? ' is-visible' : ''}`}
              aria-hidden="true"
            >
              {stickyHistoryTitle}
            </div>
            <div
              ref={historyScrollRef}
              className={`conversation-scroll ui-scrollbar${openMenu ? ' is-scroll-locked' : ''}`}
              role="region"
              aria-label={t('最近对话')}
              onScroll={(event) => {
                const element = event.currentTarget
                updateStickyHistoryTitle(element)
                if (paginationAnchorRef.current) capturePaginationAnchor()
                if (paginationScrollBurstTimerRef.current != null) {
                  window.clearTimeout(paginationScrollBurstTimerRef.current)
                }
                paginationScrollBurstTimerRef.current = window.setTimeout(() => {
                  paginationRequestedInScrollBurstRef.current = false
                  paginationScrollBurstTimerRef.current = null
                }, HISTORY_SCROLL_BURST_IDLE_MS)
                if (openMenu) {
                  element.scrollTop = lockedHistoryScrollTop.current
                  return
                }
                const nearBottom = (
                  element.scrollTop + element.clientHeight
                  >= element.scrollHeight - HISTORY_OBSERVER_MARGIN_PX
                )
                if (!nearBottom) {
                  paginationArmedRef.current = true
                  return
                }
                if (!paginationRequestedInScrollBurstRef.current) {
                  paginationRequestedInScrollBurstRef.current = true
                  paginationArmedRef.current = true
                  requestHistoryPage(true)
                }
              }}
            >
              <nav className="primary-nav" aria-label={t('工作区功能')}>
                <Button size="sm" variant="ghost" leadingIcon={<Workflow size={18} />} disabled>{t('智能体')}</Button>
                <Button size="sm" variant="ghost" leadingIcon={<BookOpenCheck size={18} />} disabled>{t('技能库')}</Button>
                <Button size="sm" variant="ghost" leadingIcon={<Cable size={18} />} disabled>{t('MCP管理')}</Button>
                <Button size="sm" variant="ghost" leadingIcon={<CircleEllipsis size={18} />} disabled>{t('更多')}</Button>
              </nav>
              <div className="conversation-groups">
                {groups.map((group) => (
                  <Fragment key={group.key}>
                    <h2
                      id={`conversation-group-${group.key}`}
                      className="conversation-group-title"
                      data-history-group-label={group.label}
                    >
                      {group.label}
                    </h2>
                    <section className="conversation-group-items" aria-labelledby={`conversation-group-${group.key}`}>
                      {renderItems(group.items)}
                    </section>
                  </Fragment>
                ))}
              {isHistorySearching && (
                <div className="history-skeleton-list history-search-skeletons" aria-label={t('正在搜索会话')}>
                  {Array.from({ length: 5 }, (_, index) => (
                    <div key={index} className="history-skeleton" data-testid="history-search-skeleton" aria-hidden="true"><span /></div>
                  ))}
                </div>
              )}
              {!isHistorySearching && groups.length === 0 && !loadMoreError && (
                <p className="no-search-result">{historyQuery.trim() ? t('没有匹配的对话') : t('暂无最近对话')}</p>
              )}
              <div ref={historyLoadSentinelRef} className="history-load-sentinel" aria-hidden="true" />
              </div>
            </div>
            <TransientScrollbar viewportRef={historyScrollRef} />
          </div>

          <div ref={userMenuRef} className="user-account">
            {userMenu && (
              <div className="user-menu">
                <Button
                  variant="ghost"
                  leadingIcon={<Settings2 size={16} />}
                  onClick={() => {
                    setUserMenu(false)
                    onOpenSettings(userMenuButtonRef.current)
                  }}
                >
                  {t('设置')}
                </Button>
                <Button variant="ghost" leadingIcon={<LogOut size={16} />} onClick={onLogout}>{t('退出登录')}</Button>
              </div>
            )}
            <button
              ref={userMenuButtonRef}
              type="button"
              className="user-card"
              aria-label={userMenu ? t('关闭用户菜单') : t('打开用户菜单')}
              aria-expanded={userMenu}
              onClick={() => setUserMenu((value) => !value)}
            >
              <UserAvatar
                avatarUrl={user.avatar_url}
                displayName={user.display_name}
                username={user.username}
              />
              <strong className="user-name">{user.display_name.trim() || user.username}</strong>
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
            label={t('打开侧边栏')}
            tooltip={t('打开侧边栏')}
            icon={<PanelRight size={17} />}
            aria-controls="workspace-sidebar"
            aria-expanded="false"
            onClick={onToggleMode}
          />
          <IconButton label={t('新会话')} tooltip={t('新会话')} icon={<SquarePen size={18} />} selected={isNewConversation} onClick={onNew} />
          <IconButton
            label={historyQuery ? t('搜索会话，当前查询：{query}', { query: historyQuery }) : t('搜索会话')}
            tooltip={t('搜索会话')}
            icon={<Search size={18} />}
            selected={isSearchOpen || isHistorySearchActive}
            aria-expanded={isSearchOpen}
            aria-controls="sidebar-search"
            onClick={() => openSearch(true)}
          />
          <IconButton label={t('智能体')} tooltip={t('智能体')} icon={<Workflow size={18} />} disabled />
          <span className="rail-spacer" />
          <IconButton
            label={t('展开侧边栏以查看账户')}
            tooltip={t('账户')}
            icon={(
              <UserAvatar
                avatarUrl={user.avatar_url}
                displayName={user.display_name}
                username={user.username}
              />
            )}
            onClick={onRequestExpanded}
          />
        </div>

        {menuConversation && openMenu && wideInteractive && (
          <div ref={menuRef} className="conversation-menu conversation-menu-floating" style={{ top: openMenu.top, left: openMenu.left }}>
            <Button variant="ghost" leadingIcon={menuConversation.pinned ? <PinOff size={15} /> : <Pin size={15} />} onClick={() => { onPin(menuConversation.threadId); closeMenu(true) }}>
              {menuConversation.pinned ? t('取消置顶') : t('置顶')}
            </Button>
            <Button variant="ghost" leadingIcon={<Pencil size={15} />} onClick={() => { onRename(menuConversation.threadId, openMenu.trigger); closeMenu() }}>{t('重命名')}</Button>
            <Button variant="ghost" className="conversation-menu-danger" leadingIcon={<Trash2 size={15} />} onClick={() => { onDelete(menuConversation.threadId, openMenu.trigger); closeMenu() }}>{t('删除')}</Button>
          </div>
        )}
      </aside>
    </>
  )
}
