import { expect, test, type Page, type Route } from '@playwright/test'
import { resolve } from 'node:path'

import type { ConversationHistoryDetail } from '../../src/api/conversation/history'
import type {
  TraceGraphNode,
  TraceGraphPage,
} from '../../src/api/conversation/traceGraph'
import { emptyTraceGraph } from '../../src/test/traceFixtures'

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

const graphNode = (
  id: string,
  kind: TraceGraphNode['kind'],
  parentId: string | null,
  startedSeq: number,
  values: Partial<TraceGraphNode> = {},
): TraceGraphNode => ({
  id,
  turnId: id.startsWith('old-') ? 'turn-browser-1' : 'turn-browser-2',
  parentId,
  structuralParentId: parentId,
  kind,
  status: 'succeeded',
  name: kind === 'human_message'
    ? 'HumanMessage'
    : kind === 'assistant_message' ? 'AssistantMessage' : id,
  runId: id.startsWith('old-') ? 'run-browser-1' : RUN_ID,
  namespace: [],
  startedAt: '2026-09-01T00:01:00.000Z',
  completedAt: '2026-09-01T00:01:01.000Z',
  startedSeq,
  updatedSeq: startedSeq,
  contentOmitted: false,
  requestOmitted: false,
  resultOmitted: false,
  hooks: [],
  linkIssues: [],
  ...values,
})

const semanticNodes = [
  graphNode('old-human', 'human_message', null, 1, { content: '第一轮浏览器任务' }),
  graphNode('old-agent', 'agent', 'old-human', 2, { name: 'Historical Agent' }),
  graphNode('human-current', 'human_message', null, 10, { content: '继续核验真实调用树' }),
  graphNode('agent-current', 'agent', 'human-current', 11, { name: 'Agent' }),
  graphNode('model-current', 'model', 'agent-current', 13, {
    name: 'deepseek-v4-pro',
    provider: 'deepseek',
    model: 'deepseek-v4-pro',
    firstOutputAt: '2026-09-01T00:01:00.500Z',
    request: {
      messages: [{ messageType: 'system', content: '# 浏览器系统提示词' }],
    },
    usage: {
      input_token_details: { cache_read: 4736 },
      input_tokens: 4872,
      output_token_details: { reasoning: 123 },
      output_tokens: 185,
      total_tokens: 5057,
    },
    responseMetadata: { finish_reason: 'tool_calls' },
  }),
  graphNode('assistant-current', 'assistant_message', 'model-current', 17, {
    content: '链路完成',
  }),
  graphNode('tool-current', 'tool', 'model-current', 19, {
    name: 'web_search',
    status: 'failed',
    request: { query: '真实链路' },
    failure: {
      errorType: 'builtins.TimeoutError',
      message: '搜索服务在期限内未响应',
    },
  }),
]

const technicalNodes = [
  ...semanticNodes.slice(0, 4),
  graphNode('middleware-visible', 'middleware', 'agent-current', 12, {
    name: 'PromptCacheMiddleware.awrap_model_call',
    className: 'deepagents.middleware.PromptCacheMiddleware',
    hooks: ['awrap_model_call'],
  }),
  semanticNodes[4],
  graphNode('system-current', 'system_message', 'model-current', 14, {
    content: '# 浏览器系统提示词',
  }),
  ...semanticNodes.slice(5),
]

const tracePage = (nodes: TraceGraphNode[]): TraceGraphPage => ({
  turns: [
    {
      id: 'turn-browser-1',
      ordinal: 1,
      rootNodeId: 'old-human',
      startedAt: BASE_TIME,
    },
    {
      id: 'turn-browser-2',
      ordinal: 2,
      rootNodeId: 'human-current',
      startedAt: '2026-09-01T00:01:00.000Z',
    },
  ],
  nodes,
  orderedNodeIds: nodes.map((node) => node.id),
  rootNodeIds: ['old-human', 'human-current'],
  matchedNodeIds: nodes.map((node) => node.id),
  nextCursor: null,
  asOfSeq: 40,
  facets: {
    kinds: {
      human_message: 2,
      assistant_message: 1,
      agent: 2,
      middleware: 1,
      model: 1,
      system_message: 1,
      tool: 1,
    },
    statuses: { succeeded: nodes.length - 1, failed: 1 },
    agents: {},
    middleware: { PromptCacheMiddleware: 1 },
    skills: {},
    providers: { deepseek: 1 },
    models: { 'deepseek-v4-pro': 1 },
  },
  completeness: {
    callTrackingMissing: false,
    relationshipEvidenceMissing: false,
    detailsOmitted: false,
  },
})

