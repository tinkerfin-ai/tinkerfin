import { fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'

import { ListboxPicker } from './ListboxPicker'

function PortalPicker() {
  const [open, setOpen] = useState(false)
  const [value, setValue] = useState<'all' | 'model'>('all')
  return (
    <ListboxPicker
      value={value}
      options={['all', 'model']}
      open={open}
      onOpenChange={setOpen}
      onChange={setValue}
      triggerLabel="类型筛选"
      listboxLabel="类型"
      rootClassName="picker-root"
      triggerClassName="picker-trigger"
      listboxClassName="picker-listbox"
      optionClassName="picker-option"
      listboxPortalTarget={document.body}
      listboxStyle={{ position: 'fixed', top: 40, left: 12 }}
      renderTrigger={(option) => option}
      renderOption={(option, selected) => `${option}${selected ? ' selected' : ''}`}
    />
  )
}

describe('ListboxPicker', () => {
  it('keeps portal options inside the shared keyboard and focus contract', () => {
    const originalScrollIntoView = HTMLElement.prototype.scrollIntoView
    const scrollIntoView = vi.fn()
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
      configurable: true,
      value: scrollIntoView,
    })
    render(<PortalPicker />)

    const trigger = screen.getByRole('button', { name: '类型筛选' })
    fireEvent.click(trigger)
    const listbox = screen.getByRole('listbox', { name: '类型' })
    expect(listbox.parentElement).toBe(document.body)
    expect(listbox).toHaveFocus()
    expect(listbox).toHaveStyle({ position: 'fixed', top: '40px', left: '12px' })
    scrollIntoView.mockClear()
    fireEvent.keyDown(listbox, { key: 'End' })
    expect(scrollIntoView).toHaveBeenCalled()

    const model = screen.getByRole('option', { name: 'model' })
    fireEvent.pointerDown(model)
    fireEvent.click(model)
    expect(trigger).toHaveTextContent('model')
    expect(trigger).toHaveFocus()
    expect(screen.queryByRole('listbox', { name: '类型' })).not.toBeInTheDocument()

    fireEvent.click(trigger)
    fireEvent.keyDown(screen.getByRole('listbox', { name: '类型' }), { key: 'Home' })
    fireEvent.keyDown(screen.getByRole('listbox', { name: '类型' }), { key: 'Enter' })
    expect(trigger).toHaveTextContent('all')
    expect(trigger).toHaveFocus()
    if (originalScrollIntoView) {
      Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
        configurable: true,
        value: originalScrollIntoView,
      })
    } else {
      Reflect.deleteProperty(HTMLElement.prototype, 'scrollIntoView')
    }
  })
})
