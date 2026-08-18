import { describe, expect, it } from 'vitest'

import type { ConversationAgUiEvent, InterruptEvent } from '../../../api/conversation/types'
import type { ConversationEventEnvelope, ConversationHistoryDetail } from '../../../api/conversation/history'
import { buildEmptyConversation } from '../../../lib/workspace'
import type { ApprovalItem, Conversation, Message, WorkspaceState } from '../../../types'
import { applyConversationEvent, applyHistoryEventEnvelope, buildResumePayload, markConversationDetached, normalizeWorkspace, prepareResumeSubmission, restoreConversationFromHistory } from './runtime'

const THREAD_ID = 'thread-order-check'
const RUN_ID = 'run-order-check'

function nativeContractEvents(): ConversationAgUiEvent[] {
  const subRunId = `${RUN_ID}:sub:graph-research`
  const mainSource = { agentType: 'main' as const, agentName: 'main', namespace: [] }
  const subSource = {
    agentType: 'subagent' as const,
    agentName: 'researcher',
    namespace: ['tools:graph-research'],
    graphTaskId: 'graph-research',
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
      type: 'RUN_STARTED',
      threadId: THREAD_ID,
      runId: subRunId,
      rawEvent: {
        streamMode: 'tasks',
        source: subSource,
        runId: subRunId,
        parentAgentRunId: RUN_ID,
        parentToolCallId: 'call-task',
        subagentInput: '研究百度与 Google',
      },
    },
    {
      type: 'TOOL_CALL_START',
      toolCallId: 'call-read',
      toolCallName: 'read_file',
      rawEvent: { streamMode: 'messages', source: subSource, runId: subRunId },
    },
    {
      type: 'TOOL_CALL_RESULT',
      toolCallId: 'call-read',
      messageId: 'message-read',
      content: '百度与 Google 调研资料',
      role: 'tool',
      rawEvent: { streamMode: 'messages', source: subSource, runId: subRunId },
    },
    {
      type: 'RUN_FINISHED',
      threadId: THREAD_ID,
      runId: subRunId,
      outcome: { type: 'success' },
      rawEvent: { streamMode: 'messages', source: subSource, runId: subRunId },
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
        relatedRunId: subRunId,
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
  return {
    id: overrides.id,
    reason: overrides.reason ?? 'tool_call',
    message: overrides.message ?? `审批 ${overrides.id}`,
    toolCallId: overrides.toolCallId,
    responseSchema: overrides.responseSchema,
    metadata: overrides.metadata ?? {
      deepagents: {
        toolName: 'write_file',
        allowedDecisions: ['approve', 'edit', 'reject'],
        originalArgs: {
          file_path: `${overrides.id}.txt`,
          content: overrides.id,
        },
      },
    },
  }
}

describe('AG-UI runtime reducer', () => {
  it('uses server-owned RAW task identities for live subagent cards and child tools', () => {
    const subRunId = 'subrun-server-owned'
    const mainSource = { agentType: 'main' as const, agentName: 'main', namespace: [] }
    const subSource = {
      agentType: 'subagent' as const,
      agentName: 'researcher',
      namespace: ['tools:graph-server'],
      graphTaskId: 'graph-server',
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
              namespace: ['tools:graph-server'],
              graphTaskId: 'graph-server',
              agentName: 'researcher',
              parentToolCallId: 'call-task-server',
              description: '检索 LangGraph',
              runId: subRunId,
              parentAgentRunId: RUN_ID,
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
          runId: subRunId,
          parentAgentRunId: RUN_ID,
          parentToolCallId: 'call-task-server',
          subagentInput: '检索 LangGraph',
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
          runId: subRunId,
          parentAgentRunId: RUN_ID,
          parentToolCallId: 'call-task-server',
          subagentInput: '检索 LangGraph',
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
          relatedRunId: subRunId,
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
      parentRunId: RUN_ID,
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

  it('isolates parallel server subruns that share one graph task id', () => {
    const mainSource = { agentType: 'main' as const, agentName: 'main', namespace: [] }
    const graphTaskId = 'shared-graph-task'
    const descriptors = ['a', 'b'].map((suffix) => ({
      namespace: [`tools:${graphTaskId}:${suffix}`],
      graphTaskId,
      agentName: 'researcher',
      parentToolCallId: `call-task-${suffix}`,
      description: `研究任务 ${suffix.toUpperCase()}`,
      runId: `subrun-server-${suffix}`,
      parentAgentRunId: RUN_ID,
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
      }
      return [
        {
          type: 'TEXT_MESSAGE_START',
          messageId: `message-${descriptor.runId}`,
          role: 'assistant',
          rawEvent: {
            streamMode: 'messages',
            source,
            runId: descriptor.runId,
            parentAgentRunId: RUN_ID,
          },
        },
        {
          type: 'TEXT_MESSAGE_CONTENT',
          messageId: `message-${descriptor.runId}`,
          delta: `结果 ${descriptor.runId}`,
          rawEvent: {
            streamMode: 'messages',
            source,
            runId: descriptor.runId,
            parentAgentRunId: RUN_ID,
          },
        },
      ]
    }).reduce(applyConversationEvent, initial)
    const subagents = withText.messages.filter((message) => message.role === 'subagent')

    expect(subagents).toHaveLength(2)
    expect(subagents.map((message) => [message.meta?.subRunId, message.meta?.result])).toEqual([
      ['subrun-server-a', '结果 subrun-server-a'],
      ['subrun-server-b', '结果 subrun-server-b'],
    ])
  })

  it('fails a discovered subrun and its child tools when the main run errors', () => {
    const subRunId = 'subrun-main-error'
    const mainSource = { agentType: 'main' as const, agentName: 'main', namespace: [] }
    const subSource = {
      agentType: 'subagent' as const,
      agentName: 'researcher',
      namespace: ['tools:graph-main-error'],
      graphTaskId: 'graph-main-error',
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
              namespace: subSource.namespace,
              graphTaskId: subSource.graphTaskId,
              agentName: subSource.agentName,
              parentToolCallId: 'call-task-main-error',
              description: '执行研究',
              runId: subRunId,
              parentAgentRunId: RUN_ID,
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
          runId: subRunId,
          parentAgentRunId: RUN_ID,
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
        todos: [
          { content: '读取 url.json', status: 'completed' },
          { content: '写入 result.txt', status: 'in_progress' },
        ],
      },
    })
    const afterDelta = applyConversationEvent(afterState, {
      type: 'STATE_DELTA',
      delta: [{ op: 'replace', path: '/todos/1/status', value: 'completed' }],
    })
    const completedTool = afterDelta.messages.find(
      (item) => item.role === 'tool' && item.meta?.toolCallId === 'call-write-todos-test',
    )

    expect(completedTool?.meta?.status).toBe('completed')
    expect(completedTool?.meta?.result).toBe(rawContent)
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

    const subRunId = `${RUN_ID}:sub:graph-reasoning`
    const withSubagent = applyConversationEvent(initial, {
      type: 'RUN_STARTED',
      threadId: THREAD_ID,
      runId: subRunId,
      rawEvent: {
        streamMode: 'messages',
        source: {
          agentType: 'subagent',
          agentName: 'researcher',
          namespace: ['tools:graph-reasoning'],
          graphTaskId: 'graph-reasoning',
        },
        runId: subRunId,
        parentAgentRunId: RUN_ID,
      },
    })
    const subRawEvent = {
      streamMode: 'messages' as const,
      source: {
        agentType: 'subagent' as const,
        agentName: 'researcher',
        namespace: ['tools:graph-reasoning'],
        graphTaskId: 'graph-reasoning',
      },
      runId: subRunId,
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
    const subSource = (graphTaskId: string) => ({
      agentType: 'subagent' as const,
      agentName: 'researcher',
      namespace: [`tools:${graphTaskId}`],
      graphTaskId,
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

    for (const suffix of ['a', 'b']) {
      const graphTaskId = `graph-${suffix}`
      const subRunId = `${RUN_ID}:sub:${graphTaskId}`
      const subRunContext = {
        streamMode: 'messages' as const,
        source: subSource(graphTaskId),
        runId: subRunId,
        parentAgentRunId: RUN_ID,
        parentToolCallId: `task-${suffix}`,
        subagentInput: `研究任务 ${suffix.toUpperCase()}`,
      }
      apply({
        type: 'RUN_STARTED',
        threadId: THREAD_ID,
        runId: subRunId,
        rawEvent: subRunContext,
      })
      apply({
        type: 'TOOL_CALL_START',
        rawEvent: {
          streamMode: 'messages',
          source: subSource(graphTaskId),
          runId: subRunId,
        },
        toolCallId: `child-tool-${suffix}`,
        toolCallName: 'web_search',
        parentMessageId: `child-message-${suffix}`,
      })
    }

    const runningSubagents = current.messages.filter((message) => message.role === 'subagent')
    const runningSubagentA = runningSubagents.find((message) => message.meta?.subRunId?.endsWith('graph-a'))
    const runningSubagentB = runningSubagents.find((message) => message.meta?.subRunId?.endsWith('graph-b'))
    const runningTaskA = current.messages.find((message) => message.meta?.toolCallId === 'task-a')
    const runningTaskB = current.messages.find((message) => message.meta?.toolCallId === 'task-b')

    expect(runningSubagentA?.meta?.input).toBe('研究任务 A')
    expect(runningSubagentA?.meta?.toolCallId).toBe('task-a')
    expect(runningSubagentB?.meta?.input).toBe('研究任务 B')
    expect(runningSubagentB?.meta?.toolCallId).toBe('task-b')
    expect(runningTaskA?.meta?.subRunId).toBe(`${RUN_ID}:sub:graph-a`)
    expect(runningTaskB?.meta?.subRunId).toBe(`${RUN_ID}:sub:graph-b`)

    apply({
      type: 'RUN_FINISHED',
      threadId: THREAD_ID,
      runId: `${RUN_ID}:sub:graph-b`,
      outcome: { type: 'success' },
    })
    expect(current.runStatus).toBe('streaming')

    for (const suffix of ['b', 'a']) {
      apply({
        type: 'TOOL_CALL_RESULT',
        rawEvent: {
          streamMode: 'messages',
          source: mainSource,
          runId: RUN_ID,
          relatedRunId: `${RUN_ID}:sub:graph-${suffix}`,
        },
        messageId: `task-result-${suffix}`,
        toolCallId: `task-${suffix}`,
        content: `最终结果 ${suffix.toUpperCase()}`,
        role: 'tool',
      })
    }

    const subagents = current.messages.filter((message) => message.role === 'subagent')
    const subagentA = subagents.find((message) => message.meta?.subRunId?.endsWith('graph-a'))
    const subagentB = subagents.find((message) => message.meta?.subRunId?.endsWith('graph-b'))
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

  it('keeps subagent normalization idempotent across refreshes and preserves sibling runs', () => {
    const conversation = buildEmptyConversation({
      threadId: 'thread-refresh-subagents',
      now: '2026-08-05T00:00:00.000Z',
      model: 'GPT-5.5',
    })
    const messages = ['a', 'b'].flatMap<Message>((suffix) => {
      const subRunId = `${RUN_ID}:sub:graph-${suffix}`
      const sharedMeta = {
        agentName: 'researcher',
        subRunId,
        runId: subRunId,
        parentRunId: RUN_ID,
        graphTaskId: `graph-${suffix}`,
        status: 'completed' as const,
      }
      return [
        {
          id: `legacy-task-card-${suffix}`,
          role: 'subagent',
          content: `研究任务 ${suffix.toUpperCase()}`,
          createdAt: `2026-08-05T00:00:0${suffix === 'a' ? '1' : '3'}.000Z`,
          meta: {
            ...sharedMeta,
            toolName: 'task',
            toolCallId: `task-${suffix}`,
          },
        },
        {
          id: `subagent-${subRunId}`,
          role: 'subagent',
          content: `研究任务 ${suffix.toUpperCase()}`,
          createdAt: `2026-08-05T00:00:0${suffix === 'a' ? '2' : '4'}.000Z`,
          meta: {
            ...sharedMeta,
            input: `研究任务 ${suffix.toUpperCase()}`,
            result: `最终结果 ${suffix.toUpperCase()}`,
          },
        },
      ]
    })
    const workspace: WorkspaceState = {
      conversations: [{ ...conversation, messages }],
      currentThreadId: conversation.threadId,
    }

    const once = normalizeWorkspace(workspace)
    const twice = normalizeWorkspace(JSON.parse(JSON.stringify(once)) as WorkspaceState)
    const subagents = twice.conversations[0].messages.filter((message) => message.role === 'subagent')
    const taskTools = twice.conversations[0].messages.filter(
      (message) => message.role === 'tool' && message.meta?.toolName === 'task',
    )

    expect(subagents).toHaveLength(2)
    expect(taskTools).toHaveLength(2)
    expect(taskTools.map((message) => message.meta?.subRunId)).toEqual([
      `${RUN_ID}:sub:graph-a`,
      `${RUN_ID}:sub:graph-b`,
    ])
    expect(subagents.map((message) => message.id)).toEqual([
      `subagent-${RUN_ID}:sub:graph-a`,
      `subagent-${RUN_ID}:sub:graph-b`,
    ])
    expect(new Set(subagents.map((message) => message.meta?.subRunId)).size).toBe(2)
    expect(twice).toEqual(once)
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
    }, 'default')

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

    expect(() => buildResumePayload({ ...current, approval }, 'default')).toThrow()
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
      'default',
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

  it('marks only the failed subagent when a nested RUN_ERROR arrives', () => {
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
    const subRunId = `${RUN_ID}:sub:graph-error`
    const running = applyConversationEvent(task, {
      type: 'RUN_STARTED',
      threadId: THREAD_ID,
      runId: subRunId,
      rawEvent: {
        streamMode: 'tasks',
        source: {
          agentType: 'subagent',
          agentName: 'researcher',
          namespace: ['tools:graph-error'],
          graphTaskId: 'graph-error',
        },
        runId: subRunId,
        parentAgentRunId: RUN_ID,
        parentToolCallId: 'call-subagent-error',
        subagentInput: '失败任务',
      },
    })

    const failed = applyConversationEvent(running, {
      type: 'RUN_ERROR',
      message: '子智能体运行失败',
      code: 'task_error',
      rawEvent: {
        streamMode: 'tasks',
        source: {
          agentType: 'subagent',
          agentName: 'researcher',
          namespace: ['tools:graph-error'],
          graphTaskId: 'graph-error',
        },
        runId: subRunId,
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
      snapshotVersion: 2,
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

  it('clears replayed approval when authoritative history has no pending interrupt', () => {
    const detail: ConversationHistoryDetail = {
      id: 2,
      threadId: THREAD_ID,
      title: '已解决审批',
      status: 'idle',
      lastRunId: RUN_ID,
      lastSeq: 1,
      snapshotSeq: 0,
      snapshotVersion: 2,
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
    const SUB_RUN_ID = `${RUN_ID}:sub:task-0`
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
        eventType: 'RUN_STARTED',
        event: {
          type: 'RUN_STARTED',
          threadId: THREAD_ID,
          runId: SUB_RUN_ID,
          rawEvent: {
            streamMode: 'messages',
            source: { agentType: 'subagent', agentName: 'researcher', namespace: ['task'], graphTaskId: 'task-0' },
            runId: SUB_RUN_ID,
            parentAgentRunId: RUN_ID,
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
      snapshotVersion: 2,
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

  it('hydrates a v2 UI snapshot with complete sub-agent input and applies only tail events', () => {
    const snapshotCreatedAt = '2026-08-05T00:00:00.000Z'
    const tailCreatedAt = '2026-08-05T00:00:01.000Z'
    const detail: ConversationHistoryDetail = {
      id: 4,
      threadId: THREAD_ID,
      title: 'v2 快照',
      status: 'idle',
      lastRunId: RUN_ID,
      lastSeq: 12,
      snapshotSeq: 10,
      snapshotVersion: 2,
      messageCount: 3,
      toolCallCount: 1,
      hasPendingInterrupt: false,
      pinned: false,
      snapshot: {
        snapshotSeq: 10,
        snapshotVersion: 2,
        messages: [
          { id: 'user-v2', role: 'user', content: '研究一下', createdAt: snapshotCreatedAt },
          {
            id: 'subagent-v2',
            role: 'subagent',
            content: '研究任务',
            createdAt: snapshotCreatedAt,
            meta: {
              agentName: 'researcher',
              input: '对比 A 与 B，并给出处',
              status: 'completed',
              subRunId: `${RUN_ID}:sub:task-v2`,
              graphTaskId: 'task-v2',
            },
          },
          { id: 'assistant-v2', role: 'assistant', content: '已有', createdAt: snapshotCreatedAt },
        ],
        todos: [
          {
            id: 'todo-v2',
            content: '历史 Todo',
            status: 'completed',
            targetMessageId: 'tool-write-todos-v2',
          },
        ],
        approval: null,
        runStatus: 'idle',
        activeRunId: null,
        serverState: {},
        runs: {},
        activities: [],
        interrupts: [],
      },
      events: [
        {
          seq: 11,
          eventId: 'evt-tail-content',
          eventType: 'TEXT_MESSAGE_CONTENT',
          event: {
            type: 'TEXT_MESSAGE_CONTENT',
            messageId: 'assistant-v2',
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
    const subagent = restored.messages.find((message) => message.id === 'subagent-v2')
    expect(restored.todos[0]?.targetMessageId).toBeUndefined()
    const assistant = restored.messages.find((message) => message.id === 'assistant-v2')

    expect(subagent?.meta?.input).toBe('对比 A 与 B，并给出处')
    expect(assistant?.content).toBe('已有内容')
    expect(restored.messages.filter((message) => message.id === 'assistant-v2')).toHaveLength(1)
    expect(restored.lastSeq).toBe(12)
    expect(restored.runStatus).toBe('idle')
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
      snapshotVersion: 2,
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
      snapshotVersion: 2,
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
})
