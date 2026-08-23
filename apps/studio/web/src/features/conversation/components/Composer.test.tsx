import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { Composer } from './Composer'

describe('Composer', () => {
  let scrollHeight = 24
  let originalScrollHeight: PropertyDescriptor | undefined

  beforeEach(() => {
    originalScrollHeight = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'scrollHeight')
    Object.defineProperty(HTMLTextAreaElement.prototype, 'scrollHeight', {
      configurable: true,
      get: () => scrollHeight,
    })
  })

  afterEach(() => {
    if (originalScrollHeight) {
      Object.defineProperty(HTMLTextAreaElement.prototype, 'scrollHeight', originalScrollHeight)
    } else {
      delete (HTMLTextAreaElement.prototype as { scrollHeight?: number }).scrollHeight
    }
  })

  it('uses the branded placeholder and polished send icon', () => {
    render(
      <Composer
        value=""
        isRunning={false}
        onChange={vi.fn()}
        onSend={vi.fn()}
        onStop={vi.fn()}
      />,
    )

    expect(screen.getByPlaceholderText('给 TinkerFin 发消息')).toBeVisible()
    const sendButton = screen.getByRole('button', { name: '发送消息' })
    expect(sendButton).toBeDisabled()
    expect(sendButton.querySelector('.lucide-arrow-up')).not.toBeNull()
    expect(sendButton.closest('.composer')).toHaveClass('composer')
    expect(screen.queryByText(/Shift \+ Enter/)).not.toBeInTheDocument()
  })

  it('focuses the input from the full borderless composer hit area', () => {
    render(
      <Composer
        value=""
        isRunning={false}
        onChange={vi.fn()}
        onSend={vi.fn()}
        onStop={vi.fn()}
      />,
    )

    const input = screen.getByLabelText('消息输入')
    fireEvent.pointerDown(input.closest('.composer-wrap') as HTMLElement)
    expect(input).toHaveFocus()
  })

  it('shows the dedicated running stop control', () => {
    render(
      <Composer
        value="正在发送"
        isRunning
        onChange={vi.fn()}
        onSend={vi.fn()}
        onStop={vi.fn()}
      />,
    )

    const stopButton = screen.getByRole('button', { name: '停止任务' })
    expect(stopButton).toHaveClass('stop')
    expect(stopButton.querySelector('.lucide-square')).not.toBeNull()
  })

  it.each([
    ['composition state', { isComposing: true }],
    ['IME compatibility key code', { keyCode: 229 }],
  ])('does not send while Enter confirms an IME %s', (_name, nativeFields) => {
    const onSend = vi.fn()
    render(
      <Composer
        value="拼音输入"
        isRunning={false}
        onChange={vi.fn()}
        onSend={onSend}
        onStop={vi.fn()}
      />,
    )

    fireEvent.keyDown(screen.getByLabelText('消息输入'), {
      key: 'Enter',
      ...nativeFields,
    })

    expect(onSend).not.toHaveBeenCalled()
  })

  it('disables input and send while the selected history is hydrating', () => {
    render(
      <Composer
        value="暂存内容"
        isRunning={false}
        isHydrating
        onChange={vi.fn()}
        onSend={vi.fn()}
        onStop={vi.fn()}
      />,
    )

    expect(screen.getByLabelText('消息输入')).toBeDisabled()
    expect(screen.getByPlaceholderText('正在加载会话…')).toBeDisabled()
    expect(screen.getByRole('button', { name: '发送消息' })).toBeDisabled()
  })

  it('grows with wrapped content up to six lines, then scrolls internally', () => {
    const props = {
      isRunning: false,
      onChange: vi.fn(),
      onSend: vi.fn(),
      onStop: vi.fn(),
    }
    const { rerender } = render(<Composer {...props} value="一行" />)
    const input = screen.getByLabelText('消息输入')

    expect(input).toHaveStyle({ height: '24px', overflowY: 'hidden' })

    scrollHeight = 96
    rerender(<Composer {...props} value={'一\n二\n三\n四'} />)
    expect(input).toHaveStyle({ height: '96px', overflowY: 'hidden' })

    scrollHeight = 220
    rerender(<Composer {...props} value={'一\n二\n三\n四\n五\n六\n七'} />)
    expect(input).toHaveStyle({ height: '144px', overflowY: 'auto' })
  })
})
