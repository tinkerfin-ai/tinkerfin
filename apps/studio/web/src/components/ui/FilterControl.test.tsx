import { fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it } from 'vitest'

import { FilterToggle } from './FilterToggle'
import uiStyles from './ui.css?raw'

function FilterControls() {
  const [technical, setTechnical] = useState(false)
  return (
    <FilterToggle
      pressed={technical}
      label="技术"
      onPressedChange={setTechnical}
    />
  )
}

describe('Filter controls', () => {
  it('keeps the neutral toggle contract across pressed states', () => {
    render(<FilterControls />)

    const toggle = screen.getByRole('button', { name: '技术' })
    expect(toggle).toHaveAttribute('aria-pressed', 'false')
    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-pressed', 'true')

    const filterStyles = uiStyles.slice(
      uiStyles.indexOf('.ui-filter-control'),
      uiStyles.indexOf('.ui-view-tabs'),
    )
    expect(filterStyles).not.toContain('--color-brand')
    expect(filterStyles).toMatch(
      /\.ui-filter-toggle\[aria-pressed='true'\]\s*\{[^}]*border-color:\s*var\(--color-border-strong\);[^}]*background:\s*var\(--color-subtle\);[^}]*color:\s*var\(--color-text-primary\);/s,
    )
    expect(filterStyles).toMatch(
      /\.ui-filter-toggle\[aria-pressed='true'\] \.ui-filter-toggle__mark\s*\{[^}]*background:\s*var\(--color-text-primary\);[^}]*color:\s*var\(--color-canvas\);/s,
    )
  })
})
