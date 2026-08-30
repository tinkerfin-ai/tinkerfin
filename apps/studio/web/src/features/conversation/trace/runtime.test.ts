import { describe, expect, it } from 'vitest'

import type { ConversationHistoryDetail } from '../../../api/conversation/history'
import { applyConversationTraceUpdate, restoreConversationFromTrace } from './runtime'

const detail = (): ConversationHistoryDetail => ({
  id: 1,
  threadId: 'thread-trace',
  title: 'Trace 会话',
  lastModel: 'main',
  runtimeProfile: 'deepagents-v2',
  pinned: false,
  asOfSeq: 8,
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
  nodes: [
    {
      id: 'tool-node',
      traceSeq: 3,
      parentId: 'run-node',
      kind: 'tool',
      label: 'write_file',
      runId: 'run-1',
      namespace: [],
      sourceId: 'call-write',
      input: { file_path: '/result.txt' },
      inputOmitted: false,
      result: 'written',
      resultOmitted: false,
      status: 'succeeded',
      startedAt: '2026-08-28T00:00:02Z',
      completedAt: '2026-08-28T00:00:03Z',
    },
  ],
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
  createdAt: '2026-08-28T00:00:00',
  updatedAt: '2026-08-28T00:00:03',
})

describe('Trace conversation projection', () => {
  it('hydrates messages, tools, reasoning, todos and status without AG-UI replay', () => {
    const restored = restoreConversationFromTrace(detail(), { model: 'fallback' })

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
  })

  it('preserves a caller-owned Messaging cursor across Trace projection', () => {
    const restored = restoreConversationFromTrace(detail(), {
      model: 'fallback',
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

    const restored = restoreConversationFromTrace(source, { model: 'fallback' })

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
    source.nodes = [{
      ...source.nodes[0]!,
      id: 'subagent-running',
      kind: 'subagent',
      label: 'researcher',
      namespace: ['tools:parent-task'],
      sourceId: 'call-task',
      status: 'running',
      input: { description: '检查当前行为', subagent_type: 'researcher' },
      result: undefined,
      resultOmitted: false,
      completedAt: null,
    }]

    const restored = restoreConversationFromTrace(source, { model: 'fallback' })
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
    ]
    source.nodes = [
      {
        ...source.nodes[0]!,
        id: 'tool-root',
        namespace: [],
        sourceId: 'call-shared',
      },
      {
        ...source.nodes[0]!,
        id: 'tool-task-parent',
        label: 'task',
        namespace: [],
        sourceId: 'call-task',
        input: {
          description: '研究当前契约',
          subagent_type: 'researcher',
        },
        result: 'subagent-result',
      },
      {
        ...source.nodes[0]!,
        id: 'subagent-child',
        kind: 'subagent',
        label: 'researcher',
        namespace: ['tools:child'],
        sourceId: 'call-task',
        input: {
          description: '研究当前契约',
          subagent_type: 'researcher',
        },
      },
      {
        ...source.nodes[0]!,
        id: 'tool-subgraph',
        traceSeq: 5,
        parentId: 'subagent-child',
        namespace: ['tools:child'],
        sourceId: 'call-shared',
      },
    ]

    const restored = restoreConversationFromTrace(source, { model: 'fallback' })
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
    const initial = restoreConversationFromTrace(detail(), { model: 'fallback' })

    const updated = applyConversationTraceUpdate(initial, {
      asOfSeq: 10,
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
      nodes: { upserts: [], removes: ['tool-node'] },
      interactions: { upserts: [], removes: [] },
      state: { root: { todos: [] }, subgraphs: {} },
      status: { execution: 'failed', headRunId: 'run-1' },
      completeness: { missingPrefix: false, missingTail: true, payloadOmitted: false },
      messageCount: 2,
      toolCallCount: 1,
      projections: {},
    })

    expect(updated.trace?.asOfSeq).toBe(10)
    expect(updated.messages.find((message) => message.role === 'assistant')?.content)
      .toBe('更新后的回答')
    expect(updated.messages.some((message) => message.role === 'tool')).toBe(false)
    expect(updated.todos).toEqual([])
    expect(updated.runStatus).toBe('error')
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
    source.nodes = [
      { ...source.nodes[0]!, id: 'tool-b', status: 'waiting', sourceId: 'call-b' },
      { ...source.nodes[0]!, id: 'tool-a', status: 'waiting', sourceId: 'call-a' },
    ]

    const restored = restoreConversationFromTrace(source, { model: 'fallback' })

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
    source.nodes = [
      {
        ...source.nodes[0]!,
        id: 'tool-b',
        status: 'waiting',
        sourceId: 'call-b',
        input: { content: 'B', file_path: '/multi-hitl-b.txt' },
      },
      {
        ...source.nodes[0]!,
        id: 'tool-a',
        status: 'waiting',
        sourceId: 'call-a',
        input: { content: 'A', file_path: '/multi-hitl-a.txt' },
      },
    ]

    const restored = restoreConversationFromTrace(source, { model: 'fallback' })

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
    source.nodes = [
      {
        ...source.nodes[0]!,
        id: 'tool-b',
        traceSeq: 6,
        namespace: ['tools:child'],
        status: 'running',
        sourceId: 'call-b',
      },
      {
        ...source.nodes[0]!,
        id: 'tool-a',
        traceSeq: 4,
        namespace: [],
        status: 'waiting',
        sourceId: 'call-a',
      },
    ]

    const restored = restoreConversationFromTrace(source, { model: 'fallback' })

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

    expect(() => restoreConversationFromTrace(source, { model: 'fallback' }))
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

    const restored = restoreConversationFromTrace(source, { model: 'fallback' })

    expect(restored.planInteraction).toMatchObject({
      kind: 'questions',
      interruptId: 'plan-interrupt',
      title: '确认范围',
    })
    expect(restored.pendingInteractionKind).toBe('plan_clarification')
    expect(restored.runStatus).toBe('waiting_approval')
  })
})
