import { Menu } from 'lucide-react'
import type { RefObject } from 'react'

import { Button, IconButton } from '../../../components/ui'
import { useI18n } from '../../../i18n'

export function WorkspaceHeader({
  conversationTitle,
  drawerOpen,
  todoCount,
  drawerToggleRef,
  overlayTriggerRef,
  onOpenOverlay,
  onToggleDrawer,
}: {
  conversationTitle: string
  drawerOpen: boolean
  todoCount: number
  drawerToggleRef: RefObject<HTMLButtonElement | null>
  overlayTriggerRef: RefObject<HTMLButtonElement | null>
  onOpenOverlay: () => void
  onToggleDrawer: () => void
}) {
  const { t } = useI18n()
  return (
    <header className="chat-header">
      <div className="header-left">
        <IconButton ref={overlayTriggerRef} className="menu-toggle" label={t('打开导航')} icon={<Menu size={19} />} onClick={onOpenOverlay} />
        <h1 className="workspace-title">{conversationTitle || t('新会话')}</h1>
      </div>
      <div className="header-actions">
        {!drawerOpen && (
          <Button
            ref={drawerToggleRef}
            className="drawer-toggle"
            variant="secondary"
            aria-label={t('打开任务抽屉')}
            aria-expanded={false}
            aria-controls="task-drawer"
            onClick={onToggleDrawer}
          >
            {t('任务')} <span className="drawer-count">{todoCount}</span>
          </Button>
        )}
      </div>
    </header>
  )
}
