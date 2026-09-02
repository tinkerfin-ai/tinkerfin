import { expect, test, type Page, type Route } from '@playwright/test'
import { resolve } from 'node:path'

import type { ConversationHistoryDetail } from '../../src/api/conversation/history'
import type { TraceEntryPage } from '../../src/api/conversation/traceEntries'

const THREAD_ID = 'chain-trace-browser-thread'
const RUN_ID = 'chain-trace-browser-run'
const BASE_TIME = '2026-09-01T00:00:00.000Z'
const user = {
  user_id: 27,
  username: 'chain-browser-user',
  display_name: '链路用户',
  avatar_url: null,
  roles: [],
  disabled: false,
}

const success = (data: unknown) => ({ code: 0, message: 'success', data })
const fulfillJson = (route: Route, data: unknown) => route.fulfill({
  status: 200,
  contentType: 'application/json',
  body: JSON.stringify(success(data)),
})

const tracePage: TraceEntryPage = {
  turns: [
    {
      id: 'turn-browser-1',
      ordinal: 1,
      startedAt: BASE_TIME,
      userMessage: {
        id: 'message-browser-1',
        traceSeq: 1,
        sourceId: 'user-browser-1',
        namespace: [],
        runId: 'run-browser-1',
        role: 'user',
        content: '第一轮浏览器任务',
        contentOmitted: false,
        status: 'completed',
        createdAt: BASE_TIME,
      },
    },
    {
      id: 'turn-browser-2',
      ordinal: 2,
      startedAt: '2026-09-01T00:01:00.000Z',
      userMessage: {
        id: 'message-browser-2',
        traceSeq: 10,
        sourceId: 'user-browser-2',
        namespace: [],
        runId: RUN_ID,
        role: 'user',
        content: '继续核验真实调用树',
        contentOmitted: false,
        status: 'completed',
        createdAt: '2026-09-01T00:01:00.000Z',
      },
    },
  ],
  items: [
    {
      id: 'agent-old',
      turnId: 'turn-browser-1',
      kind: 'agent',
      status: 'succeeded',
      name: 'Historical Agent',
      runId: 'run-browser-1',
      namespace: [],
      startedAt: BASE_TIME,
      completedAt: '2026-09-01T00:00:01.000Z',
      startedSeq: 2,
      updatedSeq: 3,
      requestOmitted: false,
      resultOmitted: false,
      hooks: [],
    },
    {
      id: 'agent-current',
      turnId: 'turn-browser-2',
      kind: 'agent',
      status: 'failed',
      name: 'Agent',
      runId: RUN_ID,
      namespace: [],
      startedAt: '2026-09-01T00:01:00.000Z',
      completedAt: '2026-09-01T00:01:04.000Z',
      startedSeq: 11,
      updatedSeq: 22,
      requestOmitted: false,
      resultOmitted: false,
      hooks: [],
    },
    {
      id: 'middleware-visible',
      turnId: 'turn-browser-2',
      parentId: 'agent-current',
      kind: 'middleware',
      status: 'configured',
      name: 'PromptCacheMiddleware.awrap_model_call',
      runId: RUN_ID,
      namespace: [],
      startedAt: '2026-09-01T00:01:00.010Z',
      completedAt: '2026-09-01T00:01:00.010Z',
      startedSeq: 12,
      updatedSeq: 12,
      requestOmitted: false,
      resultOmitted: false,
      className: 'deepagents.middleware.PromptCacheMiddleware',
      hooks: ['awrap_model_call'],
    },
    {
      id: 'model-current',
      turnId: 'turn-browser-2',
      parentId: 'agent-current',
      kind: 'model',
      status: 'succeeded',
      name: 'Model',
      runId: RUN_ID,
      namespace: [],
      startedAt: '2026-09-01T00:01:00.100Z',
      completedAt: '2026-09-01T00:01:01.500Z',
      startedSeq: 13,
      updatedSeq: 17,
      requestOmitted: false,
      resultOmitted: false,
      hooks: [],
    },
    {
      id: 'provider-current',
      turnId: 'turn-browser-2',
      parentId: 'model-current',
      kind: 'provider',
      status: 'succeeded',
      name: 'deepseek-v4-pro',
      runId: RUN_ID,
      namespace: [],
      provider: 'deepseek',
      model: 'deepseek-v4-pro',
      startedAt: '2026-09-01T00:01:00.200Z',
      firstOutputAt: '2026-09-01T00:01:00.500Z',
      completedAt: '2026-09-01T00:01:01.400Z',
      startedSeq: 14,
      updatedSeq: 16,
      request: {
        messages: [{ messageType: 'system', content: '浏览器系统提示词' }],
      },
      requestOmitted: false,
      resultOmitted: false,
      hooks: [],
    },
    {
      id: 'tools-current',
      turnId: 'turn-browser-2',
      parentId: 'agent-current',
      kind: 'tools',
      status: 'failed',
      name: 'Tools',
      runId: RUN_ID,
      namespace: [],
      startedAt: '2026-09-01T00:01:01.600Z',
      completedAt: '2026-09-01T00:01:03.800Z',
      startedSeq: 18,
      updatedSeq: 21,
      requestOmitted: false,
      resultOmitted: false,
      hooks: [],
    },
    {
      id: 'tool-current',
      turnId: 'turn-browser-2',
      parentId: 'tools-current',
      proposalId: 'proposal-current',
      kind: 'tool',
      status: 'failed',
      name: 'web_search',
      runId: RUN_ID,
      namespace: [],
      startedAt: '2026-09-01T00:01:01.700Z',
      completedAt: '2026-09-01T00:01:03.700Z',
      startedSeq: 19,
      updatedSeq: 20,
      requestOmitted: false,
      resultOmitted: false,
      failure: {
        errorType: 'builtins.TimeoutError',
        message: '搜索服务在期限内未响应',
      },
      hooks: [],
    },
  ],
  nextCursor: null,
  asOfSeq: 40,
  facets: {
    kinds: { agent: 2, middleware: 1, model: 1, provider: 1, tools: 1, tool: 1 },
    statuses: { succeeded: 3, failed: 3, configured: 1 },
    agents: {},
    middleware: { PromptCacheMiddleware: 1 },
    skills: {},
    providers: { deepseek: 1 },
    models: { 'deepseek-v4-pro': 1 },
  },
  completeness: { callTrackingMissing: false, executionTreeMissing: false },
}

