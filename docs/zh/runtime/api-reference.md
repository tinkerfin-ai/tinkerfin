# Runtime 使用参考

[Runtime 入门](index.md) · [English](../../en/runtime/api-reference.md)

AG-UI Runtime 入口需要安装 `pip install "tinkerfin[agui]"`。原生 Runtime、Plan Mode、
Observation 与原生 SSE 属于基础安装。

这一页按实际使用顺序汇总 Runtime 的公开能力。常见项目通常只会用到前两组。

## 常用入口

| API | 什么时候用 | 主要参数或结果 |
| --- | --- | --- |
| `TinkerFin(checkpointer=None, run_coordinator=None, state_schema=None, runtime_profile=None)` | 创建统一入口 | 可选 borrowed 默认 saver、共享 coordinator、Definition 级 state 与集成 Profile |
| `TinkerFin.observe(observer)` | 不可变地增加一个 Runtime observer | `tinkerfin-contracts` 的 `RuntimeObserver` |
| `TinkerFin.plan(...)` | 创建不可变的 Plan-capable factory | 能力和 mode 默认值、可选 Planner 模型、澄清表单、计划内容 Schema 与审阅动作 |
| `TinkerFin.create_deep_agent(...)` | 创建可重复生成 Runtime 的 Agent 定义 | 参数见[创建和运行 Deep Agent](deep-agents.md) |
| `TinkerFin.open_run(identity, *, agent, input, ...)` | 打开一次 managed 原生运行 | 单次使用的 `NativeGraphRunStream` |
| `TinkerFin.open_agui_run(identity, *, agent, input=... or resume=..., ...)` | 打开一次 managed AG-UI 运行 | 单次使用的 `AgUiEventStream` |
| `DeepAgentDefinition.create_graph(mode=...)` | 在 managed 生命周期外复用异步 Runnable | 完整 native 或 Plan-capable `DeepAgentGraph` |
| `RunIdentity(threadId=..., runId=...)` | 表示一次框架运行 | 只包含 thread 和 run |

`agent` 可以是已有 Definition，也可以是返回 Definition 的同步或异步 callable。Managed 门面
负责 Definition 解析、异步 Graph 构造、身份绑定、Observation、协调、setup 失败转换、取消与
清理。`open_agui_run()` 要求 `input` 与 `resume` 严格二选一。

## 高级集成入口

| API | 什么时候用 | 主要参数或结果 |
| --- | --- | --- |
| `DeepAgentsRuntimeProfile` | 实现完整上游集成 | canonical Profile ID、Graph factory、Native Stream Driver 与 resume checkpoint 语义 |
| `DeepAgentsFactoryPreparation` | 返回 Profile 管理的 factory overrides | 只读 override mapping 与外部 subagent 取消边界 |
| `DeepAgentsV2RuntimeProfile(...)` | 使用当前内置集成 | 锁定的建图、调用、校验、Observation、reasoning extractor 与 replay |
| `DeepAgentDefinition.new(...)` | 创建原生 Runtime | 必填 `identity`，可选本次请求 `mode` 和 `on_part` |
| `DeepAgentDefinition.new_agui(...)` | 创建 AG-UI Runtime | 必填 canonical `identity`；可选 parent、mode、resume、checkpoint callback 与 observer |
| `TinkerFin.failed_agui_run(...)` | 表达 Run 接受后的初始化失败 | 错误、identity，以及真实可用的 input/config/resume |
| `trace_contribution(kind=..., name=..., input=None)` | 发布显式 Memory、Guardrail、retrieval 或自定义语义 | 可用 `set_result(...)` 补充结果的异步上下文管理器 |

默认当前 Profile 是 `DeepAgentsV2RuntimeProfile`。TinkerFin 在创建 Definition 前选定一个
Profile，把其 `profile_id` 写入 checkpoint 谱系，并在 Graph continuation 前拒绝由另一个
Profile 执行 branch 或 resume；Runtime 不从流数据探测或协商 Profile。只有向 Profile 显式
传入已验证的 `ReasoningExtractor`（例如 `DeepSeekReasoningExtractor`）才会启用对应 provider
reasoning 路径；这本身不会授权 Trace 持久化。自定义 Profile 还必须实现自身明确的
`stage_resume_intent()`、`pending_resume_values()` 与 `native_resume_submitted()` checkpointer
语义；TinkerFin 不会为它回退到 v2 checkpoint 行为。

`DeepAgentDefinition` 可以重复使用。`DeepAgentRuntime`、`DeepAgentAgUiRuntime` 和
`DeepAgentAgUiResumeRuntime` 都是一次性运行对象，不要自行构造。

`.plan(enabled=True)` 只影响从返回 factory 创建的 Definition，不会给
`create_deep_agent(...)` 增加参数。返回 factory 保留 coordinator 和全局 state schema。
真正选择 Plan 时必须提供 Planner model 或 Agent model 之一，以及具体生产级 checkpointer；
省略 `planner_model` 时使用 Agent model，default 保持上游要求。
Plan 配置不合法时抛出 `tinkerfin.plan.PlanModeConfigurationError`。

