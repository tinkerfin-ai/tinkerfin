import { act, fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { THEME_PREFERENCE_STORAGE_KEY } from '../theme'
import { ThemePicker } from './ThemePicker'

type MediaQueryController = {
  mediaQuery: MediaQueryList
  setMatches: (matches: boolean) => void
}

const createMediaQuery = (initialMatches: boolean): MediaQueryController => {
  let matches = initialMatches
  const listeners = new Set<(event: MediaQueryListEvent) => void>()
  const mediaQuery = {
    get matches() { return matches },
    media: '(prefers-color-scheme: dark)',
    onchange: null,
    addEventListener: vi.fn((_type: string, listener: (event: MediaQueryListEvent) => void) => listeners.add(listener)),
    removeEventListener: vi.fn((_type: string, listener: (event: MediaQueryListEvent) => void) => listeners.delete(listener)),
    addListener: vi.fn((listener: (event: MediaQueryListEvent) => void) => listeners.add(listener)),
    removeListener: vi.fn((listener: (event: MediaQueryListEvent) => void) => listeners.delete(listener)),
    dispatchEvent: vi.fn(() => true),
  } as unknown as MediaQueryList

  return {
    mediaQuery,
    setMatches(nextMatches) {
      matches = nextMatches
      const event = { matches, media: mediaQuery.media } as MediaQueryListEvent
      listeners.forEach((listener) => listener(event))
    },
  }
}

const installThemeColorMeta = () => {
  const meta = document.createElement('meta')
  meta.name = 'theme-color'
  document.head.appendChild(meta)
  return meta
}

describe('ThemePicker', () => {
  beforeEach(() => {
    delete document.documentElement.dataset.theme
    delete document.documentElement.dataset.themePreference
    document.querySelectorAll('meta[name="theme-color"]').forEach((meta) => meta.remove())
  })

  it('renders a three-segment radio group, defaults to light, and persists dark', async () => {
    const media = createMediaQuery(true)
    vi.stubGlobal('matchMedia', vi.fn(() => media.mediaQuery))
    const themeColor = installThemeColorMeta()
    const user = userEvent.setup()

    const firstRender = render(<ThemePicker />)
    const themeGroup = screen.getByRole('group', { name: '主题' })
    const systemOption = screen.getByRole('radio', { name: '跟随系统' })
    const lightOption = screen.getByRole('radio', { name: '浅色' })
    const darkOption = screen.getByRole('radio', { name: '深色' })

    expect(themeGroup).toContainElement(systemOption)
    expect(themeGroup).toContainElement(lightOption)
    expect(themeGroup).toContainElement(darkOption)
    expect(systemOption).not.toBeChecked()
    expect(lightOption).toBeChecked()
    expect(darkOption).not.toBeChecked()
    expect(document.documentElement).toHaveAttribute('data-theme', 'light')
    await user.click(darkOption)

    expect(darkOption).toBeChecked()
    expect(document.documentElement).toHaveAttribute('data-theme-preference', 'dark')
    expect(document.documentElement).toHaveAttribute('data-theme', 'dark')
    expect(window.localStorage.getItem(THEME_PREFERENCE_STORAGE_KEY)).toBe('dark')
    expect(themeColor).toHaveAttribute('content', '#111014')

    firstRender.unmount()
    render(<ThemePicker />)
    expect(screen.getByRole('radio', { name: '深色' })).toBeChecked()
  })

  it('tracks operating-system changes only while following the system', async () => {
    const media = createMediaQuery(false)
    vi.stubGlobal('matchMedia', vi.fn(() => media.mediaQuery))
    installThemeColorMeta()
    const user = userEvent.setup()

    render(<ThemePicker />)
    expect(document.documentElement).toHaveAttribute('data-theme', 'light')

    await user.click(screen.getByRole('radio', { name: '跟随系统' }))
    act(() => media.setMatches(true))
    expect(document.documentElement).toHaveAttribute('data-theme', 'dark')

    await user.click(screen.getByRole('radio', { name: '浅色' }))
    expect(document.documentElement).toHaveAttribute('data-theme', 'light')

    act(() => media.setMatches(false))
    act(() => media.setMatches(true))
    expect(document.documentElement).toHaveAttribute('data-theme', 'light')
  })

  it('stays as one circle until hover or keyboard focus expands the choices', async () => {
    const media = createMediaQuery(false)
    vi.stubGlobal('matchMedia', vi.fn(() => media.mediaQuery))
    installThemeColorMeta()
    const user = userEvent.setup()

    render(
      <>
        <ThemePicker />
        <button type="button">主题控件外</button>
      </>,
    )

    const themeGroup = screen.getByRole('group', { name: '主题' })
    const lightOption = screen.getByRole('radio', { name: '浅色' })

    expect(themeGroup).toHaveAttribute('data-expanded', 'false')
    await user.hover(themeGroup)
    expect(themeGroup).toHaveAttribute('data-expanded', 'true')
    await user.unhover(themeGroup)
    expect(themeGroup).toHaveAttribute('data-expanded', 'false')

    await user.tab()
    expect(themeGroup).toContainElement(document.activeElement as HTMLElement)
    expect(themeGroup).toHaveAttribute('data-expanded', 'true')
    await user.tab()
    expect(screen.getByRole('button', { name: '主题控件外' })).toHaveFocus()
    expect(themeGroup).toHaveAttribute('data-expanded', 'false')

    await user.hover(themeGroup)
    await user.click(lightOption)
    expect(lightOption).toHaveFocus()
    await user.unhover(themeGroup)
    expect(themeGroup).toHaveAttribute('data-expanded', 'false')
    expect(lightOption).not.toHaveFocus()
  })

  it('opens on touch and collapses after a theme is selected', () => {
    const media = createMediaQuery(false)
    vi.stubGlobal('matchMedia', vi.fn(() => media.mediaQuery))
    installThemeColorMeta()

    render(<ThemePicker />)
    const themeGroup = screen.getByRole('group', { name: '主题' })
    const lightOption = screen.getByRole('radio', { name: '浅色' })
    const darkOption = screen.getByRole('radio', { name: '深色' })

    const touchPointerDown = new MouseEvent('pointerdown', { bubbles: true })
    Object.defineProperty(touchPointerDown, 'pointerType', { value: 'touch' })
    fireEvent(lightOption, touchPointerDown)
    expect(themeGroup).toHaveAttribute('data-expanded', 'true')

    fireEvent.click(darkOption)
    expect(darkOption).toBeChecked()
    expect(themeGroup).toHaveAttribute('data-expanded', 'false')
  })
})
