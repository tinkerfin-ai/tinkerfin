import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { AuthScreen } from './AuthScreen'
import authStyles from './auth.css?raw'
import uiStyles from '../../components/ui/ui.css?raw'

describe('AuthScreen', () => {
  it('submits only the real username and password login to the caller', async () => {
    const user = userEvent.setup()
    const onLogin = vi.fn()
    render(<AuthScreen onLogin={onLogin} />)

    await user.type(screen.getByLabelText('用户名'), '  yunsan  ')
    await user.type(screen.getByLabelText('密码'), 'secret')
    await user.click(screen.getByRole('button', { name: '登录' }))

    expect(onLogin).toHaveBeenCalledWith({ username: 'yunsan', password: 'secret' })
  })

  it('associates the login password with exactly one label', () => {
    render(<AuthScreen onLogin={vi.fn()} />)

    const passwordInput = screen.getByLabelText('密码')
    expect(document.querySelectorAll('label[for="login-password"]')).toHaveLength(1)
    expect(passwordInput.closest('label')).toBeNull()
  })

  it('uses the shared TextField contract with semibold labels and regular input text', () => {
    render(<AuthScreen onLogin={vi.fn()} />)

    const usernameInput = screen.getByLabelText('用户名')
    expect(usernameInput.closest('.ui-text-field')).toHaveClass('ui-text-field--capsule')
    expect(uiStyles).toMatch(/\.ui-text-field__label\s*{[^}]*font-weight:\s*var\(--weight-semibold\)/s)
    expect(uiStyles).toMatch(/\.ui-text-field__control input\s*{[^}]*font-weight:\s*var\(--weight-regular\)/s)
  })

  it('moves through login credentials before password recovery', async () => {
    const user = userEvent.setup()
    render(<AuthScreen onLogin={vi.fn()} />)

    screen.getByLabelText('用户名').focus()
    await user.tab()
    expect(screen.getByLabelText('密码')).toHaveFocus()
    await user.tab()
    expect(screen.getByRole('button', { name: '显示密码' })).toHaveFocus()
    await user.tab()
    expect(screen.getByRole('button', { name: '忘记密码？' })).toHaveFocus()
    await user.tab()
    expect(screen.getByRole('button', { name: '登录' })).toHaveFocus()
  })

  it('does not reject existing short passwords while keeping new passwords at eight characters', async () => {
    const user = userEvent.setup()
    render(<AuthScreen onLogin={vi.fn()} />)

    expect(screen.getByLabelText('密码')).not.toHaveAttribute('minlength')
    await user.click(screen.getByRole('button', { name: '免费注册' }))
    expect(screen.getByLabelText('设置密码')).toHaveAttribute('minlength', '8')
  })

  it('replaces native login validation bubbles with inline field feedback', async () => {
    const user = userEvent.setup()
    render(<AuthScreen onLogin={vi.fn()} />)

    const usernameInput = screen.getByLabelText('用户名')
    const passwordInput = screen.getByLabelText('密码')
    const loginForm = screen.getByRole('button', { name: '登录' }).closest('form')

    expect(loginForm).toHaveAttribute('novalidate')
    await user.click(screen.getByRole('button', { name: '登录' }))

    expect(usernameInput).toHaveAttribute('aria-invalid', 'true')
    expect(passwordInput).toHaveAttribute('aria-invalid', 'true')
    expect(usernameInput).toHaveAccessibleDescription('请输入用户名')
    expect(passwordInput).toHaveAccessibleDescription('请输入密码')
    expect(usernameInput.closest('[data-validation-field]')).toHaveAttribute(
      'data-validation-field',
      'loginUsername',
    )
    expect(screen.getByText('请输入用户名')).toBeInTheDocument()
    expect(screen.getByText('请输入密码')).toBeInTheDocument()
    expect(usernameInput).toHaveFocus()

    await user.type(usernameInput, 'yunsan')
    expect(usernameInput).toHaveAttribute('aria-invalid', 'false')
    expect(screen.queryByText('请输入用户名')).not.toBeInTheDocument()
  })

  it('keeps short viewport forms inside a definite scroll container', () => {
    expect(authStyles).toMatch(/\.auth-page\s*{[^}]*min-height:\s*100dvh;[^}]*overflow:\s*hidden;/s)
    expect(authStyles).toMatch(/@media \(max-width:\s*1023px\)[\s\S]*\.auth-page\s*{[^}]*overflow-y:\s*auto;/s)
    expect(authStyles).toMatch(/\.auth-form-stage\s*{[^}]*calc\(100vw - var\(--space-8\)\)/s)
  })

  it('applies the shared focus ring to the password control instead of the input', () => {
    render(<AuthScreen onLogin={vi.fn()} />)

    const passwordInput = screen.getByLabelText('密码')
    expect(passwordInput.closest('.ui-text-field__control')).not.toBeNull()
    expect(uiStyles).toMatch(/\.ui-text-field__control:focus-within\s*{[^}]*box-shadow:\s*var\(--shadow-focus\)/s)
    expect(uiStyles).toMatch(/\.ui-text-field__control input\s*{[^}]*outline:\s*0/s)
  })

  it('uses visible hover feedback and the shared motion scale for auth controls', () => {
    expect(authStyles).toMatch(/\.auth-brand:hover\s*{[^}]*background:\s*var\(--color-hover\)/s)
    expect(uiStyles).toMatch(/\.ui-button\s*{[^}]*var\(--motion-fast\)/s)
  })

  it('completes registration with demo code 123456 and returns to login without creating a session', async () => {
    const user = userEvent.setup()
    const onLogin = vi.fn()
    render(<AuthScreen onLogin={onLogin} />)

    await user.click(screen.getByRole('button', { name: '免费注册' }))
    await user.type(screen.getByLabelText('你的称呼'), '云杉')
    await user.type(screen.getByLabelText('邮箱地址'), 'hello@example.com')
    await user.type(screen.getByLabelText('设置密码'), 'demo-pass')
    await user.click(screen.getByLabelText('同意服务条款与隐私政策'))
    await user.click(screen.getByRole('button', { name: '创建账号' }))

    expect(screen.getByRole('heading', { name: '查看你的邮箱' })).toBeInTheDocument()
    await user.type(screen.getByLabelText('6 位验证码'), '123456')
    await user.click(screen.getByRole('button', { name: '完成演示验证' }))

    expect(screen.getByRole('heading', { name: '欢迎回来' })).toBeInTheDocument()
    expect(screen.getByText('注册演示已完成，请使用已有账号登录。')).toBeInTheDocument()
    expect(onLogin).not.toHaveBeenCalled()
  })

  it('shows registration validation inline and clears only corrected fields', async () => {
    const user = userEvent.setup()
    render(<AuthScreen onLogin={vi.fn()} />)

    await user.click(screen.getByRole('button', { name: '免费注册' }))
    const registerForm = screen.getByRole('button', { name: '创建账号' }).closest('form')
    expect(registerForm).toHaveAttribute('novalidate')
    await user.click(screen.getByRole('button', { name: '创建账号' }))

    expect(screen.getByText('请输入称呼')).toBeInTheDocument()
    expect(screen.getByText('请输入邮箱地址')).toBeInTheDocument()
    expect(screen.getByText('密码至少需要 8 位字符')).toBeInTheDocument()
    expect(screen.getByText('请先同意服务条款与隐私政策')).toBeInTheDocument()
    expect(screen.getByLabelText('你的称呼')).toHaveFocus()

    await user.type(screen.getByLabelText('你的称呼'), '云杉')
    expect(screen.queryByText('请输入称呼')).not.toBeInTheDocument()
    expect(screen.getByText('请输入邮箱地址')).toBeInTheDocument()
  })

  it('keeps registration feedback next to its field in normal document flow', async () => {
    const user = userEvent.setup()
    render(<AuthScreen onLogin={vi.fn()} />)

    await user.click(screen.getByRole('button', { name: '免费注册' }))
    await user.click(screen.getByRole('button', { name: '创建账号' }))

    const nameError = screen.getByText('请输入称呼')
    const termsError = screen.getByText('请先同意服务条款与隐私政策')
    expect(nameError.closest('[data-validation-field]')).toContainElement(screen.getByLabelText('你的称呼'))
    expect(termsError.closest('[data-validation-field]')).toContainElement(screen.getByLabelText('同意服务条款与隐私政策'))
    expect(authStyles).not.toMatch(/\.validated-field-error\s*{[^}]*position:\s*absolute/s)
  })

  it('reports registration email and password constraints without native bubbles', async () => {
    const user = userEvent.setup()
    render(<AuthScreen onLogin={vi.fn()} />)

    await user.click(screen.getByRole('button', { name: '免费注册' }))
    await user.type(screen.getByLabelText('你的称呼'), '云杉')
    await user.type(screen.getByLabelText('邮箱地址'), 'not-an-email')
    await user.type(screen.getByLabelText('设置密码'), '1234567')
    await user.click(screen.getByRole('button', { name: '创建账号' }))

    expect(screen.getByLabelText('邮箱地址')).toHaveAccessibleDescription('请输入有效的邮箱地址')
    expect(screen.getByLabelText('设置密码')).toHaveAccessibleDescription('密码至少需要 8 位字符')
    expect(screen.getByLabelText('同意服务条款与隐私政策')).toHaveAccessibleDescription('请先同意服务条款与隐私政策')
    expect(screen.getByRole('heading', { name: '创建你的工作空间' })).toBeInTheDocument()
  })

  it('keeps the user in the demo when the OTP is not 123456', async () => {
    const user = userEvent.setup()
    render(<AuthScreen onLogin={vi.fn()} />)

    await user.click(screen.getByRole('button', { name: '免费注册' }))
    await user.type(screen.getByLabelText('你的称呼'), '云杉')
    await user.type(screen.getByLabelText('邮箱地址'), 'hello@example.com')
    await user.type(screen.getByLabelText('设置密码'), 'demo-pass')
    await user.click(screen.getByLabelText('同意服务条款与隐私政策'))
    await user.click(screen.getByRole('button', { name: '创建账号' }))
    await user.type(screen.getByLabelText('6 位验证码'), '000000')
    await user.click(screen.getByRole('button', { name: '完成演示验证' }))

    expect(screen.getByRole('alert')).toHaveTextContent('演示验证码为 123456')
    expect(screen.getByRole('heading', { name: '查看你的邮箱' })).toBeInTheDocument()
  })

  it('shows OTP validation inline before applying the demo-code rule', async () => {
    const user = userEvent.setup()
    render(<AuthScreen onLogin={vi.fn()} />)

    await user.click(screen.getByRole('button', { name: '免费注册' }))
    await user.type(screen.getByLabelText('你的称呼'), '云杉')
    await user.type(screen.getByLabelText('邮箱地址'), 'hello@example.com')
    await user.type(screen.getByLabelText('设置密码'), 'demo-pass')
    await user.click(screen.getByLabelText('同意服务条款与隐私政策'))
    await user.click(screen.getByRole('button', { name: '创建账号' }))

    const otpInput = screen.getByLabelText('6 位验证码')
    const otpForm = screen.getByRole('button', { name: '完成演示验证' }).closest('form')
    expect(otpForm).toHaveAttribute('novalidate')
    await user.click(screen.getByRole('button', { name: '完成演示验证' }))
    expect(otpInput).toHaveAccessibleDescription('请输入 6 位验证码')
    expect(otpInput).toHaveFocus()

    await user.type(otpInput, '000000')
    await user.click(screen.getByRole('button', { name: '完成演示验证' }))
    expect(otpInput).toHaveAccessibleDescription('演示验证码为 123456')
  })

  it('runs the forgot-password demo back to real login without logging in', async () => {
    const user = userEvent.setup()
    const onLogin = vi.fn()
    render(<AuthScreen onLogin={onLogin} />)

    await user.click(screen.getByRole('button', { name: '忘记密码？' }))
    await user.type(screen.getByLabelText('邮箱地址'), 'hello@example.com')
    await user.click(screen.getByRole('button', { name: '发送重设邮件' }))
    await user.click(screen.getByRole('button', { name: '模拟打开邮件' }))
    await user.type(screen.getByLabelText('新密码'), 'new-demo-pass')
    await user.type(screen.getByLabelText('确认新密码'), 'new-demo-pass')
    await user.click(screen.getByRole('button', { name: '完成密码演示' }))

    expect(screen.getByRole('heading', { name: '欢迎回来' })).toBeInTheDocument()
    expect(screen.getByText('密码重设演示已完成，账号数据未发生变化。')).toBeInTheDocument()
    expect(onLogin).not.toHaveBeenCalled()
  })

  it('shows password recovery email validation without native bubbles', async () => {
    const user = userEvent.setup()
    render(<AuthScreen onLogin={vi.fn()} />)

    await user.click(screen.getByRole('button', { name: '忘记密码？' }))
    const recoveryEmail = screen.getByLabelText('邮箱地址')
    const recoveryForm = screen.getByRole('button', { name: '发送重设邮件' }).closest('form')
    expect(recoveryForm).toHaveAttribute('novalidate')
    await user.click(screen.getByRole('button', { name: '发送重设邮件' }))
    expect(recoveryEmail).toHaveAccessibleDescription('请输入邮箱地址')
    expect(recoveryEmail).toHaveFocus()

    await user.type(recoveryEmail, 'not-an-email')
    await user.click(screen.getByRole('button', { name: '发送重设邮件' }))
    expect(recoveryEmail).toHaveAccessibleDescription('请输入有效的邮箱地址')

    await user.clear(recoveryEmail)
    await user.type(recoveryEmail, 'hello@example.com')
    await user.click(screen.getByRole('button', { name: '发送重设邮件' }))
    expect(screen.getByRole('heading', { name: '邮件已发送' })).toBeInTheDocument()
  })

  it('shows new-password length and confirmation validation inline', async () => {
    const user = userEvent.setup()
    render(<AuthScreen onLogin={vi.fn()} />)

    await user.click(screen.getByRole('button', { name: '忘记密码？' }))
    await user.type(screen.getByLabelText('邮箱地址'), 'hello@example.com')
    await user.click(screen.getByRole('button', { name: '发送重设邮件' }))
    await user.click(screen.getByRole('button', { name: '模拟打开邮件' }))

    const newPasswordInput = screen.getByLabelText('新密码')
    const confirmationInput = screen.getByLabelText('确认新密码')
    const passwordForm = screen.getByRole('button', { name: '完成密码演示' }).closest('form')
    expect(passwordForm).toHaveAttribute('novalidate')
    await user.click(screen.getByRole('button', { name: '完成密码演示' }))
    expect(screen.getAllByText('密码至少需要 8 位字符')).toHaveLength(2)
    expect(newPasswordInput).toHaveFocus()

    await user.type(newPasswordInput, 'new-demo-pass')
    await user.type(confirmationInput, 'different-pass')
    await user.click(screen.getByRole('button', { name: '完成密码演示' }))
    expect(confirmationInput).toHaveAccessibleDescription('两次输入的密码不一致')

    await user.clear(confirmationInput)
    await user.type(confirmationInput, 'new-demo-pass')
    expect(confirmationInput).not.toHaveAccessibleDescription('两次输入的密码不一致')
  })

  it.each(['Google', 'Microsoft', 'Apple'])('treats %s login as a demo only', async (provider) => {
    const user = userEvent.setup()
    const onLogin = vi.fn()
    render(<AuthScreen onLogin={onLogin} />)

    await user.click(screen.getByRole('button', { name: `使用 ${provider} 登录` }))

    expect(screen.getByText(`${provider} 登录仅用于界面演示，请使用账号密码登录。`)).toBeInTheDocument()
    expect(onLogin).not.toHaveBeenCalled()
  })

  it('renders the official artwork for every third-party sign-in provider', () => {
    render(<AuthScreen onLogin={vi.fn()} />)

    for (const provider of ['Google', 'Microsoft', 'Apple']) {
      const button = screen.getByRole('button', { name: `使用 ${provider} 登录` })
      expect(button.querySelector(`[data-brand-logo="${provider.toLowerCase()}"]`)).not.toBeNull()
      expect(button.querySelector('svg.lucide')).toBeNull()
    }

    const appleBackground = screen
      .getByRole('button', { name: '使用 Apple 登录' })
      .querySelector('.auth-provider-apple-background')
    expect(appleBackground).not.toBeNull()
    expect(appleBackground).toHaveClass('auth-provider-apple-background')
    expect(authStyles).toMatch(/\.auth-provider-apple-background\s*{\s*fill:\s*transparent;/)
  })

  it('renders unavailable legal destinations as visible non-interactive text', () => {
    render(<AuthScreen onLogin={vi.fn()} />)

    expect(screen.getByText('服务条款').tagName).toBe('SPAN')
    expect(screen.getByText('隐私政策').tagName).toBe('SPAN')
  })
})
