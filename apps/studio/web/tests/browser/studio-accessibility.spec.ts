import { expect, test, type Page, type Route } from '@playwright/test'
import type { Message } from '../../src/types'

const THREAD_ID = 'browser-thread'
const BASE_TIME = '2026-08-25T00:00:00.000Z'
const user = {
  user_id: 7,
  username: 'browser-user',
  display_name: '浏览器用户',
  avatar_url: null,
  roles: [],
  disabled: false,
}

const messages: Message[] = Array.from({ length: 150 }, (_, index) => ({
  id: `browser-message-${index + 1}`,
  role: 'assistant',
  content: `浏览器历史消息 ${index + 1}`,
  createdAt: BASE_TIME,
}))

messages.push({
  id: 'browser-subagent',
  role: 'subagent',
  content: '浏览器子智能体结果',
  createdAt: BASE_TIME,
  meta: {
    agentName: 'researcher',
    result: '子智能体已完成',
    status: 'completed',
    subRunId: 'browser-subagent-run-completed',
  },
})

messages.push({
  id: 'browser-tool',
  role: 'tool',
  content: 'read_file',
  createdAt: BASE_TIME,
  meta: {
    toolName: 'read_file',
    params: '{"path":"README.md"}',
    result: '读取完成',
    status: 'completed',
  },
})

messages.push({
  id: 'browser-final-answer',
  role: 'assistant',
  content: '浏览器最终回答',
  createdAt: BASE_TIME,
  meta: { status: 'completed' },
})

const runningActivityMessages: Message[] = [
  ...messages,
  {
    id: 'browser-running-user',
    role: 'user',
    content: '继续执行新一轮任务',
    createdAt: BASE_TIME,
  },
  {
    id: 'browser-running-stage',
    role: 'assistant',
    content: '我先检查当前状态',
    createdAt: BASE_TIME,
    meta: { status: 'completed', runId: 'browser-run' },
  },
  {
    id: 'browser-running-subagent',
    role: 'subagent',
    content: '浏览器运行中子智能体',
    createdAt: BASE_TIME,
    meta: {
      agentName: 'researcher',
      input: '检查运行中标题稳定性',
      result: '',
      status: 'running',
      toolCallId: 'browser-task-call',
      subRunId: 'browser-subagent-run',
      runId: 'browser-subagent-run',
    },
  },
  {
    id: 'browser-running-child-tool',
    role: 'tool',
    content: 'read_file',
    createdAt: BASE_TIME,
    meta: {
      toolName: 'read_file',
      params: '{"path":"README.md"}',
      result: '',
      status: 'running',
      toolCallId: 'browser-child-tool-call',
      runId: 'browser-subagent-run',
      sourceAgentName: 'researcher',
    },
  },
  {
    id: 'browser-running-main-tool',
    role: 'tool',
    content: 'read_file',
    createdAt: BASE_TIME,
    meta: {
      toolName: 'read_file',
      params: '{"path":"AGENTS.md"}',
      result: '',
      status: 'running',
      toolCallId: 'browser-main-tool-call',
      runId: 'browser-run',
    },
  },
]

const spacingAuditMessages: Message[] = [
  {
    id: 'spacing-user-a',
    role: 'user',
    content: '气泡 A',
    createdAt: BASE_TIME,
  },
  {
    id: 'spacing-assistant-a',
    role: 'assistant',
    content: '普通文本 A',
    createdAt: BASE_TIME,
    meta: { status: 'completed' },
  },
  {
    id: 'spacing-user-b',
    role: 'user',
    content: '气泡 B',
    createdAt: BASE_TIME,
  },
  {
    id: 'spacing-subagent',
    role: 'subagent',
    content: '子智能体结果',
    createdAt: BASE_TIME,
    meta: {
      agentName: 'researcher',
      result: '完成',
      status: 'completed',
      subRunId: 'spacing-subagent-run',
    },
  },
  {
    id: 'spacing-tool',
    role: 'tool',
    content: 'read_file',
    createdAt: BASE_TIME,
    meta: {
      toolName: 'read_file',
      params: '{"path":"README.md"}',
      result: '完成',
      status: 'completed',
    },
  },
  {
    id: 'spacing-assistant-b',
    role: 'assistant',
    content: '普通文本 B',
    createdAt: BASE_TIME,
    meta: { status: 'completed' },
  },
  {
    id: 'spacing-user-c',
    role: 'user',
    content: '气泡 C',
    createdAt: BASE_TIME,
  },
  {
    id: 'spacing-error',
    role: 'error',
    content: '错误反馈',
    createdAt: BASE_TIME,
  },
]

