import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { LocaleProvider } from './LocaleProvider'
import { englishMessages } from './messages'
import { useI18n } from './useI18n'
import {
  LANGUAGE_STORAGE_KEY,
  resolveLanguage,
  resolveSystemLanguage,
} from './locale'

function LocaleHarness() {
  const { locale, preference, setPreference, t } = useI18n()
  return (
    <div>
      <span>{locale}:{preference}</span>
      <span>{t('设置')}</span>
      <button type="button" onClick={() => setPreference('en')}>English</button>
      <button type="button" onClick={() => setPreference('system')}>System</button>
    </div>
  )
}

describe('locale preference', () => {
  afterEach(() => {
    window.localStorage.clear()
    vi.restoreAllMocks()
  })

  it('defaults to Simplified Chinese and resolves supported system languages', () => {
    expect(resolveLanguage('zh-CN')).toBe('zh-CN')
    expect(resolveSystemLanguage(['zh-TW'])).toBe('zh-CN')
    expect(resolveSystemLanguage(['fr-FR', 'en-US'])).toBe('en')
    expect(resolveSystemLanguage(['fr-FR'])).toBe('en')
  })

  it('switches immediately and synchronizes an external browser preference', async () => {
    render(<LocaleProvider><LocaleHarness /></LocaleProvider>)
    expect(screen.getByText('zh-CN:zh-CN')).toBeInTheDocument()
    expect(screen.getByText('设置')).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'English' }))
    expect(screen.getByText('en:en')).toBeInTheDocument()
    expect(screen.getByText('Settings')).toBeInTheDocument()
    expect(document.documentElement.lang).toBe('en')
    expect(window.localStorage.getItem(LANGUAGE_STORAGE_KEY)).toBe('en')

    window.localStorage.setItem(LANGUAGE_STORAGE_KEY, 'zh-CN')
    act(() => window.dispatchEvent(new StorageEvent('storage', {
      key: LANGUAGE_STORAGE_KEY,
      newValue: 'zh-CN',
    })))
    expect(screen.getByText('zh-CN:zh-CN')).toBeInTheDocument()
  })

  it('recomputes the system language when another tab switches to system', () => {
    const languages = vi.spyOn(navigator, 'languages', 'get')
    languages.mockReturnValue(['en-US'])
    window.localStorage.setItem(LANGUAGE_STORAGE_KEY, 'en')
    render(<LocaleProvider><LocaleHarness /></LocaleProvider>)
    expect(screen.getByText('en:en')).toBeInTheDocument()

    languages.mockReturnValue(['zh-CN'])
    window.localStorage.setItem(LANGUAGE_STORAGE_KEY, 'system')
    act(() => window.dispatchEvent(new StorageEvent('storage', {
      key: LANGUAGE_STORAGE_KEY,
      newValue: 'system',
    })))

    expect(screen.getByText('zh-CN:system')).toBeInTheDocument()
    expect(screen.getByText('设置')).toBeInTheDocument()
  })

  it('国际化产品提示不以句号结尾', () => {
    const invalid = Object.entries(englishMessages)
      .filter(([chinese, english]) => /。$/.test(chinese) || /\.$/.test(english))
      .map(([chinese]) => chinese)
    expect(invalid).toEqual([])
  })
})
