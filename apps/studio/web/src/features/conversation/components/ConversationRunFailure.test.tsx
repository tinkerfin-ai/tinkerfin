import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import { ConversationRunFailure } from './ConversationRunFailure'

it('仅展示会话异常及重试，支持键盘和禁用状态', async () => {
  const user = userEvent.setup()
  const retry = vi.fn()
  const { rerender } = render(<ConversationRunFailure retryable disabled={false} onRetry={retry} />)
  expect(screen.getByText('会话异常')).toBeInTheDocument()
  expect(screen.getAllByRole('button')).toHaveLength(1)
  await user.tab()
  await user.keyboard('{Enter}')
  expect(retry).toHaveBeenCalledOnce()
  rerender(<ConversationRunFailure retryable disabled onRetry={retry} />)
  expect(screen.getByRole('button', { name: '重试' })).toBeDisabled()
  rerender(<ConversationRunFailure retryable={false} disabled={false} onRetry={retry} />)
  expect(screen.queryByRole('button')).not.toBeInTheDocument()
})