const detail = (includeTaskTrace: boolean): ConversationHistoryDetail => ({
  id: 1,
  threadId: THREAD_ID,
  title: 'Turn 链路浏览器会话',
  lastModel: 'deepseek-v4-pro',
  runtimeProfile: 'deepagents-v2',
  pinned: false,
  asOfSeq: 22,
  headRunId: RUN_ID,
  availableHeads: [RUN_ID],
  historyCursor: null,
  messageCount: 24,
  toolCallCount: 1,
  messages: Array.from({ length: 24 }, (_, index) => ({
    id: `history-message-${index + 1}`,
    traceSeq: index + 1,
    sourceId: `assistant-history-${index + 1}`,
    namespace: [],
    runId: RUN_ID,
    role: 'assistant' as const,
    content: `第 ${index + 1} 段历史回复，用于验证链路与对话切换后仍能精确恢复用户阅读位置。`,
    contentOmitted: false,
    status: 'completed' as const,
    createdAt: `2026-09-01T00:${String(index).padStart(2, '0')}:00.000Z`,
    completedAt: `2026-09-01T00:${String(index).padStart(2, '0')}:01.000Z`,
  })),
  reasoning: [],
  nodes: [],
  state: { root: {}, subgraphs: {} },
  interactions: [],
  status: { execution: 'failed', headRunId: RUN_ID },
  completeness: { missingPrefix: false, missingTail: false, payloadOmitted: false },
  taskTrace: includeTaskTrace ? {
    status: 'ready',
    todoGroups: [{
      id: 'trace-browser-todos',
      userMessageId: 'turn-two-user',
      userMessagePreview: '继续核验真实调用树',
      groupToolCallId: 'todo-tool-call',
      createdAt: '2026-09-01T00:01:00.000Z',
      status: 'completed',
      todos: [{ id: 'verify-tree', content: '核验调用树', status: 'completed' }],
    }],
  } : null,
  createdAt: BASE_TIME,
  updatedAt: '2026-09-01T00:01:04.000Z',
})

