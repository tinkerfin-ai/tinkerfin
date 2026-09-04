import type {
  ConversationHistoryCoreDetail,
  ConversationHistoryDetail,
  ConversationTraceUpdate,
  TraceInteraction,
} from '../../../api/conversation/history'
import {
  parseTraceGraph,
  type TraceGraph,
  type TraceGraphDelta,
  type TraceGraphNode,
} from '../../../api/conversation/traceGraph'
import type { TaskTraceSnapshot } from '../../../api/conversation/taskTrace'
import { ConversationError } from '../../../api/conversation/errors'
import { translateCurrent } from '../../../i18n'
import type {
  ApprovalAllowedDecision,
  ApprovalItem,
  ApprovalState,
  Conversation,
  JsonObject,
  JsonValue,
  Message,
  PendingInteractionKind,
  TodoItem,
  WebTaskTraceViewState,
} from '../../../types'
import { planInteractionFromTracePayload } from '../agui'

const isObject = (value: unknown): value is JsonObject => (
  value != null && typeof value === 'object' && !Array.isArray(value)
)

const text = (value: JsonValue | null | undefined): string => {
  if (typeof value === 'string') return value
  if (value == null) return ''
  return JSON.stringify(value, null, 2)
}

const elapsedMs = (startedAt: string, completedAt?: string | null) => {
  if (!completedAt) return undefined
  const duration = Date.parse(completedAt) - Date.parse(startedAt)
  return Number.isFinite(duration) && duration >= 0 ? duration : undefined
}

const nodeMessageStatus = (
  status: TraceGraphNode['status'],
): NonNullable<Message['meta']>['status'] => {
  switch (status) {
    case 'running': return 'running'
    case 'waiting': return 'paused'
    case 'succeeded': return 'completed'
    case 'cancelled':
    case 'abandoned': return 'cancelled'
    default: return 'failed'
  }
}

const todosFromState = (root: JsonObject): TodoItem[] => {
  if (!Array.isArray(root.todos)) return []
  return root.todos.flatMap((value, index) => {
    if (!isObject(value) || typeof value.content !== 'string') return []
    const rawStatus = value.status
    const status: TodoItem['status'] = rawStatus === 'completed'
      ? 'completed'
      : rawStatus === 'in_progress'
        ? 'running'
        : rawStatus === 'failed'
          ? 'failed'
          : rawStatus === 'cancelled'
            ? 'cancelled'
            : 'pending'
    return [{
      id: typeof value.id === 'string' ? value.id : 'trace-todo-' + index,
      content: value.content,
      status,
    }]
  })
}

const modeFromState = (root: JsonObject): Conversation['mode'] => {
  const plan = root.tinkerfin_plan
  return isObject(plan) && plan.effectiveMode === 'plan' ? 'plan' : 'default'
}

const applyEntityDelta = <T extends { id: string }>(
  current: T[],
  upserts: T[],
  removes: string[],
): T[] => {
  const values = new Map(current.map((item) => [item.id, item]))
  for (const id of removes) values.delete(id)
  for (const item of upserts) values.set(item.id, structuredClone(item))
  return [...values.values()]
}

