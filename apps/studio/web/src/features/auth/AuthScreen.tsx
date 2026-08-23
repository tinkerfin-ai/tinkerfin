import { useGSAP } from '@gsap/react'
import gsap from 'gsap'
import {
  ArrowLeft,
  ArrowRight,
  Check,
  Eye,
  EyeOff,
  KeyRound,
  LockKeyhole,
  Mail,
  UserRound,
} from 'lucide-react'
import { useRef, useState } from 'react'
import type { FormEvent, ReactNode } from 'react'

import { Button, IconButton, MOTION_DURATION_SECONDS, TextField } from '../../components/ui'
import { BrandMark } from '../../components/ui/BrandMark'
import { ThemePicker } from '../../components/ui/ThemePicker'
import { ValidatedField, ValidatedForm } from './components/ValidatedForm'
import { ProviderBrandLogo } from './ProviderBrandLogo'
import './auth.css'

gsap.registerPlugin(useGSAP)

interface Credentials {
  username: string
  password: string
}

type AuthView = 'login' | 'register' | 'otp' | 'forgot' | 'reset-sent' | 'new-password'

type AuthValidationField =
  | 'loginUsername'
  | 'loginPassword'
  | 'registerName'
  | 'registerEmail'
  | 'registerPassword'
  | 'acceptedTerms'
  | 'otp'
  | 'resetEmail'
  | 'newPassword'
  | 'confirmPassword'

type AuthValidationErrors = Partial<Record<AuthValidationField, string>>

const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/

const isValidEmail = (value: string) => EMAIL_PATTERN.test(value)

interface AuthScreenProps {
  onLogin: (credentials: Credentials) => Promise<void> | void
  pending?: boolean
  error?: string
  message?: string
}

interface PasswordFieldProps {
  id: string
  fieldName: AuthValidationField
  label: ReactNode
  value: string
  onChange: (value: string) => void
  autoComplete: string
  placeholder: string
  minLength?: number
  error?: string
  className?: string
  fieldAction?: ReactNode
}

function PasswordField({
  id,
  fieldName,
  label,
  value,
  onChange,
  autoComplete,
  placeholder,
  minLength,
  error,
  className = 'auth-field',
  fieldAction,
}: PasswordFieldProps) {
  const [isVisible, setVisible] = useState(false)

  return (
    <>
      <TextField
        rootClassName={className}
        id={id}
        name={fieldName}
        error={error}
        label={label}
        shape="capsule"
        leadingContent={<LockKeyhole size={17} />}
        trailingContent={(
          <IconButton
            className="auth-password-toggle"
            label={isVisible ? '隐藏密码' : '显示密码'}
            icon={isVisible ? <EyeOff size={17} /> : <Eye size={17} />}
            onClick={() => setVisible((current) => !current)}
          />
        )}
        type={isVisible ? 'text' : 'password'}
        autoComplete={autoComplete}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        minLength={minLength}
        required
      />
      {fieldAction}
    </>
  )
}

function PrimaryButton({ children, pending = false }: { children: ReactNode; pending?: boolean }) {
  return (
    <Button
      className="auth-primary"
      type="submit"
      variant="primary"
      size="xl"
      loading={pending}
      trailingIcon={!pending ? <ArrowRight size={17} /> : undefined}
    >
      {pending ? '登录中...' : children}
    </Button>
  )
}