const success = (data: unknown) => ({ code: 0, message: 'success', data })

const fulfillJson = (route: Route, data: unknown) => route.fulfill({
  status: 200,
  contentType: 'application/json',
  body: JSON.stringify(success(data)),
})

interface MockStudioOptions {
  conversationMessages?: Message[]
  emptyHistory?: boolean
  expectedMessageText?: string
  onHistoryRequest?: (request: { cursor: string | null; receivedAt: number }) => void
  paginatedHistory?: boolean
  paginationPageCount?: number
  paginationResponseDelayMs?: number
  runError?: boolean
  runningActivity?: boolean
}

async function mockStudio(page: Page, {
  conversationMessages,
  emptyHistory = false,
  expectedMessageText = '浏览器历史消息 150',
  onHistoryRequest,
  paginatedHistory = false,
  paginationPageCount = 2,
  paginationResponseDelayMs = 0,
  runError = false,
  runningActivity = false,
}: MockStudioOptions = {}) {
  let historyRequestCount = 0
  const historyMessages = conversationMessages ?? (runningActivity ? runningActivityMessages : messages)
  page.on('console', (message) => {
    if (message.type() === 'error') console.error(`browser console: ${message.text()}`)
  })
  page.on('pageerror', (error) => console.error(`browser pageerror: ${error.message}`))
  page.on('response', (response) => {
    if (response.status() >= 400) {
      console.error(`browser response: ${response.status()} ${response.url()}`)
    }
  })
  await page.addInitScript(({ storageKey, session }) => {
    window.localStorage.setItem(storageKey, JSON.stringify(session))
  }, {
    storageKey: 'tinkerfin.auth.session.v1',
    session: {
      token: 'browser-token',
      tokenType: 'Bearer',
      expiresAt: '2099-01-01T00:00:00.000Z',
      user,
    },
  })

  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url())
    if (!url.pathname.startsWith('/api/')) {
      await route.continue()
      return
    }
    if (url.pathname === '/api/auth/me') {
      await fulfillJson(route, { expires_at: '2099-01-01T00:00:00.000Z', user })
      return
    }
    if (url.pathname === '/api/models') {
      await fulfillJson(route, {
        items: [
          { modelId: 'GPT-5.5', displayName: 'GPT-5.5', reasoningEnabled: false, isDefault: true },
          { modelId: 'Qwen-3.7', displayName: 'Qwen-3.7', reasoningEnabled: false, isDefault: false },
        ],
        defaultModelId: 'GPT-5.5',
      })
      return
    }
    if (url.pathname === '/api/conversation/config') {
      await fulfillJson(route, { dayRanges: [7, 30] })
      return
    }
    if (runError && route.request().method() === 'POST' && url.pathname === '/api/conversation/chat') {
      const runId = `browser-error-run-${Date.now()}`
      const events = [
        { type: 'RUN_STARTED', threadId: THREAD_ID, runId },
        { type: 'RUN_ERROR', rawEvent: { runId }, message: '后端运行错误。', code: 'failed' },
      ]
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join(''),
      })
      return
    }
    if (url.pathname === '/api/conversation/history') {
      const cursor = url.searchParams.get('cursor')
      onHistoryRequest?.({ cursor, receivedAt: Date.now() })
      const pageStart = cursor ? 20 + Math.max(0, historyRequestCount - 1) * 10 : 0
      const pageSize = paginatedHistory ? (cursor ? 10 : 20) : 1
      historyRequestCount += 1
      if (cursor && paginationResponseDelayMs > 0) {
        await new Promise((resolve) => setTimeout(resolve, paginationResponseDelayMs))
      }
      await fulfillJson(route, {
        items: emptyHistory ? [] : Array.from({ length: pageSize }, (_, offset) => {
          const index = pageStart + offset
          return {
            id: index + 1,
            threadId: index === 0 ? THREAD_ID : `browser-history-${index}`,
            title: index === 0 ? '浏览器会话' : `分页验证会话 ${index}`,
            status: 'idle',
            lastRunId: index === 0 ? 'browser-run' : undefined,
            lastModel: 'GPT-5.5',
            lastSeq: index === 0 ? 151 : 0,
            messageCount: index === 0 ? historyMessages.length : 0,
            toolCallCount: index === 0 ? 1 : 0,
            hasPendingInterrupt: false,
            pinned: false,
            createdAt: BASE_TIME,
            updatedAt: BASE_TIME,
          }
        }),
        nextCursor: paginatedHistory && historyRequestCount < paginationPageCount
          ? `browser-page-${historyRequestCount + 1}`
          : null,
      })
      return
    }
    if (url.pathname === `/api/conversation/${THREAD_ID}/history`) {
      await fulfillJson(route, {
        id: 1,
        threadId: THREAD_ID,
        title: '浏览器会话',
        status: 'idle',
        lastRunId: 'browser-run',
        lastModel: 'GPT-5.5',
        lastSeq: 151,
        snapshotSeq: 151,
        snapshotVersion: 3,
        messageCount: historyMessages.length,
        toolCallCount: 1,
        hasPendingInterrupt: false,
        pinned: false,
        snapshot: {
          snapshotSeq: 151,
          snapshotVersion: 3,
          messages: historyMessages,
          todos: [],
          mode: 'default',
          approval: null,
          runStatus: runningActivity ? 'streaming' : 'idle',
          activeRunId: runningActivity ? 'browser-run' : null,
          serverState: {},
          runs: {},
          interrupts: [],
        },
        events: [],
        createdAt: BASE_TIME,
        updatedAt: BASE_TIME,
      })
      return
    }
    if (url.pathname === `/api/conversation/${THREAD_ID}/events`) {
      await fulfillJson(route, [])
      return
    }
    await route.fulfill({ status: 404, contentType: 'application/json', body: JSON.stringify({}) })
  })

  await page.goto('/')
  await expect(page.getByRole('textbox', { name: '消息输入' })).toBeVisible()
  if (emptyHistory) await expect(page.locator('.composer-dock.is-hero')).toBeVisible()
  else await expect(page.getByText(expectedMessageText, { exact: true })).toBeVisible()
}

