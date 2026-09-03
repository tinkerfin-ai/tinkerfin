import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { clearAuthSession, saveAuthSession } from '../../auth/session'
import {
  followTraceGraph,
  parseTraceGraph,
  parseTraceGraphPage,
  queryTraceGraphPage,
  type TraceGraphPage,
} from './traceGraph'

const page = (): TraceGraphPage => ({
  turns: [{
    id: 'turn-1',
    ordinal: 1,
    rootNodeId: 'human-1',
    startedAt: '2026-08-31T00:00:00Z',
  }],
  nodes: [
    {
      id: 'human-1',
      turnId: 'turn-1',
      parentId: null,
      structuralParentId: null,
      kind: 'human_message',
      status: 'succeeded',
      name: 'HumanMessage',
      runId: 'run-1',
      namespace: [],
      startedAt: '2026-08-31T00:00:00Z',
      completedAt: '2026-08-31T00:00:00Z',
      startedSeq: 2,
      updatedSeq: 2,
      content: 'Show the current trace',
      contentOmitted: false,
      requestOmitted: false,
      resultOmitted: false,
      hooks: [],
      linkIssues: [],
    },
    {
      id: 'model-call',
      turnId: 'turn-1',
      parentId: 'human-1',
      structuralParentId: 'human-1',
      kind: 'model',
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
      contentOmitted: false,
      request: { messages: [] },
      requestOmitted: false,
      resultOmitted: false,
      usage: { input_tokens: 2, output_tokens: 1 },
      hooks: [],
      linkIssues: [],
    },
  ],
  orderedNodeIds: ['human-1', 'model-call'],
  rootNodeIds: ['human-1'],
  nextCursor: null,
  asOfSeq: 8,
  facets: {
    kinds: { human_message: 1, model: 1 },
    statuses: { succeeded: 2 },
    agents: {},
    middleware: {},
    skills: {},
    providers: { deepseek: 1 },
    models: { 'deepseek-chat': 1 },
  },
  completeness: {
    callTrackingMissing: false,
    relationshipEvidenceMissing: false,
    detailsOmitted: false,
  },
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

describe('Trace Graph client', () => {
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

  it('pushes every selected filter to the current Graph route', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request
        ? input
        : new Request(new URL(String(input), window.location.origin), init)
      const url = new URL(request.url)
      expect(request.headers.get('Authorization')).toBe('Bearer trace-token')
      expect(url.pathname).toBe('/api/conversation/thread-1/trace/graph/follow')
      expect(url.searchParams.getAll('kind')).toEqual(['model', 'skill'])
      expect(url.searchParams.getAll('status')).toEqual(['failed'])
      expect(url.searchParams.getAll('provider')).toEqual(['deepseek'])
      expect(url.searchParams.get('namespace')).toBe('root')
      expect(url.searchParams.get('query')).toBe('deepseek')
      expect(url.searchParams.get('includeTechnicalNodes')).toBe('true')
      expect(url.searchParams.get('includeAncestorNodes')).toBe('true')
      return eventStream({ type: 'snapshot', snapshot: page() })
    }))

    const events = []
    for await (const event of followTraceGraph('thread-1', {
      kinds: ['model', 'skill'],
      statuses: ['failed'],
      providers: ['deepseek'],
      namespaces: [[]],
      query: 'deepseek',
      includeTechnicalNodes: true,
      includeAncestorNodes: true,
    })) events.push(event)

    expect(events[0]).toHaveProperty('snapshot.nodes.0.name', 'HumanMessage')
  })

  it('loads an older fixed page through its opaque cursor', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request
        ? input
        : new Request(new URL(String(input), window.location.origin), init)
      const url = new URL(request.url)
      expect(url.pathname).toBe('/api/conversation/thread-1/trace/graph')
      expect(url.searchParams.get('cursor')).toBe('older-page')
      expect(url.searchParams.get('limit')).toBe('200')
      return new Response(JSON.stringify({ code: 0, message: 'ok', data: page() }), {
        headers: { 'Content-Type': 'application/json' },
      })
    }))

    const result = await queryTraceGraphPage(
      'thread-1',
      { includeAncestorNodes: true },
      { cursor: 'older-page' },
    )

    expect(result.nodes[0]?.name).toBe('HumanMessage')
  })

  it('parses authoritative ordering and removal updates', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => eventStream(
      { type: 'snapshot', snapshot: page() },
      {
        type: 'update',
        update: {
          asOfSeq: 9,
          nextCursor: 'current-older-page',
          turnUpserts: [],
          turnRemoves: [],
          nodeUpserts: [],
          nodeRemoves: ['model-call'],
          orderedNodeIds: ['human-1'],
          rootNodeIds: ['human-1'],
          facets: { ...page().facets, kinds: { human_message: 1 } },
          completeness: page().completeness,
        },
      },
    )))

    const events = []
    for await (const event of followTraceGraph('thread-1', { kinds: ['model'] })) {
      events.push(event)
    }

    expect(events.map((event) => event.type)).toEqual(['snapshot', 'update'])
    expect(events[1]).toMatchObject({
      update: {
        nextCursor: 'current-older-page',
        nodeRemoves: ['model-call'],
      },
    })
  })

  it('rejects unknown event-envelope fields', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => eventStream({
      type: 'snapshot',
      snapshot: page(),
      legacyEntries: [],
    })))

    await expect(async () => {
      for await (const _event of followTraceGraph('thread-1', {})) {
        void _event
      }
    }).rejects.toThrow()
  })

  it('rejects malformed nodes, facets, ordering and completeness', () => {
    const invalid = page()
    Reflect.deleteProperty(invalid.nodes[0] as object, 'startedSeq')
    expect(() => parseTraceGraphPage(invalid)).toThrow()

    const invalidFacet = page()
    invalidFacet.facets.kinds.model = -1
    expect(() => parseTraceGraphPage(invalidFacet)).toThrow()

    const invalidOrder = page()
    invalidOrder.orderedNodeIds = ['model-call']
    expect(() => parseTraceGraphPage(invalidOrder)).toThrow()

    const invalidParent = page()
    invalidParent.nodes[1].parentId = 'unknown'
    expect(() => parseTraceGraphPage(invalidParent)).toThrow()

    const invalidCompleteness = page()
    Reflect.set(invalidCompleteness.completeness, 'detailsOmitted', 'false')
    expect(() => parseTraceGraphPage(invalidCompleteness)).toThrow()

    const unknownNodeField = page()
    Reflect.set(unknownNodeField.nodes[0], 'legacyEntryId', 'entry-1')
    expect(() => parseTraceGraphPage(unknownNodeField)).toThrow()

    const graphWithoutCursor = page()
    Reflect.deleteProperty(graphWithoutCursor, 'nextCursor')
    expect(() => parseTraceGraphPage(graphWithoutCursor)).toThrow()
    expect(parseTraceGraph(graphWithoutCursor).asOfSeq).toBe(8)
    expect(() => parseTraceGraph(page())).toThrow()
  })
})