async function mockChainTraceStudio(page: Page, theme: 'light' | 'dark' = 'light') {
  const pageErrors: string[] = []
  page.on('pageerror', (error) => pageErrors.push(error.message))
  page.on('console', (message) => {
    if (message.type() === 'error') pageErrors.push(message.text())
  })
  await page.addInitScript(({ session, threadId, snapshot, selectedTheme }) => {
    window.localStorage.setItem('tinkerfin.auth.session', JSON.stringify(session))
    window.localStorage.setItem('tinkerfin:theme', selectedTheme)
    const originalFetch = window.fetch.bind(window)
    window.fetch = (input, init) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
      if (url.includes(`/api/conversation/${threadId}/trace/entries/follow`)) {
        const encoder = new TextEncoder()
        const stream = new ReadableStream({
          start(controller) {
            controller.enqueue(encoder.encode(
              `event: trace\ndata: ${JSON.stringify({ type: 'snapshot', snapshot })}\n\n`,
            ))
            init?.signal?.addEventListener('abort', () => controller.close(), { once: true })
          },
        })
        return Promise.resolve(new Response(stream, {
          status: 200,
          headers: { 'Content-Type': 'text/event-stream' },
        }))
      }
      return originalFetch(input, init)
    }
  }, {
    session: {
      token: 'chain-browser-token',
      tokenType: 'Bearer',
      expiresAt: '2099-01-01T00:00:00.000Z',
      user,
    },
    threadId: THREAD_ID,
    snapshot: tracePage,
    selectedTheme: theme,
  })
  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url())
    if (url.pathname === '/api/auth/me') {
      await fulfillJson(route, { expires_at: '2099-01-01T00:00:00.000Z', user })
      return
    }
    if (url.pathname === '/api/models') {
      await fulfillJson(route, {
        items: [{
          modelId: 'deepseek-v4-pro',
          displayName: 'DeepSeek V4 Pro',
          reasoningEnabled: false,
          runtimeProfile: 'deepagents-v2',
          isDefault: true,
        }],
        defaultModelId: 'deepseek-v4-pro',
      })
      return
    }
    if (url.pathname === '/api/conversation/config') {
      await fulfillJson(route, { dayRanges: [7, 30] })
      return
    }
    if (url.pathname === '/api/conversation/history') {
      await fulfillJson(route, {
        items: [{
          id: 1,
          threadId: THREAD_ID,
          title: 'Turn 链路浏览器会话',
          status: 'idle',
          lastRunId: RUN_ID,
          lastModel: 'deepseek-v4-pro',
          messageCount: 24,
          toolCallCount: 1,
          hasPendingInterrupt: false,
          pendingInteractionKind: null,
          pinned: false,
          createdAt: BASE_TIME,
          updatedAt: '2026-09-01T00:01:04.000Z',
        }],
        nextCursor: null,
      })
      return
    }
    if (url.pathname === `/api/conversation/${THREAD_ID}/history`) {
      await fulfillJson(route, detail(url.searchParams.get('includeTaskTrace') !== 'false'))
      return
    }
    if (url.pathname === `/api/conversation/${THREAD_ID}/trace`) {
      const includeTaskTrace = url.searchParams.get('includeTaskTrace') !== 'false'
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: `event: trace\ndata: ${JSON.stringify({
          type: 'snapshot',
          snapshot: detail(includeTaskTrace),
        })}\n\n`,
      })
      return
    }
    await route.fulfill({ status: 404, contentType: 'application/json', body: '{}' })
  })
  await page.goto(`/?thread=${THREAD_ID}`)
  await expect(page.locator('.chain-trace-view-tabs')).toHaveCount(0)
  const chainLauncher = page.getByRole('button', { name: '链路分析' })
  const taskLauncher = page.getByRole('button', { name: '任务轨迹 1' })
  await expect(chainLauncher).toBeVisible()
  await expect(taskLauncher).toBeVisible()
  const [chainBox, taskBox] = await Promise.all([
    chainLauncher.boundingBox(),
    taskLauncher.boundingBox(),
  ])
  expect(chainBox).not.toBeNull()
  expect(taskBox).not.toBeNull()
  expect(Math.abs((chainBox?.y ?? 0) - (taskBox?.y ?? 0))).toBeLessThanOrEqual(1)
  expect(Math.abs((chainBox?.height ?? 0) - (taskBox?.height ?? 0))).toBeLessThanOrEqual(1)
  expect((chainBox?.x ?? 0) + (chainBox?.width ?? 0)).toBeLessThanOrEqual(taskBox?.x ?? 0)
  const launcherStyles = await Promise.all([chainLauncher, taskLauncher].map((launcher) => (
    launcher.evaluate((element) => {
      const style = getComputedStyle(element)
      return {
        borderTopWidth: style.borderTopWidth,
        backgroundColor: style.backgroundColor,
        boxShadow: style.boxShadow,
        sharedVariant: element.classList.contains('composer-trace-launcher'),
      }
    })
  )))
  expect(launcherStyles[0]).toEqual(launcherStyles[1])
  expect(launcherStyles[0]?.borderTopWidth).toBe('0px')
  expect(launcherStyles[0]?.sharedVariant).toBe(true)
  if (theme === 'light' && process.env.TINKERFIN_VISUAL_QA_DIR) {
    await page.screenshot({
      path: resolve(process.env.TINKERFIN_VISUAL_QA_DIR, 'chain-trace-launchers-light-1024.png'),
      fullPage: true,
    })
  }
  await chainLauncher.click()
  await expect(page.getByRole('region', { name: '链路' })).toBeVisible()
  await expect(page.getByRole('button', { name: '返回对话' })).toBeFocused()
  await expect(page.getByText('第 2 轮')).toBeVisible()
  await expect(page.getByRole('searchbox')).toHaveCount(0)
  await expect(page.getByRole('button', { name: /^提供方：/ })).toHaveCount(0)
  await expect(page.getByRole('button', { name: /^模型：/ })).toHaveCount(0)
  await expect(page.locator('select')).toHaveCount(0)
  const typeFilter = page.getByRole('button', { name: '类型：全部类型' })
  await typeFilter.click()
  const typeListbox = page.getByRole('listbox', { name: '类型' })
  await expect(typeListbox).toBeVisible()
  await expect(typeListbox).toHaveCSS('position', 'fixed')
  const [typeTriggerBox, typeListboxBox] = await Promise.all([
    typeFilter.boundingBox(),
    typeListbox.boundingBox(),
  ])
  expect(typeTriggerBox).not.toBeNull()
  expect(typeListboxBox).not.toBeNull()
  expect(typeListboxBox?.y ?? 0).toBeGreaterThanOrEqual(
    (typeTriggerBox?.y ?? 0) + (typeTriggerBox?.height ?? 0) + 5,
  )
  const selectedOptionColors = await typeListbox.locator('[aria-selected="true"]').evaluate((option) => {
    const primaryProbe = document.createElement('span')
    const brandProbe = document.createElement('span')
    primaryProbe.style.color = 'var(--color-text-primary)'
    brandProbe.style.color = 'var(--color-brand-text)'
    document.body.append(primaryProbe, brandProbe)
    const colors = {
      selected: getComputedStyle(option).color,
      primary: getComputedStyle(primaryProbe).color,
      brand: getComputedStyle(brandProbe).color,
    }
    primaryProbe.remove()
    brandProbe.remove()
    return colors
  })
  expect(selectedOptionColors.selected).toBe(selectedOptionColors.primary)
  expect(selectedOptionColors.selected).not.toBe(selectedOptionColors.brand)
  if (theme === 'light' && process.env.TINKERFIN_VISUAL_QA_DIR) {
    await page.screenshot({
      path: resolve(process.env.TINKERFIN_VISUAL_QA_DIR, 'chain-trace-filter-listbox-light-1024.png'),
      fullPage: true,
    })
  }
  await page.keyboard.press('Escape')
  await expect(typeFilter).toBeFocused()
  await expect(page.locator('.chain-trace-toolbar')).toHaveCount(0)
  const alignment = await page.locator('.workspace-main').evaluate((workspace) => {
    const header = workspace.querySelector<HTMLElement>('.chat-header')
    const controls = header?.querySelector<HTMLElement>('.chain-trace-header-controls')
    const firstControl = controls?.querySelector<HTMLElement>('.chain-trace-back')
    const treeScroll = workspace.querySelector<HTMLElement>('.chain-trace-tree-scroll')
    const turn = workspace.querySelector<HTMLElement>('.chain-trace-turn')
    if (!header || !controls || !firstControl || !treeScroll || !turn) return null
    const headerStyle = getComputedStyle(header)
    const treeScrollStyle = getComputedStyle(treeScroll)
    const headerRect = header.getBoundingClientRect()
    const firstControlRect = firstControl.getBoundingClientRect()
    const treeScrollRect = treeScroll.getBoundingClientRect()
    const turnRect = turn.getBoundingClientRect()
    return {
      headerHeight: headerRect.height,
      expectedHeight: Number.parseFloat(
        getComputedStyle(document.documentElement).getPropertyValue('--layout-header-height'),
      ),
      borderTopWidth: headerStyle.borderTopWidth,
      borderBottomWidth: headerStyle.borderBottomWidth,
      firstControlHeight: firstControlRect.height,
      controlCenterOffset: Math.abs(
        firstControlRect.top + firstControlRect.height / 2
          - (headerRect.top + headerRect.height / 2),
      ),
      turnOffset: turnRect.left - treeScrollRect.left,
      turnWidth: turnRect.width,
      treeContentWidth: treeScroll.clientWidth
        - Number.parseFloat(treeScrollStyle.paddingLeft)
        - Number.parseFloat(treeScrollStyle.paddingRight),
      treePaddingLeft: Number.parseFloat(treeScrollStyle.paddingLeft),
    }
  })
  expect(alignment).not.toBeNull()
  expect(alignment?.headerHeight).toBe(alignment?.expectedHeight)
  expect(alignment?.borderTopWidth).toBe('0px')
  expect(alignment?.borderBottomWidth).toBe('0px')
  expect(alignment?.firstControlHeight).toBe(32)
  expect(alignment?.controlCenterOffset).toBeLessThanOrEqual(1)
  expect(Math.abs((alignment?.turnOffset ?? 0) - (alignment?.treePaddingLeft ?? 0)))
    .toBeLessThanOrEqual(1)
  expect(Math.abs((alignment?.turnWidth ?? 0) - (alignment?.treeContentWidth ?? 0)))
    .toBeLessThanOrEqual(1)
  const treeButtonSurfaces = await page.locator(
    '.chain-trace-turn-toggle, .chain-trace-node-toggle, .chain-trace-node-select',
  ).evaluateAll((buttons) => buttons.map((button) => {
    const style = getComputedStyle(button)
    return {
      backgroundColor: style.backgroundColor,
      borderTopWidth: style.borderTopWidth,
      borderRightWidth: style.borderRightWidth,
      borderBottomWidth: style.borderBottomWidth,
      borderLeftWidth: style.borderLeftWidth,
    }
  }))
  expect(treeButtonSurfaces.length).toBeGreaterThan(0)
  treeButtonSurfaces.forEach((surface) => {
    expect(surface.backgroundColor).toBe('rgba(0, 0, 0, 0)')
    expect(new Set([
      surface.borderTopWidth,
      surface.borderRightWidth,
      surface.borderBottomWidth,
      surface.borderLeftWidth,
    ])).toEqual(new Set(['0px']))
  })
  return pageErrors
}