const contrastRatios = async (page: Page, selector: string) => page.locator(selector).evaluateAll((elements) => {
  const parse = (value: string) => (value.match(/[\d.]+/g) ?? []).slice(0, 3).map(Number)
  const luminance = (value: string) => {
    const channels = parse(value).map((channel) => {
      const normalized = channel / 255
      return normalized <= 0.04045
        ? normalized / 12.92
        : ((normalized + 0.055) / 1.055) ** 2.4
    })
    return (0.2126 * channels[0]) + (0.7152 * channels[1]) + (0.0722 * channels[2])
  }
  return elements.map((element) => {
    const foreground = luminance(getComputedStyle(element).color)
    const background = luminance(getComputedStyle(element.parentElement!).backgroundColor)
    return (Math.max(foreground, background) + 0.05) / (Math.min(foreground, background) + 0.05)
  })
})

test('四个目标视口保持正确导航形态且没有页面级横向溢出', async ({ page }) => {
  await mockStudio(page)
  for (const [width, expectedMode] of [
    [320, 'overlay'],
    [768, 'rail'],
    [1024, 'expanded'],
    [1440, 'expanded'],
  ] as const) {
    await page.setViewportSize({ width, height: 900 })
    await expect(page.locator('.app-shell')).toHaveAttribute('data-sidebar-mode', expectedMode)
    const overflow = await page.evaluate(() => Math.max(
      document.documentElement.scrollWidth - document.documentElement.clientWidth,
      document.body.scrollWidth - document.body.clientWidth,
    ))
    expect(overflow).toBeLessThanOrEqual(0)
  }
})

test('折叠侧栏 tooltip 与 Rail 外边界保持稳定间距', async ({ page }) => {
  await mockStudio(page)
  const shell = page.locator('.app-shell')
  const rail = page.locator('.sidebar-rail')
  const searchButton = rail.getByRole('button', { name: '搜索会话' })
  const tooltip = rail.getByRole('tooltip').filter({ hasText: '搜索会话' })

  for (const colorScheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme })
    for (const width of [768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 900 })
      if (await shell.getAttribute('data-sidebar-mode') !== 'rail') {
        await page.getByRole('button', { name: '收起侧边栏' }).click()
      }
      await expect(shell).toHaveAttribute('data-sidebar-mode', 'rail')
      await searchButton.hover()
      await expect(tooltip).toBeVisible()

      const railBounds = await rail.boundingBox()
      const buttonBounds = await searchButton.boundingBox()
      const tooltipBounds = await tooltip.boundingBox()
      if (!railBounds || !buttonBounds || !tooltipBounds) throw new Error('折叠侧栏 tooltip 几何不可用')
      expect(tooltipBounds.x - (railBounds.x + railBounds.width)).toBeGreaterThanOrEqual(8)
      expect(Math.abs(
        (tooltipBounds.y + (tooltipBounds.height / 2))
        - (buttonBounds.y + (buttonBounds.height / 2)),
      )).toBeLessThanOrEqual(1)
    }
  }
})

