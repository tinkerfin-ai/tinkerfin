import { describe, expect, it } from 'vitest'

import type { ConversationHistoryDetail, ConversationTraceUpdate } from '../../../api/conversation/history'
import { applyConversationTraceUpdate, restoreConversationFromTrace } from './runtime'

const detail = (): ConversationHistoryDetail => ({
  id: 1,
  threadId: 'thread-trace',
  title: 'Trace 会话',
  lastModel: 'main',
  pinned: false,
  asOfSeq: 8,
  generation: 'generation-test',
  observedAt: '2026-09-05T00:00:00.000000Z',
  headRunId: 'run-1',
  availableHeads: ['run-1'],
  historyCursor: null,
  messageCount: 2,
  toolCallCount: 1,
  messages: [
    {
      id: 'message:user-1',
      traceSeq: 1,
      sourceId: 'user-1',
      namespace: [],
      runId: 'run-1',
      role: 'user',
      content: '执行任务',
      contentOmitted: false,
      status: 'completed',
      createdAt: '2026-08-28T00:00:00Z',
      completedAt: '2026-08-28T00:00:00Z',
    },
    {
      id: 'message:assistant-1',
      traceSeq: 2,
      sourceId: 'assistant-1',
      namespace: [],
      runId: 'run-1',
      role: 'assistant',
      content: '完成',
      contentOmitted: false,
      status: 'completed',
      createdAt: '2026-08-28T00:00:01Z',
      completedAt: '2026-08-28T00:00:02Z',
    },
    {
      id: 'message:tool-result',
      traceSeq: 4,
      sourceId: 'tool-result',
      namespace: [],
      runId: 'run-1',
      role: 'tool',
      content: 'written',
      contentOmitted: false,
      name: 'write_file',
      toolCallId: 'call-write',
      status: 'completed',
      createdAt: '2026-08-28T00:00:03Z',
      completedAt: '2026-08-28T00:00:03Z',
    },
  ],
  reasoning: [
    {
      id: 'reasoning-1',
      traceSeq: 3,
      messageId: 'message:assistant-1',
      namespace: [],
      runId: 'run-1',
      extractor: 'deepseek.reasoning_content',
      content: '已授权推理',
      contentOmitted: false,
      status: 'completed',
      createdAt: '2026-08-28T00:00:01Z',
      completedAt: '2026-08-28T00:00:02Z',
    },
  ],
  graph: {
    turns: [{
      id: 'turn-1',
      ordinal: 1,
      startedAt: '2026-08-28T00:00:02Z',
    }],
    nodes: [{
      id: 'tool-node',
      turnId: 'turn-1',
      parentSubagentId: null,
      modelCallId: null,
      kind: 'tool',
      status: 'succeeded',
      name: 'write_file',
      runId: 'run-1',
      namespace: [],
      sourceId: 'call-write',
      startedAt: '2026-08-28T00:00:02Z',
      completedAt: '2026-08-28T00:00:03Z',
      startedSeq: 3,
      updatedSeq: 4,
      contentOmitted: false,
      toolCallOnly: false,
      request: { file_path: '/result.txt' },
      requestOmitted: false,
      result: 'written',
      resultOmitted: false,
      linkIssues: [],
    }],
    orderedNodeIds: ['tool-node'],
    matchedNodeIds: ['tool-node'],
    asOfSeq: 8,
    completeness: {
      callTrackingMissing: false,
      relationshipEvidenceMissing: false,
      detailsOmitted: false,
    },
  },
  state: {
    root: {
      todos: [{ content: '验证结果', status: 'completed' }],
      tinkerfin_plan: { effectiveMode: 'plan' },
    },
    subgraphs: {},
  },
  interactions: [],
  status: { execution: 'succeeded', headRunId: 'run-1' },
  completeness: { missingPrefix: false, missingTail: false, payloadOmitted: false },
  taskTrace: { status: 'ready', todoGroups: [] },
  createdAt: '2026-08-28T00:00:00',
  updatedAt: '2026-08-28T00:00:03',
})

