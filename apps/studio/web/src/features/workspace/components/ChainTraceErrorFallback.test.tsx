import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { ChainTraceErrorFallback } from './ChainTraceErrorFallback'

describe('ChainTraceErrorFallback', () => {
  it('keeps both recovery paths available when the trace tree cannot render', () => {
    const onReturn = vi.fn()
    const onRetry = vi.fn()
    render(<ChainTraceErrorFallback onReturn={onReturn} onRetry={onRetry} />)

    fireEvent.click(screen.getByRole('button', { name: '返回对话' }))
    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    expect(onReturn).toHaveBeenCalledOnce()
    expect(onRetry).toHaveBeenCalledOnce()
  })
})