test('首页与会话态使用相同的输入卡片高度', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await mockStudio(page, { emptyHistory: true })
  const heroHeight = await page.locator('.composer').evaluate((element) => (
    element.getBoundingClientRect().height
  ))

  await page.unroute('**/api/**')
  await mockStudio(page)
  await expect(page.locator('.composer-dock')).not.toHaveClass(/is-hero/)
  const conversationHeight = await page.locator('.composer').evaluate((element) => (
    element.getBoundingClientRect().height
  ))

  expect(heroHeight).toBe(conversationHeight)
  expect(heroHeight).toBe(94)
})

test('气泡、文本、Task、Tool 与反馈卡片使用统一光学节奏', async ({ page }) => {
  await mockStudio(page, {
    conversationMessages: spacingAuditMessages,
    expectedMessageText: '普通文本 B',
  })
  const locators = {
    userA: page.locator('#spacing-user-a .message-markdown'),
    assistantABody: page.locator('#spacing-assistant-a .message-markdown > :first-child'),
    assistantA: page.locator('#spacing-assistant-a'),
    userB: page.locator('#spacing-user-b .message-markdown'),
    subagent: page.locator('#spacing-subagent'),
    tool: page.locator('#spacing-tool'),
    assistantBBody: page.locator('#spacing-assistant-b .message-markdown > :first-child'),
    assistantB: page.locator('#spacing-assistant-b'),
    userC: page.locator('#spacing-user-c .message-markdown'),
    error: page.locator('#spacing-error'),
  }
  const gap = (before: { y: number; height: number }, after: { y: number }) => (
    after.y - (before.y + before.height)
  )

  for (const colorScheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme })
    for (const width of [320, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 900 })
      const entries = Object.entries(locators)
      const bounds = Object.fromEntries(await Promise.all(entries.map(async ([name, locator]) => {
        const value = await locator.boundingBox()
        if (!value) throw new Error('缺少 ' + name + ' 几何')
        return [name, value]
      }))) as Record<keyof typeof locators, { y: number; height: number }>

      expect(gap(bounds.userA, bounds.assistantABody)).toBeCloseTo(20, 5)
      expect(gap(bounds.assistantA, bounds.userB)).toBeCloseTo(20, 5)
      expect(gap(bounds.userB, bounds.subagent)).toBeCloseTo(20, 5)
      expect(gap(bounds.subagent, bounds.tool)).toBeCloseTo(16, 5)
      expect(gap(bounds.tool, bounds.assistantBBody)).toBeCloseTo(16, 5)
      expect(gap(bounds.assistantB, bounds.userC)).toBeCloseTo(20, 5)
      expect(gap(bounds.userC, bounds.error)).toBeCloseTo(24, 5)
    }
  }
})

test('Tool 与回答复制图标严格对齐正文左缘', async ({ page }) => {
  await mockStudio(page)
  const lastAssistant = page.locator('.assistant-message').last()
  const previousBody = page.locator('#browser-message-150 .message-markdown > :first-child')
  const body = lastAssistant.locator('.message-markdown > :first-child')
  const copyIcon = lastAssistant.locator('.message-action-row .ui-icon-button svg')
  const subagent = page.locator('#browser-subagent > summary')
  const tool = page.locator('.tool-card > summary')
  const toolIcon = tool.locator('.tool-row-icon svg')

  await expect(page.locator('.message-action-row')).toHaveCount(1)
  await expect(lastAssistant.locator('.message-action-row')).toHaveCSS('margin-top', '4px')

  for (const colorScheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme })
    for (const width of [320, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 900 })
      const [previousBodyBounds, bodyBounds, copyBounds, subagentBounds, toolBounds, toolIconBounds] = await Promise.all([
        previousBody.boundingBox(),
        body.boundingBox(),
        copyIcon.boundingBox(),
        subagent.boundingBox(),
        tool.boundingBox(),
        toolIcon.boundingBox(),
      ])
      if (!previousBodyBounds || !bodyBounds || !copyBounds || !subagentBounds || !toolBounds || !toolIconBounds) {
        throw new Error('对话几何不可用')
      }

      expect(copyBounds.x).toBeCloseTo(bodyBounds.x, 5)
      expect(toolIconBounds.x).toBeCloseTo(bodyBounds.x, 5)
      expect(copyBounds.y - (bodyBounds.y + bodyBounds.height)).toBeCloseTo(12, 5)
      expect(subagentBounds.y - (previousBodyBounds.y + previousBodyBounds.height)).toBeCloseTo(16, 5)
      expect(toolBounds.y - (subagentBounds.y + subagentBounds.height)).toBeCloseTo(16, 5)
      expect(bodyBounds.y - (toolBounds.y + toolBounds.height)).toBeCloseTo(16, 5)
    }
  }
})

