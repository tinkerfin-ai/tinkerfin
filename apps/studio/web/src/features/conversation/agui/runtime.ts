import type {
  AgentMode,
  ChatMessageInput,
  ChatRequestPayload,
  ChatResumeEntry,
  ConversationAgUiEvent,
  InterruptEvent,
  RawEventContext,
} from "../../../api/conversation/types"
import type {
  ConversationEventEnvelope,
  ConversationHistoryDetail,
  ConversationSnapshotJson,
} from "../../../api/conversation/history"
import type {
  ApprovalAllowedDecision,
  ApprovalItem,
  ApprovalState,
  Conversation,
  ConversationNotice,
  JsonObject,
  JsonValue,
  Message,
  TodoItem,
  TodoStatus,
} from "../../../types"
import { parseToolReviewInterrupt } from "./toolReviewContract"
import {
  parseSubagentProvenance,
  type SubagentProvenance,
} from "./subagentProvenanceContract"

export const createRunId = () =>
  `run-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`

const nowIso = () => new Date().toISOString()

const elapsedMs = (startedAt: string, completedAt: string) => {
  const elapsed = Date.parse(completedAt) - Date.parse(startedAt)
  return Number.isFinite(elapsed) ? Math.max(0, elapsed) : 0
}

const parseJsonObject = (value: string) => {
  try {
    const parsed = JSON.parse(value) as JsonValue
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? parsed as JsonObject
      : null
  } catch {
    return null
  }
}

const isStateTodo = (value: JsonValue): value is { content: string; status: "pending" | "in_progress" | "completed" } =>
  Boolean(
    value
    && typeof value === "object"
    && !Array.isArray(value)
    && typeof (value as { content?: unknown }).content === "string"
    && (
      (value as { status?: unknown }).status === "pending"
      || (value as { status?: unknown }).status === "in_progress"
      || (value as { status?: unknown }).status === "completed"
    ),
  )

const toTodoStatus = (status: "pending" | "in_progress" | "completed"): TodoStatus =>
  status === "in_progress" ? "running" : status

const findMessageIndex = (
  conversation: Conversation,
  predicate: (message: Message) => boolean,
) => conversation.messages.findIndex(predicate)

const updateMessage = (
  conversation: Conversation,
  predicate: (message: Message) => boolean,
  updater: (message: Message) => Message,
): Conversation => ({
  ...conversation,
  messages: conversation.messages.map((message) => (predicate(message) ? updater(message) : message)),
})

const setConversationNotice = (
  conversation: Conversation,
  content: string,
  kind: ConversationNotice["kind"],
): Conversation => ({
  ...conversation,
  notice: { kind, content },
})

const markInterruptedToolCards = (
  conversation: Conversation,
  interruptedRunId: string,
  interrupts: InterruptEvent[],
): Conversation => ({
  ...conversation,
  messages: conversation.messages.map((message) => {
    if (
      message.role !== "tool"
      || message.meta?.status !== "running"
      || message.meta.runId !== interruptedRunId
    ) return message
    const interrupt = interrupts.find((item) => item.toolCallId === message.meta?.toolCallId)
    return {
      ...message,
      meta: {
        ...message.meta,
        status: "paused",
        interruptId: interrupt?.id,
      },
    }
  }),
})

const approvalInputFromArgs = (args: JsonObject, fallback: string | undefined) => {
  const filePath = args.file_path
  if (typeof filePath === "string" && filePath) return filePath
  return fallback ?? "请确认该操作"
}

const approvalItemsFromInterrupts = (interrupts: InterruptEvent[]): ApprovalItem[] =>
  interrupts.map((interrupt) => {
    const review = parseToolReviewInterrupt(interrupt)
    const originalArgs = review.originalArgs
    const allowedDecisions: ApprovalAllowedDecision[] = [...review.allowedDecisions]
    const toolName = review.toolName

    return {
      id: interrupt.id,
      interruptId: interrupt.id,
      toolCallId: interrupt.toolCallId,
      toolName,
      params: JSON.stringify(originalArgs, null, 2),
      input: approvalInputFromArgs(originalArgs, interrupt.message),
      description: interrupt.message ?? `请确认工具 ${toolName}`,
      originalArgs,
      allowedDecisions,
    }
  })

interface PlanInterruptLike {
  id: string
  reason: string
  message?: string | null
  responseSchema?: JsonObject | null
  metadata?: JsonObject | null
}

const runtimeEnvelopeMetadata = (interrupt: PlanInterruptLike): JsonObject | null => {
  const runtimeInterrupt = interrupt.metadata?.runtimeInterrupt
  if (!runtimeInterrupt || typeof runtimeInterrupt !== 'object' || Array.isArray(runtimeInterrupt)) return null
  const envelope = runtimeInterrupt.envelope
  if (!envelope || typeof envelope !== 'object' || Array.isArray(envelope)) return null
  const metadata = envelope.metadata
  return metadata && typeof metadata === 'object' && !Array.isArray(metadata)
    ? metadata as JsonObject
    : null
}

