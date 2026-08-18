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

import { BrandMark } from '../../components/BrandMark'
import { ThemePicker } from '../../components/ThemePicker'
import { ValidatedField, ValidatedForm } from '../../components/forms/ValidatedForm'
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
    <ValidatedField
      className={className}
      controlId={id}
      error={error}
      fieldName={fieldName}
      label={label}
    >
      {({ controlAria, feedbackAttributes }) => (
        <>
          <span className="auth-input-wrap" {...feedbackAttributes}>
            <LockKeyhole size={17} aria-hidden="true" />
            <input
              {...controlAria}
              id={id}
              type={isVisible ? 'text' : 'password'}
              autoComplete={autoComplete}
              value={value}
              onChange={(event) => onChange(event.target.value)}
              placeholder={placeholder}
              minLength={minLength}
              required
            />
            <button
              className="auth-password-toggle"
              type="button"
              aria-label={isVisible ? '隐藏密码' : '显示密码'}
              onClick={() => setVisible((current) => !current)}
            >
              {isVisible ? <EyeOff size={17} /> : <Eye size={17} />}
            </button>
          </span>
          {fieldAction}
        </>
      )}
    </ValidatedField>
  )
}

function PrimaryButton({ children, pending = false }: { children: ReactNode; pending?: boolean }) {
  return (
    <button className="auth-primary" type="submit" disabled={pending}>
      <span>{pending ? '登录中...' : children}</span>
      {pending ? <span className="auth-button-spinner" aria-hidden="true" /> : <ArrowRight size={17} aria-hidden="true" />}
    </button>
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

    const brand = root.querySelector<HTMLElement>('.auth-header')
    const introHeading = root.querySelector<HTMLElement>('.auth-intro h2')
    const introCopy = root.querySelector<HTMLElement>('.auth-intro p')
    const signalField = root.querySelector<HTMLElement>('.auth-signal-field')
    const panel = root.querySelector<HTMLElement>('.auth-panel')
    const topGlow = root.querySelector<HTMLElement>('.auth-glow--top')
    const bottomGlow = root.querySelector<HTMLElement>('.auth-glow--bottom')
    const staticTargets = [brand, introHeading, introCopy, signalField, panel, topGlow, bottomGlow].filter(
      (target): target is HTMLElement => target !== null,
    )
    const motion = gsap.matchMedia()

    motion.add(
      {
        isDesktop: '(min-width: 1024px)',
        isMobile: '(max-width: 1023px)',
        reduceMotion: '(prefers-reduced-motion: reduce)',
      },
      (context) => {
        const { isDesktop, reduceMotion } = context.conditions as {
          isDesktop: boolean
          isMobile: boolean
          reduceMotion: boolean
        }
        const ambient: gsap.core.Animation[] = []
        const authView = root.querySelector<HTMLElement>('.auth-view')
        const authItems = authView
          ? Array.from(authView.querySelectorAll<HTMLElement>(
              '.auth-heading, .auth-form > *, .auth-divider, .auth-providers > button, .auth-switch',
            ))
          : []

        if (reduceMotion) {
          gsap.set([...staticTargets, ...authItems], { clearProps: 'all' })
          return
        }

        const intro = gsap.timeline({ defaults: { ease: 'power3.out' } })
        if (isDesktop) {
          if (brand) intro.from(brand, { autoAlpha: 0, y: -8, duration: 0.62 }, 0)
          if (introHeading) intro.from(introHeading, { autoAlpha: 0, y: 22, duration: 0.86 }, 0.16)
          if (introCopy) intro.from(introCopy, { autoAlpha: 0, y: 14, duration: 0.7 }, 0.26)
          if (panel) intro.from(panel, { autoAlpha: 0, x: 24, duration: 0.82 }, 0.26)
          if (signalField) intro.from(signalField, { autoAlpha: 0, scale: 0.9, duration: 0.94 }, 0.32)
          if (authItems.length) {
            intro.fromTo(
              authItems,
              { autoAlpha: 0, y: 10 },
              {
                autoAlpha: 1,
                y: 0,
                duration: 0.42,
                stagger: 0.035,
                clearProps: 'opacity,visibility,transform',
              },
              0.45,
            )
          }
        } else {
          if (brand) intro.from(brand, { autoAlpha: 0, y: -7, duration: 0.5 }, 0)
          if (panel) intro.from(panel, { autoAlpha: 0, y: 14, duration: 0.7 }, 0.08)
          if (authItems.length) {
            intro.fromTo(
              authItems,
              { autoAlpha: 0, y: 9 },
              {
                autoAlpha: 1,
                y: 0,
                duration: 0.38,
                stagger: 0.03,
                clearProps: 'opacity,visibility,transform',
              },
              0.2,
            )
          }
        }

        if (topGlow) {
          ambient.push(gsap.to(topGlow, {
            xPercent: 7,
            yPercent: -4,
            scale: 1.06,
            duration: 11,
            repeat: -1,
            yoyo: true,
            ease: 'sine.inOut',
          }))
        }
        if (bottomGlow) {
          ambient.push(gsap.to(bottomGlow, {
            xPercent: -6,
            yPercent: 6,
            scale: 0.95,
            duration: 14,
            repeat: -1,
            yoyo: true,
            ease: 'sine.inOut',
          }))
        }

        if (isDesktop && signalField) {
          ambient.push(gsap.to(signalField, {
            x: 10,
            y: -8,
            rotation: 1.5,
            duration: 9,
            repeat: -1,
            yoyo: true,
            ease: 'sine.inOut',
          }))
          const orbitMotion = [
            ['.auth-signal-orbit--outer', 360, 30],
            ['.auth-signal-orbit--middle', -360, 38],
            ['.auth-signal-orbit--inner', 360, 24],
          ] as const
          orbitMotion.forEach(([selector, rotation, duration]) => {
            const orbit = root.querySelector<HTMLElement>(selector)
            if (orbit) ambient.push(gsap.to(orbit, { rotation, duration, repeat: -1, ease: 'none' }))
          })
          const coreItems = root.querySelectorAll<HTMLElement>('.auth-signal-core i')
          if (coreItems.length) {
            ambient.push(gsap.to(coreItems, {
              autoAlpha: 0.5,
              scale: 0.72,
              duration: 1.8,
              stagger: 0.18,
              repeat: -1,
              yoyo: true,
              ease: 'sine.inOut',
            }))
          }
        }

        return () => {
          intro.kill()
          ambient.forEach((animation) => animation.kill())
        }
      },
    )

    return () => motion.revert()

  }, { scope: rootRef })

  return (
    <main ref={rootRef} className="auth-page" aria-label="TinkerFin 账户登录">
      <div className="auth-ambient" aria-hidden="true">
        <span className="auth-glow auth-glow--top" />
        <span className="auth-glow auth-glow--bottom" />
        <span className="auth-signal-field">
          <span className="auth-signal-orbit auth-signal-orbit--outer"><i /></span>
          <span className="auth-signal-orbit auth-signal-orbit--middle"><i /></span>
          <span className="auth-signal-orbit auth-signal-orbit--inner"><i /></span>
          <span className="auth-signal-core"><i /><i /><i /><i /></span>
        </span>
        <span className="auth-grain" />
      </div>

      <header className="auth-header">
        <button className="auth-brand" type="button" onClick={() => returnToLogin()} aria-label="返回 TinkerFin 登录页">
          <BrandMark />
          <span>TinkerFin</span>
        </button>
        <ThemePicker />
      </header>

      <section className="auth-intro" aria-label="TinkerFin 产品简介">
        <h2>让智能体协作，<br />像思考一样自然</h2>
        <p>把复杂目标交给一支会协作的智能体团队</p>
      </section>

      <section className="auth-panel" aria-live="polite">
        <div className="auth-form-stage" data-auth-view={view}>
          {view !== 'login' && (
            <button className="auth-back" type="button" onClick={() => returnToLogin()}>
              <ArrowLeft size={17} />返回登录
            </button>
          )}

          {view === 'login' && (
            <div className="auth-view" data-testid="real-login">
              <header className="auth-heading">
                <h1>欢迎回来</h1>
                <p>登录后继续与你的智能体团队协作。</p>
              </header>

              {(demoMessage || message) && <p className="auth-demo-message" role="status">{demoMessage ?? message}</p>}
              {error && <p className="auth-form-error" role="alert">{error}</p>}

              <ValidatedForm className="auth-form" errors={validationErrors} validationAttempt={validationAttempt} onSubmit={submitLogin}>
                <ValidatedField className="auth-field" controlId="login-username" error={validationErrors.loginUsername} fieldName="loginUsername" label="用户名">
                  {({ controlAria, feedbackAttributes }) => (
                    <span className="auth-input-wrap" {...feedbackAttributes}>
                      <UserRound size={17} aria-hidden="true" />
                      <input
                        {...controlAria}
                        id="login-username"
                        name="username"
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
                    </span>
                  )}
                </ValidatedField>

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
                  fieldAction={<button
                    className="auth-forgot-password"
                    type="button"
                    onClick={() => { setView('forgot'); setDemoMessage(undefined); setValidationErrors({}) }}
                  >
                    忘记密码？
                  </button>}
                />

                <PrimaryButton pending={pending}>登录</PrimaryButton>
              </ValidatedForm>

              <div className="auth-divider"><span>其他登录方式</span></div>
              <div className="auth-providers">
                <button type="button" aria-label="使用 Google 登录" onClick={() => showProviderDemo('Google')}><ProviderBrandLogo provider="Google" />Google</button>
                <button type="button" aria-label="使用 Microsoft 登录" onClick={() => showProviderDemo('Microsoft')}><ProviderBrandLogo provider="Microsoft" />Microsoft</button>
                <button type="button" aria-label="使用 Apple 登录" onClick={() => showProviderDemo('Apple')}><ProviderBrandLogo provider="Apple" />Apple</button>
              </div>
              <p className="auth-switch">还没有账号？ <button type="button" onClick={() => { setView('register'); setDemoMessage(undefined); setValidationErrors({}) }}>免费注册</button></p>
            </div>
          )}

          {view === 'register' && (
            <div className="auth-view">
              <header className="auth-heading">
                <h1>创建你的工作空间</h1>
                <p>这是界面演示，不会创建真实账号。</p>
              </header>
              <ValidatedForm className="auth-form" errors={validationErrors} validationAttempt={validationAttempt} onSubmit={submitRegistration}>
                <ValidatedField className="auth-field" controlId="register-name" error={validationErrors.registerName} fieldName="registerName" label="你的称呼">
                  {({ controlAria, feedbackAttributes }) => (
                    <span className="auth-input-wrap" {...feedbackAttributes}>
                      <UserRound size={17} />
                      <input {...controlAria} id="register-name" value={displayName} onChange={(event) => { setDisplayName(event.target.value); clearValidationError('registerName') }} placeholder="例如：云杉" required />
                    </span>
                  )}
                </ValidatedField>
                <ValidatedField className="auth-field" controlId="register-email" error={validationErrors.registerEmail} fieldName="registerEmail" label="邮箱地址">
                  {({ controlAria, feedbackAttributes }) => (
                    <span className="auth-input-wrap" {...feedbackAttributes}>
                      <Mail size={17} />
                      <input {...controlAria} id="register-email" type="email" value={email} onChange={(event) => { setEmail(event.target.value); clearValidationError('registerEmail') }} placeholder="name@company.com" required />
                    </span>
                  )}
                </ValidatedField>
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
                <h1>查看你的邮箱</h1>
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
                <h1>找回密码</h1>
                <p>输入注册邮箱，继续前端演示流程。</p>
              </header>
              <ValidatedForm className="auth-form" errors={validationErrors} validationAttempt={validationAttempt} onSubmit={submitPasswordRecovery}>
                <ValidatedField className="auth-field" controlId="reset-email" error={validationErrors.resetEmail} fieldName="resetEmail" label="邮箱地址">
                  {({ controlAria, feedbackAttributes }) => (
                    <span className="auth-input-wrap" {...feedbackAttributes}>
                      <Mail size={17} />
                      <input {...controlAria} id="reset-email" type="email" value={resetEmail} onChange={(event) => { setResetEmail(event.target.value); clearValidationError('resetEmail') }} placeholder="name@company.com" required autoFocus />
                    </span>
                  )}
                </ValidatedField>
                <PrimaryButton>发送重设邮件</PrimaryButton>
              </ValidatedForm>
            </div>
          )}

          {view === 'reset-sent' && (
            <div className="auth-view auth-view--compact">
              <span className="auth-status-icon" aria-hidden="true"><Mail size={24} /></span>
              <header className="auth-heading auth-heading--center">
                <h1>邮件已发送</h1>
                <p>演示重设链接已发送至 <strong>{resetEmail}</strong>。</p>
              </header>
              <button className="auth-primary" type="button" onClick={() => { setView('new-password'); setValidationErrors({}) }}><span>模拟打开邮件</span><ArrowRight size={17} /></button>
            </div>
          )}

          {view === 'new-password' && (
            <div className="auth-view auth-view--compact">
              <header className="auth-heading auth-heading--center">
                <h1>设置新密码</h1>
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
          登录即表示你同意 TinkerFin 的 <button type="button" disabled>服务条款</button> 与 <button type="button" disabled>隐私政策</button>
        </p>
      </section>
    </main>
  )
}