export function AuthScreen({ onLogin, pending = false, error, message }: AuthScreenProps) {
  const rootRef = useRef<HTMLElement>(null)
  const [view, setView] = useState<AuthView>('login')
  const [demoMessage, setDemoMessage] = useState<string>()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [validationErrors, setValidationErrors] = useState<AuthValidationErrors>({})
  const [validationAttempt, setValidationAttempt] = useState(0)
  const [displayName, setDisplayName] = useState('')
  const [email, setEmail] = useState('')
  const [registerPassword, setRegisterPassword] = useState('')
  const [hasAcceptedTerms, setAcceptedTerms] = useState(false)
  const [otp, setOtp] = useState('')
  const [resetEmail, setResetEmail] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const clearValidationError = (field: AuthValidationField) => {
    setValidationErrors((current) => {
      if (!current[field]) return current
      const next = { ...current }
      delete next[field]
      return next
    })
  }

  const reportValidationErrors = (nextErrors: AuthValidationErrors) => {
    setValidationErrors(nextErrors)
    const hasErrors = Object.values(nextErrors).some(Boolean)
    if (hasErrors) setValidationAttempt((current) => current + 1)
    return hasErrors
  }

  const returnToLogin = (nextMessage?: string) => {
    setView('login')
    setDemoMessage(nextMessage)
    setValidationErrors({})
  }

  const submitLogin = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const nextErrors: AuthValidationErrors = {
      loginUsername: username.trim() ? undefined : '请输入用户名',
      loginPassword: password ? undefined : '请输入密码',
    }
    if (reportValidationErrors(nextErrors)) return
    await onLogin({ username: username.trim(), password })
  }

  const submitRegistration = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const nextErrors: AuthValidationErrors = {
      registerName: displayName.trim() ? undefined : '请输入称呼',
      registerEmail: !email.trim()
        ? '请输入邮箱地址'
        : isValidEmail(email.trim()) ? undefined : '请输入有效的邮箱地址',
      registerPassword: registerPassword.length >= 8 ? undefined : '密码至少需要 8 位字符',
      acceptedTerms: hasAcceptedTerms ? undefined : '请先同意服务条款与隐私政策',
    }
    if (reportValidationErrors(nextErrors)) return
    setView('otp')
  }

  const submitOtp = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const otpError = otp.length !== 6
      ? '请输入 6 位验证码'
      : otp === '123456' ? undefined : '演示验证码为 123456'
    if (reportValidationErrors({ otp: otpError })) return
    returnToLogin('注册演示已完成，请使用已有账号登录。')
  }

  const submitPasswordRecovery = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const normalizedEmail = resetEmail.trim()
    const resetEmailError = !normalizedEmail
      ? '请输入邮箱地址'
      : isValidEmail(normalizedEmail) ? undefined : '请输入有效的邮箱地址'
    if (reportValidationErrors({ resetEmail: resetEmailError })) return
    setView('reset-sent')
  }

  const submitNewPassword = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const nextErrors: AuthValidationErrors = {
      newPassword: newPassword.length >= 8 ? undefined : '密码至少需要 8 位字符',
      confirmPassword: confirmPassword.length < 8
        ? '密码至少需要 8 位字符'
        : newPassword === confirmPassword ? undefined : '两次输入的密码不一致',
    }
    if (reportValidationErrors(nextErrors)) return
    returnToLogin('密码重设演示已完成，账号数据未发生变化。')
  }

  const showProviderDemo = (provider: string) => {
    returnToLogin(`${provider} 登录仅用于界面演示，请使用账号密码登录。`)
  }

  useGSAP(() => {
    const root = rootRef.current
    if (!root) return

    const motion = gsap.matchMedia()
    motion.add('(prefers-reduced-motion: no-preference)', () => {
      const targets = root.querySelectorAll<HTMLElement>(
        '.auth-header, .auth-intro, .auth-signal-field, .auth-panel',
      )
      const entrance = gsap.fromTo(targets, {
        autoAlpha: 0,
        y: 12,
      }, {
        autoAlpha: 1,
        y: 0,
        duration: MOTION_DURATION_SECONDS.slow,
        ease: 'power2.out',
        stagger: MOTION_DURATION_SECONDS.fast / 3,
        clearProps: 'opacity,visibility,transform',
      })
      return () => entrance.kill()
    })

    return () => motion.revert()
  }, { scope: rootRef })

  return (
    <main id="main-content" ref={rootRef} className="auth-page" aria-label="TinkerFin 账户登录">
      <h1 className="visually-hidden">TinkerFin Studio 账户</h1>
      <div className="auth-ambient" aria-hidden="true">
        <span className="auth-signal-field">
          <span className="auth-signal-orbit auth-signal-orbit--outer"><i /></span>
          <span className="auth-signal-orbit auth-signal-orbit--middle"><i /></span>
          <span className="auth-signal-orbit auth-signal-orbit--inner"><i /></span>
          <span className="auth-signal-core"><i /><i /><i /><i /></span>
        </span>
      </div>

      <header className="auth-header">
        <button className="auth-brand" type="button" onClick={() => returnToLogin()} aria-label="返回 TinkerFin 登录页">
          <BrandMark />
          <span>TinkerFin</span>
        </button>
        <ThemePicker />
      </header>

      <section className="auth-intro" aria-label="TinkerFin 产品简介">
        <p className="auth-intro-title">让智能体协作，<br />像思考一样自然</p>
        <p>把复杂目标交给一支会协作的智能体团队</p>
      </section>

      <section className="auth-panel" aria-live="polite">
        <div className="auth-form-stage" data-auth-view={view}>
          {view !== 'login' && (
            <Button className="auth-back" variant="text" leadingIcon={<ArrowLeft size={17} />} onClick={() => returnToLogin()}>
              返回登录
            </Button>
          )}

          {view === 'login' && (
            <div className="auth-view" data-testid="real-login">
              <header className="auth-heading">
                <h2>欢迎回来</h2>
                <p>登录后继续与你的智能体团队协作。</p>
              </header>

              {(demoMessage || message) && <p className="auth-demo-message" role="status">{demoMessage ?? message}</p>}
              {error && <p className="auth-form-error" role="alert">{error}</p>}

              <ValidatedForm className="auth-form" errors={validationErrors} validationAttempt={validationAttempt} onSubmit={submitLogin}>
                <TextField
                  rootClassName="auth-field"
                  id="login-username"
                  name="loginUsername"
                  label="用户名"
                  shape="capsule"
                  error={validationErrors.loginUsername}
                  leadingContent={<UserRound size={17} />}
                  autoComplete="username"
                  value={username}
                  onChange={(event) => {
                    setUsername(event.target.value)
                    clearValidationError('loginUsername')
                  }}
                  placeholder="输入用户名"
                  disabled={pending}
                  required
                />

                <PasswordField
                  id="login-password"
                  fieldName="loginPassword"
                  label="密码"
                  value={password}
                  onChange={(value) => {
                    setPassword(value)
                    clearValidationError('loginPassword')
                  }}
                  autoComplete="current-password"
                  placeholder="输入密码"
                  error={validationErrors.loginPassword}
                  className="auth-field auth-login-password"
                  fieldAction={<Button
                    className="auth-forgot-password"
                    variant="text"
                    size="sm"
                    onClick={() => { setView('forgot'); setDemoMessage(undefined); setValidationErrors({}) }}
                  >
                    忘记密码？
                  </Button>}
                />

                <PrimaryButton pending={pending}>登录</PrimaryButton>
              </ValidatedForm>

              <div className="auth-divider"><span>其他登录方式</span></div>
              <div className="auth-providers">
                <Button aria-label="使用 Google 登录" leadingIcon={<ProviderBrandLogo provider="Google" />} onClick={() => showProviderDemo('Google')}>Google</Button>
                <Button aria-label="使用 Microsoft 登录" leadingIcon={<ProviderBrandLogo provider="Microsoft" />} onClick={() => showProviderDemo('Microsoft')}>Microsoft</Button>
                <Button aria-label="使用 Apple 登录" leadingIcon={<ProviderBrandLogo provider="Apple" />} onClick={() => showProviderDemo('Apple')}>Apple</Button>
              </div>
              <p className="auth-switch">还没有账号？ <Button variant="text" size="sm" onClick={() => { setView('register'); setDemoMessage(undefined); setValidationErrors({}) }}>免费注册</Button></p>
            </div>
          )}

          {view === 'register' && (
            <div className="auth-view">
              <header className="auth-heading">
                <h2>创建你的工作空间</h2>
                <p>这是界面演示，不会创建真实账号。</p>
              </header>
              <ValidatedForm className="auth-form" errors={validationErrors} validationAttempt={validationAttempt} onSubmit={submitRegistration}>
                <TextField rootClassName="auth-field" id="register-name" name="registerName" label="你的称呼" shape="capsule" error={validationErrors.registerName} leadingContent={<UserRound size={17} />} value={displayName} onChange={(event) => { setDisplayName(event.target.value); clearValidationError('registerName') }} placeholder="例如：云杉" required />
                <TextField rootClassName="auth-field" id="register-email" name="registerEmail" label="邮箱地址" shape="capsule" error={validationErrors.registerEmail} leadingContent={<Mail size={17} />} type="email" value={email} onChange={(event) => { setEmail(event.target.value); clearValidationError('registerEmail') }} placeholder="name@company.com" required />
                <PasswordField id="register-password" fieldName="registerPassword" label="设置密码" value={registerPassword} onChange={(value) => { setRegisterPassword(value); clearValidationError('registerPassword') }} autoComplete="new-password" placeholder="至少 8 位字符" minLength={8} error={validationErrors.registerPassword} />
                <ValidatedField controlId="accepted-terms" error={validationErrors.acceptedTerms} fieldName="acceptedTerms">
                  {({ controlAria, feedbackAttributes }) => (
                    <label className="auth-check" {...feedbackAttributes}>
                      <input {...controlAria} id="accepted-terms" type="checkbox" checked={hasAcceptedTerms} onChange={(event) => { setAcceptedTerms(event.target.checked); clearValidationError('acceptedTerms') }} aria-label="同意服务条款与隐私政策" required />
                      <span><Check size={13} /></span>
                      <small>我已阅读并同意服务条款与隐私政策</small>
                    </label>
                  )}
                </ValidatedField>
                <PrimaryButton>创建账号</PrimaryButton>
              </ValidatedForm>
            </div>
          )}

          {view === 'otp' && (
            <div className="auth-view auth-view--compact">
              <span className="auth-status-icon" aria-hidden="true"><Mail size={24} /></span>
              <header className="auth-heading auth-heading--center">
                <h2>查看你的邮箱</h2>
                <p>验证码已发送至 <strong>{email}</strong>，演示验证码为 123456。</p>
              </header>
              <ValidatedForm className="auth-form" errors={validationErrors} validationAttempt={validationAttempt} onSubmit={submitOtp}>
                <ValidatedField className="auth-field auth-field--center" controlId="demo-otp" error={validationErrors.otp} fieldName="otp" label="输入验证码">
                  {({ controlAria, feedbackAttributes }) => (
                    <input {...controlAria} {...feedbackAttributes} className="auth-otp" id="demo-otp" aria-label="6 位验证码" inputMode="numeric" pattern="[0-9]{6}" maxLength={6} value={otp} onChange={(event) => { setOtp(event.target.value.replace(/\D/g, '')); clearValidationError('otp') }} required autoFocus />
                  )}
                </ValidatedField>
                <PrimaryButton>完成演示验证</PrimaryButton>
              </ValidatedForm>
            </div>
          )}

          {view === 'forgot' && (
            <div className="auth-view auth-view--compact">
              <span className="auth-status-icon" aria-hidden="true"><KeyRound size={24} /></span>
              <header className="auth-heading auth-heading--center">
                <h2>找回密码</h2>
                <p>输入注册邮箱，继续前端演示流程。</p>
              </header>
              <ValidatedForm className="auth-form" errors={validationErrors} validationAttempt={validationAttempt} onSubmit={submitPasswordRecovery}>
                <TextField rootClassName="auth-field" id="reset-email" name="resetEmail" label="邮箱地址" shape="capsule" error={validationErrors.resetEmail} leadingContent={<Mail size={17} />} type="email" value={resetEmail} onChange={(event) => { setResetEmail(event.target.value); clearValidationError('resetEmail') }} placeholder="name@company.com" required autoFocus />
                <PrimaryButton>发送重设邮件</PrimaryButton>
              </ValidatedForm>
            </div>
          )}

          {view === 'reset-sent' && (
            <div className="auth-view auth-view--compact">
              <span className="auth-status-icon" aria-hidden="true"><Mail size={24} /></span>
              <header className="auth-heading auth-heading--center">
                <h2>邮件已发送</h2>
                <p>演示重设链接已发送至 <strong>{resetEmail}</strong>。</p>
              </header>
              <Button className="auth-primary" variant="primary" size="xl" trailingIcon={<ArrowRight size={17} />} onClick={() => { setView('new-password'); setValidationErrors({}) }}>模拟打开邮件</Button>
            </div>
          )}

          {view === 'new-password' && (
            <div className="auth-view auth-view--compact">
              <header className="auth-heading auth-heading--center">
                <h2>设置新密码</h2>
                <p>此步骤只验证界面，不会修改账号数据。</p>
              </header>
              <ValidatedForm className="auth-form" errors={validationErrors} validationAttempt={validationAttempt} onSubmit={submitNewPassword}>
                <PasswordField id="new-password" fieldName="newPassword" label="新密码" value={newPassword} onChange={(value) => { setNewPassword(value); clearValidationError('newPassword') }} autoComplete="new-password" placeholder="至少 8 位字符" minLength={8} error={validationErrors.newPassword} />
                <PasswordField id="confirm-password" fieldName="confirmPassword" label="确认新密码" value={confirmPassword} onChange={(value) => { setConfirmPassword(value); clearValidationError('confirmPassword') }} autoComplete="new-password" placeholder="再次输入新密码" minLength={8} error={validationErrors.confirmPassword} />
                <PrimaryButton>完成密码演示</PrimaryButton>
              </ValidatedForm>
            </div>
          )}
        </div>

        <p className="auth-legal">
          登录即表示你同意 TinkerFin 的 <span>服务条款</span> 与 <span>隐私政策</span>
        </p>
      </section>
    </main>
  )
}