test('Turn 树、错误归属、独立收起和详情焦点可真实交互', async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 800 })
  const pageErrors = await mockChainTraceStudio(page)
  const traceRegion = page.getByRole('region', { name: '链路' })

  await expect(page.getByText('第 1 轮')).toBeVisible()
  await expect(page.getByText('Historical Agent')).toHaveCount(0)
  await expect(traceRegion.getByText('继续核验真实调用树')).toBeVisible()
  await expect(page.locator('.chain-trace-duration', { hasText: '仅配置' })).toHaveCount(0)
  await expect(page.getByText('错误', { exact: true })).toHaveCount(1)
  await expect(page.getByText('搜索服务在期限内未响应')).toBeVisible()
  await expect(page.getByRole('button', { name: '工具阶段，Tools' })).toHaveCount(0)
  await expect(page.getByRole('button', {
    name: '中间件，PromptCacheMiddleware.awrap_model_call',
  })).toHaveCount(0)
  const technicalNodes = page.getByRole('button', { name: '技术节点' })
  await expect(page.locator('.chain-trace-entry-icon.is-middleware')).toHaveCount(0)
  await expect(technicalNodes).toHaveAttribute('aria-pressed', 'false')
  await technicalNodes.click()
  await expect(technicalNodes).toHaveAttribute('aria-pressed', 'true')
  const technicalColors = await technicalNodes.evaluate((button) => {
    const mark = button.querySelector<HTMLElement>('.ui-filter-toggle__mark')
    const probe = document.createElement('span')
    probe.style.color = 'var(--color-text-primary)'
    document.body.append(probe)
    const primary = getComputedStyle(probe).color
    probe.remove()
    return {
      color: getComputedStyle(button).color,
      markBackground: mark ? getComputedStyle(mark).backgroundColor : '',
      primary,
    }
  })
  expect(technicalColors.color).toBe(technicalColors.primary)
  expect(technicalColors.markBackground).toBe(technicalColors.primary)
  await expect(page.locator('.chain-trace-entry-icon.is-middleware')).toHaveCount(1)
  await expect(page.getByRole('button', { name: '工具阶段，Tools' })).toBeVisible()
  await technicalNodes.click()
  await expect(technicalNodes).toHaveAttribute('aria-pressed', 'false')
  await expect(page.locator('.chain-trace-entry-icon.is-middleware')).toHaveCount(0)
  await expect(page.getByRole('button', { name: '工具阶段，Tools' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '工具执行，web_search' })).toBeVisible()
  await expect(page.locator('table')).toHaveCount(0)
  await expect(page.getByText(/运行 · run-/)).toHaveCount(0)

  await page.getByRole('button', { name: '收起 Model' }).click()
  await expect(page.getByRole('button', { name: '模型请求，deepseek-v4-pro' })).toHaveCount(0)
  await page.getByRole('button', { name: '展开 Model' }).click()
  const provider = page.getByRole('button', { name: '模型请求，deepseek-v4-pro' })
  const providerNode = page.locator('[data-trace-entry-id="provider-current"]').locator('xpath=../..')
  const modelNode = page.locator('[data-trace-entry-id="model-current"]').locator('xpath=../..')
  const siblingToolNode = page.locator('[data-trace-entry-id="tool-current"]').locator('xpath=../..')
  await provider.click()
  await expect(providerNode).toHaveClass(/is-on-selected-path/)
  await expect(modelNode).toHaveClass(/is-on-selected-path/)
  await expect(siblingToolNode).not.toHaveClass(/is-on-selected-path/)
  const pathColors = await providerNode.evaluate((node) => {
    const elbow = node.querySelector<HTMLElement>('.chain-trace-connector-elbow')
    const probe = document.createElement('span')
    probe.style.color = 'var(--color-brand)'
    document.body.append(probe)
    const brand = getComputedStyle(probe).color
    probe.remove()
    return { brand, connector: elbow ? getComputedStyle(elbow).borderBottomColor : '' }
  })
  expect(pathColors.connector).toBe(pathColors.brand)
  const layerOrder = await page.locator('[data-trace-entry-id="provider-current"]').evaluate((button) => {
    const icon = button.querySelector<HTMLElement>('.chain-trace-entry-icon')
    const connector = button.closest('.chain-trace-node')
      ?.querySelector<HTMLElement>('.chain-trace-connector-elbow')
    return {
      icon: Number(getComputedStyle(icon!).zIndex),
      connector: Number(getComputedStyle(connector!).zIndex),
    }
  })
  expect(layerOrder.icon).toBeGreaterThan(layerOrder.connector)
  if (process.env.TINKERFIN_VISUAL_QA_DIR) {
    await page.screenshot({
      path: resolve(process.env.TINKERFIN_VISUAL_QA_DIR, 'chain-trace-selected-path-light-1024.png'),
      fullPage: true,
    })
  }
  const details = page.getByRole('complementary', { name: '链路详情' })
  const treeScroll = page.locator('.chain-trace-tree-scroll')
  await expect(details).toBeVisible()
  await expect(page.locator('.chain-trace-detail-resizer')).toHaveCount(0)
  await expect(treeScroll).toHaveAttribute('inert', '')
  await expect(details.getByRole('button', { name: '关闭链路详情' })).toBeFocused()
  await page.setViewportSize({ width: 1440, height: 800 })
  await expect(treeScroll).not.toHaveAttribute('inert')
  await provider.focus()
  await page.setViewportSize({ width: 768, height: 800 })
  await expect(treeScroll).toHaveAttribute('inert', '')
  await expect(details.getByRole('button', { name: '关闭链路详情' })).toBeFocused()
  await details.getByRole('tab', { name: '系统提示词' }).click()
  await expect(details.getByText('浏览器系统提示词')).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(details).toBeHidden()
  await expect(provider).toBeFocused()

  await page.getByRole('button', { name: '收起 Agent' }).click()
  await expect(page.getByRole('button', { name: '模型，Model' })).toHaveCount(0)
  await page.getByRole('button', { name: '展开 Agent' }).click()
  await expect(page.getByRole('button', { name: '模型，Model' })).toBeVisible()

  await page.getByRole('button', { name: '展开第 1 轮' }).click()
  await expect(page.getByText('Historical Agent')).toBeVisible()
  await page.getByRole('button', { name: '收起第 1 轮' }).click()
  await expect(page.getByText('Historical Agent')).toHaveCount(0)
  await page.getByRole('button', { name: '返回对话' }).click()
  await expect(page.getByRole('button', { name: '链路分析' })).toBeFocused()
  expect(pageErrors).toEqual([])
})

