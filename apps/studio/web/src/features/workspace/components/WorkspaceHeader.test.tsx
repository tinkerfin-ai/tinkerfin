import { createRef } from 'react'
import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { WorkspaceHeader } from './WorkspaceHeader'

describe('WorkspaceHeader', () => {
  it('keeps the current conversation title available for responsive presentation', () => {
    render(
      <WorkspaceHeader
        conversationTitle="研究下一季度产品路线"
        drawerOpen={false}
        todoCount={3}
        drawerToggleRef={createRef<HTMLButtonElement>()}
        overlayTriggerRef={createRef<HTMLButtonElement>()}
        onOpenOverlay={vi.fn()}
        onToggleDrawer={vi.fn()}
      />,
    )

    expect(screen.getByRole('heading', { level: 1, name: '研究下一季度产品路线' }))
      .toHaveClass('workspace-title')
  })

  it('uses the product term for an untitled draft', () => {
    render(
      <WorkspaceHeader
        conversationTitle=""
        drawerOpen
        todoCount={0}
        drawerToggleRef={createRef<HTMLButtonElement>()}
        overlayTriggerRef={createRef<HTMLButtonElement>()}
        onOpenOverlay={vi.fn()}
        onToggleDrawer={vi.fn()}
      />,
    )

    expect(screen.getByRole('heading', { level: 1, name: '新会话' })).toBeInTheDocument()
  })
})