const planInteractionFromInterrupts = (
  interrupts: readonly PlanInterruptLike[],
): Conversation['planInteraction'] => {
  if (interrupts.length !== 1) return undefined
  const interrupt = interrupts[0]
  if (!interrupt) return undefined
  const metadata = runtimeEnvelopeMetadata(interrupt)
  if (!metadata) return undefined

  if (interrupt.reason === 'plan_clarification') {
    const clarification = metadata.clarification
    if (!clarification || typeof clarification !== 'object' || Array.isArray(clarification)) return undefined
    if (clarification.schema !== 'tinkerfin.plan-clarification.v1') return undefined
    const form = clarification.form
    if (!form || typeof form !== 'object' || Array.isArray(form)) return undefined
    if (form.schemaVersion !== 1 || !Array.isArray(form.questions)) return undefined
    const questions = form.questions.flatMap((rawQuestion) => {
      if (!rawQuestion || typeof rawQuestion !== 'object' || Array.isArray(rawQuestion)) return []
      const question = rawQuestion as JsonObject
      if (
        typeof question.id !== 'string'
        || typeof question.prompt !== 'string'
        || typeof question.allowFreeText !== 'boolean'
      ) return []
      const questionAttributes = question.attributes
      if (
        questionAttributes !== undefined
        && questionAttributes !== null
        && (typeof questionAttributes !== 'object' || Array.isArray(questionAttributes))
      ) return []
      const options = Array.isArray(question.options)
        ? question.options.flatMap((rawOption) => {
            if (!rawOption || typeof rawOption !== 'object' || Array.isArray(rawOption)) return []
            const option = rawOption as JsonObject
            if (typeof option.id !== 'string' || typeof option.label !== 'string') return []
            const optionAttributes = option.attributes
            if (
              optionAttributes !== undefined
              && optionAttributes !== null
              && (typeof optionAttributes !== 'object' || Array.isArray(optionAttributes))
            ) return []
            return [{
              id: option.id,
              label: option.label,
              description: typeof option.description === 'string' ? option.description : null,
              attributes: optionAttributes as JsonObject | null | undefined,
            }]
          })
        : []
      return [{
        id: question.id,
        prompt: question.prompt,
        options,
        allowFreeText: question.allowFreeText,
        attributes: questionAttributes as JsonObject | null | undefined,
      }]
    })
    if (questions.length !== form.questions.length || questions.length === 0) return undefined
    return {
      kind: 'questions',
      interruptId: interrupt.id,
      form: structuredClone(form as JsonObject),
      questions,
      submitted: false,
    }
  }

  if (interrupt.reason === 'plan_review') {
    const revision = metadata.planRevision
    const draft = metadata.draft
    if (
      typeof revision !== 'number'
      || !Number.isInteger(revision)
      || !draft
      || typeof draft !== 'object'
      || Array.isArray(draft)
    ) return undefined
    return {
      kind: 'review',
      interruptId: interrupt.id,
      revision,
      draft: draft as JsonObject,
      submitted: false,
    }
  }

  return undefined
}

const attachApproval = (conversation: Conversation, interrupts: InterruptEvent[]) => ({
  ...conversation,
  approval: {
    items: approvalItemsFromInterrupts(interrupts),
    activeIndex: 0,
    submitted: false,
    mode: "options" as const,
  },
})

const upsertAssistantMessage = (
  conversation: Conversation,
  messageId: string,
  patch: (message: Message | null) => Message,
) => {
  const index = findMessageIndex(conversation, (message) => message.id === messageId)
  if (index < 0) {
    return {
      ...conversation,
      messages: [...conversation.messages, patch(null)],
    }
  }

  return {
    ...conversation,
    messages: conversation.messages.map((message, currentIndex) =>
      currentIndex === index ? patch(message) : message),
  }
}

const upsertToolMessage = (
  conversation: Conversation,
  toolCallId: string,
  patch: (message: Message | null) => Message,
) => {
  const index = findMessageIndex(
    conversation,
    (message) => message.role === "tool" && message.meta?.toolCallId === toolCallId,
  )
  if (index < 0) {
    return {
      ...conversation,
      messages: [...conversation.messages, patch(null)],
    }
  }

  return {
    ...conversation,
    messages: conversation.messages.map((message, currentIndex) =>
      currentIndex === index ? patch(message) : message),
  }
}

const parseTaskDescriptor = (params: string) => {
  const parsed = parseJsonObject(params)
  if (!parsed) return null

  return {
    agentName:
      typeof parsed.subagent_type === "string" && parsed.subagent_type
        ? parsed.subagent_type
        : "subagent",
    input:
      typeof parsed.description === "string" && parsed.description
        ? parsed.description
        : "",
  }
}

type ResolvedRawEventContext = RawEventContext & Required<
  Pick<RawEventContext, "streamMode" | "source">
>

const runIdForSource = (
  conversation: Conversation,
  rawEvent: ResolvedRawEventContext,
) => (
  rawEvent.source.agentType === "subagent"
  && rawEvent.source.subagentInvocationId
    ? rawEvent.source.subagentInvocationId
    : rawEvent.runId
) ?? conversation.messages.find(
  (message) =>
    message.role === "subagent"
    && rawEvent.source.graphTaskId != null
    && message.meta?.graphTaskId === rawEvent.source.graphTaskId,
)?.meta?.subRunId

const rawEventOrMain = (
  conversation: Conversation,
  rawEvent: RawEventContext | undefined,
): ResolvedRawEventContext => ({
  ...rawEvent,
  streamMode: rawEvent?.streamMode ?? "messages",
  source: rawEvent?.source ?? {
    agentType: "main",
    agentName: "main",
    namespace: [],
  },
  runId: rawEvent?.runId ?? conversation.activeRunId,
})