test('链路与对话往返恢复阅读位置并保留末尾跟随状态', async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 800 })
  const pageErrors = await mockChainTraceStudio(page)
  await page.getByRole('button', { name: '返回对话' }).click()
  const pane = page.getByRole('region', { name: '对话内容' })
  await expect(pane).toBeVisible()

  const readingPosition = await pane.evaluate((element) => {
    const target = Math.round((element.scrollHeight - element.clientHeight) * .43)
    element.scrollTop = target
    element.dispatchEvent(new WheelEvent('wheel', { bubbles: true, deltaY: -120 }))
    element.dispatchEvent(new Event('scroll'))
    return element.scrollTop
  })
  await page.waitForTimeout(50)
  await page.getByRole('button', { name: '链路分析' }).click()
  await page.getByRole('button', { name: '返回对话' }).click()
  await expect(page.getByRole('region', { name: '对话内容' })).toBeVisible()
  await expect.poll(() => page.getByRole('region', { name: '对话内容' }).evaluate(
    (element) => element.scrollTop,
  )).toBe(readingPosition)

  await pane.evaluate((element) => {
    element.scrollTop = element.scrollHeight
    element.dispatchEvent(new Event('scroll'))
  })
  await page.waitForTimeout(50)
  await page.getByRole('button', { name: '链路分析' }).click()
  await page.getByRole('button', { name: '返回对话' }).click()
  await expect.poll(() => page.getByRole('region', { name: '对话内容' }).evaluate(
    (element) => Math.abs(
      element.scrollHeight - element.clientHeight - element.scrollTop,
    ),
  )).toBeLessThanOrEqual(1)
  const bottomLayout = await page.locator('.workspace-main').evaluate((workspace) => {
    const shell = workspace.closest<HTMLElement>('.app-shell')
    const list = workspace.querySelector<HTMLElement>('.message-list')
    const dock = workspace.querySelector<HTMLElement>('.composer-dock')
    const actionRows = list?.querySelectorAll<HTMLElement>('.message-action-row--assistant')
    const lastAction = actionRows?.item((actionRows?.length ?? 1) - 1)
    const end = list?.lastElementChild
    const content = list ? Array.from(list.children).slice(0, -1) : []
    if (!shell || !list || !dock || !end || content.length === 0) return null
    const dockRect = dock.getBoundingClientRect()
    return {
      composerHeight: dockRect.height,
      measuredHeight: Number.parseFloat(
        getComputedStyle(shell).getPropertyValue('--composer-height'),
      ),
      reservedHeight: Number.parseFloat(getComputedStyle(list).paddingBottom),
      contentBottom: Math.max(...content.map((element) => (
        element.getBoundingClientRect().bottom
      ))),
      endBottom: end.getBoundingClientRect().bottom,
      actionBottom: lastAction?.getBoundingClientRect().bottom ?? null,
      composerTop: dockRect.top,
    }
  })
  expect(bottomLayout).not.toBeNull()
  expect(Math.abs(
    (bottomLayout?.composerHeight ?? 0) - (bottomLayout?.measuredHeight ?? 0),
  )).toBeLessThanOrEqual(1)
  expect(Math.abs(
    (bottomLayout?.composerHeight ?? 0) - (bottomLayout?.reservedHeight ?? 0),
  )).toBeLessThanOrEqual(1)
  expect(bottomLayout?.contentBottom ?? Number.POSITIVE_INFINITY)
    .toBeLessThanOrEqual((bottomLayout?.composerTop ?? 0) + 1)
  expect(bottomLayout?.endBottom ?? Number.POSITIVE_INFINITY)
    .toBeLessThanOrEqual((bottomLayout?.composerTop ?? 0) + 1)
  if (bottomLayout?.actionBottom != null) {
    expect(bottomLayout.actionBottom).toBeLessThanOrEqual(bottomLayout.composerTop + 1)
  }
  expect(pageErrors).toEqual([])
})

