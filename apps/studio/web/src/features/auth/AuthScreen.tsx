import { useGSAP } from '@gsap/react'
import gsap from 'gsap'
import {
  ArrowRight,
  Eye,
  EyeOff,
  LockKeyhole,
  UserRound,
} from 'lucide-react'
import { useRef, useState } from 'react'
import type { FormEvent } from 'react'

import { Button, IconButton, MOTION_DURATION_SECONDS, TextField } from '../../components/ui'
import { BrandMark } from '../../components/ui/BrandMark'
import { ThemePicker } from '../../components/ui/ThemePicker'
import { useI18n } from '../../i18n'
import { ValidatedForm } from './components/ValidatedForm'
import { ProviderBrandLogo } from './ProviderBrandLogo'
import './auth.css'

gsap.registerPlugin(useGSAP)

interface Credentials {
  username: string
  password: string
}

interface AuthValidationErrors {
  loginUsername?: string
  loginPassword?: string
}

interface AuthScreenProps {
  onLogin: (credentials: Credentials) => Promise<void> | void
  pending?: boolean
  error?: string
}

export function AuthScreen({ onLogin, pending = false, error }: AuthScreenProps) {
  const { t } = useI18n()
  const rootRef = useRef<HTMLElement>(null)
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [isPasswordVisible, setPasswordVisible] = useState(false)
  const [validationErrors, setValidationErrors] = useState<AuthValidationErrors>({})
  const [validationAttempt, setValidationAttempt] = useState(0)
  const clearValidationError = (field: keyof AuthValidationErrors) => {
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

  const submitLogin = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const nextErrors: AuthValidationErrors = {
      loginUsername: username.trim() ? undefined : t('请输入用户名'),
      loginPassword: password ? undefined : t('请输入密码'),
    }
    if (reportValidationErrors(nextErrors)) return
    await onLogin({ username: username.trim(), password })
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
    <main id="main-content" ref={rootRef} className="auth-page" aria-label={t('TinkerFin 账户登录')}>
      <h1 className="visually-hidden">{t('TinkerFin Studio 账户')}</h1>
      <div className="auth-ambient" aria-hidden="true">
        <span className="auth-signal-field">
          <span className="auth-signal-orbit auth-signal-orbit--outer"><i /></span>
          <span className="auth-signal-orbit auth-signal-orbit--middle"><i /></span>
          <span className="auth-signal-orbit auth-signal-orbit--inner"><i /></span>
          <span className="auth-signal-core"><i /><i /><i /><i /></span>
        </span>
      </div>

      <header className="auth-header">
        <div className="auth-brand">
          <BrandMark />
          <span>TinkerFin</span>
        </div>
        <ThemePicker />
      </header>

      <section className="auth-intro" aria-label={t('TinkerFin 产品简介')}>
        <p className="auth-intro-title">{t('让智能体协作，')}<br />{t('像思考一样自然')}</p>
        <p>{t('把复杂目标交给一支会协作的智能体团队')}</p>
      </section>

      <section className="auth-panel" aria-live="polite">
        <div className="auth-form-stage">
          <div className="auth-view" data-testid="real-login">
            <header className="auth-heading">
              <h2>{t('欢迎回来')}</h2>
              <p>{t('登录后继续与你的智能体团队协作')}</p>
            </header>

            {error && <p className="auth-form-error" role="alert">{error}</p>}

            <ValidatedForm className="auth-form" errors={validationErrors} validationAttempt={validationAttempt} onSubmit={submitLogin}>
              <TextField
                rootClassName="auth-field"
                id="login-username"
                name="loginUsername"
                label={t('用户名')}
                shape="capsule"
                error={validationErrors.loginUsername}
                leadingContent={<UserRound size={17} />}
                autoComplete="username"
                value={username}
                onChange={(event) => {
                  setUsername(event.target.value)
                  clearValidationError('loginUsername')
                }}
                placeholder={t('输入用户名')}
                disabled={pending}
                required
              />

              <TextField
                rootClassName="auth-field auth-login-password"
                id="login-password"
                name="loginPassword"
                label={t('密码')}
                shape="capsule"
                error={validationErrors.loginPassword}
                leadingContent={<LockKeyhole size={17} />}
                trailingContent={(
                  <IconButton
                    className="auth-password-toggle"
                    label={isPasswordVisible ? t('隐藏密码') : t('显示密码')}
                    icon={isPasswordVisible ? <EyeOff size={17} /> : <Eye size={17} />}
                    onClick={() => setPasswordVisible((current) => !current)}
                  />
                )}
                type={isPasswordVisible ? 'text' : 'password'}
                autoComplete="current-password"
                value={password}
                onChange={(event) => {
                  setPassword(event.target.value)
                  clearValidationError('loginPassword')
                }}
                placeholder={t('输入密码')}
                required
              />
              <Button className="auth-forgot-password" variant="text" size="sm">{t('忘记密码？')}</Button>

              <Button
                className="auth-primary"
                type="submit"
                variant="primary"
                size="xl"
                loading={pending}
                trailingIcon={!pending ? <ArrowRight size={17} /> : undefined}
              >
                {pending ? t('登录中…') : t('登录')}
              </Button>
            </ValidatedForm>

            <div className="auth-divider"><span>{t('其他登录方式')}</span></div>
            <div className="auth-providers">
              <Button size="lg" aria-label={t('使用 Google 登录')} leadingIcon={<ProviderBrandLogo provider="Google" />}>Google</Button>
              <Button size="lg" aria-label={t('使用 Microsoft 登录')} leadingIcon={<ProviderBrandLogo provider="Microsoft" />}>Microsoft</Button>
              <Button size="lg" aria-label={t('使用 Apple 登录')} leadingIcon={<ProviderBrandLogo provider="Apple" />}>Apple</Button>
            </div>
            <p className="auth-switch">{t('还没有账号？')} <Button variant="text" size="sm">{t('免费注册')}</Button></p>
          </div>
        </div>

        <p className="auth-legal">
          {t('登录即表示你同意 TinkerFin 的')} <span>{t('服务条款')}</span> {t('与')} <span>{t('隐私政策')}</span>
        </p>
      </section>
    </main>
  )
}
