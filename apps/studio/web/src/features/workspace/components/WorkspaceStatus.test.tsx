import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { WorkspaceStatus } from './WorkspaceStatus'

describe('WorkspaceStatus', () => {
  it('只显示单行加载标题，并保留错误状态的说明和重试操作', () => {
    const onRetry = vi.fn()
    const { rerender } = render(
      <WorkspaceStatus kind="loading" title="正在加载会话" />,
    )

    expect(screen.getByRole('status')).toHaveTextContent(/^正在加载会话$/)
    expect(screen.queryByText('正在恢复消息、任务和运行状态')).not.toBeInTheDocument()

    rerender(
      <WorkspaceStatus
        kind="error"
        title="会话加载失败"
        description="该会话尚未完整恢复"
        onRetry={onRetry}
      />,
    )

    expect(screen.getByRole('alert')).toHaveTextContent('会话加载失败该会话尚未完整恢复')
    screen.getByRole('button', { name: '重试' }).click()
    expect(onRetry).toHaveBeenCalledOnce()
  })
})