const applyTraceGraphDelta = (
  current: TraceGraph,
  delta: TraceGraphDelta,
): TraceGraph => {
  if (delta.asOfSeq <= current.asOfSeq) throw new ConversationError('stream_event_invalid')
  const currentTurns = new Map(current.turns.map((turn) => [turn.id, turn]))
  delta.turnUpserts.forEach((turn) => {
    const previous = currentTurns.get(turn.id)
    if (previous && (
      previous.ordinal !== turn.ordinal
      || previous.startedAt !== turn.startedAt
    )) throw new ConversationError('stream_event_invalid')
  })
  const currentNodes = new Map(current.nodes.map((node) => [node.id, node]))
  delta.nodeUpserts.forEach((node) => {
    const previous = currentNodes.get(node.id)
    if (previous && (
      node.updatedSeq < previous.updatedSeq
      || node.turnId !== previous.turnId
      || node.kind !== previous.kind
      || node.name !== previous.name
      || JSON.stringify(node.namespace) !== JSON.stringify(previous.namespace)
    )) throw new ConversationError('stream_event_invalid')
  })
  const nodeValues = applyEntityDelta(
    current.nodes,
    delta.nodeUpserts,
    delta.nodeRemoves,
  )
  const nodesById = new Map(nodeValues.map((node) => [node.id, node]))
  if (
    delta.orderedNodeIds.length !== nodeValues.length
    || new Set(delta.orderedNodeIds).size !== nodeValues.length
    || delta.orderedNodeIds.some((id) => !nodesById.has(id))
    || delta.matchedNodeIds.some((id) => !nodesById.has(id))
  ) throw new ConversationError('stream_event_invalid')
  const turns = applyEntityDelta(
    current.turns,
    delta.turnUpserts,
    delta.turnRemoves,
  ).sort((left, right) => left.ordinal - right.ordinal || left.id.localeCompare(right.id))
  return parseTraceGraph({
    turns,
    nodes: delta.orderedNodeIds.map((id) => nodesById.get(id) as TraceGraphNode),
    orderedNodeIds: [...delta.orderedNodeIds],
    matchedNodeIds: [...delta.matchedNodeIds],
    asOfSeq: delta.asOfSeq,
    completeness: structuredClone(delta.completeness),
  })
}

const decodePointerToken = (value: string) => value.replaceAll('~1', '/').replaceAll('~0', '~')

const capturedArguments = (value: JsonValue | undefined): JsonObject => {
  if (!isObject(value) || value.disposition !== 'inline' || !isObject(value.value)) return {}
  const root = value.value['']
  if (Object.keys(value.value).length === 1 && isObject(root)) {
    return structuredClone(root)
  }
  const entries = Object.entries(value.value)
  if (!entries.every(([pointer]) => pointer.startsWith('/'))) {
    return structuredClone(value.value)
  }
  const result: JsonObject = {}
  for (const [pointer, item] of entries) {
    if (!pointer.startsWith('/') || pointer.slice(1).includes('/')) continue
    result[decodePointerToken(pointer.slice(1))] = structuredClone(item)
  }
  return result
}

const allowedDecisions = (value: JsonValue | undefined): ApprovalAllowedDecision[] => {
  if (!Array.isArray(value)) return []
  return value.filter((item): item is ApprovalAllowedDecision => (
    item === 'approve' || item === 'edit' || item === 'reject' || item === 'respond'
  ))
}

const scopedSourceKey = (namespace: string[], sourceId: string): string => (
  JSON.stringify([namespace, sourceId])
)

const approvalFromInteraction = (
  interaction: TraceInteraction,
  nodes: TraceGraphNode[],
): ApprovalState | undefined => {
  if (interaction.kind !== 'tool_approval' || !isObject(interaction.payload)) return undefined
  const actions = interaction.payload.action_requests
  const reviews = interaction.payload.review_configs
  if (!Array.isArray(actions) || !Array.isArray(reviews) || actions.length !== reviews.length) {
    return undefined
  }
  if (interaction.toolCallIds.length !== actions.length) return undefined
  const items = actions.flatMap<ApprovalItem>((rawAction, index) => {
    const rawReview = reviews[index]
    if (!isObject(rawAction) || !isObject(rawReview) || typeof rawAction.name !== 'string') return []
    const decisions = allowedDecisions(rawReview.allowed_decisions)
    if (decisions.length === 0) return []
    const publicId = actions.length === 1
      ? interaction.sourceId
      : interaction.sourceId + '#' + index
    const originalArgs = capturedArguments(rawAction.arguments)
    const toolCallId = interaction.toolCallIds[index]
    const toolNode = nodes.find((node) => (
      node.kind === 'tool'
      && (node.status === 'running' || node.status === 'waiting')
      && node.sourceId === toolCallId
      && node.name === rawAction.name
      && node.namespace.length === interaction.namespace.length
      && node.namespace.every(
        (value, position) => value === interaction.namespace[position],
      )
    ))
    if (!toolNode || !toolCallId) return []
    return [{
      id: publicId,
      interruptId: publicId,
      toolCallId,
      toolName: rawAction.name,
      params: JSON.stringify(originalArgs, null, 2),
      input: typeof originalArgs.file_path === 'string' ? originalArgs.file_path : '',
      description: typeof rawAction.description === 'string'
        ? rawAction.description
        : rawAction.name,
      originalArgs,
      allowedDecisions: decisions,
    }]
  })
  if (items.length !== actions.length || items.length === 0) return undefined
  return { items, activeIndex: 0, submitted: false, mode: 'options' }
}

