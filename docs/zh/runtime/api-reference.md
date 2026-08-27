# Runtime 使用参考

[Runtime 入门](index.md) · [English](../../en/runtime/api-reference.md)

这一页按实际使用顺序汇总 Runtime 的公开能力。常见项目通常只会用到前两组。

## 创建入口

| API | 什么时候用 | 主要参数或结果 |
| --- | --- | --- |
| `TinkerFin(run_coordinator=None, state_schema=None)` | 创建统一入口 | 可选共享 coordinator 和 Definition 级 state |
| `TinkerFin.plan(...)` | 创建不可变的 Plan-capable factory | 能力和 mode 默认值、可选 Planner 模型、澄清表单、计划内容 Schema 与审阅动作 |
| `TinkerFin.create_deep_agent(...)` | 创建可重复生成 Runtime 的 Agent 定义 | 参数见[创建和运行 Deep Agent](deep-agents.md) |
| `Identity(threadId=..., runId=...)` | 表示一次框架运行 | 只包含 thread 和 run |
| `DeepAgentDefinition.new(...)` | 创建原生 Runtime | 必填 `identity`，可选本次请求 `mode` 和 `on_part` |
| `DeepAgentDefinition.new_agui(...)` | 创建 AG-UI Runtime | 必填 canonical `identity`；可选 parent、mode、resume、checkpoint callback 与 observer |

`DeepAgentDefinition` 可以重复使用。`DeepAgentRuntime`、`DeepAgentAgUiRuntime` 和
`DeepAgentAgUiResumeRuntime` 都是一次性运行对象，不要自行构造。

`.plan(enabled=True)` 只影响从返回 factory 创建的 Definition，不会给
`create_deep_agent(...)` 增加参数。返回 factory 保留 coordinator 和全局 state schema。
真正选择 Plan 时必须提供明确 Planner 模型和具体生产级 checkpointer；default 保持上游要求。
Plan 配置不合法时抛出 `tinkerfin.plan.PlanModeConfigurationError`。

每次 `new()` 或 `new_agui()` 通过 `mode="default"` 或 `mode="plan"` 选择当前路径；省略时
使用 `.plan(default_mode=...)`。default 直接运行原生 Deep Agent；plan 运行独立 Planning
Graph，并在批准后自动交给原生执行。同一 checkpoint thread 后续可以再次选择 Plan。普通
Definition 只接受 `default`。

## Plan Mode 数据

顶层包导出 `AgentMode`。`tinkerfin.plan` 导出 `ClarificationModel`、
`ClarificationOption`、`ClarificationForm`、`BuiltInClarificationForm`、四类内置 Question、
`ClarificationType`、`clarification_type`、`DefaultClarificationForm`、`PlanContentModel`、`StructuredPlanStep`、
`StructuredPlanContent`、`MarkdownPlanContent`、`PlanSchemaReference`、`PlanDraft`、
`ConfirmedPlan`、`RequirementAnswer`、`PendingClarification`、`ClarificationExchange`、
`PlanState`、`PlanStatus`、`PlanHandoff`、`PlanHandoffPhase`、`PlanReviewAction` 和 Plan 错误类型。这些模型不可变。
Planning 状态以 camel case JSON 保存在 `tinkerfin_plan`；批准 handoff 提交后
`effectiveMode` 为 `default`。

`PlanHandoffPhase` 按 `pending → accepted → completed` 推进。accepted 会记录包含确定性 handoff
消息的原生 checkpoint；completed 还会记录原生终止 checkpoint。重试会对账这些 checkpoint，
不会重新派发已经批准的 Plan。

`.plan(content_schema=...)` 接受一个具体 `PlanContentModel` 子类。省略时使用
`StructuredPlanContent`；`MarkdownPlanContent` 原样保留一段非空 Markdown，不改写空白。
运行时自动校验并计算所选 JSON Schema 的 fingerprint，在恢复时拒绝 Definition 漂移。
被审阅的草稿与 `ConfirmedPlan` 始终使用同一个冻结内容 Schema。

`.plan(allowed_review_actions=...)` 接受一组有序、非空且不重复的 `PlanReviewAction`。默认动作为
`APPROVE`、`RESPOND` 和 `REJECT`，interrupt 的响应 Schema 只包含实际配置的动作。只有宿主
提供可信计划编辑器时才应显式加入 `EDIT`。

`.plan(clarification_schema=...)` 接受一个具体 `ClarificationFormBase` 子类；省略时启用四类内置
answer type。共享强类型 metadata 使用
`BuiltInClarificationForm[QuestionAttributes, OptionModel]`。完全自定义语义题型通过
`.plan(clarification_types=(clarification_type(...),))` 一次注册。逐题 Schema、校验或规范化
语义变化时必须升级 type ID 版本。

澄清响应的 `answers` 是以 checkpoint question ID 为 key 的 object。已回答 value 包含
`status: answered`、对应 `answerType` 和题型字段；可选题跳过只包含 `status: skipped`。
pending 的精确 JSON Schema 会在 Graph resume 前验证完整覆盖、Option ID、多选数量、非空文本和
真实日历日期。宿主模型不能重新定义框架拥有的 ID、required/skip 语义或 checkpoint 所有权。

`TinkerFin(state_schema=...)` 为该 factory 创建的每个原生 Deep Agent Definition 提供应用级
state。独立 Planning Graph 组合自己的所需视图，不改变原生 default topology 或 middleware。
reducer、`Required` / `NotRequired` 和 schema metadata 会保留；同名字段合同不兼容时在 Plan
运行前失败。运行时 `context_schema` 仍是独立且不进入 checkpoint 的 context 合同。

