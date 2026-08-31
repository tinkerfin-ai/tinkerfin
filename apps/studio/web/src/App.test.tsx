import { StrictMode, useCallback, useEffect, useState } from 'react'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type {
  ConversationHistoryDetail,
  ConversationHistoryListItem,
} from './api/conversation/history'
import type { ChatRequestPayload, ConversationAgUiEvent } from './api/conversation/types'
import type { AgentModelCatalog } from './api/models/types'
import { subscribeApiErrors } from './api/shared/http'
import { clearAuthSession, saveAuthSession } from './auth/session'
import { ToastViewport } from './components/ui/ToastViewport'
import type { ToastItem, ToastKind } from './components/ui/ToastViewport'
import { WorkspaceScreen } from './features/workspace/WorkspaceScreen'

const TEST_USER = {
  user_id: 7,
  username: 'yunsan',
  display_name: '云杉',
  avatar_url: null,
  roles: [],
  disabled: false,
}

function App() {
  const [toasts, setToasts] = useState<ToastItem[]>([])
  const onToast = useCallback((kind: ToastKind, message: string) => {
    setToasts((current) => [...current, {
      id: 'toast-' + (current.length + 1),
      kind,
      message,
    }])
  }, [])
  useEffect(() => subscribeApiErrors((error) => onToast('error', error.message)), [onToast])
  return (
    <>
      <WorkspaceScreen user={TEST_USER} onLogout={vi.fn()} onToast={onToast} />
      <ToastViewport
        toasts={toasts}
        onDismiss={(id) => setToasts((current) => current.filter((item) => item.id !== id))}
      />
    </>
  )
}

const THREAD_ID = 'thread-app'
const RUN_ID = 'run-app'
const BASE_TIME = '2026-08-28T00:00:00.000Z'

const MODEL_CATALOG: AgentModelCatalog = {
  items: [{
    modelId: 'main',
    displayName: 'Main Model',
    reasoningEnabled: false,
    runtimeProfile: 'deepagents-v2',
    isDefault: true,
  }],
  defaultModelId: 'main',
}

const historyItem = (
  overrides: Partial<ConversationHistoryListItem> = {},
): ConversationHistoryListItem => ({
  id: 1,
  threadId: THREAD_ID,
  title: 'Trace 会话',
  status: 'idle',
  lastRunId: RUN_ID,
  lastModel: 'main',
  messageCount: 1,
  toolCallCount: 0,
  hasPendingInterrupt: false,
  pendingInteractionKind: null,
  pinned: false,
  createdAt: BASE_TIME,
  updatedAt: BASE_TIME,
  ...overrides,
})

const traceDetail = (
  overrides: Partial<ConversationHistoryDetail> = {},
): ConversationHistoryDetail => ({
  id: 1,
  threadId: THREAD_ID,
  title: 'Trace 会话',
  lastModel: 'main',
  runtimeProfile: 'deepagents-v2',
  pinned: false,
  asOfSeq: 5,
  headRunId: RUN_ID,
  availableHeads: [RUN_ID],
  historyCursor: null,
  messageCount: 1,
  toolCallCount: 0,
  messages: [{
    id: 'message-history',
    traceSeq: 1,
    sourceId: 'assistant-history',
    namespace: [],
    runId: RUN_ID,
    role: 'assistant',
    content: '来自 Trace 的历史回复',
    contentOmitted: false,
    status: 'completed',
    createdAt: BASE_TIME,
    completedAt: BASE_TIME,
  }],
  reasoning: [],
  nodes: [],
  state: { root: {}, subgraphs: {} },
  interactions: [],
  status: { execution: 'succeeded', headRunId: RUN_ID },
  completeness: { missingPrefix: false, missingTail: false, payloadOmitted: false },
  createdAt: BASE_TIME,
  updatedAt: BASE_TIME,
  ...overrides,
  taskTrace: overrides.taskTrace ?? { status: 'ready', todoGroups: [] },
})

