import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { clearAuthSession, saveAuthSession } from '../../auth/session'
import {
  followTraceEntries,
  parseTraceEntryPage,
  type TraceEntryPage,
} from './traceEntries'

const page = (): TraceEntryPage => ({
  turns: [{
    id: 'turn-1',
    ordinal: 1,
    startedAt: '2026-08-31T00:00:00Z',
    userMessage: {
      id: 'message-user-1',
      traceSeq: 2,
      sourceId: 'user-1',
      namespace: [],
      runId: 'run-1',
      role: 'user',
      content: 'Show the current trace',
      contentOmitted: false,
      status: 'completed',
      createdAt: '2026-08-31T00:00:00Z',
    },
  }],
  items: [{
    id: 'model-call',
    turnId: 'turn-1',
    parentId: 'model-step',
    kind: 'provider',
    status: 'succeeded',
    name: 'deepseek-chat',
    runId: 'run-1',
    namespace: [],
    provider: 'deepseek',
    model: 'deepseek-chat',
    startedAt: '2026-08-31T00:00:00Z',
    firstOutputAt: '2026-08-31T00:00:00.200Z',
    completedAt: '2026-08-31T00:00:01Z',
    startedSeq: 4,
    updatedSeq: 6,
    request: { messages: [] },
    requestOmitted: false,
    resultOmitted: false,
    usage: { input_tokens: 2, output_tokens: 1 },
    hooks: [],
  }],
  nextCursor: null,
  asOfSeq: 8,
  facets: {
    kinds: { provider: 1 },
    statuses: { succeeded: 1 },
    agents: {},
    middleware: {},
    skills: {},
    providers: { deepseek: 1 },
    models: { 'deepseek-chat': 1 },
  },
  completeness: { callTrackingMissing: false, executionTreeMissing: false },
})

const eventStream = (...events: unknown[]) => {
  const encoder = new TextEncoder()
  return new Response(new ReadableStream({
    start(controller) {
      events.forEach((event) => {
        controller.enqueue(encoder.encode(`event: trace\ndata: ${JSON.stringify(event)}\n\n`))
      })
      controller.close()
    },
  }), { headers: { 'Content-Type': 'text/event-stream' } })
}

describe('Trace entry client', () => {
  beforeEach(() => {
    saveAuthSession({
      token: 'trace-token',
      tokenType: 'Bearer',
      expiresAt: '2099-01-01T00:00:00.000Z',
      user: {
        user_id: 7,
        username: 'trace-user',
        display_name: 'Trace User',
        avatar_url: null,
        roles: [],
        disabled: false,
      },
    })
  })

  afterEach(() => {
    clearAuthSession()
    vi.unstubAllGlobals()
  })

  it('pushes every selected filter to the server query', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request
        ? input
        : new Request(new URL(String(input), window.location.origin), init)
      const url = new URL(request.url)
      expect(request.headers.get('Authorization')).toBe('Bearer trace-token')
      expect(url.searchParams.getAll('kind')).toEqual(['provider', 'skill'])
      expect(url.searchParams.getAll('status')).toEqual(['failed'])
      expect(url.searchParams.getAll('provider')).toEqual(['deepseek'])
      expect(url.searchParams.get('namespace')).toBe('root')
      expect(url.searchParams.get('query')).toBe('deepseek')
      expect(url.searchParams.get('includeAncestors')).toBe('true')
      return eventStream({ type: 'snapshot', snapshot: page() })
    }))

    const events = []
    for await (const event of followTraceEntries('thread-1', {
      kinds: ['provider', 'skill'],
      statuses: ['failed'],
      providers: ['deepseek'],
      namespaces: [[]],
      query: 'deepseek',
      includeAncestors: true,
    })) events.push(event)

    expect(events[0]).toMatchObject({ snapshot: { items: [{ name: 'deepseek-chat' }] } })
  })

  it('parses snapshot and filtered remove updates from SSE', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => eventStream(
      { type: 'snapshot', snapshot: page() },
      {
        type: 'update',
        update: {
          asOfSeq: 9,
          turnUpserts: [],
          turnRemoves: ['turn-1'],
          upserts: [],
          removes: ['model-call'],
          facets: { ...page().facets, kinds: {} },
          completeness: { callTrackingMissing: false, executionTreeMissing: false },
        },
      },
    )))

    const events = []
    for await (const event of followTraceEntries('thread-1', { kinds: ['provider'] })) {
      events.push(event)
    }

    expect(events.map((event) => event.type)).toEqual(['snapshot', 'update'])
    expect(events[1]).toMatchObject({ update: { removes: ['model-call'] } })
  })

  it('rejects malformed lifecycle and facet values', () => {
    const invalid = page()
    Reflect.deleteProperty(invalid.items[0] as object, 'startedSeq')
    expect(() => parseTraceEntryPage(invalid)).toThrow()

    const invalidOmission = page()
    Reflect.set(invalidOmission.items[0] as object, 'requestOmitted', 'false')
    expect(() => parseTraceEntryPage(invalidOmission)).toThrow()

    const invalidFacet = page()
    invalidFacet.facets.kinds.provider = -1
    expect(() => parseTraceEntryPage(invalidFacet)).toThrow()

    const invalidFailure = page()
    Reflect.set(invalidFailure.items[0] as object, 'failure', { errorType: 500 })
    expect(() => parseTraceEntryPage(invalidFailure)).toThrow()

    const invalidTurn = page()
    Reflect.set(invalidTurn.turns[0] as object, 'ordinal', 0)
    expect(() => parseTraceEntryPage(invalidTurn)).toThrow()
  })
})