const detail = (includeTaskTrace: boolean): ConversationHistoryDetail => ({
  id: 1,
  threadId: THREAD_ID,
  title: 'Turn 链路浏览器会话',
  lastModel: 'deepseek-v4-pro',
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
  graph: emptyTraceGraph(22),
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

async function mockChainTraceStudio(
  page: Page,
  theme: 'light' | 'dark' = 'light',
  semanticSnapshot: TraceGraphPage = tracePage(semanticNodes),
  directSnapshot?: TraceGraphPage,
  language: 'zh-CN' | 'en' = 'zh-CN',
  technicalSnapshot: TraceGraphPage = tracePage(technicalNodes),
) {
  const pageErrors: string[] = []
  page.on('pageerror', (error) => pageErrors.push(error.message))
  page.on('console', (message) => {
    if (message.type() === 'error') pageErrors.push(message.text())
  })
  await page.addInitScript(({
    session,
    threadId,
    semanticSnapshot,
    technicalSnapshot,
    selectedTheme,
    selectedLanguage,
  }) => {
    window.localStorage.setItem('tinkerfin.auth.session', JSON.stringify(session))
    window.localStorage.setItem('tinkerfin:theme', selectedTheme)
    window.localStorage.setItem('tinkerfin:language', selectedLanguage)
    const traceWindow = window as typeof window & { __traceFollowUrls: string[] }
    traceWindow.__traceFollowUrls = []
    const originalFetch = window.fetch.bind(window)
    window.fetch = (input, init) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
      if (url.includes(`/api/conversation/${threadId}/trace/graph/follow`)) {
        traceWindow.__traceFollowUrls.push(url)
        const configuredSnapshot = url.includes('includeTechnicalNodes=true')
          ? technicalSnapshot
          : semanticSnapshot
        const requestedKinds = new Set(new URL(url, window.location.origin).searchParams.getAll('kind'))
        const configuredMatches = new Set(configuredSnapshot.matchedNodeIds)
        const snapshot = {
          ...configuredSnapshot,
          matchedNodeIds: configuredSnapshot.orderedNodeIds.filter((nodeId) => {
            const node = configuredSnapshot.nodes.find((item) => item.id === nodeId)
            return Boolean(node && configuredMatches.has(nodeId) && requestedKinds.has(node.kind))
          }),
        }
        const encoder = new TextEncoder()
        const stream = new ReadableStream({
          start(controller) {
            let closed = false
            controller.enqueue(encoder.encode(
              `event: trace\ndata: ${JSON.stringify({ type: 'snapshot', snapshot })}\n\n`,
            ))
            init?.signal?.addEventListener('abort', () => {
              if (closed) return
              closed = true
              controller.close()
            }, { once: true })
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
    semanticSnapshot,
    technicalSnapshot,
    selectedTheme: theme,
    selectedLanguage: language,
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
    if (url.pathname === `/api/conversation/${THREAD_ID}/trace/graph` && directSnapshot) {
      await fulfillJson(route, directSnapshot)
      return
    }
    await route.fulfill({ status: 404, contentType: 'application/json', body: '{}' })
  })
  await page.goto(`/?thread=${THREAD_ID}`)
  const conversationLabel = language === 'en' ? 'Conversation' : '对话'
  const traceLabel = language === 'en' ? 'Trace' : '链路'
  await expect(page.getByRole('tab', { name: conversationLabel })).toHaveAttribute(
    'aria-selected',
    'true',
  )
  await expect(page.getByRole('tab', { name: traceLabel })).toBeVisible()
  await expect(page.getByRole('button', { name: traceLabel, exact: true })).toHaveCount(0)
  return pageErrors
}

const followUrls = (page: Page) => page.evaluate(() => (
  (window as typeof window & { __traceFollowUrls: string[] }).__traceFollowUrls
))

async function verifyTraceViewports(
  page: Page,
  theme: 'light' | 'dark',
) {
  for (const width of [320, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 900 })
    await expect(page.getByRole('tabpanel', { name: '链路' })).toBeVisible()
    await expect(page.locator('.is-layout-flipping')).toHaveCount(0)
    if (width < 768) {
      await expect(page.locator('.workspace-sidebar')).toHaveCSS('visibility', 'hidden')
    } else if (width < 1024) {
      await expect(page.locator('.app-shell')).toHaveAttribute('data-sidebar-mode', 'rail')
      await expect(page.locator('.sidebar-wide')).toHaveCSS('opacity', '0')
      await expect(page.locator('.sidebar-rail')).toHaveCSS('visibility', 'visible')
    } else {
      await expect(page.locator('.app-shell')).toHaveAttribute('data-sidebar-mode', 'expanded')
      await expect(page.locator('.sidebar-wide')).toHaveCSS('opacity', '1')
    }
    const detailsBox = await page.getByRole('complementary', { name: '链路详情' })
      .boundingBox()
    expect(Math.round(detailsBox?.width ?? 0)).toBe(Math.min(width, 400))
    expect(Math.round(detailsBox?.x ?? -1)).toBe(Math.max(0, width - 400))
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
    expect(overflow).toBeLessThanOrEqual(0)
    if (process.env.TINKERFIN_VISUAL_QA_DIR) {
      await page.screenshot({
        path: resolve(process.env.TINKERFIN_VISUAL_QA_DIR, `chain-trace-${theme}-${width}.png`),
        fullPage: true,
      })
    }
  }
}

test('Header、时间线、Turn 台账、树形和详情形成同一条可交互链路', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  const pageErrors = await mockChainTraceStudio(page)
  const traceTab = page.getByRole('tab', { name: '链路' })
  const sidebarToggle = page.getByRole('button', { name: '收起侧边栏' })
  const [traceTabBox, sidebarToggleBox] = await Promise.all([
    traceTab.boundingBox(),
    sidebarToggle.boundingBox(),
  ])
  expect(traceTabBox).not.toBeNull()
  expect(sidebarToggleBox).not.toBeNull()
  expect(Math.abs(
    (traceTabBox?.y ?? 0) + (traceTabBox?.height ?? 0) / 2
      - ((sidebarToggleBox?.y ?? 0) + (sidebarToggleBox?.height ?? 0) / 2),
  )).toBeLessThanOrEqual(1)
  expect((traceTabBox?.y ?? 0) + (traceTabBox?.height ?? 0) / 2).toBeCloseTo(32, 5)

  await traceTab.click()
  const tracePanel = page.getByRole('tabpanel', { name: '链路' })
  await expect(tracePanel).toBeVisible()
  await expect(page.getByRole('tab', { name: '时间线' })).toHaveAttribute('aria-selected', 'true')
  await expect(page.getByRole('region', { name: '调用时间线' })).toBeVisible()
  await expect(page.getByLabel('链路节点', { exact: true })).toBeVisible()
  const initialDetails = page.getByRole('complementary', { name: '链路详情' })
  await expect(initialDetails.getByText('搜索服务在期限内未响应')).toBeVisible()
  await expect(initialDetails).toBeVisible()
  await expect(traceTab).toBeFocused()
  await expect.poll(async () => (await followUrls(page)).length).toBe(1)
  expect(new URL(
    (await followUrls(page))[0]!,
    'http://127.0.0.1',
  ).searchParams.get('limit')).toBe('1000')

  const headerLayout = await page.locator('.workspace-main').evaluate((workspace) => {
    const header = workspace.querySelector<HTMLElement>('.chat-header')!
    const toolbar = workspace.querySelector<HTMLElement>('.chain-trace-toolbar')!
    const timeline = workspace.querySelector<HTMLElement>('.chain-trace-timeline')!
    const timelineLabels = workspace.querySelector<HTMLElement>('.chain-trace-timeline-labels')!
    const ledgerHeader = workspace.querySelector<HTMLElement>('.chain-trace-ledger-header')!
    const turnHeading = workspace.querySelector<HTMLElement>('.chain-trace-turn-heading')!
    const ledgerRow = workspace.querySelector<HTMLElement>('.chain-trace-ledger-row')!
    const ledgerRail = workspace.querySelector<HTMLElement>('.chain-trace-ledger-rail')!
    const ledgerDot = ledgerRail.querySelector<HTMLElement>('i')!
    const typePill = ledgerRow.querySelector<HTMLElement>('.chain-trace-type-pill')!
    const duration = ledgerRow.querySelector<HTMLElement>('.chain-trace-duration')!
    const ledgerMain = ledgerRow.querySelector<HTMLElement>('.chain-trace-ledger-main')!
    const details = workspace.querySelector<HTMLElement>('.chain-trace-details')!
    const detailHeader = workspace.querySelector<HTMLElement>('.chain-trace-details-header')!
    const detailTabs = workspace.querySelector<HTMLElement>('.chain-trace-detail-tabs')!
    const firstHeaderTab = workspace.querySelector<HTMLElement>('.workspace-view-tabs > button')!
    const rangeSummary = workspace.querySelector<HTMLElement>('.chain-trace-range-summary')!
    const headerStyle = getComputedStyle(header)
    const toolbarStyle = getComputedStyle(toolbar)
    const ledgerHeaderStyle = getComputedStyle(ledgerHeader)
    const ledgerRowStyle = getComputedStyle(ledgerRow)
    const detailsStyle = getComputedStyle(details)
    const rowRect = ledgerRow.getBoundingClientRect()
    const headerCells = Array.from(ledgerHeader.children).map((element) => (
      element.getBoundingClientRect()
    ))
    const railCenter = ledgerRail.getBoundingClientRect().left
      + Number.parseFloat(getComputedStyle(ledgerRail, '::before').left)
    return {
      headerHeight: header.getBoundingClientRect().height,
      headerTabLeft: firstHeaderTab.getBoundingClientRect().left,
      toolbarContentLeft: rangeSummary.getBoundingClientRect().left,
      toolbarHeight: toolbar.getBoundingClientRect().height,
      toolbarPaddingLeft: toolbarStyle.paddingLeft,
      toolbarPaddingRight: toolbarStyle.paddingRight,
      timelineHeight: timeline.getBoundingClientRect().height,
      timelineLabelsWidth: timelineLabels.getBoundingClientRect().width,
      ledgerHeaderHeight: ledgerHeader.getBoundingClientRect().height,
      ledgerColumns: ledgerHeaderStyle.gridTemplateColumns,
      ledgerPaddingLeft: ledgerHeaderStyle.paddingLeft,
      ledgerPaddingRight: ledgerHeaderStyle.paddingRight,
      turnHeight: turnHeading.getBoundingClientRect().height,
      rowHeight: ledgerRow.getBoundingClientRect().height,
      railLeft: getComputedStyle(ledgerRail, '::before').left,
      rootInset: ledgerRail.getBoundingClientRect().left - rowRect.left,
      durationInset: rowRect.right - duration.getBoundingClientRect().right,
      nodeColumnDelta: headerCells[0]!.left - railCenter,
      typeColumnDelta: headerCells[1]!.left - typePill.getBoundingClientRect().left,
      contentColumnDelta: headerCells[2]!.left - ledgerMain.getBoundingClientRect().left,
      durationColumnDelta: headerCells[3]!.right - duration.getBoundingClientRect().right,
      railBranchWidth: getComputedStyle(ledgerRail, '::after').width,
      railBranchHeight: getComputedStyle(ledgerRail, '::after').height,
      railBranchRadius: getComputedStyle(ledgerRail, '::after').borderBottomLeftRadius,
      railDotSize: ledgerDot.getBoundingClientRect().width,
      typePillWidth: typePill.getBoundingClientRect().width,
      typePillHeight: typePill.getBoundingClientRect().height,
      typePillFontSize: getComputedStyle(typePill).fontSize,
      typePillFontWeight: getComputedStyle(typePill).fontWeight,
      ledgerHasTypeIcon: Boolean(ledgerRow.querySelector('.chain-trace-entry-icon')),
      borderTop: headerStyle.borderTopWidth,
      borderBottom: headerStyle.borderBottomWidth,
      detailsPosition: detailsStyle.position,
      detailsWidth: details.getBoundingClientRect().width,
      detailHeaderHeight: detailHeader.getBoundingClientRect().height,
      detailTabsHeight: detailTabs.getBoundingClientRect().height,
      rowPaddingLeft: ledgerRowStyle.paddingLeft,
      rowPaddingRight: ledgerRowStyle.paddingRight,
    }
  })
  expect(headerLayout).toMatchObject({
    headerHeight: 64,
    toolbarHeight: 40,
    timelineLabelsWidth: 104,
    ledgerHeaderHeight: 34,
    toolbarPaddingLeft: '28px',
    toolbarPaddingRight: '28px',
    ledgerColumns: '36px 68px 545px 74px',
    ledgerPaddingLeft: '28px',
    ledgerPaddingRight: '28px',
    turnHeight: 28,
    rowHeight: 44,
    railLeft: '0px',
    rootInset: 28,
    durationInset: 28,
    railBranchWidth: '22px',
    railBranchHeight: '10px',
    railBranchRadius: '10px',
    railDotSize: 9,
    typePillWidth: 42,
    typePillHeight: 22,
    typePillFontSize: '11px',
    typePillFontWeight: '600',
    ledgerHasTypeIcon: false,
    borderTop: '0px',
    borderBottom: '0px',
    detailsPosition: 'relative',
    detailsWidth: 400,
    detailHeaderHeight: 64,
    detailTabsHeight: 44,
    rowPaddingLeft: '28px',
    rowPaddingRight: '28px',
  })
  expect(headerLayout.timelineHeight).toBeGreaterThanOrEqual(90)
  expect(Math.abs(headerLayout.headerTabLeft - headerLayout.toolbarContentLeft)).toBeLessThanOrEqual(1)
  expect(Math.abs(headerLayout.nodeColumnDelta)).toBeLessThanOrEqual(1)
  expect(Math.abs(headerLayout.typeColumnDelta)).toBeLessThanOrEqual(1)
  expect(Math.abs(headerLayout.contentColumnDelta)).toBeLessThanOrEqual(1)
  expect(Math.abs(headerLayout.durationColumnDelta)).toBeLessThanOrEqual(1)
  expect(await page.locator('.chain-trace-lane-label').allTextContents()).toEqual([
    '用户',
    '助手',
    '工具',
  ])
  await expect(page.locator('[data-trace-node-id="agent-current"]')).toHaveCount(0)
  await expect(page.locator('[data-trace-node-id="model-current"]')).toHaveCount(0)
  const categoryColors = await page.locator('.chain-trace').evaluate((trace) => {
    const color = (selector: string) => getComputedStyle(
      trace.querySelector<HTMLElement>(selector)!,
    ).color
    return {
      user: [color('.chain-trace-lane-label.is-user'), color('[data-trace-node-id="human-current"] .chain-trace-type-pill')],
      assistant: [color('.chain-trace-lane-label.is-assistant'), color('[data-trace-node-id="assistant-current"] .chain-trace-type-pill')],
      tool: [color('.chain-trace-lane-label.is-tool'), color('[data-trace-node-id="tool-current"] .chain-trace-type-pill')],
    }
  })
  Object.values(categoryColors).forEach((colors) => expect(new Set(colors).size).toBe(1))
  if (process.env.TINKERFIN_VISUAL_QA_DIR) {
    await page.screenshot({
      path: resolve(process.env.TINKERFIN_VISUAL_QA_DIR, 'prototype-aligned-trace-light-1440.png'),
      fullPage: true,
    })
  }
  await expect(page.locator('.chain-trace-detail-resizer')).toHaveCount(0)
  await expect(page.getByRole('button', { name: '状态：全部状态' })).toHaveCount(0)
  const collapsedSearchGeometry = await page.locator('.chain-trace-filters').evaluate((filters) => {
    const control = filters.querySelector<HTMLElement>('.chain-trace-search-control')!
    const trigger = filters.querySelector<HTMLElement>('.chain-trace-search-trigger')!
    const controlRect = control.getBoundingClientRect()
    const triggerRect = trigger.getBoundingClientRect()
    return {
      overflow: filters.scrollWidth - filters.clientWidth,
      centerDelta: triggerRect.top + triggerRect.height / 2
        - (controlRect.top + controlRect.height / 2),
    }
  })
  expect(collapsedSearchGeometry.overflow).toBeLessThanOrEqual(0)
  expect(Math.abs(collapsedSearchGeometry.centerDelta)).toBeLessThanOrEqual(1)
  await page.getByRole('button', { name: '搜索链路节点' }).click()
  await expect(page.getByRole('searchbox', { name: '搜索链路节点' }))
    .toHaveAttribute('placeholder', '搜索节点、内容')
  await expect(page.getByRole('searchbox', { name: '搜索链路节点' }))
    .toHaveAttribute('type', 'text')
  await expect(page.getByRole('button', { name: '清除链路搜索' })).toHaveCount(1)
  const expandedSearchOverflow = await page.locator('.chain-trace-filters').evaluate(
    (filters) => filters.scrollWidth - filters.clientWidth,
  )
  expect(expandedSearchOverflow).toBeLessThanOrEqual(0)
  await expect(page.getByRole('button', { name: /全部类型/ })).toHaveCount(0)
  const categoryFilters = page.getByRole('group', { name: '节点类型' })
  for (const label of ['用户', '助手', '工具', '上下文']) {
    await expect(categoryFilters.getByRole('button', { name: label, exact: true }))
      .toHaveAttribute('aria-pressed', 'true')
  }
  await expect(tracePanel.getByRole('button', {
    name: /HumanMessage|AssistantMessage|ToolMessage/,
  })).toHaveCount(0)

  await initialDetails.getByRole('button', { name: '关闭链路详情' }).click()
  await expect(initialDetails).toBeHidden()

  await page.getByRole('button', {
    name: '助手，链路完成，查看详情',
  }).click()
  await expect(initialDetails.getByRole('heading', {
    name: '助手 第 2 轮 · 步骤 2',
  })).toBeVisible()
  await expect(page.locator('.chain-trace-ledger-row .chain-trace-entry-icon')).toHaveCount(0)
  await expect(page.getByText('AssistantMessage', { exact: true })).toHaveCount(0)
  await expect(initialDetails.locator('.markdown-content--compact')).toHaveCSS('font-size', '13px')
  await expect(initialDetails.locator('.markdown-content--compact')).toHaveCSS('line-height', '20px')
  if (process.env.TINKERFIN_VISUAL_QA_DIR) {
    await page.screenshot({
      path: resolve(
        process.env.TINKERFIN_VISUAL_QA_DIR,
        'prototype-aligned-assistant-detail-light-1440.png',
      ),
      fullPage: true,
    })
  }

  const technical = page.getByRole('button', { name: '技术' })
  await technical.click()
  await expect.poll(async () => (await followUrls(page)).length).toBe(2)
  await expect(page.getByRole('button', {
    name: '技术，PromptCacheMiddleware.awrap_model_call，查看详情',
  })).toBeVisible()
  await expect(page.locator('[data-trace-node-id="system-current"] .chain-trace-type-pill'))
    .toHaveText('技术')
  const technicalColors = await page.locator('.chain-trace').evaluate((trace) => [
    getComputedStyle(trace.querySelector<HTMLElement>('.chain-trace-lane-label.is-technical')!).color,
    getComputedStyle(trace.querySelector<HTMLElement>(
      '[data-trace-node-id="system-current"] .chain-trace-type-pill',
    )!).color,
    getComputedStyle(trace.querySelector<HTMLElement>(
      '[data-trace-node-id="model-current"] .chain-trace-type-pill',
    )!).color,
  ])
  expect(new Set(technicalColors).size).toBe(1)
  const requestsBeforeViewChange = (await followUrls(page)).length
  await page.getByRole('tab', { name: '树形' }).click()
  await expect(page.getByRole('tab', { name: '树形' })).toHaveAttribute('aria-selected', 'true')
  await expect(page.getByLabel('链路树')).toBeVisible()
  expect((await followUrls(page)).length).toBe(requestsBeforeViewChange)
  const treeGeometry = await page.locator('.chain-trace-tree-scroll').evaluate((tree) => {
    const treeRect = tree.getBoundingClientRect()
    const root = tree.querySelector<HTMLElement>('[data-trace-node-id="human-current"]')!
    const icon = root.querySelector<HTMLElement>('.chain-trace-entry-icon')!
    const duration = root.querySelector<HTMLElement>('.chain-trace-duration')!
    const toggleLefts = Array.from(tree.querySelectorAll<HTMLElement>('.chain-trace-node-toggle'))
      .map((toggle) => Math.round(toggle.getBoundingClientRect().left - treeRect.left))
    return {
      rootInset: Math.round(icon.getBoundingClientRect().left - treeRect.left),
      durationInset: Math.round(treeRect.right - duration.getBoundingClientRect().right),
      toggleLefts,
    }
  })
  expect(treeGeometry.rootInset).toBe(28)
  expect(treeGeometry.durationInset).toBe(28)
  expect(new Set(treeGeometry.toggleLefts)).toEqual(new Set([0]))
  const crossViewAssistant = page.getByRole('button', {
    name: '助手，链路完成，查看详情',
  })
  await initialDetails.getByRole('button', { name: '关闭链路详情' }).click()
  await expect(crossViewAssistant).toBeFocused()
  await crossViewAssistant.click()

  await page.getByRole('button', { name: '收起 技术，deepseek-v4-pro' }).click()
  await expect(page.getByRole('button', {
    name: '助手，链路完成，查看详情',
  })).toHaveCount(0)
  await page.getByRole('button', { name: '展开 技术，deepseek-v4-pro' }).click()
  const assistant = page.getByRole('button', {
    name: '助手，链路完成，查看详情',
  })
  await assistant.click()
  const assistantNode = page.locator('[data-trace-node-id="assistant-current"]').locator('xpath=../..')
  const modelNode = page.locator('[data-trace-node-id="model-current"]').locator('xpath=../..')
  const siblingToolNode = page.locator('[data-trace-node-id="tool-current"]').locator('xpath=../..')
  await expect(assistantNode).toHaveClass(/is-on-selected-path/)
  await expect(modelNode).toHaveClass(/is-on-selected-path/)
  await expect(siblingToolNode).not.toHaveClass(/is-on-selected-path/)
  const selectionStyle = await assistantNode.locator('.chain-trace-node-row').evaluate((row) => ({
    background: getComputedStyle(row, '::before').backgroundColor,
    stripeContent: getComputedStyle(row, '::after').content,
  }))
  expect(selectionStyle.background).not.toBe('rgba(0, 0, 0, 0)')
  expect(selectionStyle.stripeContent).toBe('none')
  const connectorLayer = await assistant.evaluate((button) => {
    const icon = button.querySelector<HTMLElement>('.chain-trace-entry-icon')!
    const treeNode = button.closest('.chain-trace-node')
    const connector = treeNode?.querySelector<HTMLElement>('.chain-trace-connector-elbow')
    if (!connector) throw new Error('Selected node connector is unavailable')
    const iconRect = icon.getBoundingClientRect()
    const connectorRect = connector.getBoundingClientRect()
    return {
      iconLayer: Number(getComputedStyle(icon).zIndex),
      connectorLayer: Number(getComputedStyle(connector).zIndex),
      connectorRight: connectorRect.right,
      iconLeft: iconRect.left,
      iconRight: iconRect.right,
    }
  })
  expect(connectorLayer.iconLayer).toBeGreaterThan(connectorLayer.connectorLayer)
  expect(connectorLayer.connectorRight).toBeGreaterThan(connectorLayer.iconLeft)
  expect(connectorLayer.connectorRight).toBeLessThan(connectorLayer.iconRight)

  const details = page.getByRole('complementary', { name: '链路详情' })
  await page.getByRole('button', { name: '技术，deepseek-v4-pro，查看详情' }).click()
  await details.getByRole('tab', { name: '响应' }).click()
  await expect(details.getByText('链路完成')).toBeVisible()
  await expect(details.locator('.markdown-content--compact')).toHaveCSS('font-size', '13px')
  await expect(details.locator('.markdown-content--compact')).toHaveCSS('line-height', '20px')
  await expect(details.getByText(/"name": "web_search"/)).toBeVisible()
  await expect(details.getByText(/"finish_reason": "tool_calls"/)).toBeVisible()
  if (process.env.TINKERFIN_VISUAL_QA_DIR) {
    await page.screenshot({
      path: resolve(process.env.TINKERFIN_VISUAL_QA_DIR, 'prototype-aligned-response-light-1440.png'),
      fullPage: true,
    })
  }
  await details.getByRole('tab', { name: '用量' }).click()
  await expect(details.getByRole('heading', { name: 'Token 用量' })).toBeVisible()
  await expect(details.getByText('输入 Tokens')).toBeVisible()
  await expect(details.getByText('4,872')).toBeVisible()
  await expect(details.getByText('缓存读取 Tokens')).toBeVisible()
  await expect(details.getByText('4,736')).toBeVisible()
  await expect(details.getByText('推理 Tokens')).toBeVisible()
  await expect(details.getByText('123')).toBeVisible()
  await expect(details.getByText(/input_token_details|cache_read/)).toHaveCount(0)
  if (process.env.TINKERFIN_VISUAL_QA_DIR) {
    await page.screenshot({
      path: resolve(process.env.TINKERFIN_VISUAL_QA_DIR, 'prototype-aligned-usage-light-1440.png'),
      fullPage: true,
    })
  }
  await details.getByRole('tab', { name: '系统提示词' }).click()
  await expect(details.getByRole('heading', { name: '浏览器系统提示词' })).toBeVisible()
  expect(pageErrors).toEqual([])
})

test('搜索只在防抖完成后替换一次 SSE，视图切换不重连', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 800 })
  const pageErrors = await mockChainTraceStudio(page)
  await page.getByRole('tab', { name: '链路' }).click()
  await expect.poll(async () => (await followUrls(page)).length).toBe(1)

  await page.getByRole('button', { name: '搜索链路节点' }).click()
  const search = page.getByRole('searchbox', { name: '搜索链路节点' })
  await search.pressSequentially('model', { delay: 20 })
  await page.waitForTimeout(180)
  expect((await followUrls(page)).length).toBe(1)
  await expect.poll(async () => (await followUrls(page)).length).toBe(2)
  const latest = new URL((await followUrls(page)).at(-1)!, 'http://127.0.0.1')
  expect(latest.searchParams.get('query')).toBe('model')

  await page.getByRole('tab', { name: '树形' }).click()
  await page.getByRole('tab', { name: '时间线' }).click()
  expect((await followUrls(page)).length).toBe(2)
  expect(pageErrors).toEqual([])
})

test('四类筛选直接查询对应底层节点而不在前端全量过滤', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 800 })
  const pageErrors = await mockChainTraceStudio(page)
  await page.getByRole('tab', { name: '链路' }).click()
  await expect.poll(async () => (await followUrls(page)).length).toBe(1)

  const initialQuery = new URL((await followUrls(page))[0]!, 'http://127.0.0.1')
  expect(initialQuery.searchParams.getAll('kind')).toEqual([
    'human_message',
    'interaction',
    'assistant_message',
    'subagent',
    'tool',
    'skill',
    'memory',
    'guardrail',
    'retrieval',
    'custom',
    'plan',
  ])
  const categoryFilters = page.getByRole('group', { name: '节点类型' })
  await categoryFilters.getByRole('button', { name: '用户', exact: true }).click()
  await expect.poll(async () => (await followUrls(page)).length).toBe(2)
  await categoryFilters.getByRole('button', { name: '工具', exact: true }).click()
  await expect.poll(async () => (await followUrls(page)).length).toBe(3)
  await categoryFilters.getByRole('button', { name: '上下文', exact: true }).click()
  await expect.poll(async () => (await followUrls(page)).length).toBe(4)
  const assistantQuery = new URL((await followUrls(page)).at(-1)!, 'http://127.0.0.1')
  expect(assistantQuery.searchParams.getAll('kind')).toEqual([
    'assistant_message',
    'subagent',
  ])
  expect(assistantQuery.searchParams.has('status')).toBe(false)

  await categoryFilters.getByRole('button', { name: '上下文', exact: true }).click()
  await expect.poll(async () => (await followUrls(page)).length).toBe(5)
  await categoryFilters.getByRole('button', { name: '助手', exact: true }).click()
  await expect.poll(async () => (await followUrls(page)).length).toBe(6)
  const contextQuery = new URL((await followUrls(page)).at(-1)!, 'http://127.0.0.1')
  expect(contextQuery.searchParams.getAll('kind')).toEqual([
    'skill',
    'memory',
    'guardrail',
    'retrieval',
    'custom',
    'plan',
  ])
  await expect(categoryFilters.getByRole('button', { name: '上下文', exact: true })).toBeDisabled()
  await expect(page.getByText('没有匹配的链路节点')).toBeVisible()
  await expect(page.getByLabel('链路筛选')).toBeVisible()
  expect(pageErrors).toEqual([])
})