const startSubagentRun = (
  conversation: Conversation,
  provenance: SubagentProvenance,
): Conversation => {
  const subRunId = provenance.subagentInvocationId
  const existing = conversation.messages.find(
    (message) => message.role === "subagent" && message.meta?.subRunId === subRunId,
  )
  if (existing) {
    if (
      existing.meta?.originMainRunId == null
      || existing.meta?.agentName !== provenance.agentName
      || existing.meta?.graphTaskId !== provenance.graphTaskId
      || existing.meta?.toolCallId !== provenance.parentToolCallId
      || existing.meta?.input !== provenance.description
    ) throw new Error(`子 Agent 身份冲突: ${subRunId}`)
    return updateMessage(
      conversation,
      (message) => message.id === existing.id,
      (message) => ({
        ...message,
        meta: {
          ...message.meta,
          status: "running",
          lastMainRunId: provenance.requestRunId,
          completedAt: undefined,
          durationMs: undefined,
        },
      }),
    )
  }

  const graphTaskId = provenance.graphTaskId
  const pendingTasks = conversation.messages.filter(
    (message) =>
      message.role === "tool"
      && message.meta?.toolName === "task"
      && message.meta?.status === "running"
      && !message.meta?.subRunId,
  )
  const task = pendingTasks.find(
    (message) => message.meta?.toolCallId === provenance.parentToolCallId,
  )
  const createdAt = nowIso()
  const subagentMessage: Message = {
    id: subRunId,
    role: "subagent",
    content: task?.content ?? "子智能体运行",
    createdAt,
    meta: {
      agentName: provenance.agentName,
      input: provenance.description,
      result: "",
      status: "running",
      toolCallId: provenance.parentToolCallId,
      subRunId,
      runId: subRunId,
      originMainRunId: provenance.requestRunId,
      lastMainRunId: provenance.requestRunId,
      graphTaskId,
    },
  }

  return {
    ...conversation,
    messages: [
      ...conversation.messages.map((message) => message.id === task?.id
        ? {
            ...message,
            meta: {
              ...message.meta,
              subRunId,
              graphTaskId,
            },
          }
        : message),
      subagentMessage,
    ],
  }
}

const updateSubagentRun = (
  conversation: Conversation,
  rawEvent: ResolvedRawEventContext,
  updater: (message: Message) => Message,
): Conversation => {
  const runId = runIdForSource(conversation, rawEvent)
  const index = findMessageIndex(
    conversation,
    (message) =>
      message.role === "subagent"
      && (runId != null
        ? message.meta?.subRunId === runId
        : (
          rawEvent.source.graphTaskId != null
          && message.meta?.graphTaskId === rawEvent.source.graphTaskId
        )),
  )

  if (index < 0) return conversation

  return {
    ...conversation,
    messages: conversation.messages.map((message, currentIndex) =>
      currentIndex === index ? updater(message) : message),
  }
}

const syncTodosFromState = (
  conversation: Conversation,
  state: JsonObject | undefined,
): Conversation => {
  const rawTodos = state?.todos
  if (!Array.isArray(rawTodos)) return conversation

  const nextTodos: TodoItem[] = rawTodos
    .filter(isStateTodo)
    .map((todo, index) => {
      const previous = conversation.todos[index]
      return {
        id: previous?.id ?? `todo-${index}`,
        content: todo.content,
        status: toTodoStatus(todo.status),
        result: previous?.result,
      }
    })

  return {
    ...conversation,
    todos: nextTodos,
    plan: null,
  }
}

const applyStateDelta = (current: JsonObject | undefined, delta: { path: string; value?: JsonValue; op: "add" | "remove" | "replace" }[]) => {
  let next: JsonValue = structuredClone(current ?? {})
  for (const operation of delta) {
    const pathParts = operation.path.split("/").slice(1)
      .map((part) => part.replaceAll("~1", "/").replaceAll("~0", "~"))
    if (!pathParts.length) {
      next = operation.op === "remove" ? {} : structuredClone(operation.value ?? null)
      continue
    }
    let target: JsonObject | JsonValue[] | null = (
      next && typeof next === "object" ? next : null
    ) as JsonObject | JsonValue[] | null
    for (const part of pathParts.slice(0, -1)) {
      if (target == null) break
      const key = Array.isArray(target) ? Number.parseInt(part, 10) : part
      if (Array.isArray(target) && !Number.isInteger(key)) {
        target = null
        break
      }
      const value = target[key as never]
      if (!value || typeof value !== "object") {
        target[key as never] = {} as never
      }
      target = target[key as never] as JsonObject | JsonValue[]
    }
    const finalPart = pathParts.at(-1)
    if (target == null || finalPart == null) continue
    if (Array.isArray(target)) {
      const index = finalPart === "-" ? target.length : Number.parseInt(finalPart, 10)
      if (!Number.isInteger(index) || index < 0) continue
      if (operation.op === "remove") {
        if (index < target.length) target.splice(index, 1)
      } else if (operation.op === "add") {
        if (index <= target.length) target.splice(index, 0, operation.value ?? null)
      } else if (index < target.length) {
        target[index] = operation.value ?? null
      }
    } else if (operation.op === "remove") {
      delete target[finalPart]
    } else {
      target[finalPart] = operation.value ?? null
    }
  }
  return next && typeof next === "object" && !Array.isArray(next) ? next : {}
}

const isAgentMode = (value: unknown): value is AgentMode =>
  value === "default" || value === "plan"

export const buildInitialPayload = (
  conversation: Conversation,
  content: string,
): ChatRequestPayload => {
  const runId = createRunId()
  return {
    threadId: conversation.threadId,
    runId,
    state: {},
    messages: [
      {
        id: `request-${runId}`,
        role: "user",
        content,
      } satisfies ChatMessageInput,
    ],
    tools: [],
    context: [],
    forwardedProps: {
      model: conversation.model,
      mode: conversation.mode,
    },
  }
}