## 流对象

| API | 用法 |
| --- | --- |
| `NativeGraphRunStream` | 异步迭代规范化的 LangGraph v2 数据，可直接交给 Messaging |
| `AgUiEventStream` | 异步迭代 AG-UI 事件；支持 `abort()`、`aclose()` 和 `to_sse()` |
| `SseBody` | 异步迭代 SSE 字符串；先 `prepare()`，结束时 `aclose()` |

### `AgUiEventStream.abort()`

请求停止当前 AG-UI 运行，并返回为正确结束开放事件所需的剩余事件。多次调用不会重复产生终止尾部。

### `AgUiEventStream.from_initialization_error(...)`

如果 Graph 尚未开始就初始化失败，可以用它生成一条合法的 AG-UI 失败流。常规 `new_agui()` 调用不需要直接使用。

## 原生数据模型

`NativeStreamPart` 是可持久化的原生 v2 数据。

| 字段 | 类型 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `type` | 字符串 | 必填 | `messages`、`tasks`、`values`、`updates`、`checkpoints`、`debug` 或 `custom` |
| `ns` | 字符串元组 | 必填 | Graph namespace；空元组表示根 Graph |
| `data` | JSON 值 | 必填 | 当前模式的数据 |
| `interrupts` | JSON 值元组 | `()` | `values` 携带的 interrupt |

通常由 Runtime 自动创建，不需要手工拼装。

## AG-UI 流预检

`new_agui()` 固定使用 `messages`、`tasks`、`values`、`version="v2"` 和
`subgraphs=True`。调用方可以通过 Runtime 的 `astream(stream_mode=...)` 增加
`updates`、`checkpoints`、`debug` 或 `custom`。缺少必需 mode、重复或不支持的 mode、
冲突的 version、关闭 subgraphs 都会在 Graph 或 coordinator 产生副作用前抛出
`AgUiNativeStreamConfigurationError`。

## SSE 类型

| API | 作用 |
| --- | --- |
| `SsePayload` | 描述 `data`、可选 `event` 和可选 `retry` |
| `SseMapper` | 把流对象异步转换成 `SsePayload` 或 `None` 的函数类型 |
| `SseEventIdResolver` | 异步返回每条事件 ID 的函数类型 |
| `SsePreflight` | `prepare()` 执行的异步预检查函数类型 |

## 观察和协调类型

| API | 作用 |
| --- | --- |
| `PartObserver` | 观察原生数据的异步函数 |
| `EventObserver` | 观察 AG-UI 事件的异步函数 |
| `join_task(task, cancel=False, suppress_task_cancellation=False)` | owned task 完整结算后再传播调用方取消 |
| `RunCoordinator` | 自定义运行互斥边界 |
| `InMemoryRunCoordinator(key_resolver=...)` | 当前进程内按业务 key 串行运行 |

安装 `tinkerfin[redis]` 后还可使用：

| API | 作用 |
| --- | --- |
| `RedisRunCoordinator` | 多进程按 `Identity` 串行运行 |
| `RedisLeaseLock` | 自动续期的通用 Redis 租约锁 |
| `RedisLease` | `hold()` 返回的不可变资源 key 与 fencing token |
| `RedisLeaseLost` | 续期不确定、租约过期或所有权丢失 |

构造参数、默认值和连接池要求见[运行协调与 Redis 租约](extensions.md#如果需要通用-redis-租约锁)。

## 恢复和错误

| API | 什么时候遇到 |
| --- | --- |
| `AgUiResumeBinding` | 恢复被 interrupt 暂停的 AG-UI 运行 |
| `AgUiResumeCheckpoint` | resume marker 持久化后的稳定回调值 |
| `tinkerfin.plan.PlanModeConfigurationError` | Plan Definition 缺少具体 saver 或明确模型、state schema 不兼容，或使用了非 sync durability |
| `AgUiSettlementTimeoutError` | 调用方停止等待，但 Runtime 的清理仍未在限定时间内完成 |
| `AgUiNativeStreamConfigurationError` | 原生流配置不符合 AG-UI 转换要求 |

`DeepAgentDefinition.new_agui(...)` 使用以下请求级参数：

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `identity` | 必填 | canonical thread 与 run 身份 |
| `parent_run_id` | `None` | `RUN_STARTED` 暴露的可选 checkpoint 谱系 |
| `mode` | Definition 默认值 | 为本次请求选择原生 default 或 Plan 路径 |
| `timeout` | `None` | 等待原生数据的总时限 |
| `settlement_timeout` | `None` | 调用方等待安全清理的时限 |
| `expose_reasoning_events` | `False` | 是否交付支持的推理事件 |
| `expose_subagent_events` | `True` | 是否交付子 Agent 事件 |
| `resume` | `None` | 恢复请求使用的完整可信 `AgUiResumeBinding` |
| `on_resume_checkpointed` | `None` | 确切 resume marker 可读取后的幂等 callback |
| `on_event` | `None` | AG-UI 事件交付前的观察函数 |

返回的 Runtime 保留已安装 Graph 的 `astream(...)` 参数形状。框架会把同一个 `Identity`
写入 Graph 配置，`astream()` 不再接收第二套 Runtime 身份。

`AgUiResumeBinding.from_agui(...)` 校验完整可信 AG-UI interrupt 与 entries，包括原生分组、
取消模式、Tool ID 与来源 Agent。Binding 不保存 identity 或 parent，也不公开原生 command；宿主可把
它作为一个完整 Pydantic 模型持久化和恢复。

恢复流程见 [interrupt 与恢复](../agui/interrupts-and-resume.md)。
