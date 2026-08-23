import { describe, expect, it } from 'vitest'

import { parseConversationAgUiEvent } from './eventParser'

describe('AG-UI 事件边界解析', () => {
  it.each([
    ['RUN_STARTED', { type: 'RUN_STARTED', threadId: 'thread-1', runId: 'run-1' }],
    ['MESSAGES_SNAPSHOT', { type: 'MESSAGES_SNAPSHOT', messages: [{ id: 'message-1', role: 'assistant', content: '完成' }] }],
    ['STATE_SNAPSHOT', { type: 'STATE_SNAPSHOT', snapshot: { todos: [] } }],
    ['STATE_DELTA', { type: 'STATE_DELTA', delta: [{ op: 'remove', path: '/todos/0' }] }],
    ['TEXT_MESSAGE_START', { type: 'TEXT_MESSAGE_START', messageId: 'message-1', role: 'assistant' }],
    ['TEXT_MESSAGE_CONTENT', { type: 'TEXT_MESSAGE_CONTENT', messageId: 'message-1', delta: '增量' }],
    ['TEXT_MESSAGE_END', { type: 'TEXT_MESSAGE_END', messageId: 'message-1' }],
    ['REASONING_START', { type: 'REASONING_START', messageId: 'reasoning-1' }],
    ['REASONING_MESSAGE_START', { type: 'REASONING_MESSAGE_START', messageId: 'reasoning-1', role: 'reasoning' }],
    ['REASONING_MESSAGE_CONTENT', { type: 'REASONING_MESSAGE_CONTENT', messageId: 'reasoning-1', delta: '分析' }],
    ['REASONING_MESSAGE_END', { type: 'REASONING_MESSAGE_END', messageId: 'reasoning-1' }],
    ['REASONING_END', { type: 'REASONING_END', messageId: 'reasoning-1' }],
    ['TOOL_CALL_START', { type: 'TOOL_CALL_START', toolCallId: 'tool-1', toolCallName: 'write_todos' }],
    ['TOOL_CALL_ARGS', { type: 'TOOL_CALL_ARGS', toolCallId: 'tool-1', delta: '{"todos":[]}' }],
    ['TOOL_CALL_END', { type: 'TOOL_CALL_END', toolCallId: 'tool-1' }],
    ['TOOL_CALL_RESULT', { type: 'TOOL_CALL_RESULT', messageId: 'tool-message-1', toolCallId: 'tool-1', content: '完成', role: 'tool' }],
    ['CUSTOM', { type: 'CUSTOM', name: 'notice', value: { visible: true } }],
    ['RAW', { type: 'RAW', source: 'langgraph.tasks', event: { phase: 'start' } }],
    ['RUN_FINISHED', { type: 'RUN_FINISHED', threadId: 'thread-1', runId: 'run-1', outcome: { type: 'success' } }],
    ['RUN_ERROR', { type: 'RUN_ERROR', rawEvent: { runId: 'run-1' }, message: '运行失败', code: 'failed' }],
  ])('接受当前线协议中的 %s', (_name, event) => {
    expect(parseConversationAgUiEvent(event)).toBe(event)
  })

  it('接受当前服务端使用的来源、输入和中断扩展', () => {
    const event = {
      type: 'RUN_STARTED',
      threadId: 'thread-1',
      runId: 'run-1',
      title: '权威标题',
      rawEvent: {
        streamMode: 'tasks',
        runId: 'run-1',
        source: {
          agentType: 'subagent',
          agentName: 'researcher',
          namespace: ['tools:task-1'],
          graphTaskId: 'task-1',
          parentNamespace: [],
          parentToolCallId: 'tool-1',
          subagentInput: '研究问题',
          subagentInvocationId: 'invocation-1',
        },
      },
      input: {
        threadId: 'thread-1',
        runId: 'run-1',
        state: {},
        messages: [{ id: 'request-1', role: 'user', content: '开始' }],
        tools: [],
        context: [],
        forwardedProps: { model: 'deepseek-v4-pro' },
        resume: [{ interruptId: 'interrupt-1', status: 'resolved', payload: { type: 'approve' } }],
      },
    }

    expect(parseConversationAgUiEvent(event)).toBe(event)
    expect(parseConversationAgUiEvent({
      type: 'RUN_FINISHED',
      threadId: 'thread-1',
      runId: 'run-1',
      outcome: {
        type: 'interrupt',
        interrupts: [{
          id: 'interrupt-1',
          reason: 'tool_review',
          toolCallId: 'tool-1',
          responseSchema: {},
          metadata: { source: 'deepagents' },
        }],
      },
    }).type).toBe('RUN_FINISHED')
  })

  it.each([
    ['非对象', null],
    ['数组', []],
    ['缺少 type', { runId: 'run-1' }],
    ['未知 type', { type: 'STEP_STARTED', stepName: 'model' }],
    ['RUN_STARTED 缺 threadId', { type: 'RUN_STARTED', runId: 'run-1' }],
    ['消息 ID 类型错误', { type: 'TEXT_MESSAGE_CONTENT', messageId: 1, delta: '内容' }],
    ['状态根不是对象', { type: 'STATE_SNAPSHOT', snapshot: [] }],
    ['JSON Patch op 非法', { type: 'STATE_DELTA', delta: [{ op: 'move', path: '/a' }] }],
    ['工具结果缺 role', { type: 'TOOL_CALL_RESULT', messageId: 'message-1', toolCallId: 'tool-1', content: '完成' }],
    ['来源 namespace 非字符串数组', {
      type: 'RUN_ERROR',
      rawEvent: {
        source: { agentType: 'main', agentName: 'main', namespace: [1] },
      },
    }],
    ['interrupt outcome 为空', {
      type: 'RUN_FINISHED',
      threadId: 'thread-1',
      runId: 'run-1',
      outcome: { type: 'interrupt', interrupts: [] },
    }],
    ['RAW event 不是对象', { type: 'RAW', event: 'task' }],
  ])('拒绝%s', (_name, event) => {
    expect(() => parseConversationAgUiEvent(event)).toThrow('事件流包含无效的 AG-UI 事件')
  })
})