test('英文单轮文案正确且省略详情不会被展示为完整响应', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  const oneTurnPage: TraceGraphPage = {
    ...tracePage(semanticNodes.slice(2)),
    turns: [{
      id: 'turn-browser-2',
      ordinal: 2,
      rootNodeId: 'human-current',
      startedAt: '2026-09-01T00:01:00.000Z',
    }],
    rootNodeIds: ['human-current'],
    completeness: {
      ...tracePage(semanticNodes).completeness,
      detailsOmitted: true,
    },
  }
  const omittedResponsePage: TraceGraphPage = {
    ...tracePage(semanticNodes.slice(5)),
    completeness: {
      ...tracePage(semanticNodes).completeness,
      detailsOmitted: true,
    },
  }
  const pageErrors = await mockChainTraceStudio(
    page,
    'light',
    oneTurnPage,
    omittedResponsePage,
    'en',
    oneTurnPage,
  )

  await page.getByRole('tab', { name: 'Trace' }).click()
  await expect(page.locator('.chain-trace-range-summary')).toContainText('1 turn')
  await page.getByRole('button', { name: 'Technical' }).click()
  await page.getByRole('button', {
    name: 'Technical，deepseek-v4-pro，View details',
  }).click()
  const details = page.getByRole('complementary', { name: 'Trace details' })
  await details.getByRole('tab', { name: 'Response' }).click()
  await expect(details.getByRole('alert')).toContainText('Failed to load the complete response')
  await expect(details.getByRole('button', { name: 'Retry' })).toBeVisible()
  await expect(details.getByText('链路完成')).toHaveCount(0)
  if (process.env.TINKERFIN_VISUAL_QA_DIR) {
    await page.screenshot({
      path: resolve(
        process.env.TINKERFIN_VISUAL_QA_DIR,
        'omitted-response-fail-closed-en-1440.png',
      ),
      fullPage: true,
    })
  }
  expect(pageErrors).toEqual([])
})

