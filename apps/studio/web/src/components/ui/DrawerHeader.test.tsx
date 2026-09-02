import { createRef } from 'react'
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { DrawerHeader } from './DrawerHeader'

describe('DrawerHeader', () => {
  it('呈现统一的标题、辅助信息和关闭操作', () => {
    const closeButton = createRef<HTMLButtonElement>()
    const onClose = vi.fn()
    render(
      <DrawerHeader
        ref={closeButton}
        title="任务轨迹"
        description="当前会话 · 2 组"
        closeLabel="关闭任务轨迹"
        onClose={onClose}
      />,
    )

    expect(screen.getByRole('heading', { name: '任务轨迹' })).toBeInTheDocument()
    expect(screen.getByText('当前会话 · 2 组')).toBeInTheDocument()
    expect(closeButton.current).toBe(screen.getByRole('button', { name: '关闭任务轨迹' }))
    fireEvent.click(closeButton.current!)
    expect(onClose).toHaveBeenCalledOnce()
  })
})
