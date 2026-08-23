import { describe, expect, it } from 'vitest'

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
    expect(typographyStyles).toMatch(/--type-h1-size:\s*24px;[\s\S]*--type-h1-line:\s*34px;/)
  })

  it('锁定布局、控件、圆角、层级与 100/200/300ms 动效尺度', () => {
    for (const declaration of [
      '--layout-sidebar-expanded: 261px;',
      '--layout-sidebar-rail: 56px;',
      '--layout-content-wide: 840px;',
      '--layout-task-drawer: 348px;',
      '--control-lg: 44px;',
      '--layer-local: 1;',
      '--layer-local-raised: 2;',
      '--motion-fast: 100ms;',
      '--motion-normal: 200ms;',
      '--motion-slow: 300ms;',
      '--motion-loading-cycle: 1000ms;',
      '--shadow-focus: 0 0 0 3px var(--color-focus-soft);',
    ]) expect(tokensStyles).toContain(declaration)
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

  it('所有功能 CSS 入口都受扫描且不直接声明十六进制色或原始色令牌', () => {
    expect(Object.keys(cssFiles)).toEqual(expect.arrayContaining([
      '../components/ui/ui.css',
      '../features/auth/auth.css',
      '../features/conversation/conversation.css',
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
    expect(breakpointValues).toEqual(['440', '767', '1023', '1600'])
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
      [cssFiles['../features/auth/auth.css'], '.auth-brand'],
      [cssFiles['../components/ui/ui.css'], '.toast-card > button'],
      [cssFiles['../features/conversation/conversation.css'], '.subagent-trace-head button'],
      [cssFiles['../features/workspace/workspace.css'], '.conversation-main'],
      [cssFiles['../features/workspace/workspace.css'], '.user-card'],
      [cssFiles['../features/workspace/workspace.css'], '.model-select'],
      [cssFiles['../features/workspace/workspace.css'], '.agent-preset'],
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
    expect(markdownStyles).not.toMatch(/tbody tr:nth-child|tbody tr:hover/)
  })

  it('三态侧栏、inline search、任务抽屉与必要断点均由 workspace 所有', () => {
    const workspaceStyles = cssFiles['../features/workspace/workspace.css']
    expect(workspaceStyles).toMatch(/grid-template-columns:\s*var\(--layout-sidebar-expanded\)/)
    expect(workspaceStyles).toMatch(/data-sidebar-mode='rail'[\s\S]*var\(--layout-sidebar-rail\)/)
    expect(workspaceStyles).toContain('.history-toolbar.is-search-open')
    expect(workspaceStyles).toMatch(/@media \(max-width:\s*767px\)/)
    expect(workspaceStyles).toMatch(/@media \(min-width:\s*1600px\)/)
    expect(workspaceStyles).toContain('scrollbar-gutter: stable')
  })

  it('回到底部使用独立操作轨道、44px 命中区和较小视觉圆面，不覆盖滚动正文', () => {
    const workspaceStyles = cssFiles['../features/workspace/workspace.css']
    expect(workspaceStyles).toMatch(
      /\.conversation-region\s*\{[^}]*display:\s*grid;[^}]*grid-template-rows:\s*minmax\(0, 1fr\) var\(--control-lg\);/s,
    )
    expect(workspaceStyles).toMatch(
      /\.conversation-scroll-action\s*\{[^}]*height:\s*var\(--control-lg\);[^}]*padding:\s*0 var\(--space-10\);/s,
    )
    expect(workspaceStyles).toMatch(
      /\.scroll-to-bottom\s*\{[^}]*width:\s*var\(--control-lg\);[^}]*height:\s*var\(--control-lg\);/s,
    )
    expect(workspaceStyles).toMatch(
      /\.scroll-to-bottom::before\s*\{[^}]*width:\s*var\(--control-sm\);[^}]*height:\s*var\(--control-sm\);/s,
    )
    expect(workspaceStyles).not.toMatch(
      /\.scroll-to-bottom\s*\{[^}]*(?:position:\s*(?:absolute|fixed)|bottom:|right:)/s,
    )
  })

  it('Header 模型、Agent 和任务计数保持同一行且具有稳定可读宽度', () => {
    const workspaceStyles = cssFiles['../features/workspace/workspace.css']
    expect(workspaceStyles).toMatch(/\.model-picker\s*\{\s*width:\s*176px;/)
    expect(workspaceStyles).toMatch(/\.agent-preset-picker\s*\{\s*width:\s*124px;/)
    expect(workspaceStyles).toMatch(
      /\.drawer-toggle \.ui-button__label\s*\{[^}]*display:\s*inline-flex;[^}]*white-space:\s*nowrap;/s,
    )
    expect(workspaceStyles).toMatch(
      /\.drawer-toggle b\s*\{[^}]*display:\s*inline-grid;[^}]*place-items:\s*center;/s,
    )
  })

  it('共享控件覆盖焦点、禁用、触控、forced-colors 与 reduced-motion', () => {
    const uiStyles = cssFiles['../components/ui/ui.css']
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