test('链路与对话往返恢复阅读位置和末尾跟随状态', async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 800 })
  const pageErrors = await mockChainTraceStudio(page)
  const pane = page.getByRole('region', { name: '对话内容' })
  const readingPosition = await pane.evaluate((element) => {
    const target = Math.round((element.scrollHeight - element.clientHeight) * .43)
    element.scrollTop = target
    element.dispatchEvent(new WheelEvent('wheel', { bubbles: true, deltaY: -120 }))
    element.dispatchEvent(new Event('scroll'))
    return element.scrollTop
  })
  await page.waitForTimeout(50)
  await page.getByRole('tab', { name: '链路' }).click()
  await page.getByRole('tab', { name: '对话' }).click()
  await expect.poll(() => page.getByRole('region', { name: '对话内容' }).evaluate(
    (element) => element.scrollTop,
  )).toBe(readingPosition)

  const restoredPane = page.getByRole('region', { name: '对话内容' })
  await restoredPane.evaluate((element) => {
    element.scrollTop = element.scrollHeight
    element.dispatchEvent(new Event('scroll'))
  })
  await page.waitForTimeout(50)
  await page.getByRole('tab', { name: '链路' }).click()
  await page.getByRole('tab', { name: '对话' }).click()
  await expect.poll(() => page.getByRole('region', { name: '对话内容' }).evaluate(
    (element) => Math.abs(element.scrollHeight - element.clientHeight - element.scrollTop),
  )).toBeLessThanOrEqual(1)

  const bottomLayout = await page.locator('.workspace-main').evaluate((workspace) => {
    const shell = workspace.closest<HTMLElement>('.app-shell')!
    const list = workspace.querySelector<HTMLElement>('.message-list')!
    const dock = workspace.querySelector<HTMLElement>('.composer-dock')!
    const content = Array.from(list.children).slice(0, -1)
    const end = list.lastElementChild!
    const dockRect = dock.getBoundingClientRect()
    return {
      composerHeight: dockRect.height,
      measuredHeight: Number.parseFloat(getComputedStyle(shell).getPropertyValue('--composer-height')),
      reservedHeight: Number.parseFloat(getComputedStyle(list).paddingBottom),
      contentBottom: Math.max(...content.map((element) => element.getBoundingClientRect().bottom)),
      endBottom: end.getBoundingClientRect().bottom,
      composerTop: dockRect.top,
    }
  })
  expect(Math.abs(bottomLayout.composerHeight - bottomLayout.measuredHeight)).toBeLessThanOrEqual(1)
  expect(Math.abs(bottomLayout.composerHeight - bottomLayout.reservedHeight)).toBeLessThanOrEqual(1)
  expect(bottomLayout.contentBottom).toBeLessThanOrEqual(bottomLayout.composerTop + 1)
  expect(bottomLayout.endBottom).toBeLessThanOrEqual(bottomLayout.composerTop + 1)
  expect(pageErrors).toEqual([])
})

