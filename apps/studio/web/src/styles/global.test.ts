import { describe, expect, it } from 'vitest'

import indexHtml from '../../index.html?raw'
import mainEntry from '../main.tsx?raw'
import globalStyles from './global.css?raw'
import typographyStyles from './typography.css?raw'

const typographySource = `${typographyStyles}\n${globalStyles}`

const relativeLuminance = (hexColor: string) => {
  const normalized = hexColor.slice(1)
  const channels = (normalized.length === 3
    ? normalized.split('').map((channel) => channel.repeat(2))
    : normalized.match(/.{2}/g) ?? []
  ).map((channel) => Number.parseInt(channel, 16) / 255)
    .map((channel) => channel <= 0.04045
      ? channel / 12.92
      : ((channel + 0.055) / 1.055) ** 2.4)

  return (0.2126 * channels[0]) + (0.7152 * channels[1]) + (0.0722 * channels[2])
}

const contrastRatio = (firstColor: string, secondColor: string) => {
  const firstLuminance = relativeLuminance(firstColor)
  const secondLuminance = relativeLuminance(secondColor)
  return (Math.max(firstLuminance, secondLuminance) + 0.05)
    / (Math.min(firstLuminance, secondLuminance) + 0.05)
}

const themeHexValue = (themeBlock: string, token: string) => {
  const match = themeBlock.match(new RegExp(`${token}:\\s*(#[0-9a-f]{3,6});`, 'i'))
  expect(match, `${token} must be a hex color token`).not.toBeNull()
  return match![1]
}