const matchesApprovalGroup = (
  approval: ApprovalState | undefined,
  expectedInterruptIds: readonly string[],
) => Boolean(
  approval
  && approval.items.length === expectedInterruptIds.length
  && approval.items.every(
    (item, index) => item.interruptId === expectedInterruptIds[index],
  ),
)

export const buildResumePayload = (
  conversation: Conversation,
  expectedInterruptIds?: readonly string[],
): ChatRequestPayload => {
  const approval = conversation.approval
  if (!approval || approval.items.length === 0) {
    throw new Error("当前会话没有可提交的审批项")
  }
  if (approval.submitted) throw new Error("当前审批已经提交")
  if (
    expectedInterruptIds
    && !matchesApprovalGroup(approval, expectedInterruptIds)
  ) throw new Error("审批状态已更新")

  const seenInterruptIds = new Set<string>()
  for (const item of approval.items) {
    if (!item.interruptId.trim()) throw new Error("审批项缺少 interruptId")
    if (seenInterruptIds.has(item.interruptId)) throw new Error("审批项 interruptId 重复")
    seenInterruptIds.add(item.interruptId)
    if (!item.decision) throw new Error("请先处理所有待审批项")
    if (item.decision === "rejected") {
      if (!item.allowedDecisions.includes("reject")) {
        throw new Error("审批项不允许拒绝")
      }
      continue
    }
    const requiredDecision = item.editedArgs ? "edit" : "approve"
    if (!item.allowedDecisions.includes(requiredDecision)) {
      throw new Error(`审批项不允许${requiredDecision === "edit" ? "编辑" : "批准"}`)
    }
  }

  const items = approval.items
  const resume = items.map<ChatResumeEntry>((item) => {
    if (item.decision === "rejected") {
      return {
        interruptId: item.interruptId,
        status: "resolved",
        payload: {
          type: "reject",
          ...(item.rejectionReason ? { message: item.rejectionReason } : {}),
        },
      }
    }

    return {
      interruptId: item.interruptId,
      status: "resolved",
      payload: item.editedArgs
        ? {
          type: "edit",
          edited_action: {
            name: item.toolName,
            args: item.editedArgs,
          },
        }
        : { type: "approve" },
    }
  })

  return {
    threadId: conversation.threadId,
    runId: createRunId(),
    state: {},
    messages: [],
    tools: [],
    context: [],
    forwardedProps: {
      model: conversation.model,
      mode: conversation.mode,
    },
    resume,
  }
}

export const buildPlanResumePayload = (
  conversation: Conversation,
): ChatRequestPayload => {
  const interaction = conversation.planInteraction
  if (!interaction) throw new Error("当前会话没有待处理的 Plan 请求")
  if (interaction.submitted) throw new Error("当前 Plan 请求已经提交")

  let payload: ChatResumeEntry['payload']
  if (interaction.kind === 'questions') {
    const answers = interaction.questions.map((question) => {
      const option = question.options.find((item) => item.id === question.selectedOptionId)
      const customAnswer = question.customAnswer?.trim() ?? ''
      if (!option && !customAnswer) throw new Error("请回答所有 Plan 澄清问题")
      if (!option && !question.allowFreeText) throw new Error("该问题必须选择一个选项")
      return option
        ? { questionId: question.id, optionId: option.id }
        : { questionId: question.id, answer: customAnswer }
    })
    payload = { type: 'respond', answers }
  } else {
    if (!interaction.action) throw new Error("请选择 Plan 处理方式")
    if (interaction.action === 'approve') {
      payload = { type: 'approve', baseRevision: interaction.revision }
    } else if (interaction.action === 'edit') {
      const draft = parseJsonObject(interaction.editedDraft ?? '')
      if (!draft) throw new Error("编辑后的 Plan 必须是 JSON 对象")
      const content = structuredClone(draft)
      delete content.schemaVersion
      delete content.revision
      payload = {
        type: 'edit',
        baseRevision: interaction.revision,
        draft: content,
      }
    } else if (interaction.action === 'respond') {
      const message = interaction.message?.trim()
      if (!message) throw new Error("请填写 Plan 修改意见")
      payload = {
        type: 'respond',
        baseRevision: interaction.revision,
        message,
      }
    } else {
      const message = interaction.message?.trim()
      payload = {
        type: 'reject',
        baseRevision: interaction.revision,
        ...(message ? { message } : {}),
      }
    }
  }

  return {
    threadId: conversation.threadId,
    runId: createRunId(),
    state: {},
    messages: [],
    tools: [],
    context: [],
    forwardedProps: {
      model: conversation.model,
      mode: conversation.mode,
    },
    resume: [{
      interruptId: interaction.interruptId,
      status: 'resolved',
      payload,
    }],
  }
}

export const buildPlanAbandonPayload = (
  conversation: Conversation,
): ChatRequestPayload => {
  const interaction = conversation.planInteraction
  if (!interaction) throw new Error("当前会话没有可取消的 Plan 请求")
  return {
    threadId: conversation.threadId,
    runId: createRunId(),
    state: {},
    messages: [],
    tools: [],
    context: [],
    forwardedProps: {
      model: conversation.model,
      mode: 'default',
    },
    resume: [{
      interruptId: interaction.interruptId,
      status: 'cancelled',
    }],
  }
}

export const markConversationDetached = (
  conversation: Conversation,
  reason = "已停止接收实时输出，后端任务可能仍在继续。",
): Conversation => {
  if (conversation.runStatus !== "streaming") return conversation
  return setConversationNotice(
    {
      ...conversation,
      runStatus: "detached",
      activeRunId: undefined,
      approval: conversation.approval ? { ...conversation.approval, submitted: false } : conversation.approval,
    },
    reason,
    "info",
  )
}