每次 managed run 或 direct Graph 通过 `mode="default"` 或 `mode="plan"` 选择当前路径；省略时
使用 `.plan(default_mode=...)`。default 直接运行原生 Deep Agent；plan 运行独立 Planning
Graph，并在批准后自动交给原生执行。同一 checkpoint thread 后续可以再次选择 Plan。普通
Definition 只接受 `default`。

## Plan Mode 数据

顶层包导出 `AgentMode`。`tinkerfin.plan` 导出 `ClarificationModel`、
`ClarificationOption`、`ClarificationForm`、`BuiltInClarificationForm`、六类内置 Question、
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

只有 `APPROVE` 会退出 Planning 并把确认后的草稿交给原生执行。`REJECT` 使当前草稿失效，接受可选
原因，产生一次用户可见的 Planner 回复，并以 `awaiting_input`、effective mode `plan` 等待下一条规划
输入。显式配置的 `CANCEL` 采用相同续接但不携带原因。AG-UI `status="cancelled"` 仍表示真正放弃，
不会转换为 Plan 决定。

`.plan(clarification_schema=...)` 接受一个具体 `ClarificationFormBase` 子类；省略时启用六类内置
answer type。共享强类型 metadata 使用
`BuiltInClarificationForm[QuestionAttributes, OptionModel]`。完全自定义语义题型通过
`.plan(clarification_types=(clarification_type(...),))` 一次注册。逐题 Schema、校验或规范化
使用无版本 namespaced type ID。Definition fingerprint 固定唯一当前 Schema 与 callback；合同变化
时必须重建不兼容存量数据，不得新增 type ID 版本。

澄清响应的 `answers` 是以 checkpoint question ID 为 key 的 object。已回答 value 包含
`status: answered`、对应 `answerType` 和题型字段；可选题跳过只包含 `status: skipped`。
pending 的精确 JSON Schema 会在 Graph resume 前验证完整覆盖、Option ID、多选数量、非空文本和
真实日历日期。时间回答还会验证本地分钟精度、题目声明的 IANA 时区和不跨越午夜的包含边界范围。
宿主模型不能重新定义框架拥有的 ID、required/skip 语义或 checkpoint 所有权。
日期时间回答表示必填 IANA 时区中的一个本地日期和分钟，范围可以跨日期；规范化结果包含
`localDateTime`、`timeZone` 和唯一 UTC `instant`。DST 不存在时间与重复的本地分钟都会被拒绝。


`TinkerFin(state_schema=...)` 为该 factory 创建的每个原生 Deep Agent Definition 提供应用级
state。独立 Planning Graph 组合自己的所需视图，不改变原生 default topology 或 middleware。
reducer、`Required` / `NotRequired` 和 schema metadata 会保留；同名字段合同不兼容时在 Plan
运行前失败。运行时 `context_schema` 仍是独立且不进入 checkpoint 的 context 合同。

## 流对象

| API | 用法 |
| --- | --- |
| `NativeGraphRunStream` | 异步迭代原始上游对象，同时向 Messaging 或 Native SSE 转交同一个 Driver-owned canonical frame |
| `AgUiEventStream` | 异步迭代 AG-UI 事件；支持 `abort()`、`aclose()` 和 `to_sse()` |
| `SseBody` | 单次消费的 SSE body，拥有每次上游读取与关闭任务；交给宿主 Response 前可先调用 `prepare()` |

### `AgUiEventStream.abort()`

请求停止当前 AG-UI 运行，并返回为正确结束开放事件所需的剩余事件。多次调用不会重复产生终止尾部。

### `TinkerFin.failed_agui_run(...)`

高级宿主在进入 managed 门面前已经接受语义 Run 时，可以用它生成受观察的 AG-UI 失败流。
普通模型、Sandbox、Definition 或 Graph setup 失败已经由 `open_agui_run()` 自动转换。

## 原生数据模型

`NativeStreamPart` 是当前有限、可持久化的 canonical replay 表示，由所选 Profile 产生，
不包含上游版本选择字段。

| 字段 | 类型 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `type` | 字符串 | 必填 | `messages`、`tasks`、`values`、`updates`、`checkpoints`、`debug` 或 `custom` |
| `ns` | 字符串元组 | 必填 | Graph namespace；空元组表示根 Graph |
| `data` | JSON 值 | 必填 | 当前模式的数据 |
| `interrupts` | JSON 值元组 | `()` | `values` 携带的 interrupt |

通常由 Runtime 自动创建，不需要手工拼装。

## Native 流预检

