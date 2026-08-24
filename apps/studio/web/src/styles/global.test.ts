import { describe, expect, it } from 'vitest'

import faviconSvg from '../../public/tinkerfin-favicon.svg?raw'
import indexHtml from '../../index.html?raw'
import mainEntry from '../main.tsx?raw'
import tokensStyles from './tokens.css?raw'
import typographyStyles from './typography.css?raw'

const cssFiles = import.meta.glob('../**/*.css', {
  eager: true,
  import: 'default',
  query: '?raw',
}) as Record<string, string>

const componentStyles = Object.entries(cssFiles)
  .filter(([path]) => !path.endsWith('/tokens.css'))
  .map(([, source]) => source)
  .join('\n')

const declarations = (block: string) => new Map(
  [...block.matchAll(/(--[\w-]+):\s*([^;]+);/g)].map((match) => [match[1], match[2].trim()]),
)

const themeBlocks = () => {
  const light = tokensStyles.match(/:root\s*{([^}]*)}/s)?.[1] ?? ''
  const dark = tokensStyles.match(/:root\[data-theme='dark'\]\s*{([^}]*)}/s)?.[1] ?? ''
  const lightTokens = declarations(light)
  const darkTokens = new Map([...lightTokens, ...declarations(dark)])
  return [lightTokens, darkTokens]
}

const resolveToken = (tokens: Map<string, string>, name: string, seen = new Set<string>()): string => {
  if (seen.has(name)) throw new Error(`令牌循环引用：${name}`)
  const value = tokens.get(name)
  expect(value, `缺少令牌 ${name}`).toBeDefined()
  const reference = value?.match(/^var\((--[\w-]+)\)$/)?.[1]
  if (!reference) return value ?? ''
  seen.add(name)
  return resolveToken(tokens, reference, seen)
}

const relativeLuminance = (hexColor: string) => {
  const channels = (hexColor.slice(1).match(/.{2}/g) ?? [])
    .map((channel) => Number.parseInt(channel, 16) / 255)
    .map((channel) => channel <= 0.04045
      ? channel / 12.92
      : ((channel + 0.055) / 1.055) ** 2.4)
  return (0.2126 * channels[0]) + (0.7152 * channels[1]) + (0.0722 * channels[2])
}

const contrastRatio = (firstColor: string, secondColor: string) => {
  const first = relativeLuminance(firstColor)
  const second = relativeLuminance(secondColor)
  return (Math.max(first, second) + 0.05) / (Math.min(first, second) + 0.05)
}