export const prepareResumeSubmission = (
  conversation: Conversation,
  expectedInterruptIds?: readonly string[],
): Conversation => {
  if (
    expectedInterruptIds
    && !matchesApprovalGroup(conversation.approval, expectedInterruptIds)
  ) return conversation

  return {
    ...conversation,
    runStatus: "streaming",
    notice: undefined,
    messages: conversation.messages.map((message) => {
      if (message.role !== "tool") return message
      const matchesApproval = conversation.approval?.items.some(
        (item) => item.toolCallId && item.toolCallId === message.meta?.toolCallId,
      )
      if (!matchesApproval) return message
      return {
        ...message,
        meta: {
          ...message.meta,
          status: "running",
          interruptId: undefined,
        },
      }
    }),
    approval: conversation.approval
      ? { ...conversation.approval, submitted: true, error: undefined }
      : conversation.approval,
  }
}

const restorePendingInteraction = (conversation: Conversation): Conversation => {
  const approval = conversation.approval
    ? { ...conversation.approval, submitted: false }
    : undefined
  if (approval) delete approval.error
  const planInteraction = conversation.planInteraction
    ? { ...conversation.planInteraction, submitted: false }
    : undefined
  if (planInteraction) delete planInteraction.error
  const interruptByToolId = new Map<string, string>()
  for (const item of approval?.items ?? []) {
    if (item.toolCallId) interruptByToolId.set(item.toolCallId, item.interruptId)
  }
  return {
    ...conversation,
    runStatus: "waiting_approval",
    activeRunId: undefined,
    approval,
    planInteraction,
    messages: conversation.messages.map((message) => {
      const toolCallId = message.meta?.toolCallId
      const interruptId = toolCallId
        ? interruptByToolId.get(toolCallId)
        : undefined
      if (message.role !== "tool" || !interruptId) return message
      return {
        ...message,
        meta: {
          ...message.meta,
          status: "paused",
          interruptId,
        },
      }
    }),
  }
}

