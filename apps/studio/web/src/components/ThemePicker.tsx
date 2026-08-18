import { useGSAP } from '@gsap/react'
import gsap from 'gsap'
import { Monitor, MoonStar, Sun } from 'lucide-react'
import { useEffect, useId, useRef, useState } from 'react'

import {
  applyThemePreference,
  persistThemePreference,
  readThemePreference,
  subscribeToSystemTheme,
  THEME_MEDIA_QUERY,
  type ThemePreference,
} from '../theme'

gsap.registerPlugin(useGSAP)

const THEME_OPTIONS = [
  { value: 'system', label: '跟随系统', icon: Monitor },
  { value: 'light', label: '浅色', icon: Sun },
  { value: 'dark', label: '深色', icon: MoonStar },
] as const satisfies ReadonlyArray<{
  value: ThemePreference
  label: string
  icon: typeof Monitor
}>

export function ThemePicker() {
  const [preference, setPreference] = useState<ThemePreference>(readThemePreference)
  const [isExpanded, setIsExpanded] = useState(false)
  const optionName = useId()
  const switcherRef = useRef<HTMLFieldSetElement>(null)
  const expansionAnimationsRef = useRef<gsap.core.Animation[]>([])
  const isExpandedRef = useRef(false)
  const lastPointerTypeRef = useRef<'keyboard' | 'mouse' | 'pen' | 'touch'>('keyboard')

  useEffect(() => {
    const mediaQuery = window.matchMedia(THEME_MEDIA_QUERY)
    applyThemePreference(preference, mediaQuery)
    return subscribeToSystemTheme(preference, mediaQuery)
  }, [preference])

  useGSAP(() => {
    const switcher = switcherRef.current
    const circle = switcher?.querySelector<HTMLElement>('.theme-switcher-circle')
    const surface = switcher?.querySelector<HTMLElement>('.theme-switcher-surface')
    const options = switcher
      ? [...switcher.querySelectorAll<HTMLElement>('.theme-switcher-option')]
      : []
    if (!circle || !surface || options.length === 0) return

    const media = gsap.matchMedia()
    media.add({
      allowMotion: '(prefers-reduced-motion: no-preference)',
      reduceMotion: '(prefers-reduced-motion: reduce)',
    }, (context) => {
      const duration = context.conditions?.reduceMotion ? 0 : 0.22
      const animations = [
        gsap.to(surface, {
          width: '100%',
          autoAlpha: 1,
          duration,
          ease: 'power2.out',
          overwrite: 'auto',
          paused: true,
        }),
        gsap.to(circle, {
          autoAlpha: 0,
          duration: context.conditions?.reduceMotion ? 0 : 0.12,
          ease: 'power1.out',
          overwrite: 'auto',
          paused: true,
        }),
        gsap.to(options, {
          x: 0,
          autoAlpha: 1,
          duration: context.conditions?.reduceMotion ? 0 : 0.18,
          ease: 'power2.out',
          stagger: context.conditions?.reduceMotion ? 0 : { each: 0.018, from: 'end' },
          overwrite: 'auto',
          paused: true,
        }),
      ]
      const initialProgress = isExpandedRef.current ? 1 : 0
      animations.forEach((animation) => animation.progress(initialProgress))
      expansionAnimationsRef.current = animations

      return () => {
        expansionAnimationsRef.current = []
      }
    })

    return () => media.revert()
  }, {
    dependencies: [preference],
    scope: switcherRef,
    revertOnUpdate: true,
  })

  const updateExpansion = (nextExpanded: boolean) => {
    isExpandedRef.current = nextExpanded
    setIsExpanded(nextExpanded)
    expansionAnimationsRef.current.forEach((animation) => {
      if (nextExpanded) animation.play()
      else animation.reverse()
    })
  }

  const selectPreference = (nextPreference: ThemePreference) => {
    persistThemePreference(nextPreference)
    applyThemePreference(nextPreference)
    setPreference(nextPreference)
    if (lastPointerTypeRef.current === 'touch') updateExpansion(false)
  }

  return (
    <fieldset
      ref={switcherRef}
      className={`theme-switcher${isExpanded ? ' is-expanded' : ''}`}
      data-expanded={isExpanded}
      onPointerEnter={() => updateExpansion(true)}
      onPointerLeave={(event) => {
        if (event.pointerType === 'touch') return
        const focusedElement = document.activeElement
        if (
          (lastPointerTypeRef.current === 'mouse' || lastPointerTypeRef.current === 'pen')
          && focusedElement instanceof HTMLElement
          && event.currentTarget.contains(focusedElement)
        ) {
          focusedElement.blur()
        }
        updateExpansion(false)
      }}
      onPointerDownCapture={(event) => {
        const pointerType = event.pointerType || 'mouse'
        lastPointerTypeRef.current = pointerType as 'mouse' | 'pen' | 'touch'
        if (pointerType === 'touch' && !isExpandedRef.current) updateExpansion(true)
      }}
      onKeyDownCapture={() => {
        lastPointerTypeRef.current = 'keyboard'
      }}
      onFocusCapture={() => updateExpansion(true)}
      onBlurCapture={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) updateExpansion(false)
      }}
    >
      <legend className="theme-switcher-legend">主题</legend>
      <span className="theme-switcher-panel">
        <span className="theme-switcher-circle" aria-hidden="true" />
        <span className="theme-switcher-surface" aria-hidden="true" />
        {THEME_OPTIONS.map((option) => {
          const OptionIcon = option.icon
          const optionId = `${optionName}-${option.value}`
          const isSelected = preference === option.value
          return (
            <label
              key={option.value}
              className={`theme-switcher-option${isSelected ? ' is-selected' : ''}`}
              data-theme-option={option.value}
              htmlFor={optionId}
              title={option.label}
            >
              <input
                id={optionId}
                className="theme-switcher-input"
                type="radio"
                name={optionName}
                value={option.value}
                aria-label={option.label}
                checked={isSelected}
                onChange={() => selectPreference(option.value)}
              />
              <span className="theme-switcher-visual" aria-hidden="true">
                <OptionIcon size={16} />
              </span>
            </label>
          )
        })}
      </span>
    </fieldset>
  )
}