const jsonResponse = (data: unknown) => new Response(
  JSON.stringify({ code: 0, message: 'success', data }),
  { headers: { 'Content-Type': 'application/json' } },
)

const sseResponse = (events: ConversationAgUiEvent[], startSeq = 1) => {
  const encoder = new TextEncoder()
  return new Response(new ReadableStream<Uint8Array>({
    start(controller) {
      events.forEach((event, index) => {
        controller.enqueue(encoder.encode(
          'id: ' + (startSeq + index) + '\ndata: ' + JSON.stringify(event) + '\n\n',
        ))
      })
      controller.close()
    },
  }), { headers: { 'Content-Type': 'text/event-stream' } })
}

function installFetch(options: {
  list?: ConversationHistoryListItem[]
  details?: Record<string, ConversationHistoryDetail>
  stream?: ConversationAgUiEvent[]
  streamStartSeq?: number
  onChat?: (payload: ChatRequestPayload) => void
} = {}) {
  const details = options.details ?? {}
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request
      ? input
      : new Request(new URL(String(input), window.location.origin), init)
    const url = new URL(request.url)
    if (url.pathname.endsWith('/api/models')) return jsonResponse(MODEL_CATALOG)
    if (url.pathname.endsWith('/api/conversation/config')) {
      return jsonResponse({ dayRanges: [7, 30] })
    }
    if (url.pathname.endsWith('/api/conversation/history')) {
      return jsonResponse({ items: options.list ?? [], nextCursor: null })
    }
    const history = url.pathname.match(/\/api\/conversation\/([^/]+)\/history$/)
    if (history) {
      const threadId = decodeURIComponent(history[1] ?? '')
      const detail = details[threadId]
      if (!detail) throw new Error('missing Trace detail for ' + threadId)
      return jsonResponse(detail)
    }
    if (request.method === 'POST' && url.pathname.endsWith('/api/conversation/chat')) {
      const payload = await request.clone().json() as ChatRequestPayload
      options.onChat?.(payload)
      return sseResponse((options.stream ?? []).map((event) => (
        'runId' in event && typeof event.runId === 'string'
          ? { ...event, runId: payload.runId }
          : event
      )), options.streamStartSeq)
    }
    throw new Error('unexpected fetch ' + request.method + ' ' + url.pathname)
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

describe('Studio Trace history integration', () => {
  beforeEach(() => {
    saveAuthSession({
      token: 'app-token',
      tokenType: 'Bearer',
      expiresAt: '2099-01-01T00:00:00.000Z',
      user: TEST_USER,
    })
    window.history.replaceState({}, '', '/')
  })

  afterEach(() => {
    clearAuthSession()
    window.localStorage.clear()
    window.sessionStorage.clear()
    vi.unstubAllGlobals()
  })

  it('hydrates a selected historical conversation directly from Trace', async () => {
    installFetch({
      list: [historyItem()],
      details: { [THREAD_ID]: traceDetail() },
    })

    render(<App />)

    expect(await screen.findByText('来自 Trace 的历史回复')).toBeInTheDocument()
    expect(screen.getByText('Trace 会话')).toBeInTheDocument()
  })

  it('restores a multi-action approval from native Trace interaction facts', async () => {
    const waiting = traceDetail({
      status: { execution: 'waiting', headRunId: RUN_ID },
      interactions: [{
        id: 'interaction-1',
        traceSeq: 4,
        sourceId: 'native-review',
        namespace: [],
        runId: RUN_ID,
        kind: 'tool_approval',
        toolCallIds: ['call-write'],
        status: 'pending',
        payloadOmitted: false,
        payload: {
          action_requests: [{
            name: 'write_file',
            arguments: {
              disposition: 'inline',
              safeSizeBytes: 55,
              value: { file_path: '/root-hitl.txt', content: 'ROOT_HITL' },
            },
          }],
          review_configs: [{
            action_name: 'write_file',
            allowed_decisions: ['approve', 'reject'],
          }],
        },
        openedAt: BASE_TIME,
        resolvedAt: null,
      }],
      nodes: [{
        id: 'tool-node',
        traceSeq: 3,
        parentId: null,
        kind: 'tool',
        label: 'write_file',
        runId: RUN_ID,
        namespace: [],
        sourceId: 'call-write',
        input: { file_path: '/root-hitl.txt', content: 'ROOT_HITL' },
        inputOmitted: false,
        resultOmitted: true,
        status: 'waiting',
        startedAt: BASE_TIME,
        completedAt: null,
      }],
    })
    installFetch({
      list: [historyItem({
        status: 'waiting_approval',
        hasPendingInterrupt: true,
        pendingInteractionKind: 'tool_approval',
      })],
      details: { [THREAD_ID]: waiting },
    })

    render(<App />)

    expect(await screen.findByRole('region', { name: '等待审批' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '允许' })).toBeEnabled()
    expect(screen.getByRole('button', { name: '拒绝' })).toBeEnabled()
    expect(screen.getAllByText('/root-hitl.txt')).toHaveLength(2)
  })

  it('keeps a pending child Tool inside its SubAgent instead of the main timeline', async () => {
    const childNamespace = ['tools:child-task']
    const waiting = traceDetail({
      status: { execution: 'waiting', headRunId: RUN_ID },
      interactions: [{
        id: 'interaction-child',
        traceSeq: 6,
        sourceId: 'child-review',
        namespace: childNamespace,
        runId: RUN_ID,
        kind: 'tool_approval',
        toolCallIds: ['call-child-write'],
        status: 'pending',
        payloadOmitted: false,
        payload: {
          action_requests: [{
            name: 'write_file',
            arguments: {
              disposition: 'inline',
              safeSizeBytes: 68,
              value: {
                file_path: '/ui-subagent-hitl.txt',
                content: 'UI_SUBAGENT_HITL',
              },
            },
          }],
          review_configs: [{
            action_name: 'write_file',
            allowed_decisions: ['approve', 'reject'],
          }],
        },
        openedAt: BASE_TIME,
        resolvedAt: null,
      }],
      nodes: [
        {
          id: 'task-tool',
          traceSeq: 2,
          parentId: 'run-node',
          kind: 'tool',
          label: 'task',
          runId: RUN_ID,
          namespace: [],
          sourceId: 'call-task',
          input: {
            description: '直接调用 write_file',
            subagent_type: 'general-purpose',
          },
          inputOmitted: false,
          resultOmitted: false,
          status: 'waiting',
          startedAt: BASE_TIME,
          completedAt: null,
        },
        {
          id: 'subagent-node',
          traceSeq: 3,
          parentId: 'run-node',
          kind: 'subagent',
          label: 'general-purpose',
          runId: RUN_ID,
          namespace: childNamespace,
          sourceId: 'call-task',
          input: {
            description: '直接调用 write_file',
            subagent_type: 'general-purpose',
          },
          inputOmitted: false,
          resultOmitted: false,
          status: 'waiting',
          startedAt: BASE_TIME,
          completedAt: null,
        },
        {
          id: 'child-tool',
          traceSeq: 4,
          parentId: 'subagent-node',
          kind: 'tool',
          label: 'write_file',
          runId: RUN_ID,
          namespace: childNamespace,
          sourceId: 'call-child-write',
          input: {
            file_path: '/ui-subagent-hitl.txt',
            content: 'UI_SUBAGENT_HITL',
          },
          inputOmitted: false,
          resultOmitted: false,
          status: 'waiting',
          startedAt: BASE_TIME,
          completedAt: null,
        },
      ],
    })
    installFetch({
      list: [historyItem({
        status: 'waiting_approval',
        hasPendingInterrupt: true,
        pendingInteractionKind: 'tool_approval',
      })],
      details: { [THREAD_ID]: waiting },
    })

    render(<App />)

    expect(await screen.findByRole('region', { name: '等待审批' })).toBeInTheDocument()
    expect(screen.getByText('SubAgent')).toBeInTheDocument()
    expect(screen.getAllByText('/ui-subagent-hitl.txt')).toHaveLength(2)
  })

  it('submits one resume request after the final decision in a multi-action approval', async () => {
    const user = userEvent.setup()
    const waiting = traceDetail({
      status: { execution: 'waiting', headRunId: RUN_ID },
      interactions: [{
        id: 'interaction-multi',
        traceSeq: 4,
        sourceId: 'native-review-multi',
        namespace: [],
        runId: RUN_ID,
        kind: 'tool_approval',
        toolCallIds: ['call-a', 'call-b'],
        status: 'pending',
        payloadOmitted: false,
        payload: {
          action_requests: [
            {
              name: 'write_file',
              description: '写入 A',
              arguments: {
                disposition: 'inline',
                safeSizeBytes: 24,
                value: { '': { file_path: '/a.txt', content: 'A' } },
              },
            },
            {
              name: 'write_file',
              description: '写入 B',
              arguments: {
                disposition: 'inline',
                safeSizeBytes: 24,
                value: { '': { file_path: '/b.txt', content: 'B' } },
              },
            },
          ],
          review_configs: [
            { action_name: 'write_file', allowed_decisions: ['approve', 'reject'] },
            { action_name: 'write_file', allowed_decisions: ['approve', 'reject'] },
          ],
        },
        openedAt: BASE_TIME,
        resolvedAt: null,
      }],
      nodes: [
        {
          id: 'tool-a',
          traceSeq: 2,
          parentId: null,
          kind: 'tool',
          label: 'write_file',
          runId: RUN_ID,
          namespace: [],
          sourceId: 'call-a',
          input: { file_path: '/a.txt', content: 'A' },
          inputOmitted: false,
          resultOmitted: false,
          status: 'waiting',
          startedAt: BASE_TIME,
          completedAt: null,
        },
        {
          id: 'tool-b',
          traceSeq: 3,
          parentId: null,
          kind: 'tool',
          label: 'write_file',
          runId: RUN_ID,
          namespace: [],
          sourceId: 'call-b',
          input: { file_path: '/b.txt', content: 'B' },
          inputOmitted: false,
          resultOmitted: false,
          status: 'waiting',
          startedAt: BASE_TIME,
          completedAt: null,
        },
      ],
    })
    const details: Record<string, ConversationHistoryDetail> = { [THREAD_ID]: waiting }
    const chatPayloads: ChatRequestPayload[] = []
    installFetch({
      list: [historyItem({
        status: 'waiting_approval',
        hasPendingInterrupt: true,
        pendingInteractionKind: 'tool_approval',
      })],
      details,
      stream: [
        { type: 'RUN_STARTED', threadId: THREAD_ID, runId: 'server-run' },
        {
          type: 'RUN_FINISHED',
          threadId: THREAD_ID,
          runId: 'server-run',
          outcome: { type: 'success' },
        },
      ],
      streamStartSeq: 76,
      onChat: (payload) => {
        chatPayloads.push(payload)
        details[THREAD_ID] = traceDetail({
          headRunId: payload.runId,
          availableHeads: [payload.runId],
          status: { execution: 'succeeded', headRunId: payload.runId },
          interactions: [],
          nodes: [],
        })
      },
    })
    render(
      <StrictMode>
        <App />
      </StrictMode>,
    )

    await user.click(await screen.findByRole('button', { name: '允许' }))
    await waitFor(() => expect(screen.getAllByText('/b.txt').length).toBeGreaterThan(0))
    await user.click(screen.getByRole('button', { name: '允许' }))
    await waitFor(() => expect(chatPayloads).toHaveLength(1))
    await act(async () => {
      await new Promise((resolve) => window.setTimeout(resolve, 100))
    })

    expect(chatPayloads).toHaveLength(1)
    expect(chatPayloads[0]?.resume).toHaveLength(2)
  })
})