export const applyConversationEvent = (
  conversation: Conversation,
  event: ConversationAgUiEvent,
): Conversation => {
  switch (event.type) {
    case "RAW": {
      if (
        event.source !== "langgraph.tasks"
        || event.rawEvent?.type !== "tasks"
        || event.rawEvent.phase !== "start"
      ) return conversation
      const provenanceValue = event.event.provenance
      if (
        !provenanceValue
        || typeof provenanceValue !== "object"
        || Array.isArray(provenanceValue)
      ) return conversation
      const subagents = provenanceValue.subagents
      if (!Array.isArray(subagents)) return conversation
      return subagents.reduce((current, value) => {
        const provenance = parseSubagentProvenance(value)
        return startSubagentRun(current, provenance)
      }, conversation)
    }

    case "RUN_STARTED": {
      const persistedUserMessages: Message[] = (event.input?.messages ?? [])
        .filter((message) => message.role === "user" && typeof message.content === "string")
        .filter((message) => !conversation.messages.some((existing) => existing.id === message.id))
        .map((message) => ({
          id: message.id,
          role: "user",
          content: message.content,
          createdAt: nowIso(),
        }))
      const isResume = (event.input?.resume?.length ?? 0) > 0
      const initializationFailed = event.rawEvent?.initializationFailed === true
      const preservePending = initializationFailed
        && Boolean(conversation.approval || conversation.planInteraction)
      const pending = preservePending
        ? restorePendingInteraction(conversation)
        : conversation
      const mode = isAgentMode(event.input?.forwardedProps?.mode)
        ? event.input.forwardedProps.mode
        : conversation.mode
      return {
        ...pending,
        threadId: event.threadId,
        title: event.title?.trim() || conversation.title,
        mode,
        activeRunId: preservePending ? undefined : event.runId,
        runStatus: preservePending ? "waiting_approval" : "streaming",
        notice: undefined,
        approval: isResume && !preservePending ? undefined : pending.approval,
        planInteraction: preservePending ? pending.planInteraction : undefined,
        messages: (
          persistedUserMessages.length > 0
            ? [...pending.messages, ...persistedUserMessages]
            : pending.messages
        ).map((message) => (
          isResume && !preservePending && message.meta?.status === "paused"
            ? {
                ...message,
                meta: {
                  ...message.meta,
                  status: "running" as const,
                  interruptId: undefined,
                },
              }
            : message
        )),
      }
    }

    case "MESSAGES_SNAPSHOT":
      // MESSAGES_SNAPSHOT 是标准 AG-UI 对话投影，不是完整 UI 快照，不能作为会话
      // 权威状态；Todo、工具和子智能体卡片仍由事件流驱动，仅在助手文本为空时替换，
      // 避免覆盖正在流式生成的内容
      return {
        ...conversation,
        messages: conversation.messages.length === 0
          ? event.messages
            .filter((message) => message.role === "user" || message.role === "assistant")
            .map((message) => ({
              id: message.id,
              role: message.role === "user" ? "user" : "assistant",
              content: typeof message.content === "string" ? message.content : "",
              createdAt: nowIso(),
            }))
          : conversation.messages,
      }

    case "STATE_SNAPSHOT":
      return syncTodosFromState({
        ...conversation,
        serverState: event.snapshot,
      }, event.snapshot)

    case "STATE_DELTA":
      {
        const nextState = applyStateDelta(conversation.serverState, event.delta)
        return syncTodosFromState({
          ...conversation,
          serverState: nextState,
        }, nextState)
      }

    case "TEXT_MESSAGE_START": {
      const rawEvent = rawEventOrMain(conversation, event.rawEvent)
      if (rawEvent.source.agentType === "subagent") {
        return updateSubagentRun(conversation, rawEvent, (message) => ({
          ...message,
          meta: {
            ...message.meta,
            agentName: rawEvent.source.agentName,
            status: "running",
          },
        }))
      }
      return upsertAssistantMessage(conversation, event.messageId, (message) => ({
        id: event.messageId,
        role: "assistant",
        content: message?.content ?? "",
        createdAt: message?.createdAt ?? nowIso(),
        meta: {
          ...message?.meta,
          status: "running",
          runId: conversation.activeRunId,
        },
      }))
    }

    case "TEXT_MESSAGE_CONTENT": {
      const rawEvent = rawEventOrMain(conversation, event.rawEvent)
      if (rawEvent.source.agentType === "subagent") {
        return updateSubagentRun(conversation, rawEvent, (message) => ({
          ...message,
          meta: {
            ...message.meta,
            agentName: rawEvent.source.agentName,
            result: `${message.meta?.result ?? ""}${event.delta}`,
            status: "running",
          },
        }))
      }
      return upsertAssistantMessage(conversation, event.messageId, (message) => ({
        id: event.messageId,
        role: "assistant",
        content: `${message?.content ?? ""}${event.delta}`,
        createdAt: message?.createdAt ?? nowIso(),
        meta: {
          ...message?.meta,
          status: "running",
          runId: conversation.activeRunId,
        },
      }))
    }

    case "TEXT_MESSAGE_END": {
      const rawEvent = rawEventOrMain(conversation, event.rawEvent)
      if (rawEvent.source.agentType === "subagent") {
        return updateSubagentRun(conversation, rawEvent, (message) => ({
          ...message,
          meta: {
            ...message.meta,
            agentName: rawEvent.source.agentName,
          },
        }))
      }
      return updateMessage(
        conversation,
        (message) => message.id === event.messageId,
        (message) => ({
          ...message,
          meta: {
            ...message.meta,
            status: "completed",
          },
        }),
      )
    }

    case "REASONING_START":
    case "REASONING_MESSAGE_START":
    case "REASONING_MESSAGE_CONTENT":
    case "REASONING_MESSAGE_END":
    case "REASONING_END":
      // 线上保留标准 AG-UI 事件，但会话工作区不持久化或渲染模型推理
      return conversation

    case "TOOL_CALL_START":
      {
        const rawEvent = rawEventOrMain(conversation, event.rawEvent)
        const sourceRunId = runIdForSource(conversation, rawEvent)
        const withSubagentName = rawEvent.source.agentType === "subagent"
          ? updateSubagentRun(conversation, rawEvent, (message) => ({
              ...message,
              meta: {
                ...message.meta,
                agentName: rawEvent.source.agentName,
                status: "running",
              },
            }))
          : conversation
        return upsertToolMessage(withSubagentName, event.toolCallId, (message) => ({
          id: message?.id ?? event.toolCallId,
          role: "tool",
          content: event.toolCallName === "task"
            ? `委派 ${message?.meta?.agentName ?? "subagent"}`
            : event.toolCallName,
          createdAt: message?.createdAt ?? nowIso(),
          meta: {
            ...message?.meta,
            toolName: event.toolCallName,
            params: message?.meta?.params ?? "",
            result: message?.meta?.result ?? "",
            status: message?.meta?.status === "paused" ? "paused" : "running",
            toolCallId: event.toolCallId,
            parentMessageId: event.parentMessageId,
            batchId: message?.meta?.batchId ?? event.parentMessageId,
            runId: sourceRunId ?? conversation.activeRunId,
            graphTaskId: rawEvent.source.graphTaskId ?? message?.meta?.graphTaskId,
            sourceAgentName:
              rawEvent.source.agentType === "subagent"
                ? rawEvent.source.agentName
                : undefined,
          },
        }))
      }

    case "TOOL_CALL_ARGS":
      return upsertToolMessage(conversation, event.toolCallId, (message) => {
        const params = `${message?.meta?.params ?? ""}${event.delta}`
        const taskDescriptor = message?.meta?.toolName === "task" ? parseTaskDescriptor(params) : null
        return {
          id: message?.id ?? event.toolCallId,
          role: "tool",
          content: taskDescriptor
            ? `委派 ${taskDescriptor.agentName}`
            : (message?.content ?? message?.meta?.toolName ?? "tool"),
          createdAt: message?.createdAt ?? nowIso(),
          meta: {
            ...message?.meta,
            params,
            agentName: taskDescriptor?.agentName ?? message?.meta?.agentName,
            input: taskDescriptor?.input ?? message?.meta?.input,
            status: message?.meta?.status === "paused" ? "paused" : "running",
          },
        }
      })

    case "TOOL_CALL_END":
      return conversation

    case "TOOL_CALL_RESULT":
      {
        const rawEvent = rawEventOrMain(conversation, event.rawEvent)
        const completedAt = nowIso()
        let next = upsertToolMessage(conversation, event.toolCallId, (message) => {
          const createdAt = message?.createdAt ?? completedAt
          return {
            id: message?.id ?? event.toolCallId,
            role: "tool",
            content: message?.content ?? message?.meta?.toolName ?? "tool",
            createdAt,
            meta: {
              ...message?.meta,
              result: event.content,
              status: rawEvent.toolResultStatus === "error" ? "failed" : "completed",
              completedAt,
              durationMs: elapsedMs(createdAt, completedAt),
              runId:
                message?.meta?.runId
                ?? runIdForSource(conversation, rawEvent)
                ?? conversation.activeRunId,
              graphTaskId:
                rawEvent.source.graphTaskId
                ?? message?.meta?.graphTaskId,
              sourceAgentName:
                rawEvent.source.agentType === "subagent"
                  ? rawEvent.source.agentName
                  : message?.meta?.sourceAgentName,
            },
          }
        })
        const taskMessage = next.messages.find(
          (message) =>
            message.role === "tool"
            && message.meta?.toolCallId === event.toolCallId
            && message.meta?.toolName === "task",
        )
        const relatedSubagentInvocationId = rawEvent.relatedSubagentInvocationId
        if (taskMessage && relatedSubagentInvocationId) {
          const relatedSubagent = next.messages.find(
            (message) => message.role === "subagent"
              && message.meta?.subRunId === relatedSubagentInvocationId,
          )
          const graphTaskId = relatedSubagent?.meta?.graphTaskId
          next = updateMessage(
            next,
            (message) => message.id === taskMessage.id,
            (message) => ({
              ...message,
              meta: {
                ...message.meta,
                subRunId: relatedSubagentInvocationId,
                graphTaskId,
              },
            }),
          )
          next = updateMessage(
            next,
            (message) => message.role === "subagent"
              && message.meta?.subRunId === relatedSubagentInvocationId,
            (message) => ({
              ...message,
              content: taskMessage.content,
              meta: {
                ...message.meta,
                agentName: taskMessage.meta?.agentName ?? message.meta?.agentName,
                input: taskMessage.meta?.input ?? message.meta?.input,
                result: event.content,
                status: rawEvent.toolResultStatus === "error" ? "failed" : "completed",
                toolCallId: event.toolCallId,
                graphTaskId,
                completedAt: message.meta?.completedAt ?? completedAt,
                durationMs:
                  message.meta?.durationMs
                  ?? elapsedMs(message.createdAt, completedAt),
              },
            }),
          )
        } else if (rawEvent.source.agentType === "subagent") {
          next = updateSubagentRun(next, rawEvent, (message) => ({
            ...message,
            meta: {
              ...message.meta,
              agentName: rawEvent.source.agentName,
            },
          }))
        }
        return next
      }

    case "RUN_FINISHED":
      {
        const outcome = event.outcome ?? { type: "success" as const }
        if (conversation.activeRunId && event.runId !== conversation.activeRunId) {
          return conversation
        }

      if (outcome.type === "interrupt") {
        const planInteraction = planInteractionFromInterrupts(outcome.interrupts)
        if (planInteraction) {
          return {
            ...conversation,
            threadId: event.threadId,
            runStatus: "waiting_approval",
            activeRunId: undefined,
            approval: undefined,
            planInteraction,
          }
        }
        return attachApproval(
          markInterruptedToolCards(
            {
              ...conversation,
              threadId: event.threadId,
              runStatus: "waiting_approval",
              activeRunId: undefined,
            },
            event.runId,
            outcome.interrupts,
          ),
          outcome.interrupts,
        )
      }

      return {
        ...conversation,
        threadId: event.threadId,
        runStatus: "idle",
        activeRunId: undefined,
        approval: undefined,
        planInteraction: undefined,
      }
      }

    case "RUN_ERROR":
      {
        const rawEvent = rawEventOrMain(conversation, event.rawEvent)
        const completedAt = nowIso()
        const errorMessage = event.message ?? "对话运行失败。"
        const isCancelled = event.code === "cancelled" || event.code === "resume_cancelled"
        const errorRunId = rawEvent.runId ?? conversation.activeRunId
        if (
          rawEvent.initializationFailed === true
          && (conversation.approval || conversation.planInteraction)
        ) {
          return setConversationNotice(
            restorePendingInteraction(conversation),
            errorMessage,
            "error",
          )
        }
        return setConversationNotice(
          {
            ...conversation,
            runStatus: isCancelled ? "idle" : "error",
            activeRunId: undefined,
            approval: undefined,
            planInteraction: undefined,
            messages: conversation.messages.map((message) => (
              (message.role === "tool" || message.role === "subagent")
                && (message.meta?.status === "running" || message.meta?.status === "paused")
                && (
                  message.meta?.subRunId == null
                  || message.meta.lastMainRunId === errorRunId
                )
                ? {
                    ...message,
                    meta: {
                      ...message.meta,
                      status: "failed" as const,
                      result: message.role === "subagent"
                        ? message.meta?.result || errorMessage
                        : errorMessage,
                      completedAt,
                      durationMs: elapsedMs(message.createdAt, completedAt),
                    },
                  }
                : message
            )),
          },
          errorMessage,
          "error",
        )
      }

    default:
      return conversation
  }
}