const interactionState = (
  interactions: TraceInteraction[],
  nodes: TraceGraphNode[],
): {
  approval?: ApprovalState
  planInteraction?: Conversation['planInteraction']
  pendingInteractionKind?: PendingInteractionKind
} => {
  const pending = interactions
    .filter((interaction) => interaction.status === 'pending')
    .sort((left, right) => left.traceSeq - right.traceSeq || left.id.localeCompare(right.id))
  if (pending.length === 0) return {}
  if (pending.length === 1) {
    const interaction = pending[0]
    if (interaction) {
      const plan = planInteractionFromTracePayload(interaction.sourceId, interaction.payload)
      if (plan) {
        return {
          planInteraction: plan,
          pendingInteractionKind: plan.kind === 'questions'
            ? 'plan_clarification'
            : 'plan_review',
        }
      }
    }
  }
  if (pending.every((interaction) => interaction.kind === 'tool_approval')) {
    const groups = pending.map((interaction) => approvalFromInteraction(interaction, nodes))
    if (groups.some((group) => !group)) throw new ConversationError('stream_event_invalid')
    const items = groups.flatMap((group) => group?.items ?? [])
    const interruptIds = new Set(items.map((item) => item.interruptId))
    if (items.length === 0 || interruptIds.size !== items.length) {
      throw new ConversationError('stream_event_invalid')
    }
    return {
      approval: { items, activeIndex: 0, submitted: false, mode: 'options' },
      pendingInteractionKind: 'tool_approval',
    }
  }
  if (pending.length > 1) throw new ConversationError('stream_event_invalid')
  return { pendingInteractionKind: 'input_required' }
}