test('对话运行失败提示在中英文界面都不显示末尾句号', async ({ page }) => {
  await mockStudio(page, { runError: true })

  await page.getByRole('textbox', { name: '消息输入' }).fill('触发运行失败')
  await page.getByRole('button', { name: '发送消息' }).click()
  await expect(page.getByText('对话运行失败', { exact: true })).toBeVisible()
  await expect(page.getByText('对话运行失败。', { exact: true })).toHaveCount(0)

  await page.evaluate(() => localStorage.setItem('tinkerfin:language', 'en'))
  await page.reload()
  await page.getByRole('textbox', { name: 'Message input' }).fill('Trigger run failure')
  await page.getByRole('button', { name: 'Send message' }).click()
  await expect(page.getByText('Conversation run failed', { exact: true })).toBeVisible()
  await expect(page.getByText('Conversation run failed.', { exact: true })).toHaveCount(0)
})

test('运行中 SubAgent 标题保持稳定且不影响普通 Tool 扫光', async ({ page }) => {
  await mockStudio(page, { runningActivity: true })
  const subagentHeader = page.locator('.subagent-card.running > summary')
  const toolHeader = page.locator('.tool-card.running > summary')

  await expect(page.locator('.message-action-row')).toHaveCount(1)
  await expect(page.locator('#browser-running-stage .message-action-row')).toHaveCount(0)

  for (const colorScheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme })
    for (const width of [320, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 900 })
      await expect(subagentHeader).toContainText('Task')
      await expect(subagentHeader).toContainText('SubAgent')
      expect(await subagentHeader.evaluate((element) => (
        element.getAnimations({ subtree: true })
          .some((animation) => animation instanceof CSSAnimation
            && animation.animationName === 'conversation-tool-row-sweep')
      ))).toBe(false)
      expect(await toolHeader.evaluate((element) => (
        element.getAnimations({ subtree: true })
          .some((animation) => animation instanceof CSSAnimation
            && animation.animationName === 'conversation-tool-row-sweep')
      ))).toBe(true)
    }
  }
})

test('搜索会话点击后保持标准输入高度且不显示容器描边', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await mockStudio(page)

  for (const colorScheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme })
    for (const width of [320, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 900 })
      await page.reload()
      if (width === 320) await page.getByRole('button', { name: '打开导航' }).click()
      await page.getByRole('button', { name: '搜索会话' }).click()

      const input = page.getByRole('textbox', { name: '搜索会话' })
      const search = page.locator('.sidebar-search')
      await expect(input).toBeFocused()
      const metrics = await search.evaluate((element) => {
        const bounds = element.getBoundingClientRect()
        const headerBounds = element.parentElement!.getBoundingClientRect()
        const styles = getComputedStyle(element)
        return {
          height: bounds.height,
          centerOffset: ((bounds.top + bounds.bottom) - (headerBounds.top + headerBounds.bottom)) / 2,
          borderTopWidth: styles.borderTopWidth,
          outlineStyle: styles.outlineStyle,
          outlineWidth: styles.outlineWidth,
        }
      })

      expect(metrics).toEqual({
        height: 44,
        centerOffset: 0,
        borderTopWidth: '0px',
        outlineStyle: 'none',
        outlineWidth: '0px',
      })
    }
  }
})