// ---------------------------------------------------------------------------
// 历史恢复：v3 快照与严格有序的尾部事件
// ---------------------------------------------------------------------------

const currentSnapshot = (
  detail: ConversationHistoryDetail,
): ConversationSnapshotJson | null => {
  if (detail.snapshotVersion !== 3) {
    throw new Error("会话历史只接受 snapshotVersion=3")
  }
  if (detail.lastSeq < detail.snapshotSeq) {
    throw new Error("会话历史 lastSeq 不能小于 snapshotSeq")
  }
  const snapshot = detail.snapshot ?? null
  if (snapshot == null) {
    if (detail.snapshotSeq !== 0) {
      throw new Error("空会话快照必须对应 snapshotSeq=0")
    }
    return null
  }
  if (snapshot.snapshotVersion !== 3) {
    throw new Error("会话历史快照只接受 snapshotVersion=3")
  }
  if (snapshot.snapshotSeq !== detail.snapshotSeq) {
    throw new Error("会话历史快照序号与详情不一致")
  }
  return snapshot
}

const messagesFromSnapshot = (snapshot: ConversationSnapshotJson | null | undefined): Message[] => {
  if (!snapshot) return []
  return snapshot.messages.map((message) => ({
    ...message,
    meta: message.meta ? { ...message.meta } : undefined,
  }))
}

