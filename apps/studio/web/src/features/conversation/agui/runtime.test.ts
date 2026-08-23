import { describe, expect, it } from 'vitest'

import type { ConversationAgUiEvent, InterruptEvent } from '../../../api/conversation/types'
import type { ConversationEventEnvelope, ConversationHistoryDetail } from '../../../api/conversation/history'
import { buildEmptyConversation } from '../../../lib/workspace'
import type { ApprovalAllowedDecision, ApprovalItem, Conversation, PlanReviewState } from '../../../types'
import { applyConversationEvent, applyHistoryEventEnvelope, buildPlanAbandonPayload, buildPlanResumePayload, buildResumePayload, markConversationDetached, prepareResumeSubmission, restoreConversationFromHistory } from './runtime'

const THREAD_ID = 'thread-order-check'
const RUN_ID = 'run-order-check'

function nativeContractEvents(): ConversationAgUiEvent[] {
  const subRunId = 'subagent-11111111-1111-5111-8111-111111111111'
  const mainSource = { agentType: 'main' as const, agentName: 'main', namespace: [] }
  const provenance = {
    schema: 'tinkerfin.subagent-provenance.v1' as const,
    subagentInvocationId: subRunId,
    namespace: ['tools:graph-research'],
    parentNamespace: [],
    graphTaskId: 'graph-research',
    agentName: 'researcher',
    parentToolCallId: 'call-task',
    description: '研究百度与 Google',
    requestRunId: RUN_ID,
  }
  const subSource = {
    agentType: 'subagent' as const,
    agentName: 'researcher',
    namespace: [...provenance.namespace],
    parentNamespace: [],
    graphTaskId: 'graph-research',
    parentToolCallId: 'call-task',
    subagentInput: provenance.description,
    subagentInvocationId: subRunId,
  }
  return [
    { type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID },
    {
      type: 'TOOL_CALL_START',
      toolCallId: 'call-todos',
      toolCallName: 'write_todos',
      rawEvent: { streamMode: 'messages', source: mainSource, runId: RUN_ID },
    },
    {
      type: 'TOOL_CALL_RESULT',
      toolCallId: 'call-todos',
      messageId: 'message-todos',
      content: 'Updated todo list to three completed items',
      role: 'tool',
      rawEvent: { streamMode: 'messages', source: mainSource, runId: RUN_ID },
    },
    {
      type: 'STATE_SNAPSHOT',
      snapshot: {
        todos: [
          { content: '读取资料', status: 'completed' },
          { content: '研究百度', status: 'completed' },
          { content: '研究 Google', status: 'completed' },
        ],
      },
      rawEvent: { streamMode: 'values', source: mainSource, runId: RUN_ID },
    },
    {
      type: 'TOOL_CALL_START',
      toolCallId: 'call-task',
      toolCallName: 'task',
      rawEvent: { streamMode: 'messages', source: mainSource, runId: RUN_ID },
    },
    {
      type: 'TOOL_CALL_ARGS',
      toolCallId: 'call-task',
      delta: JSON.stringify({ description: '研究百度与 Google', subagent_type: 'researcher' }),
      rawEvent: { streamMode: 'messages', source: mainSource, runId: RUN_ID },
    },
    {
      type: 'RAW',
      source: 'langgraph.tasks',
      rawEvent: { type: 'tasks', phase: 'start', ns: [] },
      event: {
        data: { id: 'graph-research', name: 'tools' },
        provenance: {
          kind: 'root',
          namespace: [],
          agentType: 'main',
          agentName: 'main',
          subagents: [provenance],
        },
      },
    },
    {
      type: 'TOOL_CALL_START',
      toolCallId: 'call-read',
      toolCallName: 'read_file',
      rawEvent: { streamMode: 'messages', source: subSource, runId: RUN_ID },
    },
    {
      type: 'TOOL_CALL_RESULT',
      toolCallId: 'call-read',
      messageId: 'message-read',
      content: '百度与 Google 调研资料',
      role: 'tool',
      rawEvent: { streamMode: 'messages', source: subSource, runId: RUN_ID },
    },
    {
      type: 'TOOL_CALL_RESULT',
      toolCallId: 'call-task',
      messageId: 'message-task',
      content: '百度与 Google 调研完成',
      role: 'tool',
      rawEvent: {
        streamMode: 'messages',
        source: mainSource,
        runId: RUN_ID,
        relatedSubagentInvocationId: subRunId,
      },
    },
    { type: 'TEXT_MESSAGE_START', messageId: 'message-final', role: 'assistant' },
    {
      type: 'TEXT_MESSAGE_CONTENT',
      messageId: 'message-final',
      delta: '已完成 **Google** 调研并写入 result1.txt',
    },
    { type: 'TEXT_MESSAGE_END', messageId: 'message-final' },
    { type: 'RUN_FINISHED', threadId: THREAD_ID, runId: RUN_ID, outcome: { type: 'success' } },
  ]
}

function interrupt(
  overrides: Partial<InterruptEvent> & Pick<InterruptEvent, 'id'>,
): InterruptEvent {
  const originalArgs = {
    file_path: `${overrides.id}.txt`,
    content: overrides.id,
  }
  const allowedDecisions: ApprovalAllowedDecision[] = ['approve', 'edit', 'reject']
  return {
    id: overrides.id,
    reason: overrides.reason ?? 'tool_call',
    message: overrides.message ?? `审批 ${overrides.id}`,
    toolCallId: overrides.toolCallId ?? `scoped-tool:${overrides.id}`,
    responseSchema: overrides.responseSchema,
    metadata: overrides.metadata ?? {
      langgraphValue: {
        action_requests: [{ name: 'write_file', args: originalArgs }],
        review_configs: [{
          action_name: 'write_file',
          allowed_decisions: allowedDecisions,
        }],
      },
      deepagents: {
        schema: 'tinkerfin.deepagents.tool-review.v1',
        nativeInterruptId: overrides.id,
        actionIndex: 0,
        toolName: 'write_file',
        allowedDecisions,
        originalArgs,
      },
    },
  }
}