describe('Trace conversation projection', () => {
  it('orders HTTP snapshots within one millisecond and allows observed owner recovery', () => {
    const first = detail()
    first.status = { execution: 'running', headRunId: first.headRunId }
    const lost: ConversationHistoryDetail = {
      ...first, observedAt: '2026-09-05T00:00:00.000002Z',
      status: { execution: 'unknown', headRunId: first.headRunId },
      completeness: { ...first.completeness, missingTail: true },
    }
    const current = restoreConversationFromTrace(lost, { model: 'main', includeTaskTrace: true })
    const stale = { ...first, observedAt: '2026-09-05T00:00:00.000001Z' }
    expect(restoreConversationFromTrace(stale, {
      previous: current, model: 'main', includeTaskTrace: true,
    })).toBe(current)
    expect(() => restoreConversationFromTrace({ ...first, observedAt: lost.observedAt }, {
      previous: current, model: 'main', includeTaskTrace: true,
    })).toThrow('stream_event_invalid')
    const recovered = restoreConversationFromTrace({
      ...first, observedAt: '2026-09-05T00:00:00.000003Z',
    }, { previous: current, model: 'main', includeTaskTrace: true })
    expect(recovered.runStatus).toBe('detached')
    expect(recovered.trace?.completeness.missingTail).toBe(false)
  })

  it('rejects conflicting contents at an identical observation', () => {
    const source = detail()
    const current = restoreConversationFromTrace(source, { model: 'main', includeTaskTrace: true })
    for (const conflicting of [
      { ...source, messageCount: source.messageCount + 1 },
      { ...source, state: { root: { changed: true }, subgraphs: {} } },
      { ...source, messages: [{ ...source.messages[0]!, content: '矛盾内容' }, ...source.messages.slice(1)] },
    ]) {
      expect(() => restoreConversationFromTrace(conflicting, {
        previous: current, model: 'main', includeTaskTrace: true,
      })).toThrow('stream_event_invalid')
    }
  })

  it('applies ownership changes at the same sequence without replaying content', () => {
    const source = detail()
    source.status = { execution: 'running', headRunId: source.headRunId }
    const initial = restoreConversationFromTrace(source, {
      model: 'main', includeTaskTrace: true, lastDeliveredSeq: 73,
    })
    const update: ConversationTraceUpdate = {
      asOfSeq: source.asOfSeq,
      generation: source.generation,
      observedAt: '2026-09-05T00:00:00.000001Z',
      events: [],
      facts: [],
      messages: { upserts: [], removes: [] },
      reasoning: { upserts: [], removes: [] },
      interactions: { upserts: [], removes: [] },
      graph: {
        asOfSeq: source.asOfSeq,
        nextCursor: null,
        turnUpserts: [],
        turnRemoves: [],
        nodeUpserts: [],
        nodeRemoves: [],
        orderedNodeIds: source.graph.orderedNodeIds,
        matchedNodeIds: source.graph.matchedNodeIds,
        completeness: source.graph.completeness,
      },
      state: source.state,
      status: { execution: 'unknown', headRunId: source.headRunId },
      completeness: { ...source.completeness, missingTail: true },
      messageCount: source.messageCount,
      toolCallCount: source.toolCallCount,
      projections: {},
    }

    const updated = applyConversationTraceUpdate(initial, update, null, true)
    expect(updated.runStatus).toBe('error')
    expect(updated.activeRunId).toBeUndefined()
    expect(updated.trace?.completeness.missingTail).toBe(true)
    expect(updated.trace?.asOfSeq).toBe(source.asOfSeq)
    expect(updated.trace?.messages).toEqual(source.messages)
    expect(updated.trace?.graph).toEqual(source.graph)
    expect(updated.lastSeq).toBe(73)
    const repeated = applyConversationTraceUpdate(updated, update, null, true)
    expect(repeated.messages).toEqual(updated.messages)
    expect(repeated.trace).toEqual(updated.trace)
    expect(applyConversationTraceUpdate(updated, {
      ...update, asOfSeq: source.asOfSeq - 1, status: source.status,
    }, null, true)).toBe(updated)
    expect(() => applyConversationTraceUpdate(updated, {
      ...update,
      messages: { upserts: [{ ...source.messages[1]!, content: '不应替换' }], removes: [] },
    }, null, true)).toThrow('stream_event_invalid')
  })

  it('hydrates messages, tools, reasoning, todos and status without AG-UI replay', () => {
    const restored = restoreConversationFromTrace(detail(), { model: 'fallback', includeTaskTrace: true })

    expect(restored.runStatus).toBe('idle')
    expect(restored.mode).toBe('plan')
    expect(restored.todos).toEqual([
      { id: 'trace-todo-0', content: '验证结果', status: 'completed' },
    ])
    expect(restored.messages.map((message) => message.role)).toEqual([
      'user',
      'assistant',
      'tool',
    ])
    expect(restored.messages[1]?.meta?.reasoning).toBe('已授权推理')
    expect(restored.messages[2]?.meta).toMatchObject({
      toolName: 'write_file',
      params: '{\n  "file_path": "/result.txt"\n}',
      result: 'written',
      status: 'completed',
    })
    expect(restored.lastSeq).toBeUndefined()
    expect(restored.trace?.asOfSeq).toBe(8)
    expect(restored.trace).not.toHaveProperty('taskTrace')
    expect(restored.taskTrace).toEqual({
      phase: 'ready',
      snapshot: { status: 'ready', todoGroups: [] },
    })
  })

  it('preserves a caller-owned Messaging cursor across Trace projection', () => {
    const restored = restoreConversationFromTrace(detail(), {
      model: 'fallback',
      includeTaskTrace: true,
      lastDeliveredSeq: 73,
    })

    expect(restored.lastSeq).toBe(73)
  })

  it('keeps subgraph messages out of the main conversation timeline', () => {
    const source = detail()
    source.messages = [
      ...source.messages,
      {
        ...source.messages[0]!,
        id: 'message:subgraph-user',
        sourceId: 'subgraph-user',
        namespace: ['tools:planner'],
        traceSeq: 5,
        content: '内部输入',
      },
      {
        ...source.messages[1]!,
        id: 'message:subgraph-assistant',
        sourceId: 'subgraph-assistant',
        namespace: ['tools:planner'],
        traceSeq: 6,
        content: '内部输出',
      },
    ]

    const restored = restoreConversationFromTrace(source, { model: 'fallback', includeTaskTrace: true })

    expect(restored.messages.filter((message) => (
      message.role === 'user' || message.role === 'assistant'
    )).map((message) => message.id)).toEqual([
      'message:user-1',
      'message:assistant-1',
    ])
  })

  it('restores active SubAgent partial output without leaking it into main messages', () => {
    const source = detail()
    source.status = { execution: 'running', headRunId: 'run-1' }
    source.messages = [
      ...source.messages,
      {
        ...source.messages[1]!,
        id: 'message:subagent-partial-a',
        sourceId: 'subagent-partial-a',
        namespace: ['tools:parent-task'],
        traceSeq: 6,
        content: '第一段子任务输出',
        status: 'streaming',
        completedAt: null,
      },
      {
        ...source.messages[1]!,
        id: 'message:subagent-partial-b',
        sourceId: 'subagent-partial-b',
        namespace: ['tools:parent-task'],
        traceSeq: 7,
        content: '第二段子任务输出',
        status: 'streaming',
        completedAt: null,
      },
    ]
    source.graph.nodes = [{
      ...source.graph.nodes[0]!,
      id: 'subagent-running',
      kind: 'subagent',
      name: 'researcher',
      namespace: ['tools:parent-task'],
      sourceId: 'call-task',
      status: 'running',
      request: { description: '检查当前行为', subagent_type: 'researcher' },
      result: undefined,
      resultOmitted: false,
      completedAt: null,
    }]

    const restored = restoreConversationFromTrace(source, { model: 'fallback', includeTaskTrace: true })
    const subagent = restored.messages.find((message) => message.role === 'subagent')

    expect(subagent?.meta?.result).toBe('第一段子任务输出\n\n第二段子任务输出')
    expect(restored.messages.filter((message) => (
      message.role === 'user' || message.role === 'assistant'
    )).map((message) => message.id)).toEqual([
      'message:user-1',
      'message:assistant-1',
    ])
  })

  it('correlates Tool results by full namespace and source ID', () => {
    const source = detail()
    source.messages = [
      ...source.messages.filter((message) => message.role !== 'tool'),
      {
        ...source.messages[2]!,
        id: 'result-subgraph',
        namespace: ['tools:child'],
        toolCallId: 'call-shared',
        content: 'subgraph-result',
      },
      {
        ...source.messages[2]!,
        id: 'result-root',
        namespace: [],
        toolCallId: 'call-shared',
        content: 'root-result',
      },
      {
        ...source.messages[2]!,
        id: 'result-subagent',
        namespace: [],
        toolCallId: 'call-task',
        content: 'subagent-result',
      },
    ]
    source.graph.nodes = [
      {
        ...source.graph.nodes[0]!,
        id: 'tool-root',
        namespace: [],
        sourceId: 'call-shared',
      },
      {
        ...source.graph.nodes[0]!,
        id: 'subagent-child',
        kind: 'subagent',
        name: 'researcher',
        parentSubagentId: null,
        namespace: ['tools:child'],
        sourceId: 'call-task',
        request: {
          description: '研究当前契约',
          subagent_type: 'researcher',
        },
        result: null,
      },
      {
        ...source.graph.nodes[0]!,
        id: 'tool-subgraph',
        startedSeq: 5,
        parentSubagentId: 'subagent-child',
        namespace: ['tools:child'],
        sourceId: 'call-shared',
      },
    ]

    const restored = restoreConversationFromTrace(source, { model: 'fallback', includeTaskTrace: true })
    const results = Object.fromEntries(
      restored.messages
        .filter((message) => message.role === 'tool')
        .map((message) => [message.id, message.meta?.result]),
    )

    expect(results).toEqual({
      'tool-root': 'root-result',
      'tool-subgraph': 'subgraph-result',
    })
    expect(restored.messages.find((message) => message.role === 'subagent')?.meta)
      .toMatchObject({
        agentName: 'researcher',
        input: '研究当前契约',
        result: 'subagent-result',
      })
  })

  it('applies id-based semantic deltas and replaces state atomically', () => {
    const initial = restoreConversationFromTrace(detail(), { model: 'fallback', includeTaskTrace: true })

    const updated = applyConversationTraceUpdate(initial, {
      asOfSeq: 10,
      generation: 'generation-test',
      observedAt: '2026-09-05T00:00:00.000001Z',
      events: [],
      facts: [],
      messages: {
        upserts: [{
          ...detail().messages[1]!,
          content: '更新后的回答',
        }],
        removes: [],
      },
      reasoning: { upserts: [], removes: [] },
      graph: {
        asOfSeq: 10,
        nextCursor: null,
        turnUpserts: [],
        turnRemoves: ['turn-1'],
        nodeUpserts: [],
        nodeRemoves: ['tool-node'],
        orderedNodeIds: [],
        matchedNodeIds: [],
        completeness: {
          callTrackingMissing: false,
          relationshipEvidenceMissing: false,
          detailsOmitted: false,
        },
      },
      interactions: { upserts: [], removes: [] },
      state: { root: { todos: [] }, subgraphs: {} },
      status: { execution: 'failed', headRunId: 'run-1' },
      completeness: { missingPrefix: false, missingTail: true, payloadOmitted: false },
      messageCount: 2,
      toolCallCount: 1,
      projections: {},
    }, null, true)

    expect(updated.trace?.asOfSeq).toBe(10)
    expect(updated.messages.find((message) => message.role === 'assistant')?.content)
      .toBe('更新后的回答')
    expect(updated.messages.some((message) => message.role === 'tool')).toBe(false)
    expect(updated.todos).toEqual([])
    expect(updated.runStatus).toBe('error')
    expect(updated.taskTrace).toBe(initial.taskTrace)
  })

  it('reconstructs stable multi-action approval IDs from native Trace facts', () => {
    const source = detail()
    source.status = { execution: 'waiting', headRunId: 'run-1' }
    source.interactions = [{
      id: 'interaction-scoped',
      traceSeq: 5,
      sourceId: 'native-interrupt',
      namespace: [],
      runId: 'run-1',
      kind: 'tool_approval',
      toolCallIds: ['call-a', 'call-b'],
      status: 'pending',
      payloadOmitted: false,
      payload: {
        action_requests: [
          {
            name: 'write_file',
            description: '写入 A 文件',
            arguments: {
              disposition: 'inline',
              safeSizeBytes: 20,
              value: { '': { file_path: '/a.txt' } },
            },
          },
          {
            name: 'write_file',
            arguments: {
              disposition: 'inline',
              safeSizeBytes: 20,
              value: { '/file_path': '/b.txt' },
            },
          },
        ],
        review_configs: [
          { action_name: 'write_file', allowed_decisions: ['approve', 'reject'] },
          { action_name: 'write_file', allowed_decisions: ['approve', 'reject'] },
        ],
      },
      openedAt: '2026-08-28T00:00:04Z',
      resolvedAt: null,
    }]
    source.graph.nodes = [
      { ...source.graph.nodes[0]!, id: 'tool-b', status: 'waiting', sourceId: 'call-b' },
      { ...source.graph.nodes[0]!, id: 'tool-a', status: 'waiting', sourceId: 'call-a' },
    ]

    const restored = restoreConversationFromTrace(source, { model: 'fallback', includeTaskTrace: true })

    expect(restored.approval?.items.map((item) => item.interruptId)).toEqual([
      'native-interrupt#0',
      'native-interrupt#1',
    ])
    expect(restored.approval?.items.map((item) => item.originalArgs.file_path)).toEqual([
      '/a.txt',
      '/b.txt',
    ])
    expect(restored.approval?.items.map((item) => item.toolCallId)).toEqual([
      'call-a',
      'call-b',
    ])
    expect(restored.approval?.items.map((item) => item.description)).toEqual([
      '写入 A 文件',
      'write_file',
    ])
    expect(restored.pendingInteractionKind).toBe('tool_approval')
  })

  it('preserves full captured arguments for each same-name HITL action', () => {
    const source = detail()
    source.status = { execution: 'waiting', headRunId: 'run-1' }
    source.interactions = [{
      id: 'interaction-full-content',
      traceSeq: 5,
      sourceId: 'native-interrupt',
      namespace: [],
      runId: 'run-1',
      kind: 'tool_approval',
      toolCallIds: ['call-a', 'call-b'],
      status: 'pending',
      payloadOmitted: false,
      payload: {
        action_requests: [
          {
            name: 'write_file',
            arguments: {
              disposition: 'inline',
              safeSizeBytes: 47,
              value: { content: 'A', file_path: '/multi-hitl-a.txt' },
            },
          },
          {
            name: 'write_file',
            arguments: {
              disposition: 'inline',
              safeSizeBytes: 47,
              value: { content: 'B', file_path: '/multi-hitl-b.txt' },
            },
          },
        ],
        review_configs: [
          { action_name: 'write_file', allowed_decisions: ['approve', 'reject'] },
          { action_name: 'write_file', allowed_decisions: ['approve', 'reject'] },
        ],
      },
      openedAt: '2026-08-28T00:00:04Z',
      resolvedAt: null,
    }]
    source.graph.nodes = [
      {
        ...source.graph.nodes[0]!,
        id: 'tool-b',
        status: 'waiting',
        sourceId: 'call-b',
        request: { content: 'B', file_path: '/multi-hitl-b.txt' },
      },
      {
        ...source.graph.nodes[0]!,
        id: 'tool-a',
        status: 'waiting',
        sourceId: 'call-a',
        request: { content: 'A', file_path: '/multi-hitl-a.txt' },
      },
    ]

    const restored = restoreConversationFromTrace(source, { model: 'fallback', includeTaskTrace: true })

    expect(restored.approval?.items.map((item) => ({
      toolCallId: item.toolCallId,
      originalArgs: item.originalArgs,
      params: item.params,
    }))).toEqual([
      {
        toolCallId: 'call-a',
        originalArgs: { content: 'A', file_path: '/multi-hitl-a.txt' },
        params: '{\n  "content": "A",\n  "file_path": "/multi-hitl-a.txt"\n}',
      },
      {
        toolCallId: 'call-b',
        originalArgs: { content: 'B', file_path: '/multi-hitl-b.txt' },
        params: '{\n  "content": "B",\n  "file_path": "/multi-hitl-b.txt"\n}',
      },
    ])
  })

  it('merges pending Tool approvals while a SubAgent Tool is still running', () => {
    const source = detail()
    source.status = { execution: 'waiting', headRunId: 'run-1' }
    const interaction = (
      id: string,
      traceSeq: number,
      sourceId: string,
      namespace: string[],
      toolCallId: string,
      filePath: string,
    ): ConversationHistoryDetail['interactions'][number] => ({
      id,
      traceSeq,
      sourceId,
      namespace,
      runId: 'run-1',
      kind: 'tool_approval',
      toolCallIds: [toolCallId],
      status: 'pending',
      payloadOmitted: false,
      payload: {
        action_requests: [{
          name: 'write_file',
          arguments: {
            disposition: 'inline',
            safeSizeBytes: 20,
            value: { '/file_path': filePath },
          },
        }],
        review_configs: [{
          action_name: 'write_file',
          allowed_decisions: ['approve', 'reject'],
        }],
      },
      openedAt: '2026-08-28T00:00:04Z',
      resolvedAt: null,
    })
    source.interactions = [
      interaction(
        'interaction-b',
        7,
        'interrupt-b',
        ['tools:child'],
        'call-b',
        '/b.txt',
      ),
      interaction('interaction-a', 5, 'interrupt-a', [], 'call-a', '/a.txt'),
    ]
    source.graph.nodes = [
      {
        ...source.graph.nodes[0]!,
        id: 'tool-b',
        startedSeq: 6,
        namespace: ['tools:child'],
        status: 'running',
        sourceId: 'call-b',
      },
      {
        ...source.graph.nodes[0]!,
        id: 'tool-a',
        startedSeq: 4,
        namespace: [],
        status: 'waiting',
        sourceId: 'call-a',
      },
    ]

    const restored = restoreConversationFromTrace(source, { model: 'fallback', includeTaskTrace: true })

    expect(restored.approval?.items.map((item) => ({
      interruptId: item.interruptId,
      toolCallId: item.toolCallId,
      filePath: item.originalArgs.file_path,
    }))).toEqual([
      { interruptId: 'interrupt-a', toolCallId: 'call-a', filePath: '/a.txt' },
      { interruptId: 'interrupt-b', toolCallId: 'call-b', filePath: '/b.txt' },
    ])
    expect(restored.pendingInteractionKind).toBe('tool_approval')
  })

  it('rejects mixed pending interaction groups instead of hiding approvals', () => {
    const source = detail()
    source.status = { execution: 'waiting', headRunId: 'run-1' }
    source.interactions = [
      {
        id: 'interaction-tool',
        traceSeq: 5,
        sourceId: 'interrupt-tool',
        namespace: [],
        runId: 'run-1',
        kind: 'tool_approval',
        toolCallIds: ['call-write'],
        status: 'pending',
        payloadOmitted: false,
        payload: {
          action_requests: [{ name: 'write_file', arguments: {} }],
          review_configs: [{
            action_name: 'write_file',
            allowed_decisions: ['approve'],
          }],
        },
        openedAt: '2026-08-28T00:00:04Z',
      },
      {
        id: 'interaction-unknown',
        traceSeq: 6,
        sourceId: 'unknown',
        namespace: [],
        runId: 'run-1',
        kind: 'host_input',
        toolCallIds: [],
        status: 'pending',
        payloadOmitted: false,
        payload: {},
        openedAt: '2026-08-28T00:00:05Z',
      },
    ]

    expect(() => restoreConversationFromTrace(source, { model: 'fallback', includeTaskTrace: true }))
      .toThrowError('stream_event_invalid')
  })

  it('hydrates Plan clarification directly from the native Runtime envelope', () => {
    const source = detail()
    source.status = { execution: 'succeeded', headRunId: 'run-1' }
    source.interactions = [{
      id: 'interaction-plan',
      traceSeq: 5,
      sourceId: 'plan-interrupt',
      namespace: [],
      runId: 'run-1',
      kind: 'tinkerfin:plan_clarification',
      toolCallIds: [],
      status: 'pending',
      payloadOmitted: false,
      payload: {
        schema: 'tinkerfin.runtime-interrupt',
        kind: 'tinkerfin:plan_clarification',
        message: '回答问题',
        responseSchema: { type: 'object' },
        metadata: {
          origin: 'plan',
          clarification: {
            form: {
              title: '确认范围',
              description: '补充执行范围',
              questions: [{
                id: 'scope',
                answerType: 'text',
                prompt: '请输入范围',
                required: true,
              }],
            },
          },
        },
      },
      openedAt: '2026-08-28T00:00:04Z',
      resolvedAt: null,
    }]

    const restored = restoreConversationFromTrace(source, { model: 'fallback', includeTaskTrace: true })

    expect(restored.planInteraction).toMatchObject({
      kind: 'questions',
      interruptId: 'plan-interrupt',
      title: '确认范围',
    })
    expect(restored.pendingInteractionKind).toBe('plan_clarification')
    expect(restored.runStatus).toBe('waiting_approval')
  })
})