describe('global typography', () => {
  it('loads all self-hosted font families before the typography and component layers', () => {
    const imports = [
      "@fontsource-variable/geist",
      "@fontsource-variable/noto-sans-sc",
      "@fontsource-variable/geist-mono",
      './styles/typography.css',
      './styles/global.css',
    ].map((path) => mainEntry.indexOf(`import '${path}'`))

    expect(imports.every((index) => index >= 0)).toBe(true)
    expect(imports).toEqual([...imports].sort((left, right) => left - right))
  })

  it('defines the approved family roles and eight-level type scale', () => {
    expect(typographyStyles).toContain(
      "--font-ui: 'Geist Variable', 'Noto Sans SC Variable', 'PingFang SC', 'Microsoft YaHei', sans-serif;",
    )
    expect(typographyStyles).toContain(
      "--font-mono: 'Geist Mono Variable', 'SFMono-Regular', Consolas, monospace;",
    )

    expect(typographyStyles).toMatch(/--type-caption-size:\s*11px;/)
    expect(typographyStyles).toMatch(/--type-meta-size:\s*12px;/)
    expect(typographyStyles).toMatch(/--type-ui-small-size:\s*13px;/)
    expect(typographyStyles).toMatch(/--type-ui-size:\s*14px;/)
    expect(typographyStyles).toMatch(/--type-body-small-size:\s*15px;/)
    expect(typographyStyles).toMatch(/--type-body-size:\s*16px;/)
    expect(typographyStyles).toMatch(/--type-title-size:\s*18px;/)
    expect(typographyStyles).toMatch(/--type-display-size:\s*clamp\(28px,\s*3\.4vw,\s*40px\);/)
  })

  it('limits explicit font sizes and weights to the shared typography contract', () => {
    const fontSizes = [...typographySource.matchAll(/font-size:\s*([^;]+);/g)]
      .map((match) => match[1].trim())
      .filter((value) => !value.startsWith('var(--type-'))
    const fontWeights = [...typographySource.matchAll(/font-weight:\s*(\d+);/g)]
      .map((match) => Number(match[1]))

    expect(fontSizes).toEqual([])
    expect(new Set(fontWeights)).toEqual(new Set([400, 500, 600, 700]))
  })

  it('uses intentional tracking, tabular numbers, and the mono family by role', () => {
    expect(typographyStyles).toMatch(/body\s*{[^}]*letter-spacing:\s*0;/s)
    expect(typographyStyles).toMatch(/\.typography-heading\s*{[^}]*letter-spacing:\s*-\.025em;/s)
    expect(typographyStyles).toMatch(/\.typography-label\s*{[^}]*letter-spacing:\s*\.06em;/s)
    expect(globalStyles).toMatch(/font-variant-numeric:\s*tabular-nums;/)
    expect(globalStyles).toMatch(/font-family:\s*var\(--font-mono\);/)
    expect(globalStyles).not.toContain('letter-spacing: .18px')
  })

  it('keeps editable controls regular inside emphasized form fields', () => {
    const styles = document.createElement('style')
    styles.textContent = globalStyles
    const field = document.createElement('div')
    field.style.fontWeight = '700'
    field.innerHTML = `
      <input aria-label="文本输入">
      <textarea aria-label="多行输入"></textarea>
      <select aria-label="下拉选择"><option>选项</option></select>
      <input aria-label="复选输入" type="checkbox">
    `
    document.head.append(styles)
    document.body.append(field)

    try {
      const controlWeight = (label: string) => getComputedStyle(
        field.querySelector(`[aria-label="${label}"]`)!,
      ).fontWeight

      expect(controlWeight('文本输入')).toBe('400')
      expect(controlWeight('多行输入')).toBe('400')
      expect(controlWeight('下拉选择')).toBe('400')
      expect(controlWeight('复选输入')).not.toBe('400')
    } finally {
      field.remove()
      styles.remove()
    }
  })

  it('targets the actual empty heading and keeps short CJK user messages horizontal', () => {
    expect(globalStyles).toMatch(/\.empty-conversation h2\s*{/)
    expect(globalStyles).not.toMatch(/\.empty-conversation h1\s*{/)
    expect(globalStyles).toMatch(/\.user-message \.message-markdown\s*{[^}]*padding:\s*10px 15px;[^}]*font-size:\s*var\(--type-body-small-size\);[^}]*line-height:\s*var\(--type-body-small-line-height\);/s)
    expect(globalStyles).toMatch(/\.user-message \.message-markdown\s*{[^}]*word-break:\s*keep-all;[^}]*overflow-wrap:\s*break-word;/s)
    expect(globalStyles).toMatch(/@media \(max-width: 480px\)[\s\S]*\.user-message \.message-markdown\s*{[^}]*max-width:\s*88%;/)
  })

  it('defines explicit light and dark theme roots for every foundational surface', () => {
    expect(globalStyles).toMatch(/:root\s*{[^}]*color-scheme:\s*light;/s)
    expect(globalStyles).toMatch(/:root\[data-theme=['"]dark['"]\]\s*{[^}]*color-scheme:\s*dark;/s)
    expect(globalStyles).toMatch(/:root\[data-theme=['"]dark['"]\][\s\S]*--bg:/)
    expect(globalStyles).toMatch(/\.sidebar\s*{[^}]*background:\s*var\(--surface-sidebar\);/s)
    expect(globalStyles).toMatch(/\.tool-card\s*{[^}]*background:\s*var\(--surface-elevated\);/s)
    expect(globalStyles).toMatch(/\.modal-dialog\s*{[^}]*background:\s*var\(--surface-elevated\);/s)
    expect(globalStyles).toMatch(/\.composer\s*{[^}]*background:\s*var\(--surface-elevated\);/s)
  })

  it('keeps semantic status foregrounds accessible in both color schemes', () => {
    const lightTheme = globalStyles.match(/^:root\s*{([^}]*)}/s)?.[1] ?? ''
    const darkTheme = globalStyles.match(/:root\[data-theme=['"]dark['"]\]\s*{([^}]*)}/s)?.[1] ?? ''

    for (const theme of [lightTheme, darkTheme]) {
      expect(contrastRatio(
        themeHexValue(theme, '--red'),
        themeHexValue(theme, '--text-on-red'),
      )).toBeGreaterThanOrEqual(4.5)
      expect(contrastRatio(
        themeHexValue(theme, '--green'),
        themeHexValue(theme, '--text-on-green'),
      )).toBeGreaterThanOrEqual(4.5)
      expect(contrastRatio(
        themeHexValue(theme, '--green-strong'),
        themeHexValue(theme, '--surface-elevated'),
      )).toBeGreaterThanOrEqual(4.5)
    }

    expect(globalStyles).toMatch(/\.approval-form \.danger\s*{[^}]*color:\s*var\(--text-on-red\);/s)
    expect(globalStyles).toMatch(/\.subagent-trace-marker\s*{[^}]*color:\s*var\(--text-on-green\);/s)
    expect(globalStyles).toMatch(/\.tool-status\s*{[^}]*color:\s*var\(--green-strong\);/s)
  })

  it('uses theme-aware borders and focus treatments on elevated controls', () => {
    expect(globalStyles).toMatch(/\.conversation-pane\s*{[^}]*scrollbar-color:\s*var\(--scroll-thumb\) transparent;/s)
    expect(globalStyles).toMatch(/\.scroll-to-bottom\s*{[^}]*border:\s*1px solid var\(--border\);/s)
    expect(globalStyles).toMatch(/\.subagent-card\s*{[^}]*border:\s*1px solid var\(--green-line\);/s)
    expect(globalStyles).toMatch(/\.subagent-card-body\s*{[^}]*border-top:\s*1px solid var\(--border-subtle\);/s)
    expect(globalStyles).toMatch(/\.composer\s*{[^}]*border:\s*1px solid var\(--border\);/s)
    expect(globalStyles).toMatch(/\.composer:focus-within\s*{[^}]*border-color:\s*var\(--border-strong\);/s)
    expect(globalStyles).toMatch(/\.modal-dialog\s*{[^}]*border:\s*1px solid var\(--border\);/s)
    expect(globalStyles).toMatch(/\.toast-card\s*{[^}]*border:\s*1px solid var\(--border\);/s)
    expect(globalStyles).toMatch(/\.drawer-splitter:focus-visible\s*{[^}]*var\(--surface-elevated\)[^}]*var\(--focus\);/s)
  })

  it('centers compact toasts and sizes each card to its own message', () => {
    expect(globalStyles).toMatch(/\.toast-viewport\s*{[^}]*position:\s*fixed;[^}]*top:\s*max\(12px,\s*env\(safe-area-inset-top\)\);[^}]*left:\s*50%;[^}]*width:\s*min\(calc\(100vw - 24px\),\s*420px\);[^}]*transform:\s*translateX\(-50%\);/s)
    expect(globalStyles).toMatch(/\.toast-card\s*{[^}]*display:\s*inline-flex;[^}]*width:\s*max-content;[^}]*max-width:\s*min\(calc\(100vw - 24px\),\s*420px\);[^}]*min-height:\s*42px;/s)
    expect(globalStyles).toMatch(/\.toast-card p\s*{[^}]*flex:\s*0 1 auto;[^}]*overflow-wrap:\s*anywhere;/s)
    expect(globalStyles).not.toMatch(/\.toast-card\s*{[^}]*(?:width:\s*390px|animation:\s*toastIn)/s)
    expect(globalStyles).not.toContain('@keyframes toastIn')
  })

  it('anchors the compact theme circle and expands its three choices to the left', () => {
    expect(globalStyles).toMatch(/\.theme-switcher\s*{[^}]*width:\s*34px;[^}]*min-width:\s*34px;/s)
    expect(globalStyles).toMatch(/\.theme-switcher-panel\s*{[^}]*position:\s*absolute;[^}]*right:\s*0;[^}]*width:\s*102px;/s)
    expect(globalStyles).toMatch(/\.theme-switcher-circle\s*{[^}]*right:\s*0;[^}]*width:\s*34px;[^}]*height:\s*34px;[^}]*border-radius:\s*50%;/s)
    expect(globalStyles).toMatch(/\.theme-switcher-surface\s*{[^}]*right:\s*0;[^}]*width:\s*34px;[^}]*height:\s*34px;[^}]*border-radius:\s*999px;/s)
    expect(globalStyles).not.toMatch(/\.theme-switcher-surface\s*{[^}]*scaleX/s)
    expect(globalStyles).toMatch(/\.theme-switcher:not\(\.is-expanded\)[^}]*\.theme-switcher-option:not\(\.is-selected\)[^}]*{[^}]*pointer-events:\s*none;/s)
  })

  it('uses one collapsed theme outline and keeps the chat header in its own workspace row', () => {
    expect(globalStyles).not.toContain('--header-bg:')
    expect(globalStyles).toMatch(/\.workspace-main\s*{[^}]*grid-template-rows:\s*58px minmax\(0,\s*1fr\);/s)
    expect(globalStyles).toMatch(/\.chat-header\s*{[^}]*position:\s*relative;[^}]*background:\s*var\(--bg\);/s)
    expect(globalStyles).not.toMatch(/\.chat-header\s*{[^}]*(?:border-bottom|pointer-events):/s)
    expect(globalStyles).not.toMatch(/\.header-left,\s*\.header-actions\s*{[^}]*pointer-events:/s)
    expect(globalStyles).not.toMatch(/\.header-left\s*>\s*\*,\s*\.header-actions\s*>\s*\*\s*{/s)
    expect(globalStyles).toMatch(/\.theme-switcher\.is-expanded \.theme-switcher-input:checked \+ \.theme-switcher-visual\s*{[^}]*box-shadow:/s)
    expect(globalStyles).toMatch(/\.theme-switcher:not\(\.is-expanded\):has\(\.theme-switcher-input:focus-visible\) \.theme-switcher-circle\s*{[^}]*box-shadow:/s)
  })

  it('compacts the right header controls and insets the task count', () => {
    expect(globalStyles).toMatch(/\.agent-preset\s*{[^}]*width:\s*104px;[^}]*min-width:\s*104px;[^}]*padding:\s*0 8px;/s)
    expect(globalStyles).toMatch(/\.agent-preset-options\s*{[^}]*width:\s*104px;/s)
    expect(globalStyles).toMatch(/\.drawer-toggle\s*{[^}]*gap:\s*5px;[^}]*padding:\s*0 10px 0 7px;/s)
    expect(globalStyles).toMatch(/\.drawer-toggle b\s*{[^}]*position:\s*relative;[^}]*right:\s*2px;/s)
  })

  it('resolves the persisted preference before the application module loads', () => {
    const bootstrapPosition = indexHtml.indexOf('tinkerfin:theme')
    const applicationPosition = indexHtml.indexOf('/src/main.tsx')

    expect(bootstrapPosition).toBeGreaterThan(-1)
    expect(bootstrapPosition).toBeLessThan(applicationPosition)
    expect(indexHtml).toContain("let preference = 'light'")
    expect(indexHtml).toContain('(prefers-color-scheme: dark)')
    expect(indexHtml).toContain('data-theme-preference')
  })
})
