import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { SettingsDialog } from './SettingsDialog'
import { LocaleProvider } from '../../i18n'

const user = {
  user_id: 7,
  username: 'yunsan',
  display_name: '云杉',
  avatar_url: 'https://cdn.example.test/avatar.webp',
  roles: [],
  disabled: false,
}

describe('SettingsDialog', () => {
  it('shows the supported user fields and changes the controlled appearance preference', async () => {
    const onThemePreferenceChange = vi.fn()
    render(<LocaleProvider>
      <SettingsDialog
        open
        user={user}
        themePreference="system"
        onThemePreferenceChange={onThemePreferenceChange}
        onClose={vi.fn()}
      />
    </LocaleProvider>)

    expect(screen.getByRole('dialog', { name: '设置' })).toBeInTheDocument()
    const settingsContent = document.querySelector('.settings-content')
    expect(settingsContent).toHaveClass('ui-scrollbar')
    expect(settingsContent?.parentElement?.querySelector('.ui-scrollbar-overlay')).toBeInTheDocument()
    expect(screen.getByText('云杉')).toBeInTheDocument()
    expect(screen.getByText('@yunsan')).toBeInTheDocument()
    expect(screen.queryByText(/用户 ID|角色|禁用/)).not.toBeInTheDocument()
    expect(screen.queryByText(/后续|暂不|修改功能/)).not.toBeInTheDocument()
    const accountSection = screen.getByRole('button', { name: '账号管理' })
    const generalSection = screen.getByRole('button', { name: '通用' })
    expect(accountSection).toHaveAttribute('aria-current', 'page')
    expect(accountSection.querySelector('.lucide-user-round')).toBeInTheDocument()
    expect(generalSection.querySelector('svg')).toBeInTheDocument()
    expect(generalSection.querySelector('.lucide-user-round')).not.toBeInTheDocument()
    expect(screen.queryByRole('radio', { name: '跟随系统' })).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: '通用' }))
    expect(screen.getByRole('radio', { name: '跟随系统' })).toBeChecked()

    await userEvent.click(screen.getByRole('radio', { name: '深色' }))

    expect(onThemePreferenceChange).toHaveBeenCalledWith('dark')

    await userEvent.click(screen.getByRole('button', { name: '界面语言' }))
    await userEvent.click(screen.getByRole('option', { name: 'English' }))
    expect(screen.getByRole('dialog', { name: 'Settings' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Account' })).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Account' }))
    expect(screen.getByText('云杉')).toBeInTheDocument()
  })

  it('closes on Escape and restores focus to the account trigger', () => {
    const trigger = document.createElement('button')
    document.body.append(trigger)
    trigger.focus()
    const onClose = vi.fn()
    const { unmount } = render(<LocaleProvider>
      <SettingsDialog
        open
        user={{ ...user, avatar_url: null }}
        themePreference="light"
        restoreFocusTo={trigger}
        onThemePreferenceChange={vi.fn()}
        onClose={onClose}
      />
    </LocaleProvider>)

    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })
    expect(onClose).toHaveBeenCalledOnce()
    unmount()
    expect(trigger).toHaveFocus()
    trigger.remove()
  })
})