describe('AG-UI runtime reducer', () => {
  it('uses server-owned RAW task identities for live subagent cards and child tools', () => {
    const subRunId = 'subagent-22222222-2222-5222-8222-222222222222'
    const mainSource = { agentType: 'main' as const, agentName: 'main', namespace: [] }
    const subSource = {
      agentType: 'subagent' as const,
      agentName: 'researcher',
      namespace: ['tools:graph-server'],
      graphTaskId: 'graph-server',
      parentNamespace: [],
      parentToolCallId: 'call-task-server',
      subagentInput: '检索 LangGraph',
      subagentInvocationId: subRunId,
    }
    const events: ConversationAgUiEvent[] = [
      { type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID },
      {
        type: 'TOOL_CALL_START',
        toolCallId: 'call-task-server',
        toolCallName: 'task',
        rawEvent: { streamMode: 'messages', source: mainSource, runId: RUN_ID },
      },
      {
        type: 'TOOL_CALL_ARGS',
        toolCallId: 'call-task-server',
        delta: JSON.stringify({
          description: '检索 LangGraph',
          subagent_type: 'researcher',
        }),
        rawEvent: { streamMode: 'messages', source: mainSource, runId: RUN_ID },
      },
      {
        type: 'RAW',
        source: 'langgraph.tasks',
        rawEvent: { type: 'tasks', phase: 'start', ns: [] },
        event: {
          data: { id: 'graph-server', name: 'tools' },
          provenance: {
            kind: 'root',
            namespace: [],
            agentType: 'main',
            agentName: 'main',
            subagents: [{
              schema: 'tinkerfin.subagent-provenance.v1',
              subagentInvocationId: subRunId,
              namespace: ['tools:graph-server'],
              parentNamespace: [],
              graphTaskId: 'graph-server',
              agentName: 'researcher',
              parentToolCallId: 'call-task-server',
              description: '检索 LangGraph',
              requestRunId: RUN_ID,
            }],
          },
        },
      },
      {
        type: 'TOOL_CALL_START',
        toolCallId: 'call-search-server',
        toolCallName: 'web_search',
        rawEvent: {
          streamMode: 'messages',
          source: subSource,
          runId: RUN_ID,
        },
      },
      {
        type: 'TOOL_CALL_RESULT',
        toolCallId: 'call-search-server',
        messageId: 'message-search-server',
        content: '搜索结果',
        role: 'tool',
        rawEvent: {
          streamMode: 'messages',
          source: subSource,
          runId: RUN_ID,
          toolResultStatus: 'success',
        },
      },
      {
        type: 'TOOL_CALL_RESULT',
        toolCallId: 'call-task-server',
        messageId: 'message-task-server',
        content: '子 Agent 完成',
        role: 'tool',
        rawEvent: {
          streamMode: 'messages',
          source: mainSource,
          runId: RUN_ID,
          relatedSubagentInvocationId: subRunId,
          toolResultStatus: 'success',
        },
      },
    ]
    const initial = buildEmptyConversation({
      threadId: THREAD_ID,
      model: 'main',
      now: '2026-08-18T00:00:00.000Z',
    })
    const current = events.reduce(applyConversationEvent, initial)
    const subagent = current.messages.find(
      (message) => message.role === 'subagent' && message.meta?.subRunId === subRunId,
    )
    const childTool = current.messages.find(
      (message) => message.meta?.toolCallId === 'call-search-server',
    )
    const task = current.messages.find(
      (message) => message.meta?.toolCallId === 'call-task-server',
    )

    expect(subagent?.meta).toMatchObject({
      agentName: 'researcher',
      input: '检索 LangGraph',
      result: '子 Agent 完成',
      status: 'completed',
      runId: subRunId,
      originMainRunId: RUN_ID,
      lastMainRunId: RUN_ID,
      graphTaskId: 'graph-server',
    })
    expect(childTool?.meta).toMatchObject({
      runId: subRunId,
      graphTaskId: 'graph-server',
      sourceAgentName: 'researcher',
      status: 'completed',
    })
    expect(childTool?.id).toBe('call-search-server')
    expect(task?.id).toBe('call-task-server')
    expect(task?.meta?.subRunId).toBe(subRunId)
  })

  it('keeps one subagent card while a resumed descriptor updates its current main run', () => {
    const subRunId = 'subagent-cccccccc-cccc-5ccc-8ccc-cccccccccccc'
    const graphTaskId = 'graph-resume'
    const parentToolCallId = 'call-task-resume'
    const descriptor = (requestRunId: string) => ({
      schema: 'tinkerfin.subagent-provenance.v1' as const,
      subagentInvocationId: subRunId,
      namespace: [`tools:${graphTaskId}`],
      parentNamespace: [],
      graphTaskId,
      agentName: 'researcher',
      parentToolCallId,
      description: '继续研究',
      requestRunId,
    })
    const rawStart = (requestRunId: string): ConversationAgUiEvent => ({
      type: 'RAW',
      source: 'langgraph.tasks',
      rawEvent: { type: 'tasks', phase: 'start', ns: [] },
      event: {
        data: { id: graphTaskId, name: 'tools' },
        provenance: {
          kind: 'root',
          namespace: [],
          agentType: 'main',
          agentName: 'main',
          subagents: [descriptor(requestRunId)],
        },
      },
    })
    let current = buildEmptyConversation({
      threadId: THREAD_ID,
      model: 'main',
      now: '2026-08-18T00:00:00.000Z',
    })
    for (const event of [
      { type: 'RUN_STARTED', threadId: THREAD_ID, runId: 'run-origin' },
      {
        type: 'TOOL_CALL_START',
        toolCallId: parentToolCallId,
        toolCallName: 'task',
      },
      rawStart('run-origin'),
      { type: 'RUN_STARTED', threadId: THREAD_ID, runId: 'run-resume' },
      rawStart('run-resume'),
      {
        type: 'TOOL_CALL_RESULT',
        toolCallId: parentToolCallId,
        messageId: 'task-resume-result',
        content: '完成',
        role: 'tool',
        rawEvent: {
          streamMode: 'messages',
          source: { agentType: 'main', agentName: 'main', namespace: [] },
          runId: 'run-resume',
          relatedSubagentInvocationId: subRunId,
          toolResultStatus: 'success',
        },
      },
    ] as ConversationAgUiEvent[]) current = applyConversationEvent(current, event)

    const subagents = current.messages.filter((message) => message.role === 'subagent')
    expect(subagents).toHaveLength(1)
    expect(subagents[0].meta).toMatchObject({
      subRunId,
      originMainRunId: 'run-origin',
      lastMainRunId: 'run-resume',
      status: 'completed',
      result: '完成',
    })
  })

  it('isolates parallel server subruns that share one graph task id', () => {
    const mainSource = { agentType: 'main' as const, agentName: 'main', namespace: [] }
    const graphTaskId = 'shared-graph-task'
    const invocationIds = {
      a: 'subagent-77777777-7777-5777-8777-777777777777',
      b: 'subagent-88888888-8888-5888-8888-888888888888',
    } as const
    const descriptors = (['a', 'b'] as const).map((suffix) => ({
      schema: 'tinkerfin.subagent-provenance.v1' as const,
      subagentInvocationId: invocationIds[suffix],
      namespace: [`tools:${graphTaskId}:${suffix}`],
      parentNamespace: [],
      graphTaskId,
      agentName: 'researcher',
      parentToolCallId: `call-task-${suffix}`,
      description: `研究任务 ${suffix.toUpperCase()}`,
      requestRunId: RUN_ID,
    }))
    const rawStart: ConversationAgUiEvent = {
      type: 'RAW',
      source: 'langgraph.tasks',
      rawEvent: { type: 'tasks', phase: 'start', ns: [] },
      event: {
        data: { id: graphTaskId, name: 'tools' },
        provenance: {
          kind: 'root',
          namespace: [],
          agentType: 'main',
          agentName: 'main',
          subagents: descriptors,
        },
      },
    }
    const initial = [
      { type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID } as ConversationAgUiEvent,
      {
        type: 'TOOL_CALL_START',
        toolCallId: 'call-task-a',
        toolCallName: 'task',
        rawEvent: { streamMode: 'messages', source: mainSource, runId: RUN_ID },
      } as ConversationAgUiEvent,
      {
        type: 'TOOL_CALL_START',
        toolCallId: 'call-task-b',
        toolCallName: 'task',
        rawEvent: { streamMode: 'messages', source: mainSource, runId: RUN_ID },
      } as ConversationAgUiEvent,
      rawStart,
    ].reduce(
      applyConversationEvent,
      buildEmptyConversation({
        threadId: THREAD_ID,
        model: 'main',
        now: '2026-08-18T00:00:00.000Z',
      }),
    )
    const withText = descriptors.flatMap<ConversationAgUiEvent>((descriptor) => {
      const source = {
        agentType: 'subagent' as const,
        agentName: descriptor.agentName,
        namespace: descriptor.namespace,
        graphTaskId,
        parentNamespace: [],
        parentToolCallId: descriptor.parentToolCallId,
        subagentInput: descriptor.description,
        subagentInvocationId: descriptor.subagentInvocationId,
      }
      return [
        {
          type: 'TEXT_MESSAGE_START',
          messageId: `message-${descriptor.subagentInvocationId}`,
          role: 'assistant',
          rawEvent: {
            streamMode: 'messages',
            source,
            runId: RUN_ID,
          },
        },
        {
          type: 'TEXT_MESSAGE_CONTENT',
          messageId: `message-${descriptor.subagentInvocationId}`,
          delta: `结果 ${descriptor.subagentInvocationId}`,
          rawEvent: {
            streamMode: 'messages',
            source,
            runId: RUN_ID,
          },
        },
      ]
    }).reduce(applyConversationEvent, initial)
    const subagents = withText.messages.filter((message) => message.role === 'subagent')

    expect(subagents).toHaveLength(2)
    expect(subagents.map((message) => [message.meta?.subRunId, message.meta?.result])).toEqual([
      [invocationIds.a, `结果 ${invocationIds.a}`],
      [invocationIds.b, `结果 ${invocationIds.b}`],
    ])
  })

  it('fails a discovered subrun and its child tools when the main run errors', () => {
    const subRunId = 'subagent-33333333-3333-5333-8333-333333333333'
    const mainSource = { agentType: 'main' as const, agentName: 'main', namespace: [] }
    const subSource = {
      agentType: 'subagent' as const,
      agentName: 'researcher',
      namespace: ['tools:graph-main-error'],
      graphTaskId: 'graph-main-error',
      parentNamespace: [],
      parentToolCallId: 'call-task-main-error',
      subagentInput: '执行研究',
      subagentInvocationId: subRunId,
    }
    const events: ConversationAgUiEvent[] = [
      { type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID },
      {
        type: 'TOOL_CALL_START',
        toolCallId: 'call-task-main-error',
        toolCallName: 'task',
        rawEvent: { streamMode: 'messages', source: mainSource, runId: RUN_ID },
      },
      {
        type: 'RAW',
        source: 'langgraph.tasks',
        rawEvent: { type: 'tasks', phase: 'start', ns: [] },
        event: {
          data: { id: 'graph-main-error', name: 'tools' },
          provenance: {
            kind: 'root',
            namespace: [],
            agentType: 'main',
            agentName: 'main',
            subagents: [{
              schema: 'tinkerfin.subagent-provenance.v1',
              subagentInvocationId: subRunId,
              namespace: subSource.namespace,
              parentNamespace: [],
              graphTaskId: subSource.graphTaskId,
              agentName: subSource.agentName,
              parentToolCallId: 'call-task-main-error',
              description: '执行研究',
              requestRunId: RUN_ID,
            }],
          },
        },
      },
      {
        type: 'TOOL_CALL_START',
        toolCallId: 'call-child-main-error',
        toolCallName: 'web_search',
        rawEvent: {
          streamMode: 'messages',
          source: subSource,
          runId: RUN_ID,
        },
      },
      {
        type: 'RUN_ERROR',
        message: '主 run 失败',
        code: 'runtime_error',
        rawEvent: { runId: RUN_ID },
      },
    ]
    const current = events.reduce(
      applyConversationEvent,
      buildEmptyConversation({
        threadId: THREAD_ID,
        model: 'main',
        now: '2026-08-18T00:00:00.000Z',
      }),
    )
    const subagent = current.messages.find(
      (message) => message.role === 'subagent' && message.meta?.subRunId === subRunId,
    )
    const childTool = current.messages.find(
      (message) => message.meta?.toolCallId === 'call-child-main-error',
    )

    expect(current.runStatus).toBe('error')
    expect(subagent?.meta?.status).toBe('failed')
    expect(childTool?.meta?.status).toBe('failed')
  })

  it('treats a standard main RUN_ERROR rawEvent containing only runId as main scope', () => {
    const conversation = {
      ...buildEmptyConversation({
        threadId: THREAD_ID,
        now: '2026-08-18T10:00:00.000Z',
        model: 'main',
      }),
      runStatus: 'streaming' as const,
      activeRunId: RUN_ID,
      messages: [{
        id: 'tool-running',
        role: 'tool' as const,
        content: 'web_search',
        createdAt: '2026-08-18T10:00:01.000Z',
        meta: {
          toolName: 'web_search',
          toolCallId: 'tool-running',
          status: 'running' as const,
          runId: RUN_ID,
        },
      }],
    }

    const next = applyConversationEvent(conversation, {
      type: 'RUN_ERROR',
      rawEvent: { runId: RUN_ID },
      code: 'cancelled',
      message: '聊天生成已取消',
    })

    expect(next.runStatus).toBe('idle')
    expect(next.activeRunId).toBeUndefined()
    expect(next.notice).toEqual({
      kind: 'error',
      content: '聊天生成已取消',
    })
    expect(next.messages).toHaveLength(1)
    expect(next.messages.some((message) => message.id.includes('client-notice'))).toBe(false)
    expect(next.messages[0]?.meta).toMatchObject({
      status: 'failed',
      result: '聊天生成已取消',
    })
  })

  it('stores a detached connection notice outside protocol messages', () => {
    const conversation = {
      ...buildEmptyConversation({
        threadId: THREAD_ID,
        now: '2026-08-18T10:00:00.000Z',
        model: 'main',
      }),
      runStatus: 'streaming' as const,
      activeRunId: RUN_ID,
    }

    const detached = markConversationDetached(conversation, '实时连接已断开')

    expect(detached.notice).toEqual({
      kind: 'info',
      content: '实时连接已断开',
    })
    expect(detached.messages).toEqual([])
  })

  it('replaces the draft identity and title from the main RUN_STARTED event', () => {
    const draft = buildEmptyConversation({
      now: '2026-08-18T10:00:00.000Z',
      model: 'main',
    })

    const next = applyConversationEvent(draft, {
      type: 'RUN_STARTED',
      threadId: 'thread-from-server',
      runId: 'run-from-client',
      title: '服务端生成的标题',
      input: {
        threadId: 'thread-from-server',
        runId: 'run-from-client',
        state: {},
        messages: [
          { id: 'message-from-server', role: 'user', content: '分析本季度现金流' },
        ],
        tools: [],
        context: [],
        forwardedProps: { model: 'main', mode: 'default' },
      },
    })

    expect(next.threadId).toBe('thread-from-server')
    expect(next.title).toBe('服务端生成的标题')
    expect(next.messages).toEqual([
      expect.objectContaining({
        id: 'message-from-server',
        role: 'user',
        content: '分析本季度现金流',
      }),
    ])
  })

  it('uses values as Todo truth while the standard tool result completes the tool card', () => {
    const initial = buildEmptyConversation({
      threadId: 'thread-write-todos-end',
      now: '2026-08-05T00:00:00.000Z',
      model: 'GPT-5.5',
    })

    const afterStart = applyConversationEvent(initial, {
      type: 'TOOL_CALL_START',
      rawEvent: {
        streamMode: 'messages',
        source: { agentType: 'main', agentName: 'main', namespace: [] },
        langgraphNode: 'model',
      },
      toolCallId: 'call-write-todos-test',
      toolCallName: 'write_todos',
      parentMessageId: 'parent-message',
    })

    const afterArgs = applyConversationEvent(afterStart, {
      type: 'TOOL_CALL_ARGS',
      rawEvent: {
        streamMode: 'messages',
        source: { agentType: 'main', agentName: 'main', namespace: [] },
        langgraphNode: 'model',
      },
      toolCallId: 'call-write-todos-test',
      delta: '{"todos":[{"content":"读取 url.json","status":"completed"},{"content":"写入 result.txt","status":"in_progress"}]}',
    })

    const afterEnd = applyConversationEvent(afterArgs, {
      type: 'TOOL_CALL_END',
      rawEvent: {
        streamMode: 'messages',
        source: { agentType: 'main', agentName: 'main', namespace: [] },
      },
      toolCallId: 'call-write-todos-test',
    })

    const message = afterEnd.messages.find(
      (item) => item.role === 'tool' && item.meta?.toolCallId === 'call-write-todos-test',
    )

    expect(message?.meta?.status).toBe('running')
    expect(afterEnd.todos).toEqual([])

    const rawContent = "Updated todo list to [{'content': '读取 url.json', 'status': 'completed'}]"
    const afterResult = applyConversationEvent(afterEnd, {
      type: 'TOOL_CALL_RESULT',
      toolCallId: 'call-write-todos-test',
      messageId: 'tool-message-write-todos-test',
      content: rawContent,
      role: 'tool',
    })
    const afterState = applyConversationEvent(afterResult, {
      type: 'STATE_SNAPSHOT',
      snapshot: {
        tinkerfin_plan: {
          workflowVersion: 'tinkerfin.plan.v3',
          effectiveMode: 'plan',
        },
        todos: [
          { content: '读取 url.json', status: 'completed' },
          { content: '写入 result.txt', status: 'in_progress' },
        ],
      },
    })
    const afterDelta = applyConversationEvent(afterState, {
      type: 'STATE_DELTA',
      delta: [
        { op: 'replace', path: '/todos/1/status', value: 'completed' },
        { op: 'replace', path: '/tinkerfin_plan/effectiveMode', value: 'default' },
      ],
    })
    const completedTool = afterDelta.messages.find(
      (item) => item.role === 'tool' && item.meta?.toolCallId === 'call-write-todos-test',
    )

    expect(completedTool?.meta?.status).toBe('completed')
    expect(completedTool?.meta?.result).toBe(rawContent)
    expect(afterState.mode).toBe('plan')
    expect(afterDelta.mode).toBe('default')
    expect(afterDelta.todos.map((todo) => todo.status)).toEqual(['completed', 'completed'])
    expect(afterDelta.todos.every((todo) => todo.targetMessageId == null)).toBe(true)
  })

  it('pauses every unresolved tool in the interrupted main run without inventing interrupt bindings', () => {
    let current = applyConversationEvent(
      buildEmptyConversation({
        threadId: THREAD_ID,
        now: '2026-08-05T00:00:00.000Z',
        model: 'GPT-5.5',
      }),
      { type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID },
    )
    const startTool = (toolCallId: string, toolCallName: string, runId: string) => {
      current = applyConversationEvent(current, {
        type: 'TOOL_CALL_START',
        toolCallId,
        toolCallName,
        parentMessageId: 'assistant-mixed-batch',
        rawEvent: {
          streamMode: 'messages',
          source: { agentType: 'main', agentName: 'main', namespace: [] },
          runId,
        },
      })
    }

    startTool('call-write-todos-paused', 'write_todos', RUN_ID)
    startTool('call-write-file-paused', 'write_file', RUN_ID)
    startTool('call-other-run', 'read_file', 'run-other')

    const interrupted = applyConversationEvent(current, {
      type: 'RUN_FINISHED',
      threadId: THREAD_ID,
      runId: RUN_ID,
      outcome: {
        type: 'interrupt',
        interrupts: [
          interrupt({
            id: 'interrupt-write-file',
            toolCallId: 'call-write-file-paused',
          }),
        ],
      },
    })
    const toolByCallId = new Map(
      interrupted.messages
        .filter((message) => message.role === 'tool' && message.meta?.toolCallId)
        .map((message) => [message.meta?.toolCallId, message]),
    )

    expect(toolByCallId.get('call-write-todos-paused')?.meta?.status).toBe('paused')
    expect(toolByCallId.get('call-write-todos-paused')?.meta?.interruptId).toBeUndefined()
    expect(toolByCallId.get('call-write-file-paused')?.meta?.status).toBe('paused')
    expect(toolByCallId.get('call-write-file-paused')?.meta?.interruptId).toBe(
      'interrupt-write-file',
    )
    expect(toolByCallId.get('call-other-run')?.meta?.status).toBe('running')
  })

  it('marks ToolMessage status error as a failed tool card', () => {
    const initial = buildEmptyConversation({
      threadId: 'thread-tool-error',
      now: '2026-08-05T00:00:00.000Z',
      model: 'GPT-5.5',
    })
    const started = applyConversationEvent(initial, {
      type: 'TOOL_CALL_START',
      toolCallId: 'call-read-error',
      toolCallName: 'read_file',
      rawEvent: {
        streamMode: 'messages',
        source: { agentType: 'main', agentName: 'main', namespace: [] },
        runId: RUN_ID,
      },
    })

    const failed = applyConversationEvent(started, {
      type: 'TOOL_CALL_RESULT',
      messageId: 'tool-message-error',
      toolCallId: 'call-read-error',
      content: 'file missing',
      role: 'tool',
      rawEvent: {
        streamMode: 'messages',
        source: { agentType: 'main', agentName: 'main', namespace: [] },
        runId: RUN_ID,
        toolResultStatus: 'error',
      },
    })

    expect(failed.messages.find((message) => message.meta?.toolCallId === 'call-read-error')?.meta?.status).toBe('failed')
  })

  it('ignores the complete reasoning lifecycle for main and subagent runs', () => {
    const initial = buildEmptyConversation({
      threadId: 'thread-reasoning-hidden',
      now: '2026-08-05T00:00:00.000Z',
      model: 'GPT-5.5',
    })
    const mainRawEvent = {
      streamMode: 'messages' as const,
      source: { agentType: 'main' as const, agentName: 'main', namespace: [] },
      runId: RUN_ID,
    }
    const mainReasoning: ConversationAgUiEvent[] = [
      { type: 'REASONING_START', rawEvent: mainRawEvent, messageId: 'reasoning-main' },
      { type: 'REASONING_MESSAGE_START', rawEvent: mainRawEvent, messageId: 'reasoning-message-main', role: 'reasoning' },
      { type: 'REASONING_MESSAGE_CONTENT', rawEvent: mainRawEvent, messageId: 'reasoning-message-main', delta: '内部推理' },
      { type: 'REASONING_MESSAGE_END', rawEvent: mainRawEvent, messageId: 'reasoning-message-main' },
      { type: 'REASONING_END', rawEvent: mainRawEvent, messageId: 'reasoning-main' },
    ]

    const afterMainReasoning = mainReasoning.reduce(applyConversationEvent, initial)
    expect(afterMainReasoning).toBe(initial)
    expect(afterMainReasoning.messages).toHaveLength(0)

    const subRunId = 'subagent-44444444-4444-5444-8444-444444444444'
    const withSubagent: Conversation = {
      ...initial,
      messages: [{
        id: subRunId,
        role: 'subagent',
        content: '研究任务',
        createdAt: '2026-08-18T00:00:00.000Z',
        meta: {
          agentName: 'researcher',
          input: '研究任务',
          result: '',
          status: 'running',
          subRunId,
          runId: subRunId,
          originMainRunId: RUN_ID,
          lastMainRunId: RUN_ID,
          graphTaskId: 'graph-reasoning',
        },
      }],
    }
    const subRawEvent = {
      streamMode: 'messages' as const,
      source: {
        agentType: 'subagent' as const,
        agentName: 'researcher',
        namespace: ['tools:graph-reasoning'],
        graphTaskId: 'graph-reasoning',
        subagentInvocationId: subRunId,
      },
      runId: RUN_ID,
    }
    const subReasoning: ConversationAgUiEvent[] = [
      { type: 'REASONING_START', rawEvent: subRawEvent, messageId: 'reasoning-sub' },
      { type: 'REASONING_MESSAGE_START', rawEvent: subRawEvent, messageId: 'reasoning-message-sub', role: 'reasoning' },
      { type: 'REASONING_MESSAGE_CONTENT', rawEvent: subRawEvent, messageId: 'reasoning-message-sub', delta: '子智能体内部推理' },
      { type: 'REASONING_MESSAGE_END', rawEvent: subRawEvent, messageId: 'reasoning-message-sub' },
      { type: 'REASONING_END', rawEvent: subRawEvent, messageId: 'reasoning-sub' },
    ]

    expect(subReasoning.reduce(applyConversationEvent, withSubagent)).toBe(withSubagent)
  })

  it('replays the messages/tasks/values contract fixture without losing state or hierarchy', () => {
    const initial = buildEmptyConversation({
      threadId: 'thread-real-stream',
      now: '2026-08-05T00:00:00.000Z',
      model: 'GPT-5.5',
    })

    const sampleEvents = nativeContractEvents()
    const finalConversation = sampleEvents.reduce(applyConversationEvent, initial)
    const expectedThreadId = sampleEvents.find(
      (event): event is Extract<ConversationAgUiEvent, { type: 'RUN_STARTED' }> => event.type === 'RUN_STARTED',
    )?.threadId
    const writeTodoMessages = finalConversation.messages.filter(
      (message) => message.role === 'tool' && message.meta?.toolName === 'write_todos',
    )
    const taskMessages = finalConversation.messages.filter(
      (message) => message.role === 'tool' && message.meta?.toolName === 'task',
    )
    const assistantMessages = finalConversation.messages.filter(
      (message) => message.role === 'assistant',
    )
    const subagentMessages = finalConversation.messages.filter(
      (message) => message.role === 'subagent',
    )
    const subagentTools = finalConversation.messages.filter(
      (message) => message.role === 'tool' && message.meta?.sourceAgentName === 'researcher',
    )
    const finalAssistant = assistantMessages.at(-1)
    const delegatedResults = taskMessages.map((message) => message.meta?.result ?? '').join('\n')
    const subagentResults = subagentMessages.map((message) => message.meta?.result ?? '').join('\n')
    const subRunIds = new Set(subagentMessages.map((message) => message.meta?.subRunId))

    expect(finalConversation.threadId).toBe(expectedThreadId)
    expect(finalConversation.runStatus).toBe('idle')
    expect(finalConversation.approval).toBeUndefined()
    expect(finalConversation.plan).toBeNull()
    expect(finalConversation.todos).toHaveLength(3)
    expect(finalConversation.todos.every((todo) => todo.status === 'completed')).toBe(true)
    expect(writeTodoMessages).toHaveLength(1)
    expect(writeTodoMessages.every((message) => message.meta?.result?.includes('Updated todo list to'))).toBe(true)
    expect(taskMessages.length).toBeGreaterThan(0)
    expect(taskMessages.every((message) => message.meta?.agentName === 'researcher')).toBe(true)
    expect(delegatedResults).toContain('百度')
    expect(delegatedResults).toMatch(/Google|谷歌/)
    expect(subagentMessages).toHaveLength(taskMessages.length)
    expect(subagentMessages.every((message) => message.meta?.agentName === 'researcher')).toBe(true)
    expect(subagentResults).toContain('百度')
    expect(subagentResults).toMatch(/Google|谷歌/)
    expect(subagentMessages.every((message) => message.meta?.reasoning === undefined)).toBe(true)
    expect(subagentTools.length).toBeGreaterThan(0)
    expect(subagentTools.every((message) => subRunIds.has(message.meta?.runId))).toBe(true)
    expect(finalAssistant?.content).toMatch(/Google|谷歌|google\.com/i)
    expect(finalAssistant?.content).toContain('result1.txt')
  })

  it('keeps parallel same-type subagents isolated when task results finish in reverse order', () => {
    let current = buildEmptyConversation({
      threadId: 'thread-parallel-subagents',
      now: '2026-08-05T00:00:00.000Z',
      model: 'GPT-5.5',
    })
    const apply = (event: ConversationAgUiEvent) => {
      current = applyConversationEvent(current, event)
    }
    const mainSource = { agentType: 'main' as const, agentName: 'main', namespace: [] }
    const subRunIds = {
      a: 'subagent-aaaaaaaa-aaaa-5aaa-8aaa-aaaaaaaaaaaa',
      b: 'subagent-bbbbbbbb-bbbb-5bbb-8bbb-bbbbbbbbbbbb',
    } as const
    const subSource = (suffix: 'a' | 'b') => ({
      agentType: 'subagent' as const,
      agentName: 'researcher',
      namespace: [`tools:graph-${suffix}`],
      parentNamespace: [],
      graphTaskId: `graph-${suffix}`,
      parentToolCallId: `task-${suffix}`,
      subagentInput: `研究任务 ${suffix.toUpperCase()}`,
      subagentInvocationId: subRunIds[suffix],
    })

    apply({ type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID })
    for (const suffix of ['a', 'b']) {
      apply({
        type: 'TOOL_CALL_START',
        rawEvent: { streamMode: 'messages', source: mainSource, runId: RUN_ID },
        toolCallId: `task-${suffix}`,
        toolCallName: 'task',
        parentMessageId: 'task-parent',
      })
      apply({
        type: 'TOOL_CALL_ARGS',
        rawEvent: { streamMode: 'messages', source: mainSource, runId: RUN_ID },
        toolCallId: `task-${suffix}`,
        delta: JSON.stringify({
          description: `研究任务 ${suffix.toUpperCase()}`,
          subagent_type: 'researcher',
        }),
      })
    }

    for (const suffix of ['a', 'b'] as const) {
      const graphTaskId = `graph-${suffix}`
      const subRunId = subRunIds[suffix]
      apply({
        type: 'RAW',
        source: 'langgraph.tasks',
        rawEvent: { type: 'tasks', phase: 'start', ns: [] },
        event: {
          data: { id: graphTaskId, name: 'tools' },
          provenance: {
            kind: 'root',
            namespace: [],
            agentType: 'main',
            agentName: 'main',
            subagents: [{
              schema: 'tinkerfin.subagent-provenance.v1',
              subagentInvocationId: subRunId,
              namespace: [`tools:${graphTaskId}`],
              parentNamespace: [],
              graphTaskId,
              agentName: 'researcher',
              parentToolCallId: `task-${suffix}`,
              description: `研究任务 ${suffix.toUpperCase()}`,
              requestRunId: RUN_ID,
            }],
          },
        },
      })
      apply({
        type: 'TOOL_CALL_START',
        rawEvent: {
          streamMode: 'messages',
          source: subSource(suffix),
          runId: RUN_ID,
        },
        toolCallId: `child-tool-${suffix}`,
        toolCallName: 'web_search',
        parentMessageId: `child-message-${suffix}`,
      })
    }

    const runningSubagents = current.messages.filter((message) => message.role === 'subagent')
    const runningSubagentA = runningSubagents.find((message) => message.meta?.subRunId === subRunIds.a)
    const runningSubagentB = runningSubagents.find((message) => message.meta?.subRunId === subRunIds.b)
    const runningTaskA = current.messages.find((message) => message.meta?.toolCallId === 'task-a')
    const runningTaskB = current.messages.find((message) => message.meta?.toolCallId === 'task-b')

    expect(runningSubagentA?.meta?.input).toBe('研究任务 A')
    expect(runningSubagentA?.meta?.toolCallId).toBe('task-a')
    expect(runningSubagentB?.meta?.input).toBe('研究任务 B')
    expect(runningSubagentB?.meta?.toolCallId).toBe('task-b')
    expect(runningTaskA?.meta?.subRunId).toBe(subRunIds.a)
    expect(runningTaskB?.meta?.subRunId).toBe(subRunIds.b)

    for (const suffix of ['b', 'a'] as const) {
      apply({
        type: 'TOOL_CALL_RESULT',
        rawEvent: {
          streamMode: 'messages',
          source: mainSource,
          runId: RUN_ID,
          relatedSubagentInvocationId: subRunIds[suffix],
        },
        messageId: `task-result-${suffix}`,
        toolCallId: `task-${suffix}`,
        content: `最终结果 ${suffix.toUpperCase()}`,
        role: 'tool',
      })
    }

    const subagents = current.messages.filter((message) => message.role === 'subagent')
    const subagentA = subagents.find((message) => message.meta?.subRunId === subRunIds.a)
    const subagentB = subagents.find((message) => message.meta?.subRunId === subRunIds.b)
    const childToolA = current.messages.find((message) => message.meta?.toolCallId === 'child-tool-a')
    const childToolB = current.messages.find((message) => message.meta?.toolCallId === 'child-tool-b')

    expect(subagents).toHaveLength(2)
    expect(subagentA?.meta?.input).toBe('研究任务 A')
    expect(subagentA?.meta?.result).toBe('最终结果 A')
    expect(subagentB?.meta?.input).toBe('研究任务 B')
    expect(subagentB?.meta?.result).toBe('最终结果 B')
    expect(childToolA?.meta?.runId).toBe(subagentA?.meta?.subRunId)
    expect(childToolB?.meta?.runId).toBe(subagentB?.meta?.subRunId)
  })

  it('keeps interrupt order stable when preparing multi-item resume payloads', () => {
    const interrupted = applyConversationEvent(
      buildEmptyConversation({
        threadId: 'thread-multi-interrupt',
        now: '2026-08-05T00:00:00.000Z',
        model: 'GPT-5.5',
      }),
      {
        type: 'RUN_FINISHED',
        threadId: THREAD_ID,
        runId: RUN_ID,
        outcome: {
          type: 'interrupt',
          interrupts: [
            interrupt({ id: 'interrupt-b', toolCallId: 'tool-b' }),
            interrupt({ id: 'interrupt-a#0', toolCallId: 'tool-a-0' }),
            interrupt({ id: 'interrupt-a#1', toolCallId: 'tool-a-1' }),
          ],
        },
      },
    )

    const approval = interrupted.approval
    expect(approval?.items.map((item) => item.interruptId)).toEqual([
      'interrupt-b',
      'interrupt-a#0',
      'interrupt-a#1',
    ])

    const items = (approval?.items ?? []).map<ApprovalItem>((item) => {
      if (item.interruptId === 'interrupt-b') {
        return { ...item, decision: 'approved' }
      }
      if (item.interruptId === 'interrupt-a#0') {
        return {
          ...item,
          decision: 'approved',
          editedArgs: {
            file_path: 'edited-a0.txt',
            content: 'edited-a0',
          },
          editedParams: JSON.stringify({
            file_path: 'edited-a0.txt',
            content: 'edited-a0',
          }),
        }
      }
      return { ...item, decision: 'rejected', rejectionReason: 'skip' }
    })

    const payload = buildResumePayload({
      ...interrupted,
      threadId: THREAD_ID,
      approval: approval ? { ...approval, items } : approval,
    })

    expect(payload.forwardedProps).toEqual({ model: 'GPT-5.5', mode: 'default' })
    expect(payload.resume?.map((entry) => entry.interruptId)).toEqual([
      'interrupt-b',
      'interrupt-a#0',
      'interrupt-a#1',
    ])
    expect(payload.resume).toEqual([
      {
        interruptId: 'interrupt-b',
        status: 'resolved',
        payload: { type: 'approve' },
      },
      {
        interruptId: 'interrupt-a#0',
        status: 'resolved',
        payload: {
          type: 'edit',
          edited_action: {
            name: 'write_file',
            args: {
              file_path: 'edited-a0.txt',
              content: 'edited-a0',
            },
          },
        },
      },
      {
        interruptId: 'interrupt-a#1',
        status: 'resolved',
        payload: { type: 'reject', message: 'skip' },
      },
    ])
  })

  it.each([
    ['missing approval state', undefined],
    ['empty approval list', { items: [], activeIndex: 0, submitted: false }],
    ['undecided item', {
      items: [{
        id: 'approval-undecided',
        interruptId: 'interrupt-undecided',
        toolName: 'write_file',
        params: '{}',
        input: '{}',
        description: '审批写入',
        originalArgs: {},
        allowedDecisions: ['approve' as const],
      }],
      activeIndex: 0,
      submitted: false,
    }],
    ['decision excluded by the interrupt', {
      items: [{
        id: 'approval-disallowed',
        interruptId: 'interrupt-disallowed',
        toolName: 'write_file',
        params: '{}',
        input: '{}',
        description: '审批写入',
        originalArgs: {},
        allowedDecisions: ['reject' as const],
        decision: 'approved' as const,
      }],
      activeIndex: 0,
      submitted: false,
    }],
    ['empty interrupt identifier', {
      items: [{
        id: 'approval-empty-id',
        interruptId: '',
        toolName: 'write_file',
        params: '{}',
        input: '{}',
        description: '审批写入',
        originalArgs: {},
        allowedDecisions: ['approve' as const],
        decision: 'approved' as const,
      }],
      activeIndex: 0,
      submitted: false,
    }],
  ])('rejects a resume payload with %s', (_name, approval) => {
    const current = buildEmptyConversation({
      threadId: 'thread-invalid-resume',
      now: '2026-08-05T00:00:00.000Z',
      model: 'GPT-5.5',
    })

    expect(() => buildResumePayload({ ...current, approval })).toThrow()
  })

  it('rejects a resume payload when the authoritative interrupt group has changed', () => {
    const current = buildEmptyConversation({
      threadId: 'thread-replaced-resume',
      now: '2026-08-05T00:00:00.000Z',
      model: 'GPT-5.5',
    })
    const approval = {
      items: [{
        id: 'approval-new',
        interruptId: 'interrupt-new',
        toolName: 'write_file',
        params: '{}',
        input: '{}',
        description: '新审批',
        originalArgs: {},
        allowedDecisions: ['approve' as const],
        decision: 'approved' as const,
      }],
      activeIndex: 0,
      submitted: false,
    }

    expect(() => buildResumePayload(
      { ...current, approval },
      ['interrupt-old'],
    )).toThrow('审批状态已更新')
  })

  it('does not claim a replacement approval group for an old resume submission', () => {
    const current = buildEmptyConversation({
      threadId: 'thread-replaced-submission',
      now: '2026-08-05T00:00:00.000Z',
      model: 'GPT-5.5',
    })
    const authoritative: Conversation = {
      ...current,
      runStatus: 'waiting_approval',
      approval: {
        items: [{
          id: 'approval-new',
          interruptId: 'interrupt-new',
          toolName: 'write_file',
          params: '{}',
          input: '{}',
          description: '新审批',
          originalArgs: {},
          allowedDecisions: ['approve'],
          decision: 'approved',
        }],
        activeIndex: 0,
        submitted: false,
      },
    }

    const result = prepareResumeSubmission(authoritative, ['interrupt-old'])

    expect(result).toBe(authoritative)
    expect(result.runStatus).toBe('waiting_approval')
    expect(result.approval?.submitted).toBe(false)
  })

  it('restores interrupted tool cards to running when approval submission starts', () => {
    const interrupted = applyConversationEvent(
      applyConversationEvent(
        buildEmptyConversation({
          threadId: 'thread-resume-running',
          now: '2026-08-05T00:00:00.000Z',
          model: 'GPT-5.5',
        }),
        {
          type: 'TOOL_CALL_START',
          rawEvent: {
            streamMode: 'messages',
            source: { agentType: 'main', agentName: 'main', namespace: [] },
            langgraphNode: 'model',
          },
          toolCallId: 'call-write-file-running',
          toolCallName: 'write_file',
          parentMessageId: 'parent-message',
        },
      ),
      {
        type: 'RUN_FINISHED',
        threadId: THREAD_ID,
        runId: RUN_ID,
        outcome: {
          type: 'interrupt',
          interrupts: [
            interrupt({ id: 'interrupt-running', toolCallId: 'call-write-file-running' }),
          ],
        },
      },
    )

    const resumed = prepareResumeSubmission({
      ...interrupted,
      approval: interrupted.approval
        ? {
            ...interrupted.approval,
            items: interrupted.approval.items.map((item) => ({ ...item, decision: 'approved' as const })),
          }
        : interrupted.approval,
    })

    const toolMessage = resumed.messages.find(
      (message) => message.role === 'tool' && message.meta?.toolCallId === 'call-write-file-running',
    )

    expect(resumed.runStatus).toBe('streaming')
    expect(resumed.approval?.submitted).toBe(true)
    expect(toolMessage?.meta?.status).toBe('running')
    expect(toolMessage?.meta?.interruptId).toBeUndefined()

    const confirmedByServer = applyConversationEvent(resumed, {
      type: 'RUN_STARTED',
      threadId: THREAD_ID,
      runId: `${RUN_ID}-resume`,
      input: {
        threadId: THREAD_ID,
        runId: `${RUN_ID}-resume`,
        resume: [
          {
            interruptId: 'interrupt-running',
            status: 'resolved',
            payload: { type: 'approve' },
          },
        ],
      },
    })

    expect(confirmedByServer.approval).toBeUndefined()
    expect(confirmedByServer.runStatus).toBe('streaming')
  })

  it('preserves pending approval when a resumed Runtime fails during initialization', () => {
    const started = applyConversationEvent(
      buildEmptyConversation({
        threadId: THREAD_ID,
        now: '2026-08-05T00:00:00.000Z',
        model: 'GPT-5.5',
      }),
      { type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID },
    )
    const withTool = applyConversationEvent(started, {
      type: 'TOOL_CALL_START',
      toolCallId: 'call-init-failure',
      toolCallName: 'write_file',
    })
    const interrupted = applyConversationEvent(withTool, {
      type: 'RUN_FINISHED',
      threadId: THREAD_ID,
      runId: RUN_ID,
      outcome: {
        type: 'interrupt',
        interrupts: [interrupt({
          id: 'interrupt-init-failure',
          toolCallId: 'call-init-failure',
        })],
      },
    })
    const submitted = prepareResumeSubmission(
      interrupted,
      ['interrupt-init-failure'],
    )
    expect(submitted.approval?.submitted).toBe(true)
    expect(submitted.messages.find(
      (message) => message.meta?.toolCallId === 'call-init-failure',
    )?.meta?.status).toBe('running')
    const initializationStarted = applyConversationEvent(submitted, {
      type: 'RUN_STARTED',
      threadId: THREAD_ID,
      runId: `${RUN_ID}-resume`,
      rawEvent: { runId: `${RUN_ID}-resume`, initializationFailed: true },
      input: {
        threadId: THREAD_ID,
        runId: `${RUN_ID}-resume`,
        resume: [{
          interruptId: 'interrupt-init-failure',
          status: 'resolved',
          payload: { type: 'approve' },
        }],
      },
    })
    const failed = applyConversationEvent(initializationStarted, {
      type: 'RUN_ERROR',
      message: 'Runtime 初始化失败',
      code: 'runtime_initialization_error',
      rawEvent: { runId: `${RUN_ID}-resume`, initializationFailed: true },
    })

    expect(initializationStarted.runStatus).toBe('waiting_approval')
    expect(initializationStarted.approval).toEqual(interrupted.approval)
    expect(failed.runStatus).toBe('waiting_approval')
    expect(failed.approval).toEqual(interrupted.approval)
    expect(failed.messages.find(
      (message) => message.meta?.toolCallId === 'call-init-failure',
    )?.meta?.status).toBe('paused')
    expect(failed.notice).toEqual({ kind: 'error', content: 'Runtime 初始化失败' })
  })

  it('marks only the related subagent when its parent task result fails', () => {
    const initial = applyConversationEvent(
      buildEmptyConversation({
        threadId: THREAD_ID,
        now: '2026-08-05T00:00:00.000Z',
        model: 'GPT-5.5',
      }),
      { type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID },
    )
    const task = applyConversationEvent(initial, {
      type: 'TOOL_CALL_START',
      toolCallId: 'call-subagent-error',
      toolCallName: 'task',
      rawEvent: {
        streamMode: 'messages',
        source: { agentType: 'main', agentName: 'main', namespace: [] },
        runId: RUN_ID,
      },
    })
    const subRunId = 'subagent-55555555-5555-5555-8555-555555555555'
    const running = applyConversationEvent(task, {
      type: 'RAW',
      source: 'langgraph.tasks',
      rawEvent: { type: 'tasks', phase: 'start', ns: [] },
      event: {
        data: { id: 'graph-error', name: 'tools' },
        provenance: {
          kind: 'root',
          namespace: [],
          agentType: 'main',
          agentName: 'main',
          subagents: [{
            schema: 'tinkerfin.subagent-provenance.v1',
            subagentInvocationId: subRunId,
            namespace: ['tools:graph-error'],
            parentNamespace: [],
            graphTaskId: 'graph-error',
            agentName: 'researcher',
            parentToolCallId: 'call-subagent-error',
            description: '失败任务',
            requestRunId: RUN_ID,
          }],
        },
      },
    })

    const failed = applyConversationEvent(running, {
      type: 'TOOL_CALL_RESULT',
      toolCallId: 'call-subagent-error',
      messageId: 'call-subagent-error-result',
      content: '子智能体运行失败',
      role: 'tool',
      rawEvent: {
        streamMode: 'messages',
        source: { agentType: 'main', agentName: 'main', namespace: [] },
        runId: RUN_ID,
        relatedSubagentInvocationId: subRunId,
        toolResultStatus: 'error',
      },
    })

    const subagent = failed.messages.find(
      (message) => message.role === 'subagent' && message.meta?.subRunId === subRunId,
    )
    expect(subagent?.meta?.status).toBe('failed')
    expect(failed.runStatus).toBe('streaming')
    expect(failed.activeRunId).toBe(RUN_ID)
    expect(failed.messages.some((message) => message.role === 'error')).toBe(false)
  })

  it('replays the full event log when no snapshot is available', () => {
    const detail: ConversationHistoryDetail = {
      id: 1,
      threadId: THREAD_ID,
      title: '历史会话',
      status: 'idle',
      lastRunId: RUN_ID,
      lastSeq: 3,
      snapshotSeq: 0,
      snapshotVersion: 3,
      messageCount: 2,
      toolCallCount: 0,
      hasPendingInterrupt: false,
      pinned: false,
      snapshot: null,
      events: [
        {
          seq: 1,
          eventId: 'evt-1',
          eventType: 'RUN_STARTED',
          event: {
            type: 'RUN_STARTED',
            threadId: THREAD_ID,
            runId: RUN_ID,
            input: {
              threadId: THREAD_ID,
              runId: RUN_ID,
              messages: [{ id: 'user-1', role: 'user', content: '你好' }],
            },
          },
          createdAt: '2026-08-05T00:00:00.000Z',
        },
        {
          seq: 2,
          eventId: 'evt-2',
          eventType: 'TEXT_MESSAGE_CONTENT',
          event: {
            type: 'TEXT_MESSAGE_CONTENT',
            rawEvent: {
              streamMode: 'messages',
              source: { agentType: 'main', agentName: 'main', namespace: [] },
              runId: RUN_ID,
            },
            messageId: 'assistant-1',
            delta: '回放内容',
          },
          createdAt: '2026-08-05T00:00:00.000Z',
        },
        {
          seq: 3,
          eventId: 'evt-3',
          eventType: 'RUN_FINISHED',
          event: { type: 'RUN_FINISHED', threadId: THREAD_ID, runId: RUN_ID, outcome: { type: 'success' } },
          createdAt: '2026-08-05T00:00:00.000Z',
        },
      ],
      createdAt: '2026-08-05T00:00:00.000Z',
      updatedAt: '2026-08-05T00:00:00.000Z',
    }

    const restored = restoreConversationFromHistory(detail, { model: 'GPT-5.5' })
    expect(restored.threadId).toBe(THREAD_ID)
    expect(restored.runStatus).toBe('idle')
    expect(restored.lastSeq).toBe(3)
    // 完整事件日志同时重建用户消息和助手消息
    expect(restored.messages.map((message) => message.id)).toEqual(['user-1', 'assistant-1'])
    expect(restored.messages[1]?.content).toBe('回放内容')
  })

  it('restores current Tool approval data from a trusted v2 snapshot', () => {
    const interruptId = 'history-tool-interrupt#0'
    const toolCallId = 'history-tool-call'
    const originalArgs = {
      file_path: '/history-result.txt',
      content: 'HISTORY_APPROVAL_OK',
    }
    const params = JSON.stringify(originalArgs, null, 2)
    const detail: ConversationHistoryDetail = {
      id: 2,
      threadId: THREAD_ID,
      title: '待处理 Tool 审批',
      status: 'waiting_approval',
      lastRunId: RUN_ID,
      lastSeq: 7,
      snapshotSeq: 7,
      snapshotVersion: 3,
      messageCount: 0,
      toolCallCount: 1,
      hasPendingInterrupt: true,
      pinned: false,
      snapshot: {
        snapshotSeq: 7,
        snapshotVersion: 3,
        messages: [],
        todos: [],
        mode: 'default',
        approval: {
          items: [{
            id: interruptId,
            interruptId,
            toolCallId,
            toolName: 'write_file',
            params,
            input: params,
            description: '确认历史写入',
            originalArgs,
            allowedDecisions: ['approve', 'edit', 'reject'],
          }],
          activeIndex: 0,
          submitted: false,
        },
        runStatus: 'waiting_approval',
        activeRunId: null,
        serverState: {},
        runs: {},
        interrupts: [{
          id: interruptId,
          reason: 'tool_call',
          toolCallId,
          message: '确认历史写入',
          metadata: {
            langgraphValue: {
              action_requests: [{ name: 'write_file', args: originalArgs }],
              review_configs: [{
                action_name: 'write_file',
                allowed_decisions: ['approve', 'edit', 'reject'],
              }],
            },
            deepagents: {
              schema: 'tinkerfin.deepagents.tool-review.v1',
              nativeInterruptId: interruptId,
              actionIndex: 0,
              toolName: 'write_file',
              originalArgs,
              allowedDecisions: ['approve', 'edit', 'reject'],
            },
          },
          toolName: 'write_file',
          allowedDecisions: ['approve', 'edit', 'reject'],
          originalArgs,
        }],
      },
      events: [],
      createdAt: '2026-08-05T00:00:00.000Z',
      updatedAt: '2026-08-05T00:00:00.000Z',
    }

    const restored = restoreConversationFromHistory(detail, { model: 'GPT-5.5' })

    expect(restored.runStatus).toBe('waiting_approval')
    expect(restored.approval).toEqual(detail.snapshot?.approval)
    expect(JSON.parse(restored.approval?.items[0]?.params ?? '')).toEqual(originalArgs)
  })

  it('clears replayed approval when authoritative history has no pending interrupt', () => {
    const detail: ConversationHistoryDetail = {
      id: 2,
      threadId: THREAD_ID,
      title: '已解决审批',
      status: 'idle',
      lastRunId: RUN_ID,
      lastSeq: 1,
      snapshotSeq: 0,
      snapshotVersion: 3,
      messageCount: 0,
      toolCallCount: 0,
      hasPendingInterrupt: false,
      pinned: false,
      snapshot: null,
      events: [{
        seq: 1,
        eventId: 'evt-stale-interrupt',
        eventType: 'RUN_FINISHED',
        runId: RUN_ID,
        event: {
          type: 'RUN_FINISHED',
          threadId: THREAD_ID,
          runId: RUN_ID,
          outcome: {
            type: 'interrupt',
            interrupts: [interrupt({ id: 'interrupt-resolved' })],
          },
        },
        createdAt: '2026-08-05T00:00:00.000Z',
      }],
      createdAt: '2026-08-05T00:00:00.000Z',
      updatedAt: '2026-08-05T00:00:00.000Z',
    }

    const restored = restoreConversationFromHistory(detail, { model: 'GPT-5.5' })

    expect(restored.runStatus).toBe('idle')
    expect(restored.approval).toBeUndefined()
  })

  it('replays the full event log to restore tool cards, sub-agent cards and resolved HITL state', () => {
    const SUB_RUN_ID = 'subagent-66666666-6666-5666-8666-666666666666'
    const events: ConversationEventEnvelope[] = [
      {
        seq: 1,
        eventId: 'evt-run-start',
        eventType: 'RUN_STARTED',
        event: {
          type: 'RUN_STARTED',
          threadId: THREAD_ID,
          runId: RUN_ID,
          input: {
            threadId: THREAD_ID,
            runId: RUN_ID,
            messages: [{ id: 'user-1', role: 'user', content: '帮我搜索' }],
          },
        },
        createdAt: '2026-08-05T00:00:00.000Z',
      },
      // 工具调用卡由 start、args、end 和 result 事件完整重建
      {
        seq: 2,
        eventId: 'evt-tool-start',
        eventType: 'TOOL_CALL_START',
        event: {
          type: 'TOOL_CALL_START',
          rawEvent: {
            streamMode: 'messages',
            source: { agentType: 'main', agentName: 'main', namespace: [] },
            runId: RUN_ID,
          },
          toolCallId: 'tool-search',
          toolCallName: 'search',
          parentMessageId: 'assistant-1',
        },
        createdAt: '2026-08-05T00:00:00.000Z',
      },
      {
        seq: 3,
        eventId: 'evt-tool-args',
        eventType: 'TOOL_CALL_ARGS',
        event: {
          type: 'TOOL_CALL_ARGS',
          rawEvent: {
            streamMode: 'messages',
            source: { agentType: 'main', agentName: 'main', namespace: [] },
            runId: RUN_ID,
          },
          toolCallId: 'tool-search',
          delta: '{"q":"hi"}',
        },
        createdAt: '2026-08-05T00:00:00.000Z',
      },
      {
        seq: 4,
        eventId: 'evt-tool-end',
        eventType: 'TOOL_CALL_END',
        event: {
          type: 'TOOL_CALL_END',
          rawEvent: {
            streamMode: 'messages',
            source: { agentType: 'main', agentName: 'main', namespace: [] },
            runId: RUN_ID,
          },
          toolCallId: 'tool-search',
        },
        createdAt: '2026-08-05T00:00:00.000Z',
      },
      {
        seq: 5,
        eventId: 'evt-tool-result',
        eventType: 'TOOL_CALL_RESULT',
        event: {
          type: 'TOOL_CALL_RESULT',
          rawEvent: {
            streamMode: 'messages',
            source: { agentType: 'main', agentName: 'main', namespace: [] },
            runId: RUN_ID,
          },
          toolCallId: 'tool-search',
          messageId: 'tool-result-search',
          content: '结果',
          role: 'tool',
        },
        createdAt: '2026-08-05T00:00:00.000Z',
      },
      // 子智能体运行按完整卡片结构重建
      {
        seq: 6,
        eventId: 'evt-sub-start',
        eventType: 'RAW',
        event: {
          type: 'RAW',
          source: 'langgraph.tasks',
          rawEvent: { type: 'tasks', phase: 'start', ns: [] },
          event: {
            data: { id: 'task-0', name: 'tools' },
            provenance: {
              kind: 'root',
              namespace: [],
              agentType: 'main',
              agentName: 'main',
              subagents: [{
                schema: 'tinkerfin.subagent-provenance.v1',
                subagentInvocationId: SUB_RUN_ID,
                namespace: ['tools:task-0'],
                parentNamespace: [],
                graphTaskId: 'task-0',
                agentName: 'researcher',
                parentToolCallId: 'tool-task',
                description: '帮我搜索',
                requestRunId: RUN_ID,
              }],
            },
          },
        },
        createdAt: '2026-08-05T00:00:00.000Z',
      },
      // HITL interrupt 先建立待审批状态
      {
        seq: 7,
        eventId: 'evt-run-interrupt',
        eventType: 'RUN_FINISHED',
        event: {
          type: 'RUN_FINISHED',
          threadId: THREAD_ID,
          runId: RUN_ID,
          outcome: {
            type: 'interrupt',
            interrupts: [interrupt({ id: 'interrupt-write', toolCallId: 'tool-write' })],
          },
        },
        createdAt: '2026-08-05T00:00:00.000Z',
      },
      // 审批后的恢复运行成功结束时必须清除审批，避免卡片再次出现
      {
        seq: 8,
        eventId: 'evt-resume-start',
        eventType: 'RUN_STARTED',
        event: { type: 'RUN_STARTED', threadId: THREAD_ID, runId: `${RUN_ID}-resume` },
        createdAt: '2026-08-05T00:00:00.000Z',
      },
      {
        seq: 9,
        eventId: 'evt-resume-finish',
        eventType: 'RUN_FINISHED',
        event: { type: 'RUN_FINISHED', threadId: THREAD_ID, runId: `${RUN_ID}-resume`, outcome: { type: 'success' } },
        createdAt: '2026-08-05T00:00:00.000Z',
      },
    ]
    const detail: ConversationHistoryDetail = {
      id: 3,
      threadId: THREAD_ID,
      title: '工具+子Agent+HITL会话',
      status: 'idle',
      lastSeq: 9,
      snapshotSeq: 0,
      snapshotVersion: 3,
      messageCount: 1,
      toolCallCount: 1,
      hasPendingInterrupt: false,
      pinned: false,
      snapshot: null,
      events,
      createdAt: '2026-08-05T00:00:00.000Z',
      updatedAt: '2026-08-05T00:00:00.000Z',
    }

    const restored = restoreConversationFromHistory(detail, { model: 'GPT-5.5' })
    expect(restored.lastSeq).toBe(9)

    // 工具卡保留完整 meta
    const tool = restored.messages.find((message) => message.role === 'tool' && message.meta?.toolCallId === 'tool-search')
    expect(tool).toBeDefined()
    expect(tool?.meta?.toolName).toBe('search')
    expect(tool?.meta?.params).toBe('{"q":"hi"}')
    expect(tool?.meta?.result).toBe('结果')
    expect(tool?.meta?.status).toBe('completed')

    // 子智能体卡按运行标识重建
    const subagent = restored.messages.find((message) => message.role === 'subagent' && message.meta?.subRunId === SUB_RUN_ID)
    expect(subagent).toBeDefined()

    // 恢复运行成功结束后清除 HITL 审批，不保留陈旧待审批卡片
    expect(restored.approval).toBeUndefined()
  })

  it('hydrates the v3 UI snapshot with complete sub-agent input and applies only tail events', () => {
    const snapshotCreatedAt = '2026-08-05T00:00:00.000Z'
    const tailCreatedAt = '2026-08-05T00:00:01.000Z'
    const detail: ConversationHistoryDetail = {
      id: 4,
      threadId: THREAD_ID,
      title: 'v3 快照',
      status: 'idle',
      lastRunId: RUN_ID,
      lastSeq: 12,
      snapshotSeq: 10,
      snapshotVersion: 3,
      messageCount: 3,
      toolCallCount: 1,
      hasPendingInterrupt: false,
      pinned: false,
      snapshot: {
        snapshotSeq: 10,
        snapshotVersion: 3,
        messages: [
          { id: 'user-v3', role: 'user', content: '研究一下', createdAt: snapshotCreatedAt },
          {
            id: 'subagent-v3',
            role: 'subagent',
            content: '研究任务',
            createdAt: snapshotCreatedAt,
            meta: {
              agentName: 'researcher',
              input: '对比 A 与 B，并给出处',
              status: 'completed',
              subRunId: `${RUN_ID}:sub:task-v3`,
              graphTaskId: 'task-v3',
            },
          },
          { id: 'assistant-v3', role: 'assistant', content: '已有', createdAt: snapshotCreatedAt },
        ],
        todos: [
          {
            id: 'todo-v3',
            content: '历史 Todo',
            status: 'completed',
            targetMessageId: 'tool-write-todos-v3',
          },
        ],
        mode: 'plan',
        approval: null,
        runStatus: 'idle',
        activeRunId: null,
        serverState: {},
        runs: {},
        interrupts: [],
      },
      events: [
        {
          seq: 11,
          eventId: 'evt-tail-content',
          eventType: 'TEXT_MESSAGE_CONTENT',
          event: {
            type: 'TEXT_MESSAGE_CONTENT',
            messageId: 'assistant-v3',
            delta: '内容',
          },
          createdAt: tailCreatedAt,
        },
        {
          seq: 12,
          eventId: 'evt-tail-finish',
          eventType: 'RUN_FINISHED',
          event: { type: 'RUN_FINISHED', threadId: THREAD_ID, runId: RUN_ID },
          createdAt: tailCreatedAt,
        },
      ],
      createdAt: snapshotCreatedAt,
      updatedAt: tailCreatedAt,
    }

    const restored = restoreConversationFromHistory(detail, { model: 'GPT-5.5' })
    const subagent = restored.messages.find((message) => message.id === 'subagent-v3')
    expect(restored.todos[0]?.targetMessageId).toBeUndefined()
    const assistant = restored.messages.find((message) => message.id === 'assistant-v3')

    expect(subagent?.meta?.input).toBe('对比 A 与 B，并给出处')
    expect(assistant?.content).toBe('已有内容')
    expect(restored.messages.filter((message) => message.id === 'assistant-v3')).toHaveLength(1)
    expect(restored.mode).toBe('plan')
    expect(restored.lastSeq).toBe(12)
    expect(restored.runStatus).toBe('idle')
  })

  it('rejects non-v3 and internally inconsistent history snapshots', () => {
    const detail: ConversationHistoryDetail = {
      id: 40,
      threadId: THREAD_ID,
      title: '严格 v3 快照',
      status: 'idle',
      lastSeq: 1,
      snapshotSeq: 1,
      snapshotVersion: 3,
      messageCount: 0,
      toolCallCount: 0,
      hasPendingInterrupt: false,
      pinned: false,
      snapshot: {
        snapshotSeq: 1,
        snapshotVersion: 3,
        messages: [],
        todos: [],
        mode: 'default',
        approval: null,
        runStatus: 'idle',
        activeRunId: null,
        serverState: {},
        runs: {},
        interrupts: [],
      },
      events: [],
      createdAt: '2026-08-05T00:00:00.000Z',
      updatedAt: '2026-08-05T00:00:00.000Z',
    }
    const nonCurrentDetail = {
      ...detail,
      snapshotVersion: 2,
    } as unknown as ConversationHistoryDetail
    const nonCurrentSnapshot = {
      ...detail,
      snapshot: { ...detail.snapshot!, snapshotVersion: 2 },
    } as unknown as ConversationHistoryDetail
    const mismatchedSequence = {
      ...detail,
      snapshot: { ...detail.snapshot!, snapshotSeq: 0 },
    } as ConversationHistoryDetail
    const missingSnapshot = {
      ...detail,
      snapshot: null,
    } as ConversationHistoryDetail

    expect(() => restoreConversationFromHistory(nonCurrentDetail, { model: 'GPT-5.5' }))
      .toThrow('会话历史只接受 snapshotVersion=3')
    expect(() => restoreConversationFromHistory(nonCurrentSnapshot, { model: 'GPT-5.5' }))
      .toThrow('会话历史快照只接受 snapshotVersion=3')
    expect(() => restoreConversationFromHistory(mismatchedSequence, { model: 'GPT-5.5' }))
      .toThrow('会话历史快照序号与详情不一致')
    expect(() => restoreConversationFromHistory(missingSnapshot, { model: 'GPT-5.5' }))
      .toThrow('空会话快照必须对应 snapshotSeq=0')
  })

  it('uses persisted event timestamps for restored message timing', () => {
    const startedAt = '2026-08-05T08:00:00.000Z'
    const completedAt = '2026-08-05T08:00:02.500Z'
    const detail: ConversationHistoryDetail = {
      id: 5,
      threadId: THREAD_ID,
      title: '事件时间',
      status: 'idle',
      lastSeq: 4,
      snapshotSeq: 0,
      snapshotVersion: 3,
      messageCount: 1,
      toolCallCount: 1,
      hasPendingInterrupt: false,
      pinned: false,
      snapshot: null,
      events: [
        {
          seq: 1,
          eventId: 'evt-tool-start-time',
          eventType: 'TOOL_CALL_START',
          event: { type: 'TOOL_CALL_START', toolCallId: 'tool-time', toolCallName: 'clock' },
          createdAt: startedAt,
        },
        {
          seq: 2,
          eventId: 'evt-tool-end-time',
          eventType: 'TOOL_CALL_END',
          event: { type: 'TOOL_CALL_END', toolCallId: 'tool-time' },
          createdAt: completedAt,
        },
        {
          seq: 3,
          eventId: 'evt-tool-result-time',
          eventType: 'TOOL_CALL_RESULT',
          event: {
            type: 'TOOL_CALL_RESULT',
            toolCallId: 'tool-time',
            messageId: 'tool-result-time',
            content: 'done',
            role: 'tool',
          },
          createdAt: completedAt,
        },
        {
          seq: 4,
          eventId: 'evt-run-finish-time',
          eventType: 'RUN_FINISHED',
          event: { type: 'RUN_FINISHED', threadId: THREAD_ID, runId: RUN_ID },
          createdAt: completedAt,
        },
      ],
      createdAt: startedAt,
      updatedAt: completedAt,
    }

    const restored = restoreConversationFromHistory(detail, { model: 'GPT-5.5' })
    const tool = restored.messages.find((message) => message.meta?.toolCallId === 'tool-time')
    expect(tool?.createdAt).toBe(startedAt)
    expect(tool?.meta?.completedAt).toBe(completedAt)
    expect(tool?.meta?.durationMs).toBe(2500)
  })

  it('hydrates a server-running history as detached because no live SSE connection is owned', () => {
    const detail: ConversationHistoryDetail = {
      id: 6,
      threadId: THREAD_ID,
      title: '仍在后端运行',
      status: 'running',
      lastRunId: RUN_ID,
      lastSeq: 1,
      snapshotSeq: 0,
      snapshotVersion: 3,
      messageCount: 0,
      toolCallCount: 0,
      hasPendingInterrupt: false,
      pinned: false,
      snapshot: null,
      events: [
        {
          seq: 1,
          eventId: 'evt-running-start',
          eventType: 'RUN_STARTED',
          event: {
            type: 'RUN_STARTED',
            threadId: THREAD_ID,
            runId: RUN_ID,
            input: {
              threadId: THREAD_ID,
              runId: RUN_ID,
              messages: [{ id: 'user-running', role: 'user', content: '仍在处理吗？' }],
            },
          },
          createdAt: '2026-08-05T08:00:00.000Z',
        },
      ],
      createdAt: '2026-08-05T08:00:00.000Z',
      updatedAt: '2026-08-05T08:00:00.000Z',
    }

    const restored = restoreConversationFromHistory(detail, { model: 'GPT-5.5' })
    expect(restored.runStatus).toBe('detached')
    expect(restored.activeRunId).toBe(RUN_ID)
  })

  it('keeps persisted catch-up events detached when a later RUN_STARTED has no owned SSE connection', () => {
    const initial: Conversation = {
      ...buildEmptyConversation({
        threadId: THREAD_ID,
        now: '2026-08-05T08:00:00.000Z',
        model: 'GPT-5.5',
      }),
      runStatus: 'detached',
      lastSeq: 4,
    }

    const caughtUp = applyHistoryEventEnvelope(initial, {
      seq: 5,
      eventId: 'evt-catch-up-start',
      eventType: 'RUN_STARTED',
      event: {
        type: 'RUN_STARTED',
        threadId: THREAD_ID,
        runId: RUN_ID,
        input: {
          threadId: THREAD_ID,
          runId: RUN_ID,
          messages: [{ id: 'user-catch-up', role: 'user', content: '继续执行' }],
        },
      },
      createdAt: '2026-08-05T08:01:00.000Z',
    })

    expect(caughtUp.runStatus).toBe('detached')
    expect(caughtUp.activeRunId).toBe(RUN_ID)
    expect(caughtUp.lastSeq).toBe(5)
  })

  it('deduplicates persisted seq and rejects an event gap', () => {
    const initial: Conversation = {
      ...buildEmptyConversation({
        threadId: THREAD_ID,
        now: '2026-08-05T08:00:00.000Z',
        model: 'GPT-5.5',
      }),
      lastSeq: 1,
    }
    const duplicate: ConversationEventEnvelope = {
      seq: 1,
      eventId: 'evt-duplicate',
      eventType: 'TEXT_MESSAGE_CONTENT',
      event: {
        type: 'TEXT_MESSAGE_CONTENT',
        messageId: 'message-duplicate',
        delta: '不应重复归约',
      },
      createdAt: '2026-08-05T08:00:01.000Z',
    }
    const gap: ConversationEventEnvelope = {
      ...duplicate,
      seq: 3,
      eventId: 'evt-gap',
    }

    expect(applyHistoryEventEnvelope(initial, duplicate)).toBe(initial)
    expect(() => applyHistoryEventEnvelope(initial, gap)).toThrow(
      '会话事件序号不连续: expected=2, actual=3',
    )
  })

  it('maps Plan clarification options without creating a Tool approval', () => {
    const current = buildEmptyConversation({
      threadId: THREAD_ID,
      now: '2026-08-05T08:00:00.000Z',
      model: 'GPT-5.5',
      mode: 'plan',
    })
    const interrupted = applyConversationEvent(current, {
      type: 'RUN_FINISHED',
      threadId: THREAD_ID,
      runId: RUN_ID,
      outcome: {
        type: 'interrupt',
        interrupts: [{
          id: 'plan-question-1',
          reason: 'plan_clarification',
          metadata: {
            runtimeInterrupt: {
              envelope: {
                metadata: {
                  origin: 'plan',
                  clarification: {
                    schema: 'tinkerfin.plan-clarification.v1',
                    form: {
                      schemaVersion: 1,
                      businessTag: 'preserved',
                      questions: [{
                        id: 'environment',
                        prompt: '部署到哪个环境？',
                        options: [{
                          id: 'staging',
                          label: '预发布',
                          description: '先验证',
                          attributes: { priority: 1 },
                        }],
                        allowFreeText: true,
                        attributes: { category: 'target' },
                      }],
                    },
                  },
                },
              },
            },
          },
        }],
      },
    })

    expect(interrupted.approval).toBeUndefined()
    expect(interrupted.planInteraction?.kind).toBe('questions')
    if (interrupted.planInteraction?.kind !== 'questions') throw new Error('missing questions')
    expect(interrupted.planInteraction.form.businessTag).toBe('preserved')
    expect(interrupted.planInteraction.questions[0]?.attributes).toEqual({ category: 'target' })
    expect(interrupted.planInteraction.questions[0]?.options[0]?.attributes).toEqual({ priority: 1 })
    const ready = {
      ...interrupted,
      planInteraction: {
        ...interrupted.planInteraction,
        questions: interrupted.planInteraction.questions.map((question) => ({
          ...question,
          selectedOptionId: 'staging',
        })),
      },
    }
    const payload = buildPlanResumePayload(ready)
    expect(payload.forwardedProps.mode).toBe('plan')
    expect(payload.resume?.[0]).toMatchObject({
      interruptId: 'plan-question-1',
      status: 'resolved',
      payload: {
        type: 'respond',
        answers: [{
          questionId: 'environment',
          optionId: 'staging',
        }],
      },
    })
    const freeTextReady = {
      ...interrupted,
      planInteraction: {
        ...interrupted.planInteraction,
        questions: interrupted.planInteraction.questions.map((question) => ({
          ...question,
          customAnswer: '隔离环境',
        })),
      },
    }
    const freeTextPayload = buildPlanResumePayload(freeTextReady)
    expect(freeTextPayload.resume?.[0]?.payload).toEqual({
      type: 'respond',
      answers: [{ questionId: 'environment', answer: '隔离环境' }],
    })
  })

  it('preserves and submits every question in a larger Plan clarification form', () => {
    const questions = Array.from({ length: 4 }, (_, index) => ({
      id: `question-${index}`,
      prompt: `第 ${index + 1} 个问题？`,
      options: index % 2 === 0
        ? [{ id: `option-${index}`, label: `选项 ${index + 1}` }]
        : [],
      allowFreeText: true,
    }))
    const current = buildEmptyConversation({
      threadId: THREAD_ID,
      now: '2026-08-05T08:00:00.000Z',
      model: 'GPT-5.5',
      mode: 'plan',
    })
    const interrupted = applyConversationEvent(current, {
      type: 'RUN_FINISHED',
      threadId: THREAD_ID,
      runId: RUN_ID,
      outcome: {
        type: 'interrupt',
        interrupts: [{
          id: 'plan-question-large',
          reason: 'plan_clarification',
          metadata: {
            runtimeInterrupt: {
              envelope: {
                metadata: {
                  origin: 'plan',
                  clarification: {
                    schema: 'tinkerfin.plan-clarification.v1',
                    form: { schemaVersion: 1, questions },
                  },
                },
              },
            },
          },
        }],
      },
    })

    expect(interrupted.planInteraction?.kind).toBe('questions')
    if (interrupted.planInteraction?.kind !== 'questions') throw new Error('missing questions')
    expect(interrupted.planInteraction.questions).toHaveLength(4)
    const ready: Conversation = {
      ...interrupted,
      planInteraction: {
        ...interrupted.planInteraction,
        questions: interrupted.planInteraction.questions.map((question, index) => ({
          ...question,
          selectedOptionId: index % 2 === 0 ? `option-${index}` : undefined,
          customAnswer: index % 2 === 0 ? '' : `答案 ${index + 1}`,
        })),
      },
    }
    const payload = buildPlanResumePayload(ready)
    expect(payload.resume?.[0]?.payload).toEqual({
      type: 'respond',
      answers: [
        { questionId: 'question-0', optionId: 'option-0' },
        { questionId: 'question-1', answer: '答案 2' },
        { questionId: 'question-2', optionId: 'option-2' },
        { questionId: 'question-3', answer: '答案 4' },
      ],
    })
  })

  it('maps Plan review decisions and abandons only the Plan request', () => {
    const current = buildEmptyConversation({
      threadId: THREAD_ID,
      now: '2026-08-05T08:00:00.000Z',
      model: 'GPT-5.5',
      mode: 'plan',
    })
    const interrupted = applyConversationEvent(current, {
      type: 'RUN_FINISHED',
      threadId: THREAD_ID,
      runId: RUN_ID,
      outcome: {
        type: 'interrupt',
        interrupts: [{
          id: 'plan-review-1',
          reason: 'plan_review',
          metadata: {
            runtimeInterrupt: {
              envelope: {
                metadata: {
                  planRevision: 2,
                  draft: {
                    revision: 2,
                    goal: '实现模式切换',
                    steps: [{ id: 'step-1', title: '实现', description: '完成实现' }],
                  },
                },
              },
            },
          },
        }],
      },
    })
    expect(interrupted.planInteraction?.kind).toBe('review')
    if (interrupted.planInteraction?.kind !== 'review') throw new Error('missing review')
    const approved = {
      ...interrupted,
      planInteraction: { ...interrupted.planInteraction, action: 'approve' as const },
    }

    const approvalPayload = buildPlanResumePayload(approved)
    expect(approvalPayload.forwardedProps.mode).toBe('default')
    expect(approvalPayload.resume?.[0]?.payload).toEqual({
      type: 'approve',
      baseRevision: 2,
    })
    expect(buildPlanAbandonPayload(interrupted)).toMatchObject({
      messages: [],
      forwardedProps: { model: 'GPT-5.5', mode: 'default' },
      resume: [{ interruptId: 'plan-review-1', status: 'cancelled' }],
    })
  })

  it('builds every fixed Plan review action without changing the action vocabulary', () => {
    const base = buildEmptyConversation({
      threadId: THREAD_ID,
      now: '2026-08-05T08:00:00.000Z',
      model: 'GPT-5.5',
      mode: 'plan',
    })
    const interaction: PlanReviewState = {
      kind: 'review',
      interruptId: 'plan-review-actions',
      revision: 4,
      submitted: false,
      draft: {
        schemaVersion: 1,
        revision: 4,
        goal: '实现四种动作',
        steps: [{ id: 'step-1', title: '实现', description: '实现合同' }],
      },
    }
    const payloadFor = (patch: Partial<PlanReviewState>) => buildPlanResumePayload({
      ...base,
      planInteraction: { ...interaction, ...patch },
    })

    expect(payloadFor({ action: 'approve' }).resume?.[0]?.payload).toEqual({
      type: 'approve',
      baseRevision: 4,
    })
    expect(payloadFor({
      action: 'edit',
      editedDraft: JSON.stringify({
        schemaVersion: 1,
        revision: 99,
        goal: '编辑后',
        steps: [{ id: 'step-1', title: '编辑', description: '编辑合同' }],
      }),
    }).resume?.[0]?.payload).toEqual({
      type: 'edit',
      baseRevision: 4,
      draft: {
        goal: '编辑后',
        steps: [{ id: 'step-1', title: '编辑', description: '编辑合同' }],
      },
    })
    expect(payloadFor({ action: 'respond', message: '补充回归验证' }).resume?.[0]?.payload).toEqual({
      type: 'respond',
      baseRevision: 4,
      message: '补充回归验证',
    })
    expect(payloadFor({ action: 'reject', message: '目标不再需要' }).resume?.[0]?.payload).toEqual({
      type: 'reject',
      baseRevision: 4,
      message: '目标不再需要',
    })
  })
})