test('四个视口、深色与 reduced-motion 不产生页面横向溢出', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  const pageErrors = await mockChainTraceStudio(page, 'dark')

  for (const width of [320, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 900 })
    await expect(page.getByText('第 2 轮')).toBeVisible()
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
    expect(overflow).toBeLessThanOrEqual(0)
  }

  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.setViewportSize({ width: 320, height: 800 })
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
  const toggle = page.getByRole('button', { name: '收起 Model' })
  await expect(toggle.locator('svg')).toHaveCSS('transition-duration', '0s')
  const cdp = await page.context().newCDPSession(page)
  await cdp.send('Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 1 })
  await expect.poll(() => page.evaluate(() => matchMedia('(any-pointer: coarse)').matches)).toBe(true)
  const touchTarget = await toggle.boundingBox()
  expect(touchTarget?.width).toBeGreaterThanOrEqual(44)
  expect(touchTarget?.height).toBeGreaterThanOrEqual(44)
  const treeOverflow = await page.locator('.chain-trace-tree-scroll').evaluate((element) => (
    element.scrollWidth - element.clientWidth
  ))
  expect(treeOverflow).toBeLessThanOrEqual(0)
  const typeFilter = page.getByRole('button', { name: '类型：全部类型' })
  await typeFilter.click()
  const typeListbox = page.getByRole('listbox', { name: '类型' })
  await page.locator('.chain-trace-header-controls').evaluate((header) => {
    header.scrollLeft = Math.min(header.scrollWidth - header.clientWidth, header.scrollLeft + 40)
    header.dispatchEvent(new Event('scroll'))
  })
  await expect.poll(async () => {
    const [triggerBox, listboxBox] = await Promise.all([
      typeFilter.boundingBox(),
      typeListbox.boundingBox(),
    ])
    if (!triggerBox || !listboxBox) return Number.POSITIVE_INFINITY
    const expectedLeft = Math.max(
      8,
      Math.min(triggerBox.x, 320 - listboxBox.width - 8),
    )
    return Math.abs(listboxBox.x - expectedLeft)
  }).toBeLessThanOrEqual(1)
  await page.keyboard.press('Escape')
  if (process.env.TINKERFIN_VISUAL_QA_DIR) {
    await page.screenshot({
      path: resolve(process.env.TINKERFIN_VISUAL_QA_DIR, 'chain-trace-turn-tree-dark-320.png'),
      fullPage: true,
    })
  }
  expect(pageErrors).toEqual([])
})