describe('前端视觉契约', () => {
  it('网站标题与菜单品牌使用同一 Blocks 几何', () => {
    expect(indexHtml).toContain('href="/tinkerfin-favicon.svg?v=10"')
    expect(faviconSvg).toContain('<rect width="7" height="7" x="14" y="3" rx="1" />')
    expect(faviconSvg).toContain('M10 21V8a1 1 0 0 0-1-1H4')
    expect(faviconSvg).not.toContain('<image')
  })

  it('按既定顺序加载自托管字体、令牌、排版和基础样式', () => {
    const imports = [
      '@fontsource-variable/inter',
      '@fontsource-variable/inter/wght-italic.css',
      '@fontsource-variable/noto-sans-sc',
      '@fontsource-variable/jetbrains-mono',
      './styles/tokens.css',
      './styles/typography.css',
      './styles/global.css',
      './components/ui/ui.css',
    ].map((path) => mainEntry.indexOf(`import '${path}'`))

    expect(imports.every((index) => index >= 0)).toBe(true)
    expect(imports).toEqual([...imports].sort((left, right) => left - right))
    expect(mainEntry).not.toContain('@fontsource-variable/geist')
  })

  it('定义 Inter、Noto Sans SC、JetBrains Mono 和四级字重', () => {
    expect(typographyStyles).toContain(
      "--font-ui: 'Inter Variable', 'Noto Sans SC Variable', 'PingFang SC', 'Microsoft YaHei UI', system-ui, sans-serif;",
    )
    expect(typographyStyles).toContain(
      "--font-code: 'JetBrains Mono Variable', 'SFMono-Regular', Consolas, 'Liberation Mono', monospace;",
    )
    for (const weight of [400, 500, 600, 700]) {
      expect(typographyStyles).toContain(`: ${weight};`)
    }
    expect(typographyStyles).toMatch(/--type-body-size:\s*16px;[\s\S]*--type-body-line:\s*28px;/)
    expect(typographyStyles).toContain('--type-composer-line: 24px;')
    expect(typographyStyles).toMatch(/--type-h1-size:\s*24px;[\s\S]*--type-h1-line:\s*34px;/)
    expect(typographyStyles).toMatch(/--type-brand-size:\s*26px;[\s\S]*--type-brand-line:\s*32px;/)
  })

  it('锁定布局、控件、圆角、层级与 100/200/300ms 动效尺度', () => {
    for (const declaration of [
      '--layout-sidebar-expanded: 261px;',
      '--layout-sidebar-rail: 56px;',
      '--layout-content-wide: 840px;',
      '--layout-task-drawer: 348px;',
      '--layout-settings-dialog: 760px;',
      '--layout-settings-nav: 180px;',
      '--layout-settings-height: 540px;',
      '--control-lg: 44px;',
      '--control-plan-chip: 24px;',
      '--control-composer: 34px;',
      '--radius-composer: 22px;',
      '--layer-local: 1;',
      '--layer-local-raised: 2;',
      '--motion-fast: 100ms;',
      '--motion-normal: 200ms;',
      '--motion-slow: 300ms;',
      '--motion-loading-cycle: 1000ms;',
      '--motion-tool-sweep-cycle: 2600ms;',
      '--layout-sidebar-expanded: 261px;',
    ]) expect(tokensStyles).toContain(declaration)
  })

  it('操作型弹窗使用紧凑按钮并让关闭图标对齐标题行', () => {
    const uiStyles = cssFiles['../components/ui/ui.css']

    expect(uiStyles).toMatch(/\.modal-dialog--action\s*\{[^}]*width:\s*min\(100%, 400px\);/s)
    expect(uiStyles).toMatch(/\.modal-dialog--action \.modal-dialog-head\s*\{[^}]*align-items:\s*flex-start;[^}]*padding:\s*var\(--space-5\) var\(--space-5\) 0;/s)
    expect(uiStyles).toMatch(/\.modal-dialog--action \.modal-dialog-head > \.ui-icon-button-wrap\s*\{[^}]*margin-block:\s*calc\(0px - var\(--space-1\)\);[^}]*margin-inline-end:\s*calc\(0px - var\(--space-2\)\);/s)
    expect(uiStyles).toMatch(/\.modal-dialog--action form\s*\{[^}]*padding:\s*var\(--space-6\) var\(--space-5\) var\(--space-5\);/s)
    expect(uiStyles).toMatch(/\.modal-dialog--action \.modal-dialog-actions\s*\{[^}]*gap:\s*var\(--space-2\);[^}]*margin-top:\s*0;/s)
    expect(uiStyles).toMatch(/\.modal-dialog--action \.modal-dialog-error \+ \.modal-dialog-actions\s*\{[^}]*margin-top:\s*var\(--space-3\);/s)
    expect(uiStyles).toMatch(/\.modal-dialog--action\.has-input \.modal-dialog-actions\s*\{[^}]*margin-top:\s*var\(--space-6\);/s)
    expect(uiStyles).not.toMatch(/\.modal-dialog--action \.modal-dialog-actions \.ui-button\s*\{/s)
    expect(uiStyles).toMatch(/@media \(hover: none\), \(pointer: coarse\)[\s\S]*\.ui-button\s*\{[^}]*min-width:\s*var\(--control-lg\);[^}]*min-height:\s*var\(--control-lg\);/s)
  })

  it('共享 Button 与 TextField 使用统一桌面尺度和单一中性焦点边', () => {
    const uiStyles = cssFiles['../components/ui/ui.css']
    const conversationStyles = cssFiles['../features/conversation/conversation.css']

    expect(uiStyles).toMatch(/\.ui-button--xs\s*\{[^}]*--button-control-size:\s*var\(--control-xs\);[^}]*min-height:\s*var\(--button-control-size\);/s)
    expect(uiStyles).toMatch(/\.ui-button--circle\s*\{[^}]*width:\s*var\(--button-control-size\);[^}]*min-width:\s*var\(--button-control-size\);/s)
    expect(uiStyles).toMatch(/\.ui-text-field--md \.ui-text-field__control\s*\{[^}]*min-height:\s*var\(--control-md\);/s)
    expect(uiStyles).toMatch(/\.ui-text-field--lg \.ui-text-field__control\s*\{[^}]*min-height:\s*var\(--control-lg\);/s)
    expect(uiStyles).toMatch(/\.ui-text-field__control:focus-within\s*\{[^}]*border-color:\s*var\(--color-border-strong\);[^}]*box-shadow:\s*none;/s)
    expect(tokensStyles).not.toContain('--shadow-focus')
    expect(conversationStyles).toMatch(/\.approval-form textarea:focus,[\s\S]*\.plan-review-input textarea:focus\s*\{[^}]*border-color:\s*var\(--color-border-strong\);[^}]*box-shadow:\s*none;/s)
  })

  it('带边框控件使用单一焦点边且鼠标焦点不触发主题选项轮廓', () => {
    const uiStyles = cssFiles['../components/ui/ui.css']
    const settingsStyles = cssFiles['../features/settings/settings.css']

    expect(uiStyles).toMatch(/\.ui-button:focus-visible\s*\{[^}]*outline:\s*2px solid var\(--color-focus\);[^}]*outline-offset:\s*0;/s)
    expect(uiStyles).toMatch(/\.theme-switcher-input:focus-visible \+ \.theme-switcher-visual\s*\{[^}]*outline-offset:\s*0;[^}]*box-shadow:\s*none;/s)
    expect(settingsStyles).not.toContain('.settings-theme-option:focus-within')
    expect(settingsStyles).toMatch(/\.settings-theme-option:has\(input:focus-visible\)\s*\{[^}]*outline:\s*2px solid var\(--color-focus\);[^}]*outline-offset:\s*0;/s)
    expect(settingsStyles).toMatch(/\.settings-language-trigger:focus-visible\s*\{[^}]*outline:\s*2px solid var\(--color-focus\);[^}]*outline-offset:\s*0;/s)
  })

  it('会话正文与输入卡片使用独立的 DSH 对齐宽度', () => {
    const workspaceStyles = cssFiles['../features/workspace/workspace.css']
    const conversationStyles = cssFiles['../features/conversation/conversation.css']

    expect(tokensStyles).toContain('--layout-conversation-width: 748px;')
    expect(tokensStyles).toContain('--layout-composer-width: 780px;')
    expect(workspaceStyles).toMatch(/\.conversation-pane\s*\{[^}]*padding-inline:\s*var\(--space-8\);/s)
    expect(workspaceStyles).toMatch(/\.message-list\s*\{[^}]*width:\s*min\(100%, var\(--layout-conversation-width\)\)/s)
    expect(workspaceStyles).toMatch(/\.message-list\s*\{[^}]*padding:\s*var\(--space-8\) 0 var\(--composer-height\);/s)
    expect(workspaceStyles).not.toMatch(/\.message-list\s*\{[^}]*padding-(?:right|left):/s)
    expect(workspaceStyles).toMatch(/@media \(max-width:\s*767px\)[\s\S]*\.conversation-pane\s*\{[^}]*padding-inline:\s*var\(--space-4\);[^}]*\}[\s\S]*\.message-list\s*\{[^}]*padding-top:\s*var\(--space-6\);/s)
    expect(workspaceStyles).toMatch(/@media \(max-width:\s*440px\)[\s\S]*\.conversation-pane\s*\{[^}]*padding-inline:\s*var\(--space-3\);/s)
    expect(conversationStyles).toMatch(/\.composer\s*\{[^}]*width:\s*min\(100%, var\(--layout-composer-width\)\)/s)
  })

  it('空会话使用横向品牌组合，并在窄屏收紧字号与间距', () => {
    const workspaceStyles = cssFiles['../features/workspace/workspace.css']

    expect(workspaceStyles).toMatch(/\.empty-brand-lockup\s*\{[^}]*grid-template-columns:\s*auto auto auto;[^}]*align-items:\s*center;[^}]*justify-content:\s*center;[^}]*column-gap:\s*var\(--space-2\);/s)
    expect(workspaceStyles).toMatch(/\.empty-brand-name\s*\{[^}]*font-size:\s*var\(--type-brand-size\);[^}]*line-height:\s*var\(--type-brand-line\);/s)
    expect(workspaceStyles).toMatch(/\.brand-plus\s*\{[^}]*padding:\s*1px var\(--space-2\) 0;[^}]*border-radius:\s*var\(--radius-pill\);[^}]*background:\s*var\(--color-brand-soft\);[^}]*color:\s*var\(--color-brand-text\);/s)
    expect(workspaceStyles).toMatch(/@media \(max-width:\s*440px\)[\s\S]*\.empty-brand-name\s*\{[^}]*font-size:\s*var\(--type-h1-size\);/s)
  })

  it('浅色和深色普通文本、辅助文本及状态文本均达到 4.5:1', () => {
    for (const tokens of themeBlocks()) {
      for (const [foreground, background] of [
        ['--color-text-primary', '--color-canvas'],
        ['--color-text-secondary', '--color-canvas'],
        ['--color-text-tertiary', '--color-canvas'],
        ['--color-text-caption', '--color-canvas'],
        ['--color-placeholder', '--color-layer-2'],
        ['--color-brand-text', '--color-brand-soft'],
        ['--color-on-selection', '--color-selection'],
        ['--color-danger-text', '--color-danger-soft'],
        ['--color-success-text', '--color-success-soft'],
        ['--color-warning-text', '--color-warning-soft'],
      ]) {
        const foregroundColor = resolveToken(tokens, foreground)
        const backgroundColor = resolveToken(tokens, background)
        expect(foregroundColor).toMatch(/^#[0-9a-f]{6}$/i)
        expect(backgroundColor).toMatch(/^#[0-9a-f]{6}$/i)
        expect(contrastRatio(foregroundColor, backgroundColor)).toBeGreaterThanOrEqual(4.5)
      }
    }
  })

  it('文本选区只由全局入口消费独立的高对比语义色', () => {
    const globalStyles = cssFiles['./global.css']
    const selectionOwners = Object.entries(cssFiles)
      .filter(([, styles]) => styles.includes('::selection'))
      .map(([path]) => path)

    expect(selectionOwners).toEqual(['./global.css'])
    expect(tokensStyles).toContain('--color-selection: var(--primitive-blue-300);')
    expect(tokensStyles).toContain('--color-on-selection: var(--primitive-gray-950);')
    expect(tokensStyles).toMatch(/:root\[data-theme='dark'\]\s*\{[^}]*--color-selection:\s*var\(--color-brand\);[^}]*--color-on-selection:\s*var\(--color-on-brand\);/s)
    expect(globalStyles).toMatch(/::selection\s*\{[^}]*background:\s*var\(--color-selection\);[^}]*color:\s*var\(--color-on-selection\);/s)
  })

  it('所有功能 CSS 入口都受扫描且不直接声明十六进制色或原始色令牌', () => {
    expect(Object.keys(cssFiles)).toEqual(expect.arrayContaining([
      '../components/ui/ui.css',
      '../features/auth/auth.css',
      '../features/conversation/conversation.css',
      '../features/settings/settings.css',
      '../features/workspace/workspace.css',
      './global.css',
      './tokens.css',
      './typography.css',
    ]))
    expect(componentStyles).not.toMatch(/#[0-9a-f]{3,8}\b/i)
    expect(componentStyles).not.toMatch(/var\(--primitive-/)

    const unregisteredRadii = [...componentStyles.matchAll(/border-radius:\s*([^;]+);/g)]
      .map((match) => match[1].trim())
      .filter((value) => !value.includes('var(--radius-') && !['0', 'inherit'].includes(value))
    const unregisteredShadows = [...componentStyles.matchAll(/box-shadow:\s*([^;]+);/g)]
      .map((match) => match[1].trim())
      .filter((value) => value !== 'none' && !value.includes('var(--shadow-'))
    const unregisteredLayers = [...componentStyles.matchAll(/z-index:\s*([^;]+);/g)]
      .map((match) => match[1].trim())
      .filter((value) => !value.includes('var(--layer-'))
    const breakpointValues = [...new Set(
      [...componentStyles.matchAll(/@media \((?:min|max)-width:\s*(\d+)px\)/g)]
        .map((match) => match[1]),
    )].sort((left, right) => Number(left) - Number(right))
    const ownerLocalMotionValues = [...new Set(
      [...componentStyles.matchAll(/(?<![-\w.])(?:\d*\.)?\d+(?:ms|s)\b/g)]
        .map((match) => match[0]),
    )].sort()

    expect(unregisteredRadii).toEqual([])
    expect(unregisteredShadows).toEqual([])
    expect(unregisteredLayers).toEqual([])
    expect(breakpointValues).toEqual(['440', '767', '1023', '1281'])
    expect(ownerLocalMotionValues).toEqual([])
  })

  it('字号和字重通过排版角色消费，不在功能样式中形成第二套尺度', () => {
    const explicitFontSizes = [...componentStyles.matchAll(/font-size:\s*([^;]+);/g)]
      .map((match) => match[1].trim())
      .filter((value) => !['0', 'inherit'].includes(value) && !value.startsWith('var(--type-'))
    const explicitFontWeights = [...componentStyles.matchAll(/font-weight:\s*([^;]+);/g)]
      .map((match) => match[1].trim())
      .filter((value) => !value.startsWith('var(--weight-'))
    const interactiveCaptionSelectors = [...componentStyles.matchAll(
      /([^{}]+)\{[^{}]*font-size:\s*var\(--type-caption-size\);/g,
    )]
      .map((match) => match[1].trim())
      .filter((selector) => /button|\.ui-button|input|textarea|select|\[role=/.test(selector))

    expect(explicitFontSizes).toEqual([])
    expect(explicitFontWeights).toEqual([])
    expect(interactiveCaptionSelectors).toEqual([])
    expect(typographyStyles).toMatch(/button,[\s\S]*select\s*{\s*font-size:\s*inherit;/)
  })

  it('共享与业务按钮都声明完整交互状态', () => {
    const contracts = [
      [cssFiles['../components/ui/ui.css'], '.toast-card > button'],
      [cssFiles['../features/workspace/workspace.css'], '.conversation-main'],
      [cssFiles['../features/workspace/workspace.css'], '.user-card'],
      [cssFiles['../features/conversation/conversation.css'], '.composer-model-select'],
      [cssFiles['../features/workspace/workspace.css'], '.scroll-to-bottom'],
      [cssFiles['../features/workspace/workspace.css'], '.todo-item'],
    ] as const

    for (const [source, selector] of contracts) {
      for (const state of ['hover', 'active', 'focus-visible', 'disabled']) {
        expect(source, `${selector} 缺少 ${state}`).toContain(`${selector}:${state}`)
      }
    }
  })

  it('Markdown 使用编辑型表格并具备完整文章语义和显式 compact variant', () => {
    const markdownStyles = cssFiles['../features/conversation/conversation.css']
    expect(markdownStyles).toMatch(/\.markdown-content h1[\s\S]*\.markdown-content h4/)
    expect(markdownStyles).toMatch(/\.markdown-content li > ul[\s\S]*padding-inline-start/)
    expect(markdownStyles).toMatch(/\.markdown-table-wrap[\s\S]*overflow-x:\s*auto/)
    expect(markdownStyles).toMatch(/\.markdown-content th,[\s\S]*border-bottom:\s*1px solid var\(--color-border\)/)
    expect(markdownStyles).toContain('.markdown-content--compact')
    expect(markdownStyles).toMatch(/\.markdown-content \.markdown-bare-url\s*\{[^}]*word-break:\s*break-all;/s)
    expect(markdownStyles).not.toMatch(/tbody tr:nth-child|tbody tr:hover/)
  })

  it('三态侧栏、头部 search、任务抽屉与必要断点均由 workspace 所有', () => {
    const workspaceStyles = cssFiles['../features/workspace/workspace.css']
    expect(workspaceStyles).toMatch(/grid-template-columns:\s*var\(--layout-sidebar-expanded\)/)
    expect(workspaceStyles).toMatch(/data-sidebar-mode='rail'[\s\S]*var\(--layout-sidebar-rail\)/)
    expect(workspaceStyles).toContain('.sidebar-head.is-search-open')
    expect(workspaceStyles).toMatch(/\.workspace-sidebar\s*\{[^}]*overflow:\s*visible;/s)
    expect(workspaceStyles).toMatch(/\.sidebar-head\s*\{[^}]*align-items:\s*center;[^}]*gap:\s*var\(--space-2\);[^}]*min-height:\s*calc\(var\(--control-xl\) \+ var\(--space-4\)\);[^}]*margin-bottom:\s*var\(--space-1\);/s)
    expect(workspaceStyles).toMatch(/\.sidebar-head-actions\s*\{[^}]*margin-left:\s*auto;[^}]*flex:\s*0 0 auto;[^}]*align-items:\s*center;[^}]*gap:\s*var\(--space-1\);/s)
    expect(workspaceStyles).toMatch(/\.brand\s*\{[^}]*overflow:\s*hidden;[^}]*flex:\s*1 1 auto;[^}]*padding:\s*0;/s)
    expect(workspaceStyles).toMatch(/\.brand\s*\{[^}]*font-size:\s*var\(--type-h3-size\);[^}]*line-height:\s*var\(--type-h3-line\);/s)
    expect(workspaceStyles).toMatch(/\.brand-name\s*\{[^}]*flex:\s*0 0 auto;[^}]*text-overflow:\s*ellipsis;[^}]*white-space:\s*nowrap;/s)
    expect(workspaceStyles).not.toContain('.brand > span')
    expect(workspaceStyles).toMatch(/\.sidebar-head \.brand-plus\s*\{[^}]*padding-inline:\s*var\(--space-1\);[^}]*font-size:\s*var\(--type-micro-size\);[^}]*line-height:\s*var\(--type-micro-line\);/s)
    expect(workspaceStyles).toMatch(/\.sidebar-head-actions \.ui-icon-button\s*\{[^}]*width:\s*var\(--control-xs\);[^}]*min-height:\s*var\(--control-xs\);[^}]*height:\s*var\(--control-xs\);/s)
    expect(workspaceStyles).toMatch(/@media \(hover:\s*none\), \(pointer:\s*coarse\)[\s\S]*\.sidebar-head-actions \.ui-icon-button\s*\{[^}]*width:\s*var\(--control-lg\);[^}]*min-width:\s*var\(--control-lg\);[^}]*height:\s*var\(--control-lg\);/s)
    expect(workspaceStyles).toMatch(/@media \(hover:\s*none\), \(pointer:\s*coarse\)[\s\S]*\.sidebar-head\s*\{[^}]*gap:\s*var\(--space-1\);[^}]*\}[\s\S]*\.sidebar-head-actions\s*\{[^}]*gap:\s*var\(--space-0\);[^}]*\}[\s\S]*\.brand\s*\{[^}]*gap:\s*var\(--space-0-5\);/s)
    expect(workspaceStyles).toMatch(/@media \(hover:\s*none\), \(pointer:\s*coarse\)[\s\S]*\.brand \.brand-mark svg\s*\{[^}]*width:\s*28px;[^}]*height:\s*28px;/s)
    expect(workspaceStyles).toMatch(/@media \(hover:\s*none\), \(pointer:\s*coarse\)[\s\S]*\.sidebar-head \.brand-plus\s*\{[^}]*padding-inline:\s*var\(--space-0\);[^}]*font-size:\s*var\(--type-micro-size\);/s)
    expect(workspaceStyles).toMatch(/\.brand:hover,[\s\S]*\.brand:active\s*{[^}]*background:\s*transparent;[^}]*box-shadow:\s*none;[^}]*color:\s*var\(--color-text-primary\);/s)
    expect(workspaceStyles).toMatch(/\.sidebar-head-actions \.ui-tooltip\s*\{[^}]*right:\s*0;[^}]*left:\s*auto;/s)
    expect(workspaceStyles).toMatch(/\.sidebar-rail \.ui-tooltip\s*\{[^}]*top:\s*50%;[^}]*left:\s*calc\(100% \+ var\(--space-2\)\);[^}]*transform:\s*translate\(var\(--space-0-5\), -50%\);/s)
    expect(workspaceStyles).toMatch(/\.sidebar-rail \.ui-icon-button-wrap:hover \.ui-tooltip,[\s\S]*\.sidebar-rail \.ui-icon-button:focus-visible \+ \.ui-tooltip\s*\{[^}]*transform:\s*translate\(0, -50%\);/s)
    expect(workspaceStyles).toMatch(/@media \(max-width:\s*767px\)/)
    expect(workspaceStyles).toMatch(/@media \(min-width:\s*1281px\)/)
    expect(workspaceStyles).toMatch(/\.workspace-main\s*\{[^}]*transition:\s*margin-right/s)
    expect(workspaceStyles).toMatch(/\.app-shell\.has-drawer \.workspace-main\s*\{[^}]*margin-right:\s*var\(--layout-task-drawer\)/s)
    expect(workspaceStyles).toMatch(/\.task-drawer\s*\{[^}]*position:\s*fixed;[^}]*box-shadow:\s*var\(--shadow-3\)/s)
    expect(workspaceStyles).toMatch(/@media \(prefers-reduced-motion:\s*reduce\)[\s\S]*\.workspace-main,[\s\S]*transition:\s*none;/s)
    expect(cssFiles['../components/ui/ui.css']).toContain('.ui-scrollbar-overlay')
  })

  it('回到底部使用文字胶囊、历史间距和滚动内容安全内边距', () => {
    const workspaceStyles = cssFiles['../features/workspace/workspace.css']
    expect(workspaceStyles).toMatch(
      /\.conversation-region\s*\{[^}]*grid-row:\s*2;[^}]*grid-column:\s*1;[^}]*position:\s*relative;/s,
    )
    expect(workspaceStyles).toMatch(
      /\.conversation-scroll-action\s*\{[^}]*position:\s*absolute;[^}]*bottom:\s*calc\(var\(--composer-height\) \+ var\(--space-3\)\);[^}]*left:\s*50%;[^}]*transform:\s*translateX\(-50%\);/s,
    )
    expect(workspaceStyles).toMatch(
      /\.message-list\s*\{[^}]*padding:[^;}]*var\(--composer-height\);/s,
    )
    expect(workspaceStyles).toMatch(
      /\.scroll-to-bottom\s*\{[^}]*gap:\s*var\(--space-2\);[^}]*min-width:\s*var\(--control-lg\);[^}]*height:\s*var\(--control-lg\);[^}]*padding:\s*0 var\(--space-4\);[^}]*border-radius:\s*var\(--radius-pill\);[^}]*background:\s*var\(--color-layer-1\);/s,
    )
    expect(workspaceStyles).toMatch(/\.scroll-to-bottom\s*\{[^}]*opacity:\s*0;[^}]*transition:[^}]*opacity var\(--motion-slow\)/s)
    expect(workspaceStyles).toMatch(/\.scroll-to-bottom\.is-fading\s*\{[^}]*transform:\s*translateY\(var\(--space-2\)\);[^}]*opacity:\s*0;[^}]*pointer-events:\s*none;/s)
    expect(workspaceStyles).not.toContain('.scroll-to-bottom::before')
  })

  it('输入区通过渐隐层衔接滚动内容，侧栏保留独立的历史滚动区和账户区', () => {
    const conversationStyles = cssFiles['../features/conversation/conversation.css']
    const workspaceStyles = cssFiles['../features/workspace/workspace.css']
    const uiStyles = cssFiles['../components/ui/ui.css']

    expect(conversationStyles).not.toContain('.composer-wrap')
    expect(conversationStyles).toMatch(/\.composer-dock\s*\{[^}]*grid-row:\s*2;[^}]*grid-column:\s*1;[^}]*align-self:\s*end;[^}]*pointer-events:\s*none;[^}]*background:\s*transparent;/s)
    expect(conversationStyles).toMatch(/\.composer-dock::before\s*\{[^}]*height:\s*calc\(100% \+ var\(--space-16\)\);[^}]*linear-gradient\(to bottom, transparent, var\(--color-canvas\)\);/s)
    expect(conversationStyles).toMatch(/\.composer-dock\.is-hero\s*\{[^}]*align-self:\s*center;[^}]*display:\s*flex;[^}]*padding-bottom:\s*max\(calc\(var\(--space-8\) \+ var\(--type-composer-line\) \+ var\(--type-composer-line\) \+ var\(--type-composer-line\)\), env\(safe-area-inset-bottom\)\);[^}]*flex-direction:\s*column;/s)
    expect(conversationStyles).toMatch(/\.composer-dock\.is-hero::before\s*\{\s*display:\s*none;/s)
    expect(conversationStyles).toMatch(/\.composer-hero\s*\{[^}]*width:\s*min\(100%, var\(--layout-composer-width\)\);[^}]*margin:\s*0 auto var\(--type-composer-line\);[^}]*place-items:\s*center;/s)
    expect(conversationStyles).toMatch(/\.composer-dock\.is-hero \.composer-input-mirror\s*\{\s*min-height:\s*52px;/s)
    expect(workspaceStyles).toMatch(/\.sidebar-wide\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\);[^}]*grid-template-rows:\s*auto auto minmax\(0, 1fr\) auto;/s)
    expect(workspaceStyles).toMatch(/\.sidebar-head\s*\{[^}]*z-index:\s*var\(--layer-dropdown\);[^}]*overflow:\s*visible;/s)
    expect(workspaceStyles).toMatch(/\.new-chat\s*\{[^}]*min-height:\s*var\(--control-lg\);[^}]*justify-content:\s*center;[^}]*box-shadow:\s*var\(--shadow-1\);/s)
    expect(workspaceStyles).toMatch(/\.new-chat-wrap\s*\{[^}]*margin-bottom:\s*var\(--space-3\);/s)
    expect(workspaceStyles).toMatch(/\.primary-nav \.ui-button\s*\{[^}]*font-size:\s*var\(--type-ui-size\);[^}]*font-weight:\s*var\(--weight-medium\);[^}]*line-height:\s*var\(--type-ui-line\);/s)
    expect(workspaceStyles).toMatch(/\.primary-nav\s*\{[^}]*padding-bottom:\s*var\(--space-4\);[^}]*border:\s*0;/s)
    expect(workspaceStyles).toContain('.new-chat:hover .new-chat-shortcut')
    expect(workspaceStyles).toMatch(/\.conversation-history::after\s*\{[^}]*position:\s*absolute;[^}]*bottom:\s*0;[^}]*height:\s*var\(--space-6\);[^}]*linear-gradient\(to bottom, transparent, var\(--color-sidebar\)\);[^}]*pointer-events:\s*none;/s)
    expect(workspaceStyles).not.toContain('.history-toolbar')
    expect(workspaceStyles).toMatch(/\.conversation-sticky-title\s*\{[^}]*position:\s*absolute;[^}]*top:\s*0;[^}]*right:\s*0;[^}]*left:\s*0;[^}]*height:\s*22px;/s)
    expect(workspaceStyles).toMatch(/\.conversation-group-title,[\s\S]*\.conversation-sticky-title\s*\{[^}]*font-size:\s*var\(--type-caption-size\);/s)
    expect(workspaceStyles).toMatch(/\.conversation-item\s*\{[^}]*min-height:\s*var\(--control-md\);[^}]*margin:\s*0;/s)
    expect(workspaceStyles).toMatch(/\.conversation-main\s*\{[^}]*padding:\s*0 10px;/s)
    expect(workspaceStyles).toMatch(/\.conversation-item:hover \.conversation-main,[\s\S]*\.conversation-item:has\(:focus-visible\) \.conversation-main,[\s\S]*\.conversation-item:has\(\.conversation-more\.is-selected\) \.conversation-main,[\s\S]*\.conversation-item\.is-active \.conversation-main\s*\{[^}]*padding-right:\s*var\(--control-sm\);/s)
    expect(workspaceStyles).toMatch(/\.conversation-title-marquee \.overflow-marquee-content\s*\{[^}]*font-size:\s*var\(--type-meta-size\);[^}]*font-weight:\s*var\(--weight-regular\);[^}]*line-height:\s*var\(--type-meta-line\);/s)
    expect(workspaceStyles).toMatch(/\.conversation-item\.is-active \.conversation-title-marquee \.overflow-marquee-content\s*\{[^}]*font-weight:\s*var\(--weight-medium\);/s)
    expect(workspaceStyles).toMatch(/\.conversation-item:hover \.conversation-title-marquee\.is-overflowing,[\s\S]*\.conversation-item:has\(:focus-visible\) \.conversation-title-marquee\.is-overflowing,[\s\S]*\.conversation-item:has\(\.conversation-more\.is-selected\) \.conversation-title-marquee\.is-overflowing,[\s\S]*\.conversation-item\.is-active \.conversation-title-marquee\.is-overflowing\s*\{[^}]*-webkit-mask-image:\s*linear-gradient\(to right, black calc\(100% - var\(--space-3\)\), transparent calc\(100% - var\(--space-1\)\)\);[^}]*mask-image:\s*linear-gradient\(to right, black calc\(100% - var\(--space-3\)\), transparent calc\(100% - var\(--space-1\)\)\);/s)
    expect(workspaceStyles).not.toContain('.conversation-more::before')
    expect(workspaceStyles).not.toContain('.conversation-item:focus-within')
    expect(workspaceStyles).toMatch(/\.conversation-item:has\(:focus-visible\) \.conversation-more,[\s\S]*\.conversation-more\.is-selected,[\s\S]*\.conversation-item\.is-active \.conversation-more\s*\{[^}]*opacity:\s*1;/s)
    expect(workspaceStyles).toMatch(/@media \(hover: none\), \(pointer: coarse\)[\s\S]*\.conversation-main\s*\{[^}]*padding-right:\s*calc\(var\(--control-lg\) \+ var\(--space-1\)\);/s)
    expect(workspaceStyles).toMatch(/\.conversation-scroll\s*\{[^}]*padding-bottom:\s*var\(--space-6\);/s)
    expect(workspaceStyles).toMatch(/\.conversation-history\s*\{[^}]*margin-right:\s*calc\(var\(--space-3\) \* -1\);/s)
    expect(workspaceStyles).toMatch(/\.conversation-scroll\s*\{[^}]*padding-right:\s*var\(--space-3\);/s)
    expect(workspaceStyles).not.toContain('--sidebar-scrollbar-thumb')
    expect(uiStyles).toMatch(/\.ui-scrollbar\s*\{[^}]*scrollbar-width:\s*none;/s)
    expect(uiStyles).toMatch(/\.ui-scrollbar::-webkit-scrollbar\s*\{[^}]*display:\s*none;[^}]*width:\s*0;[^}]*height:\s*0;/s)
    expect(uiStyles).toMatch(/\.ui-scrollbar-overlay\s*\{[^}]*position:\s*absolute;[^}]*right:\s*0;[^}]*width:\s*var\(--space-2\);[^}]*opacity:\s*0;[^}]*transition:\s*opacity var\(--motion-normal\) var\(--ease-out\);/s)
    expect(uiStyles).toMatch(/\.ui-scrollbar-overlay\.is-visible\s*\{[^}]*opacity:\s*1;/s)
    expect(uiStyles).toMatch(/\.ui-scrollbar-overlay__thumb\s*\{[^}]*right:\s*1px;[^}]*width:\s*var\(--space-1-5\);[^}]*background:\s*var\(--color-scroll-thumb\);/s)
    expect(uiStyles).toMatch(/@media \(prefers-reduced-motion:\s*reduce\)[\s\S]*\.ui-scrollbar-overlay,[\s\S]*transition:\s*none;/s)
    expect(workspaceStyles).toMatch(/\.conversation-scroll\s*\{[^}]*overflow-anchor:\s*none;/s)
    expect(workspaceStyles).toMatch(/\.history-load-sentinel\s*\{[^}]*height:\s*1px;[^}]*overflow:\s*hidden;/s)
    expect(workspaceStyles).not.toContain('.history-pagination-status')
    expect(workspaceStyles).not.toContain('.history-load-more-tail')
    expect(workspaceStyles).toMatch(/@media \(forced-colors:\s*active\)[\s\S]*\.conversation-history::after\s*{\s*display:\s*none;/s)
    expect(workspaceStyles).toMatch(/\.user-account\s*\{[^}]*min-height:\s*var\(--space-16\);[^}]*border:\s*0;/s)
    expect(uiStyles).toMatch(/\.user-avatar--sm\s*\{[^}]*width:\s*var\(--control-xs\);[^}]*height:\s*var\(--control-xs\);/s)
    expect(workspaceStyles).toMatch(/\.conversation-item\.is-active \.conversation-more\s*\{[^}]*opacity:\s*1;/s)
    expect(workspaceStyles).toMatch(/@media \(hover: none\), \(pointer: coarse\)[\s\S]*\.conversation-main,[\s\S]*min-height:\s*var\(--control-lg\);/s)
  })

  it('图标按钮全透明无阴影，标准输入框统一描边，Composer 保留独立弱边框', () => {
    const uiStyles = cssFiles['../components/ui/ui.css']
    const workspaceStyles = cssFiles['../features/workspace/workspace.css']
    const conversationStyles = cssFiles['../features/conversation/conversation.css']

    expect(uiStyles).not.toContain('.ui-icon-button::before')
    expect(uiStyles).toMatch(/\.ui-icon-button\s*\{[^}]*background:\s*transparent;/s)
    expect(uiStyles).toMatch(/\.ui-icon-button:hover:not\(:disabled\),[\s\S]*\.ui-icon-button:disabled\s*\{[^}]*background:\s*transparent;[^}]*box-shadow:\s*none;/s)
    expect(uiStyles).toMatch(/\.ui-icon-button:focus-visible\s*\{[^}]*box-shadow:\s*none;/s)
    expect(uiStyles).toMatch(/\.ui-text-field__control\s*\{[^}]*border:\s*1px solid var\(--color-border\);[^}]*background:\s*var\(--color-layer-1\);/s)
    expect(uiStyles).toMatch(/\.ui-text-field__control:focus-within\s*\{[^}]*border-color:\s*var\(--color-border-strong\);[^}]*box-shadow:\s*none;/s)
    expect(workspaceStyles).toMatch(/\.sidebar-search:focus-within\s*\{[^}]*border:\s*0;[^}]*box-shadow:\s*none;/s)
    expect(conversationStyles).toMatch(/\.composer\s*\{[^}]*border:\s*1px solid var\(--color-border\);/s)
    expect(conversationStyles).toMatch(/\.composer:focus-within\s*\{[^}]*border-color:\s*var\(--color-border-strong\);[^}]*box-shadow:\s*var\(--shadow-2\)/s)
    expect(conversationStyles).not.toMatch(/\.message-list :is\([^}]*\.subagent-card[^}]*\)/s)
    expect(conversationStyles).toMatch(/\.tool-row-title\s*\{[^}]*font-weight:\s*var\(--weight-regular\)/s)
    expect(workspaceStyles).toMatch(/\.task-drawer :is\([^}]*\.todo-item[^}]*\) \*\s*\{[^}]*font-weight:\s*var\(--weight-regular\)/s)
  })

  it('Header 只保留全局操作，模型选择归属 Composer 工具行', () => {
    const workspaceStyles = cssFiles['../features/workspace/workspace.css']
    const conversationStyles = cssFiles['../features/conversation/conversation.css']
    expect(workspaceStyles).not.toContain('.model-picker')
    expect(workspaceStyles).not.toContain('.agent-preset-picker')
    expect(conversationStyles).toMatch(/\.composer-model-picker\s*\{[^}]*width:\s*max-content;[^}]*max-width:\s*min\(220px, 45cqw\)/s)
    expect(conversationStyles).toMatch(/\.composer-toolbar\s*\{[^}]*justify-content:\s*space-between/s)
    expect(workspaceStyles).toMatch(
      /\.drawer-toggle \.ui-button__label\s*\{[^}]*display:\s*inline-flex;[^}]*white-space:\s*nowrap;/s,
    )
    expect(workspaceStyles).toMatch(
      /\.drawer-count\s*\{[^}]*display:\s*inline-grid;[^}]*place-items:\s*center;[^}]*background:\s*var\(--color-brand-soft\);[^}]*color:\s*var\(--color-brand-text\);[^}]*font-variant-numeric:\s*tabular-nums;/s,
    )
    expect(workspaceStyles).toMatch(
      /\.header-actions \.theme-switcher-circle,[\s\S]*\.drawer-toggle:focus-visible\s*{[^}]*border-color:\s*transparent;[^}]*background:\s*transparent;[^}]*box-shadow:\s*none;/s,
    )
    expect(workspaceStyles).toMatch(/\.workspace-title\s*\{[^}]*position:\s*absolute;[^}]*clip:\s*rect\(0, 0, 0, 0\);/s)
    expect(workspaceStyles).toMatch(/@media \(max-width:\s*767px\)[\s\S]*\.workspace-title\s*\{[^}]*position:\s*static;[^}]*flex:\s*1 1 auto;[^}]*font-size:\s*var\(--type-title-size\);[^}]*text-overflow:\s*ellipsis;/s)
  })

  it('新会话按控件高度使用与 Composer 一致的视觉弧度，并只通过共享选中态呈现品牌强调', () => {
    const workspaceStyles = cssFiles['../features/workspace/workspace.css']
    const uiStyles = cssFiles['../components/ui/ui.css']
    const conversationStyles = cssFiles['../features/conversation/conversation.css']

    expect(workspaceStyles).toMatch(/\.new-chat\s*\{[^}]*border-radius:\s*var\(--radius-lg\);[^}]*background:\s*var\(--color-layer-1\);/s)
    expect(conversationStyles).toMatch(/\.composer\s*\{[^}]*border-radius:\s*var\(--radius-composer\);/s)
    expect(uiStyles).toMatch(/\.ui-button\.is-selected\s*\{[^}]*background:\s*var\(--color-brand-soft\);/s)
    expect(uiStyles).toMatch(/\.ui-button\.is-selected:hover:not\(:disabled\)\s*\{[^}]*background:\s*var\(--color-brand-soft\);/s)
  })

  it('Composer 锁定命令菜单、附件条和单行工具控件的精修参数', () => {
    const conversationStyles = cssFiles['../features/conversation/conversation.css']

    expect(conversationStyles).toMatch(/\.composer-input-scroll\s*\{[^}]*max-height:\s*144px;[^}]*overflow-y:\s*auto;/s)
    expect(conversationStyles).toMatch(/\.composer-input,[\s\S]*\.composer-input-backdrop\s*\{[^}]*padding:\s*4px 12px 0 16px;[^}]*font-size:\s*var\(--type-body-size\);[^}]*line-height:\s*var\(--type-composer-line\);[^}]*white-space:\s*pre-wrap;/s)
    expect(conversationStyles).toMatch(/\.composer-toolbar\s*\{[^}]*align-items:\s*center;[^}]*min-height:\s*42px;[^}]*padding:\s*2px var\(--space-2\) 6px;/s)
    expect(conversationStyles).toMatch(/\.composer-add-button,[\s\S]*\.send-button\s*\{[^}]*height:\s*var\(--control-composer\);[^}]*min-height:\s*var\(--control-composer\);/s)
    expect(conversationStyles).toMatch(/\.composer-add-button\s*\{[^}]*border:\s*0;[^}]*background:\s*transparent;[^}]*box-shadow:\s*none;/s)
    expect(conversationStyles).toMatch(/\.composer-plan-chip\s*\{[^}]*gap:\s*var\(--space-1\);[^}]*min-width:\s*var\(--control-composer\);[^}]*height:\s*var\(--control-plan-chip\);[^}]*padding:\s*2px var\(--space-2\);[^}]*background:\s*var\(--color-warning-soft\);[^}]*font-size:\s*var\(--type-meta-size\);[^}]*line-height:\s*var\(--type-meta-line\);[^}]*box-shadow:\s*none;[^}]*transform:\s*none;/s)
    expect(conversationStyles).toMatch(/\.composer-plan-chip-close\s*\{[^}]*width:\s*var\(--space-3\);[^}]*height:\s*var\(--space-3\);/s)
    expect(conversationStyles).toMatch(/@media \(hover: none\), \(pointer: coarse\)[\s\S]*\.composer-plan-chip,[\s\S]*min-height:\s*var\(--control-lg\);[^}]*height:\s*var\(--control-lg\);/s)
    expect(conversationStyles).toMatch(/\.composer-attachment\s*\{[^}]*height:\s*var\(--control-xs\);/s)
    expect(conversationStyles).toMatch(/\.composer-model-select\s*\{[^}]*height:\s*var\(--control-composer\);[^}]*background:\s*transparent;[^}]*box-shadow:\s*none;/s)
    expect(conversationStyles).toMatch(/\.composer-model-select:hover:not\(:disabled\)\s*\{[^}]*box-shadow:\s*var\(--shadow-1\);/s)
    expect(conversationStyles).toMatch(/\.composer-model-options\s*\{[^}]*width:\s*min\(250px,[^}]*box-shadow:\s*var\(--shadow-2\);/s)
    expect(conversationStyles).toMatch(/\.composer-model-options \[role='option'\]\s*\{[^}]*min-height:\s*var\(--control-md\);[^}]*box-shadow:\s*none;/s)
    expect(conversationStyles).toMatch(/\.composer-suggestion-menu\s*\{[^}]*width:\s*min\(547px, 100%\);[^}]*max-height:\s*320px;[^}]*border-radius:\s*var\(--radius-xl\);[^}]*box-shadow:\s*var\(--shadow-3\);/s)
    expect(conversationStyles).toMatch(/\.composer-suggestion-viewport\s*\{[^}]*overflow-y:\s*auto;[^}]*overscroll-behavior:\s*contain;/s)
    expect(conversationStyles).toMatch(/@media \(forced-colors: active\)[\s\S]*\.composer-input-backdrop\s*\{\s*display:\s*none;/s)
  })

  it('共享控件覆盖焦点、禁用、触控、forced-colors 与 reduced-motion', () => {
    const uiStyles = cssFiles['../components/ui/ui.css']
    expect(tokensStyles).toContain('--motion-tooltip-hide-delay: 0ms;')
    expect(uiStyles).toMatch(/\.ui-tooltip\s*\{[^}]*opacity:\s*0;[^}]*opacity var\(--motion-instant\) linear var\(--motion-tooltip-hide-delay\)/s)
    expect(uiStyles).toMatch(/\.ui-button:focus-visible[\s\S]*outline:/)
    expect(uiStyles).toMatch(/\.ui-button:disabled[\s\S]*opacity:/)
    expect(uiStyles).toMatch(/@media \(hover: none\), \(pointer: coarse\)[\s\S]*--control-lg/)
    expect(componentStyles).toContain('@media (forced-colors: active)')
    expect(componentStyles).toContain('@media (prefers-reduced-motion: reduce)')
  })

  it('保留页面缩放并在应用执行前解析主题', () => {
    const bootstrapPosition = indexHtml.indexOf('tinkerfin:theme')
    const applicationPosition = indexHtml.indexOf('/src/main.tsx')
    expect(indexHtml).toContain('width=device-width, initial-scale=1.0')
    expect(indexHtml).not.toContain('user-scalable=no')
    expect(bootstrapPosition).toBeGreaterThan(-1)
    expect(bootstrapPosition).toBeLessThan(applicationPosition)
    expect(indexHtml).toContain("'#151517'")
  })
})