test('历史分页一次提交最终滑块比例，不产生中间位移动画', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 600 })
  const historyRequests: Array<{ cursor: string | null; receivedAt: number }> = []
  await mockStudio(page, {
    onHistoryRequest: (request) => historyRequests.push(request),
    paginatedHistory: true,
    paginationResponseDelayMs: 500,
  })
  const history = page.getByRole('region', { name: '最近对话' })
  const slot = history.locator('.history-pagination-slot')
  const scrollbar = page.locator('.conversation-history > .ui-overlay-scrollbar')
  const thumb = scrollbar.locator('.ui-overlay-scrollbar__thumb')
  await expect(slot).toHaveCSS('height', '44px')
  const idleScrollHeight = await history.evaluate((element) => element.scrollHeight)

  const fastScrollStartedAt = Date.now()
  await history.evaluate((element) => {
    element.scrollTop = element.scrollHeight
    element.dispatchEvent(new Event('scroll', { bubbles: true }))
  })
  await expect(page.getByText('正在加载更多历史会话')).toBeVisible()
  await expect.poll(() => historyRequests.length).toBe(2)
  expect(historyRequests[1]!.receivedAt - fastScrollStartedAt).toBeLessThan(100)
  expect(await history.evaluate((element) => element.scrollHeight)).toBe(idleScrollHeight)
  await expect(slot).toHaveCSS('height', '44px')
  const pendingGeometry = await thumb.evaluate(async (element) => {
    await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()))
    const bounds = element.getBoundingClientRect()
    return { height: bounds.height, top: bounds.top }
  })

  await expect(page.getByRole('button', { name: '打开会话：分页验证会话 29' })).toBeVisible()
  const finalGeometry = await thumb.evaluate(async (element) => {
    await new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve())))
    const frames: Array<{ height: number; top: number }> = []
    for (let index = 0; index < 4; index += 1) {
      await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()))
      const bounds = element.getBoundingClientRect()
      frames.push({ height: bounds.height, top: bounds.top })
    }
    return {
      animationCount: element.getAnimations().length,
      frames,
      transitionDuration: getComputedStyle(element).transitionDuration,
    }
  })
  expect(finalGeometry.transitionDuration).toBe('0s')
  expect(finalGeometry.animationCount).toBe(0)
  expect(finalGeometry.frames[0]!.top).toBeLessThan(pendingGeometry.top)
  expect(finalGeometry.frames[0]!.height).toBeLessThan(pendingGeometry.height)
  expect(new Set(
    finalGeometry.frames.map(({ top, height }) => `${top.toFixed(2)}:${height.toFixed(2)}`),
  ).size).toBe(1)
})

test('一次快速滑动最多加载一页历史会话', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 600 })
  const historyRequests: Array<{ cursor: string | null; receivedAt: number }> = []
  await mockStudio(page, {
    onHistoryRequest: (request) => historyRequests.push(request),
    paginatedHistory: true,
    paginationPageCount: 4,
    paginationResponseDelayMs: 50,
  })
  const history = page.getByRole('region', { name: '最近对话' })
  const reachBottom = () => history.evaluate((element) => {
    element.scrollTop = element.scrollHeight
    element.dispatchEvent(new Event('scroll', { bubbles: true }))
  })

  await reachBottom()
  await expect(page.getByRole('button', { name: '打开会话：分页验证会话 29' })).toBeVisible()
  await reachBottom()
  for (let index = 0; index < 4; index += 1) {
    await page.waitForTimeout(50)
    await history.evaluate((element) => {
      element.dispatchEvent(new WheelEvent('wheel', { bubbles: true, deltaY: 600 }))
    })
  }
  await history.evaluate((element) => element.dispatchEvent(new Event('scroll', { bubbles: true })))
  expect(historyRequests).toHaveLength(2)

  await page.waitForTimeout(121)
  await reachBottom()
  await expect.poll(() => historyRequests.length).toBe(3)
  await expect(page.getByRole('button', { name: '打开会话：分页验证会话 39' })).toBeVisible()
})