const traceMessages = (trace: ConversationHistoryCoreDetail): Message[] => {
  const reasoning = new Map(
    trace.reasoning
      .filter((item) => !item.contentOmitted && item.content != null)
      .map((item) => [item.messageId, text(item.content)]),
  )
  const toolResults = new Map(
    trace.messages
      .filter((item) => item.role === 'tool' && item.toolCallId)
      .map((item) => [
        scopedSourceKey(item.namespace, item.toolCallId as string),
        item,
      ]),
  )
  const nodesById = new Map(trace.graph.nodes.map((node) => [node.id, node]))
  const verifiedSubagents = trace.graph.nodes.filter((node) => (
    node.kind === 'subagent' && node.sourceId
  ))
  const subagentPartialOutput = new Map<string, Array<{ sequence: number; content: string }>>()
  trace.messages.forEach((message) => {
    if (message.role !== 'assistant' || message.namespace.length === 0) return
    const owner = verifiedSubagents
      .filter((node) => (
        node.namespace.length <= message.namespace.length
        && node.namespace.every((value, position) => value === message.namespace[position])
      ))
      .sort((left, right) => right.namespace.length - left.namespace.length)[0]
    if (!owner) return
    const content = text(message.content)
    if (!content) return
    const existing = subagentPartialOutput.get(owner.id) ?? []
    existing.push({ sequence: message.traceSeq, content })
    subagentPartialOutput.set(owner.id, existing)
  })
  const owningSubagent = (node: TraceGraphNode): TraceGraphNode | undefined => {
    if (!node.parentSubagentId) return undefined
    const parent = nodesById.get(node.parentSubagentId)
    return parent?.kind === 'subagent' ? parent : undefined
  }
  const ordered: Array<{ value: Message; sequence: number }> = trace.messages.flatMap((item) => {
    if (item.namespace.length > 0) return []
    if (item.role !== 'user' && item.role !== 'assistant') return []
    return [{
      sequence: item.traceSeq,
      value: {
        id: item.id,
        role: item.role,
        content: text(item.content),
        createdAt: item.createdAt,
        meta: item.role === 'assistant'
          ? {
              status: item.status === 'completed' ? 'completed' : 'running',
              runId: item.runId,
              reasoning: reasoning.get(item.id),
              completedAt: item.completedAt ?? undefined,
              durationMs: elapsedMs(item.createdAt, item.completedAt),
            }
          : { runId: item.runId },
      },
    }]
  })
  trace.graph.nodes.forEach((node) => {
    if (
      node.kind !== 'tool'
      && node.kind !== 'subagent'
      && node.kind !== 'plan'
    ) return
    if (node.kind === 'subagent' && !node.sourceId) return
    const resultNamespace = node.kind === 'subagent'
      ? node.namespace.slice(0, -1)
      : node.namespace
    const result = node.sourceId
      ? toolResults.get(scopedSourceKey(resultNamespace, node.sourceId))
      : undefined
    const retainedInput = node.requestOmitted ? undefined : node.request
    const retainedResult = node.resultOmitted ? undefined : node.result
    const subagent = node.kind === 'tool' ? owningSubagent(node) : undefined
    if (node.kind === 'tool' && node.namespace.length > 0 && !subagent) return
    const subagentInput = isObject(retainedInput)
      && typeof retainedInput.description === 'string'
      ? retainedInput.description
      : text(retainedInput)
    const role: Message['role'] = node.kind === 'tool'
      ? 'tool'
      : node.kind === 'subagent'
        ? 'subagent'
        : 'process'
    ordered.push({
      sequence: node.startedSeq,
      value: {
        id: node.id,
        role,
        content: node.name,
        createdAt: node.startedAt,
        meta: {
          title: node.name,
          toolName: node.kind === 'tool' ? node.name : undefined,
          agentName: node.kind === 'subagent' ? node.name : undefined,
          sourceAgentName: subagent?.name,
          params: node.kind === 'tool' ? text(retainedInput) : undefined,
          input: node.kind === 'subagent' ? subagentInput : undefined,
          result: text(
            node.kind === 'subagent'
              ? retainedResult
                ?? result?.content
                ?? subagentPartialOutput.get(node.id)
                  ?.sort((left, right) => left.sequence - right.sequence)
                  .map((item) => item.content)
                  .join('\n\n')
              : result?.content ?? retainedResult,
          ),
          status: nodeMessageStatus(node.status),
          toolCallId: node.kind === 'tool' ? node.sourceId ?? undefined : undefined,
          batchId: node.kind === 'tool' && !subagent
            ? node.modelCallId ?? undefined
            : undefined,
          subRunId: node.kind === 'subagent' ? node.id : undefined,
          runId: subagent?.id ?? node.runId,
          completedAt: node.completedAt ?? undefined,
          durationMs: elapsedMs(node.startedAt, node.completedAt),
        },
      },
    })
  })
  return ordered.sort((left, right) => (
    left.sequence - right.sequence
  )).map((item) => item.value)
}

const runStatus = (trace: ConversationHistoryCoreDetail): Conversation['runStatus'] => {
  switch (trace.status.execution) {
    case 'running': return 'detached'
    case 'waiting': return 'waiting_approval'
    case 'failed':
    case 'unknown': return 'error'
    default: return 'idle'
  }
}

const assertTraceDetail = (trace: ConversationHistoryCoreDetail) => {
  if (
    !trace.threadId
    || !trace.headRunId
    || !Number.isSafeInteger(trace.asOfSeq)
    || trace.asOfSeq < 1
    || !Array.isArray(trace.messages)
    || !Array.isArray(trace.graph?.nodes)
    || trace.graph.asOfSeq !== trace.asOfSeq
    || !Array.isArray(trace.interactions)
    || !isObject(trace.state?.root)
  ) throw new ConversationError('stream_event_invalid')
}

const taskTraceView = (snapshot: TaskTraceSnapshot): WebTaskTraceViewState => (
  snapshot.status === 'ready'
    ? { phase: 'ready', snapshot }
    : { phase: 'unavailable', snapshot }
)

