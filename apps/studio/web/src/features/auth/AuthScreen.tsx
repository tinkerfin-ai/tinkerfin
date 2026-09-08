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

import { BrandLogo, Button, IconButton, MOTION_DURATION_SECONDS, TextField } from '../../components/ui'
import { ThemePicker } from '../../components/ui/ThemePicker'
import { useI18n } from '../../i18n'
import { ValidatedForm } from '../../components/ui/ValidatedForm'
import { ProviderBrandLogo } from './ProviderBrandLogo'
import './auth.css'

gsap.registerPlugin(useGSAP)

// 环境光带使用长周期漂移，避免与表单交互动效形成节奏竞争
const AMBIENT_DRIFT_SECONDS = {
  blue: 7,
  violet: 9,
  mist: 8,
} as const

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
}

export function AuthScreen({ onLogin, pending = false }: AuthScreenProps) {
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
        '.auth-header, .auth-intro, .auth-ambient, .auth-panel',
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

      const ambientTweens: gsap.core.Tween[] = []
      const blueRibbon = root.querySelector<SVGGElement>('.auth-ribbon--blue')
      const violetRibbon = root.querySelector<SVGGElement>('.auth-ribbon--violet')
      const ambientMist = root.querySelector<HTMLElement>('.auth-ambient-mist')

      if (blueRibbon) {
        ambientTweens.push(gsap.to(blueRibbon, {
          xPercent: 5,
          yPercent: 7,
          rotation: 2,
          scale: 1.035,
          duration: AMBIENT_DRIFT_SECONDS.blue,
          ease: 'sine.inOut',
          repeat: -1,
          yoyo: true,
          force3D: true,
        }))
      }
      if (violetRibbon) {
        ambientTweens.push(gsap.to(violetRibbon, {
          xPercent: -5,
          yPercent: -6,
          rotation: -2,
          scale: 1.04,
          duration: AMBIENT_DRIFT_SECONDS.violet,
          ease: 'sine.inOut',
          repeat: -1,
          yoyo: true,
          force3D: true,
        }))
      }
      if (ambientMist) {
        ambientTweens.push(gsap.to(ambientMist, {
          xPercent: 7,
          yPercent: -7,
          scale: 1.12,
          duration: AMBIENT_DRIFT_SECONDS.mist,
          ease: 'sine.inOut',
          repeat: -1,
          yoyo: true,
          force3D: true,
        }))
      }

      return () => {
        entrance.kill()
        ambientTweens.forEach((tween) => tween.kill())
      }
    })

    return () => motion.revert()
  }, { scope: rootRef })

  return (
    <main id="main-content" ref={rootRef} className="auth-page" aria-label={t('TinkerFin 账户登录')}>
      <h1 className="visually-hidden">{t('TinkerFin Studio 账户')}</h1>
      <div className="auth-ambient" aria-hidden="true">
        <svg
          className="auth-ambient-art"
          viewBox="0 0 1000 1000"
          preserveAspectRatio="xMidYMid slice"
          focusable="false"
          aria-hidden="true"
        >
          <defs>
            <linearGradient id="auth-ribbon-blue-gradient" x1="0%" y1="0%" x2="100%" y2="0%">
              <stop offset="0%" stopColor="rgb(108 190 255)" stopOpacity="0" />
              <stop offset="20%" stopColor="rgb(108 190 255)" stopOpacity=".18" />
              <stop offset="48%" stopColor="rgb(72 158 255)" stopOpacity=".42" />
              <stop offset="76%" stopColor="rgb(126 205 255)" stopOpacity=".2" />
              <stop offset="100%" stopColor="rgb(126 205 255)" stopOpacity="0" />
            </linearGradient>
            <linearGradient id="auth-ribbon-violet-gradient" x1="0%" y1="0%" x2="100%" y2="0%">
              <stop offset="0%" stopColor="rgb(190 163 255)" stopOpacity="0" />
              <stop offset="18%" stopColor="rgb(190 163 255)" stopOpacity=".16" />
              <stop offset="50%" stopColor="rgb(143 104 255)" stopOpacity=".36" />
              <stop offset="76%" stopColor="rgb(194 169 255)" stopOpacity=".18" />
              <stop offset="100%" stopColor="rgb(194 169 255)" stopOpacity="0" />
            </linearGradient>
            <filter id="auth-ribbon-soft-filter" x="-30%" y="-30%" width="160%" height="160%" colorInterpolationFilters="sRGB">
              <feGaussianBlur stdDeviation="32" />
            </filter>
            <filter id="auth-ribbon-body-filter" x="-30%" y="-30%" width="160%" height="160%" colorInterpolationFilters="sRGB">
              <feGaussianBlur stdDeviation="8" />
            </filter>
          </defs>

          <g className="auth-ribbon auth-ribbon--blue">
            <path className="auth-ribbon__soft" d="M -160 260 C 70 46 280 232 544 82 C 706 -10 862 -54 1110 -164" stroke="url(#auth-ribbon-blue-gradient)" strokeWidth="190" filter="url(#auth-ribbon-soft-filter)" />
            <path className="auth-ribbon__body" d="M -160 260 C 70 46 280 232 544 82 C 706 -10 862 -54 1110 -164" stroke="url(#auth-ribbon-blue-gradient)" strokeWidth="120" filter="url(#auth-ribbon-body-filter)" />
          </g>

          <g className="auth-ribbon auth-ribbon--violet">
            <path className="auth-ribbon__soft" d="M 120 1160 C 330 820 490 1030 700 780 C 824 632 964 626 1160 510" stroke="url(#auth-ribbon-violet-gradient)" strokeWidth="200" filter="url(#auth-ribbon-soft-filter)" />
            <path className="auth-ribbon__body" d="M 120 1160 C 330 820 490 1030 700 780 C 824 632 964 626 1160 510" stroke="url(#auth-ribbon-violet-gradient)" strokeWidth="128" filter="url(#auth-ribbon-body-filter)" />
          </g>
        </svg>
        <span className="auth-ambient-mist" />
      </div>

      <header className="auth-header">
        <div className="auth-brand">
          <BrandLogo size="md" />
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