test('全局滚动条保持统一参数、分层显隐和直接拖拽映射', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 600 })
  await mockStudio(page, { paginatedHistory: true })
  const pane = page.getByRole('region', { name: '对话内容' })
  const history = page.getByRole('region', { name: '最近对话' })
  const mainScrollbar = page.locator('.conversation-region > .ui-overlay-scrollbar')
  const historyScrollbar = page.locator('.conversation-history > .ui-overlay-scrollbar')
  const mainThumb = mainScrollbar.locator('.ui-overlay-scrollbar__thumb')

  await expect(mainScrollbar).toHaveAttribute('data-scrollable', 'true')
  await expect(mainScrollbar).toHaveAttribute('data-visibility', 'persistent')
  await expect(mainScrollbar).toHaveClass(/is-visible/)
  await expect(mainScrollbar).toHaveCSS('width', '8px')
  await expect(historyScrollbar).toHaveAttribute('data-visibility', 'transient')
  await expect(historyScrollbar).not.toHaveClass(/is-visible/)
  const idleVisual = await mainThumb.evaluate((element) => {
    const styles = getComputedStyle(element, '::after')
    return { width: styles.width, color: styles.backgroundColor, radius: styles.borderRadius }
  })
  expect(idleVisual).toEqual({ width: '8px', color: 'rgb(229, 229, 229)', radius: '9999px' })

  await mainThumb.hover()
  await page.waitForTimeout(120)
  const hoveredVisual = await mainThumb.evaluate((element) => {
    const styles = getComputedStyle(element, '::after')
    return { width: styles.width, color: styles.backgroundColor }
  })
  expect(hoveredVisual).toEqual({ width: '10px', color: 'rgb(212, 212, 212)' })

  await history.hover()
  await expect(historyScrollbar).toHaveClass(/is-visible/)
  await page.mouse.move(900, 250)
  await page.waitForTimeout(1_100)
  await expect(historyScrollbar).not.toHaveClass(/is-visible/)

  await pane.evaluate((element) => {
    element.scrollTop = Math.min(200, element.scrollHeight - element.clientHeight)
    element.dispatchEvent(new Event('scroll', { bubbles: true }))
  })
  const beforeDrag = await pane.evaluate((element) => element.scrollTop)
  const thumbBox = await mainThumb.boundingBox()
  if (!thumbBox) throw new Error('主对话滚动滑块不可见')
  await page.mouse.move(thumbBox.x + (thumbBox.width / 2), thumbBox.y + (thumbBox.height / 2))
  await page.mouse.down()
  await page.mouse.move(thumbBox.x + (thumbBox.width / 2), thumbBox.y + (thumbBox.height / 2) + 40, { steps: 4 })
  await page.mouse.up()
  expect(await pane.evaluate((element) => element.scrollTop)).toBeGreaterThan(beforeDrag)
})

test('账户菜单、modal 隔离和定时滚动控件保持完整键盘路径', async ({ page }) => {
  await mockStudio(page)
  const account = page.getByRole('button', { name: '打开用户菜单' })
  await account.click()
  await expect(page.getByRole('menuitem', { name: '设置' })).toBeFocused()
  await page.keyboard.press('ArrowDown')
  await expect(page.getByRole('menuitem', { name: '退出登录' })).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(account).toBeFocused()

  await account.click()
  await page.getByRole('menuitem', { name: '设置' }).click()
  await expect(page.getByRole('dialog', { name: '设置' })).toBeVisible()
  await page.keyboard.press('Meta+K')
  await expect(page.getByRole('dialog', { name: '设置' })).toBeVisible()
  await expect(page.locator('.app-shell')).toHaveAttribute('inert', '')
  await page.getByRole('button', { name: '关闭对话框' }).click()

  const pane = page.getByRole('region', { name: '对话内容' })
  await pane.evaluate((element) => {
    element.scrollTop = 0
    element.dispatchEvent(new WheelEvent('wheel', { deltaY: -120, bubbles: true }))
    element.dispatchEvent(new Event('scroll', { bubbles: true }))
  })
  const scrollButton = page.getByRole('button', { name: '回到底部' })
  await expect(scrollButton).toBeVisible()
  await scrollButton.focus()
  await page.waitForTimeout(2_000)
  await expect(scrollButton).toBeVisible()
})

test('浅深主题的 Tool caption 对比度均达标，forced-colors 保留焦点', async ({ page }) => {
  await mockStudio(page)
  await page.getByText('Read', { exact: true }).click()
  expect(Math.min(...await contrastRatios(page, '.tool-field-label'))).toBeGreaterThanOrEqual(4.5)

  await page.getByRole('button', { name: '打开用户菜单' }).click()
  await page.getByRole('menuitem', { name: '设置' }).click()
  await page.getByRole('button', { name: '通用' }).click()
  await page.locator('.settings-theme-option').filter({ hasText: '深色' }).click()
  await page.getByRole('button', { name: '关闭对话框' }).click()
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
  expect(Math.min(...await contrastRatios(page, '.tool-field-label'))).toBeGreaterThanOrEqual(4.5)

  await page.emulateMedia({ forcedColors: 'active' })
  const attachment = page.getByRole('button', { name: '添加本地附件' })
  await page.getByRole('textbox', { name: '消息输入' }).focus()
  await page.keyboard.press('Tab')
  await expect(attachment).toBeFocused()
  const outline = await attachment.evaluate((element) => getComputedStyle(element).outlineStyle)
  expect(outline).not.toBe('none')
  await expect(page.locator('.ui-overlay-scrollbar').first()).toHaveCSS('display', 'none')
  const nativeScrollbar = await page.getByRole('region', { name: '对话内容' }).evaluate((element) => (
    getComputedStyle(element, '::-webkit-scrollbar').display
  ))
  expect(nativeScrollbar).toBe('block')
})