export const restoreConversationFromTrace = (
  detail: ConversationHistoryDetail,
  options: {
    model: string
    lastDeliveredSeq?: number
    includeTaskTrace: boolean
    taskTrace?: WebTaskTraceViewState
  },
): Conversation => {
  const { taskTrace: wireTaskTrace, ...wireCore } = detail
  const trace = structuredClone(wireCore)
  assertTraceDetail(trace)
  if (options.includeTaskTrace && wireTaskTrace == null) {
    throw new ConversationError('stream_event_invalid')
  }
  const taskTrace = options.includeTaskTrace && wireTaskTrace != null
    ? taskTraceView(wireTaskTrace)
    : options.taskTrace ?? { phase: 'unloaded' as const }
  const interaction = interactionState(trace.interactions, trace.graph.nodes)
  const projectedStatus = runStatus(trace)
  const status = interaction.pendingInteractionKind && projectedStatus !== 'error'
    ? 'waiting_approval'
    : projectedStatus
  const messages = traceMessages(trace)
  if (
    (trace.status.execution === 'failed' || trace.status.execution === 'unknown')
    && !messages.some((message) => message.role === 'error')
  ) {
    messages.push({
      id: 'trace-error:' + trace.headRunId,
      role: 'error',
      content: translateCurrent('对话运行失败'),
      createdAt: trace.updatedAt,
      meta: { runId: trace.headRunId, status: 'failed' },
    })
  }
  return {
    threadId: trace.threadId,
    title: trace.title,
    pinned: trace.pinned,
    updatedAt: trace.updatedAt,
    model: trace.lastModel ?? options.model,
    mode: modeFromState(trace.state.root),
    messages,
    todos: todosFromState(trace.state.root),
    taskTrace,
    approval: interaction.approval,
    planInteraction: interaction.planInteraction,
    pendingInteractionKind: interaction.pendingInteractionKind,
    runStatus: status,
    activeRunId: status === 'detached' ? trace.headRunId : undefined,
    serverState: structuredClone(trace.state.root),
    // Trace 序号与 Messaging 投递序号相互独立；实时调用方保留已知游标，纯历史水化保持未知
    lastSeq: options.lastDeliveredSeq,
    trace,
    isHydrated: true,
  }
}

export const applyConversationTraceUpdate = (
  conversation: Conversation,
  update: ConversationTraceUpdate,
  taskTraceReplacement: TaskTraceSnapshot | null,
  includeTaskTrace: boolean,
): Conversation => {
  const previous = conversation.trace
  if (!previous) throw new ConversationError('stream_event_invalid')
  if (update.asOfSeq <= previous.asOfSeq) return conversation
  if (update.graph.asOfSeq !== update.asOfSeq) {
    throw new ConversationError('stream_event_invalid')
  }
  const next: ConversationHistoryCoreDetail = {
    ...structuredClone(previous),
    asOfSeq: update.asOfSeq,
    headRunId: update.status.headRunId,
    messages: applyEntityDelta(previous.messages, update.messages.upserts, update.messages.removes),
    reasoning: applyEntityDelta(previous.reasoning, update.reasoning.upserts, update.reasoning.removes),
    graph: applyTraceGraphDelta(previous.graph, update.graph),
    interactions: applyEntityDelta(
      previous.interactions,
      update.interactions.upserts,
      update.interactions.removes,
    ),
    state: structuredClone(update.state),
    status: structuredClone(update.status),
    completeness: structuredClone(update.completeness),
    messageCount: update.messageCount,
    toolCallCount: update.toolCallCount,
    historyCursor: null,
  }
  const taskTrace = includeTaskTrace && taskTraceReplacement != null
    ? taskTraceView(taskTraceReplacement)
    : includeTaskTrace
      ? conversation.taskTrace
      : { phase: 'unloaded' as const }
  return restoreConversationFromTrace({ ...next, taskTrace: null }, {
    model: conversation.model,
    lastDeliveredSeq: conversation.lastSeq,
    includeTaskTrace: false,
    taskTrace,
  })
}
