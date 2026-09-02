import { fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it } from 'vitest'

import { FilterPicker } from './FilterPicker'
import { FilterToggle } from './FilterToggle'
import uiStyles from './ui.css?raw'

function FilterControls() {
  const [value, setValue] = useState<'all' | 'model'>('all')
  const [technical, setTechnical] = useState(false)
  return (
    <>
      <FilterPicker
        value={value}
        options={['all', 'model']}
        label="类型"
        renderLabel={(option) => option === 'all' ? '全部类型' : '模型 · 2'}
        onChange={setValue}
      />
      <FilterToggle
        pressed={technical}
        label="技术节点"
        onPressedChange={setTechnical}
      />
    </>
  )
}

describe('Filter controls', () => {
  it('shares one neutral visual contract across picker and toggle states', () => {
    render(<FilterControls />)

    const picker = screen.getByRole('button', { name: '类型：全部类型' })
    fireEvent.click(picker)
    expect(screen.getByRole('listbox', { name: '类型' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('option', { name: '模型 · 2' }))
    expect(screen.getByRole('button', { name: '类型：模型 · 2' })).toBeInTheDocument()

    const toggle = screen.getByRole('button', { name: '技术节点' })
    expect(toggle).toHaveAttribute('aria-pressed', 'false')
    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-pressed', 'true')

    const filterStyles = uiStyles.slice(
      uiStyles.indexOf('.ui-filter-control'),
      uiStyles.indexOf('.ui-button__icon'),
    )
    expect(filterStyles).not.toContain('--color-brand')
    expect(filterStyles).toMatch(
      /\.ui-filter-picker__option\[aria-selected='true'\][^{]*\{[^}]*background:\s*var\(--color-hover\);[^}]*color:\s*var\(--color-text-primary\);/s,
    )
    expect(filterStyles).toMatch(
      /\.ui-filter-toggle\[aria-pressed='true'\]\s*\{[^}]*border-color:\s*var\(--color-border-strong\);[^}]*background:\s*var\(--color-subtle\);[^}]*color:\s*var\(--color-text-primary\);/s,
    )
    expect(filterStyles).toMatch(
      /\.ui-filter-toggle\[aria-pressed='true'\] \.ui-filter-toggle__mark\s*\{[^}]*background:\s*var\(--color-text-primary\);[^}]*color:\s*var\(--color-canvas\);/s,
    )
  })
})
