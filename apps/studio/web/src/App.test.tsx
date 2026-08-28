import { useCallback, useEffect, useState } from 'react'
import { render, screen } from '@testing-library/react'
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
})

const jsonResponse = (data: unknown) => new Response(
  JSON.stringify({ code: 0, message: 'success', data }),
  { headers: { 'Content-Type': 'application/json' } },
)

const sseResponse = (events: ConversationAgUiEvent[]) => {
  const encoder = new TextEncoder()
  return new Response(new ReadableStream<Uint8Array>({
    start(controller) {
      events.forEach((event, index) => {
        controller.enqueue(encoder.encode(
          'id: ' + (index + 1) + '\ndata: ' + JSON.stringify(event) + '\n\n',
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
  onChat?: (payload: ChatRequestPayload) => void
} = {}) {
  const details = options.details ?? {}
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(input, init)
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
      )))
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
            arguments: { disposition: 'omitted', safeSizeBytes: 100 },
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
  })
})