所选 Runtime Profile 完整拥有上游调用与 checkpoint 合同，包括必需 mode、上游 version、
subgraph 行为、完整 state 输出、稳定建图/绑定流签名，以及不破坏 interrupted control state 的
durable resume-intent 写入。绑定流签名让 resume Graph 在受保护异步结算内延迟构造。自定义 Profile
需要原生异步建图时可以实现 `create_agent_graph(factory, args, kwargs)`；未实现时，Core 会在 AnyIO
有容量限制的 worker 中调用该 Profile 自己选定的同步 factory，不会替换为内置 v2 factory。当前内置
Profile 允许调用方通过 `astream(stream_mode=...)` 增加受支持的
诊断 mode；移除必需 mode 或传入冲突的上游参数，会在 Graph 或 coordinator 产生副作用前
失败。Native 与 AG-UI 路径共用同一个 Profile 边界，并暴露一致的参数校验错误。

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
| `RuntimeObserver` | 为每个已接纳 Runtime 请求打开一个受管理的 run/native 观察 session |
| `RunObservationSession` | 接收有序 Observation、强制边界、异步失败通知与关闭 |
| `join_task(task, cancel=False, suppress_task_cancellation=False)` | owned task 完整结算后再传播调用方取消 |
| `RunCoordinator` | 自定义运行互斥边界 |
| `InMemoryRunCoordinator(key_resolver=...)` | 当前进程内按业务 key 串行运行 |

安装 `tinkerfin[redis]` 后还可使用：

| API | 作用 |
| --- | --- |
| `RedisRunCoordinator` | 多进程按 `RunIdentity` 串行运行 |
| `RedisLeaseLock` | 自动续期的通用 Redis 租约锁 |
| `RedisLease` | `hold()` 返回的不可变资源 key 与 fencing token |
| `RedisLeaseLost` | 续期不确定、租约过期或所有权丢失 |

构造参数、默认值和连接池要求见[运行协调与 Redis 租约](extensions.md#如果需要通用-redis-租约锁)。

`TinkerFin.observe(...)` 返回独立 factory，并在 `.plan(...)` 后保留 Observer。受管 Deep Agent
请求会安装一个 request-scoped LangChain callback，在 provider 执行前记录 middleware 处理后的
最终模型请求，并在审批或参数编辑后记录实际 Tool 执行。Runtime 同时对每个 Native part 只做一次
强校验，然后在 `on_part` 和 AG-UI 转换前发布安全 Native Observation。Observer 失败会让 Run
fail-closed，Runtime 仍会关闭全部已打开 session。配套语义实现是 `tinkerfin-tracing.Tracer`。
AG-UI event、Messaging commit、SSE frame 和 Redis ownership 不属于 Runtime Observation。

Agent 终态在 Observer 广播前已经选定。某个 Observer 在终态广播时失败会让调用方失败，并通知
其他健康 Observer，但不会事后改写已经结束的 Agent 执行结果。

## 恢复和错误

| API | 什么时候遇到 |
| --- | --- |
| `AgUiResumeRequest` | 恢复请求中不可信的客户端决定 |
| `AgUiResumeBinding` | `new_agui()` 接受的高级 checkpoint 解析事实 |
| `AgUiResumeCheckpoint` | resume marker 持久化后的稳定回调值 |
| `tinkerfin.plan.PlanModeConfigurationError` | Plan Definition 缺少具体 saver 或明确模型、state schema 不兼容，或使用了非 sync durability |
| `AgUiSettlementTimeoutError` | 调用方停止等待，但 Runtime 的清理仍未在限定时间内完成 |

`TinkerFin.open_agui_run(...)` 是普通入口：

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `identity` | 必填 | canonical thread 与 run 身份 |
| `agent` | 必填 | Definition，或返回 Definition 的同步/异步 callable |
| `input` / `resume` | 严格二选一 | 普通 Graph 输入或只含客户端决定的 `AgUiResumeRequest` |
| `parent_run_id` | `None` | `RUN_STARTED` 暴露的可选 checkpoint 谱系 |
| `mode` | Definition 默认值 | 原生 default 或 Plan 路径 |
| `config` / `context` | `None` | Graph 配置与声明的 Runtime context |
| `stream_timeout` / `cleanup_timeout` | `None` | 原生 pull 时限与受保护清理等待时限 |
| `include_reasoning_events` | `False` | 是否交付已验证的公开推理事件 |
| `include_subagent_events` | `True` | 是否交付已验证子 Agent 事件 |
| `on_native_part` / `on_agui_event` | `None` | 交付前的异步观察函数 |
| `on_resume_saved` / `on_resume_not_saved` | `None` | durable marker 证据前后的宿主幂等结算 |
| `resume_checkpointer` | TinkerFin 默认值 | lazy resume Definition 显式覆盖 factory 默认 saver 时声明的高级持久化权威；必须传入同一对象 |

高级编排仍可直接使用 `DeepAgentDefinition.new_agui(...)`：

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
| `on_resume_initialization_failed` | `None` | marker 持久前失败、取消或关闭时执行的幂等结算 |
| `on_event` | `None` | AG-UI 事件交付前的观察函数 |

返回的 Runtime 保留已安装 Graph 的 `astream(...)` 参数形状。框架会把同一个 `RunIdentity`
写入 Graph 配置，`astream()` 不再接收第二套 Runtime 身份。

`DeepAgentDefinition.prepare_agui_resume(...)` 接收 `AgUiResumeRequest`，由所选 Profile 从权威
checkpoint 读取原生分组、取消、Tool ID、来源 Agent 和 Profile 谱系并完成校验，再返回
私有 binding。客户端请求不包含服务端 interrupt payload 或原生 command。

恢复流程见 [interrupt 与恢复](../agui/interrupts-and-resume.md)。