test('reduced-motion 跳过 Flip 布局动画', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await mockStudio(page)
  await page.getByRole('button', { name: '收起侧边栏' }).click()
  await expect(page.locator('.app-shell')).toHaveAttribute('data-sidebar-mode', 'rail')
  await expect(page.locator('.is-layout-flipping')).toHaveCount(0)
  const scrollbar = page.locator('.ui-overlay-scrollbar').first()
  await expect(scrollbar).toHaveCSS('transition-duration', '0s')
  await expect(scrollbar.locator('.ui-overlay-scrollbar__thumb')).toHaveCSS('transition-duration', '0s')
})

test('布局动效不逐帧触发布局且冷缓存只请求允许的西文字体', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  const fontResponses = new Map<string, Promise<Buffer>>()
  page.on('response', (response) => {
    if (new URL(response.url()).pathname.endsWith('.woff2')) {
      fontResponses.set(response.url(), response.body())
    }
  })
  await mockStudio(page)
  await page.evaluate(() => document.fonts.ready)

  const westernFonts = [...fontResponses.entries()].filter(([url]) => (
    url.includes('inter-') || url.includes('jetbrains-mono-')
  ))
  expect(westernFonts.length).toBeGreaterThan(0)
  expect(westernFonts.every(([url]) => (
    url.includes('-latin-wght-')
    && !/latin-ext|cyrillic|greek|vietnamese/.test(url)
  ))).toBe(true)
  const westernFontBytes = (await Promise.all(
    westernFonts.map(([, body]) => body.then((content) => content.byteLength)),
  )).reduce((total, size) => total + size, 0)
  expect(westernFontBytes).toBeLessThanOrEqual(140_492)

  const session = await page.context().newCDPSession(page)
  await session.send('Performance.enable')
  const traceComplete = new Promise<{ stream: string }>((resolve) => {
    session.once('Tracing.tracingComplete', resolve)
  })
  await session.send('Tracing.start', {
    categories: 'devtools.timeline',
    transferMode: 'ReturnAsStream',
  })
  const layoutCount = async () => {
    const metrics = await session.send('Performance.getMetrics')
    return metrics.metrics.find((metric) => metric.name === 'LayoutCount')?.value ?? 0
  }
  const before = await layoutCount()
  await page.getByRole('button', { name: '收起侧边栏' }).click()
  await page.waitForTimeout(400)
  await page.getByRole('button', { name: '打开任务抽屉' }).click()
  await page.waitForTimeout(400)
  const layoutDelta = (await layoutCount()) - before
  await session.send('Tracing.end')
  const { stream } = await traceComplete
  let traceJson = ''
  let traceEof = false
  while (!traceEof) {
    const chunk = await session.send('IO.read', { handle: stream })
    traceJson += chunk.data
    traceEof = chunk.eof
  }
  await session.send('IO.close', { handle: stream })
  const trace = JSON.parse(traceJson) as {
    traceEvents: Array<{ name: string; ph: string }>
  }
  const tracedLayouts = trace.traceEvents.filter((event) => (
    event.name === 'Layout' && event.ph === 'X'
  ))

  expect(layoutDelta).toBeLessThanOrEqual(12)
  expect(tracedLayouts.length).toBeLessThanOrEqual(12)
})

test.describe('touch/coarse pointer', () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true })

  test('Plan、模型和命令选项的目标尺寸至少为 44px', async ({ page }) => {
    await mockStudio(page)
    expect(await page.evaluate(() => matchMedia('(any-pointer: coarse)').matches)).toBe(true)
    await expect(page.locator('.ui-overlay-scrollbar__thumb').first()).toHaveCSS('pointer-events', 'none')

    const input = page.getByRole('textbox', { name: '消息输入' })
    await input.fill('/plan')
    await input.press('Enter')
    await input.press('Enter')
    await expect(page.getByRole('button', { name: 'Plan 已开启，点击关闭' })).toBeVisible()

    await page.getByRole('button', { name: '选择模型' }).click()
    const modelHeights = await page.getByRole('option').evaluateAll((options) => (
      options.map((option) => option.getBoundingClientRect().height)
    ))
    expect(Math.min(...modelHeights)).toBeGreaterThanOrEqual(44)
    await page.keyboard.press('Escape')

    await input.fill('/')
    const commandHeights = await page.locator('.composer-suggestion-item').evaluateAll((items) => (
      items.map((item) => item.getBoundingClientRect().height)
    ))
    expect(commandHeights.length).toBeGreaterThan(0)
    expect(Math.min(...commandHeights)).toBeGreaterThanOrEqual(44)
  })
})