test('浅色四视口保持统一抽屉边界且不产生页面溢出', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  const pageErrors = await mockChainTraceStudio(page, 'light')
  await page.getByRole('tab', { name: '链路' }).click()

  await verifyTraceViewports(page, 'light')

  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light')
  expect(pageErrors).toEqual([])
})

test('四视口、深色、触控与 reduced-motion 保持可用且不产生页面溢出', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  const pageErrors = await mockChainTraceStudio(page, 'dark')
  await page.getByRole('tab', { name: '链路' }).click()

  await verifyTraceViewports(page, 'dark')

  await page.setViewportSize({ width: 768, height: 900 })
  const details = page.getByRole('complementary', { name: '链路详情' })
  await expect(details).toHaveCSS('position', 'fixed')
  await expect(page.locator('.chain-trace-toolbar-host')).toHaveAttribute('inert', '')
  await page.keyboard.press('Escape')
  await expect(details).toBeHidden()

  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.evaluate(() => {
    const traceWindow = window as typeof window & { __traceScrollBehaviors: ScrollBehavior[] }
    traceWindow.__traceScrollBehaviors = []
    const original = Element.prototype.scrollIntoView
    Element.prototype.scrollIntoView = function scrollIntoView(options?: boolean | ScrollIntoViewOptions) {
      if (typeof options === 'object' && options.behavior) {
        traceWindow.__traceScrollBehaviors.push(options.behavior)
      }
      original.call(this, options)
    }
  })
  await page.locator('.chain-trace-timeline-bar').first().click()
  await expect.poll(() => page.evaluate(() => (
    (window as typeof window & { __traceScrollBehaviors: ScrollBehavior[] })
      .__traceScrollBehaviors.at(-1)
  ))).toBe('auto')
  await page.keyboard.press('Escape')
  const cdp = await page.context().newCDPSession(page)
  await cdp.send('Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 1 })
  await page.setViewportSize({ width: 320, height: 800 })
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
  await expect(page.getByRole('region', { name: '调用时间线' })).toBeHidden()
  const categoryTarget = await page.getByRole('group', { name: '节点类型' })
    .getByRole('button', { name: '用户', exact: true })
    .boundingBox()
  expect(categoryTarget?.width).toBeGreaterThanOrEqual(44)
  expect(categoryTarget?.height).toBeGreaterThanOrEqual(44)
  await page.getByRole('tab', { name: '树形' }).click()
  const toggle = page.getByRole('button', { name: '收起 用户，继续核验真实调用树' })
  const touchTarget = await toggle.boundingBox()
  expect(touchTarget?.width).toBeGreaterThanOrEqual(44)
  expect(touchTarget?.height).toBeGreaterThanOrEqual(44)
  await expect(toggle.locator('svg')).toHaveCSS('transition-duration', '0s')
  await page.getByRole('button', {
    name: '助手，链路完成，查看详情',
  }).first().click()
  const closeTarget = await page.getByRole('button', { name: '关闭链路详情' }).boundingBox()
  expect(closeTarget?.width).toBeGreaterThanOrEqual(44)
  expect(closeTarget?.height).toBeGreaterThanOrEqual(44)
  const detailTabTarget = await page.getByRole('tab', { name: '概述' }).boundingBox()
  expect(detailTabTarget?.width).toBeGreaterThanOrEqual(44)
  expect(detailTabTarget?.height).toBeGreaterThanOrEqual(44)
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
  expect(overflow).toBeLessThanOrEqual(0)
  expect(pageErrors).toEqual([])
})