/**
 * 按版本化历史契约恢复持久化会话
 *
 * v3 快照包含完整 UI 投影，只回放 `snapshotSeq` 之后的事件；新会话允许
 * `snapshot=null` 与 `snapshotSeq=0`，其他版本或序号矛盾直接拒绝
 */
export const restoreConversationFromHistory = (
  detail: ConversationHistoryDetail,
  options: { model: string },
): Conversation => {
  const snapshot = currentSnapshot(detail)
  const hasSnapshot = snapshot != null
  const snapshotPlanInteraction = hasSnapshot
    ? planInteractionFromInterrupts(snapshot.interrupts)
    : undefined
  const baseline: Conversation = {
    threadId: detail.threadId,
    title: detail.title,
    pinned: detail.pinned,
    updatedAt: detail.updatedAt,
    model: options.model,
    mode: hasSnapshot && snapshot.mode === "plan" ? "plan" : "default",
    messages: hasSnapshot ? messagesFromSnapshot(snapshot) : [],
    todos: hasSnapshot
      ? snapshot.todos.map((todo) => {
          const restoredTodo = { ...todo }
          delete restoredTodo.targetMessageId
          return restoredTodo
        })
      : [],
    plan: null,
    approval: hasSnapshot && !snapshotPlanInteraction && snapshot.approval
      ? snapshot.approval
      : undefined,
    planInteraction: snapshotPlanInteraction,
    runStatus: ((): Conversation["runStatus"] => {
      switch (detail.status) {
        // 历史水化不拥有原始 SSE 连接；服务端仍在运行时，本地应标记为断连并通过
        // 持久化事件追赶，不能展示无效的停止按钮
        case "running": return "detached"
        case "waiting_approval": return "waiting_approval"
        case "error": return "error"
        default: return "idle"
      }
    })(),
    activeRunId: hasSnapshot
      ? snapshot.activeRunId ?? undefined
      : undefined,
    serverState: hasSnapshot ? snapshot.serverState : {},
    lastSeq: hasSnapshot ? detail.snapshotSeq : 0,
  }

  const restored = detail.events.reduce<Conversation>(
    (conversation, envelope) => applyHistoryEventEnvelope(conversation, envelope),
    baseline,
  )

  // 事件回放只重建 UI 投影，不会创建浏览器持有的 SSE 连接；RUN_STARTED 不能让
  // 已水化历史停在 `streaming` 并暴露无效停止按钮，详情状态才是权威服务端状态
  const hasAuthoritativeApproval = detail.status === "waiting_approval"
    && detail.hasPendingInterrupt
  return {
    ...restored,
    approval: hasAuthoritativeApproval ? restored.approval : undefined,
    planInteraction: hasAuthoritativeApproval ? restored.planInteraction : undefined,
    runStatus: detail.status === "running"
      ? "detached"
      : hasAuthoritativeApproval
        ? "waiting_approval"
        : detail.status === "error"
          ? "error"
          : "idle",
  }
}

export const applyHistoryEventEnvelope = (
  conversation: Conversation,
  envelope: ConversationEventEnvelope,
): Conversation => applyPersistedEventEnvelope(conversation, envelope, false)

export const applyLiveEventEnvelope = (
  conversation: Conversation,
  envelope: ConversationEventEnvelope,
): Conversation => applyPersistedEventEnvelope(conversation, envelope, true)

const applyPersistedEventEnvelope = (
  conversation: Conversation,
  envelope: ConversationEventEnvelope,
  ownsLiveStream: boolean,
): Conversation => {
  const lastSeq = conversation.lastSeq ?? 0
  if (envelope.seq <= lastSeq) return conversation
  if (envelope.seq !== lastSeq + 1) {
    throw new Error(
      `会话事件序号不连续: expected=${lastSeq + 1}, actual=${envelope.seq}`,
    )
  }
  const event = envelope.event as unknown as ConversationAgUiEvent
  const next = applyConversationEvent(conversation, event)
  const previousById = new Map(conversation.messages.map((message) => [message.id, message]))
  const timestampedMessages = next.messages.map((message) => {
    const previous = previousById.get(message.id)
    const createdAt = previous?.createdAt ?? envelope.createdAt
    const completionChanged = message.meta?.completedAt != null
      && message.meta.completedAt !== previous?.meta?.completedAt
    if (!completionChanged && previous) return message
    return {
      ...message,
      createdAt,
      meta: completionChanged
        ? {
            ...message.meta,
            completedAt: envelope.createdAt,
            durationMs: elapsedMs(createdAt, envelope.createdAt),
          }
        : message.meta,
    }
  })
  return {
    ...next,
    // 持久化追赶属于回放，不是页面持有的实时连接；回放的 RUN_STARTED 可以标识
    // 服务端活跃运行，但不能暴露停止等仅适用于实时连接的控件
    runStatus: !ownsLiveStream && next.runStatus === "streaming"
      ? "detached"
      : next.runStatus,
    messages: timestampedMessages,
    lastSeq: envelope.seq,
  }
}
