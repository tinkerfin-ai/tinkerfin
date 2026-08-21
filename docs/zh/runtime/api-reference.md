# Runtime 使用参考

[Runtime 入门](index.md) · [English](../../en/runtime/api-reference.md)

这一页按实际使用顺序汇总 Runtime 的公开能力。常见项目通常只会用到前两组。

## 创建入口

| API | 什么时候用 | 主要参数或结果 |
| --- | --- | --- |
| `TinkerFin(run_coordinator=None, state_schema=None)` | 创建统一入口 | 可选共享 coordinator 和 Definition 级 state |
| `TinkerFin.plan(...)` | 创建不可变的 Plan-capable factory | 能力开关、请求默认 mode、可选 Gate/Planner 模型 |
| `TinkerFin.create_deep_agent(...)` | 创建可重复生成 Runtime 的 Agent 定义 | 参数见[创建和运行 Deep Agent](deep-agents.md) |
| `Identity(threadId=..., runId=...)` | 表示一次框架运行 | 只包含 thread 和 run |
| `TinkerFin.run(...)` | 运行自己的异步事件源 | `source_factory`、`identity`、`on_part` |
| `DeepAgentDefinition.new(...)` | 创建原生 Runtime | 必填 `identity`，可选本次请求 `mode` 和 `on_part` |
| `DeepAgentDefinition.new_agui(...)` | 创建 AG-UI Runtime | 可选本次请求 `mode`；详见 [AG-UI 入门](../agui/index.md) |

`DeepAgentDefinition` 可以重复使用。`DeepAgentRuntime`、`DeepAgentAgUiRuntime`、`TinkerFinRun` 和 `NativeTinkerFinRun` 都是一次性运行对象，不要自行构造。

`.plan(enabled=True)` 只影响从返回 factory 创建的 Definition，不会给
`create_deep_agent(...)` 增加参数。返回 factory 保留 coordinator 和全局 state schema。
Plan Definition 必须提供明确模型和具体 `BaseCheckpointSaver`；配置不合法时抛出
`tinkerfin.plan.PlanModeConfigurationError`。

每次 `new()` 或 `new_agui()` 通过 `mode="default"` 或 `mode="plan"` 选择当前路径；省略时
使用 `.plan(default_mode=...)`。Plan-capable Definition 在两种 mode 下保持同一 topology
和 state schema，因此同一 checkpoint thread 可以在后续请求切换。普通 Definition 只接受
`default`。mode 是框架请求控制数据，不会进入调用方 state；覆盖框架保留 configurable 会在
流开始前失败。

## Plan Mode 数据

顶层包导出 `AgentMode`。`tinkerfin.plan` 导出 `PlanStep`、`PlanDraft`、
`ConfirmedPlan`、`ClarificationOption`、`ClarificationQuestion`、
`RequirementAnswer`、`PlanState`、`PlanStatus`、`PlanRoute` 和 `PlanReviewAction`。
这些模型不可变。根 Graph 状态把完整 JSON 数据以 camel case 保存在
`tinkerfin_plan` 字段。

`TinkerFin(state_schema=...)` 为该 factory 创建的每个 Deep Agent Definition 提供应用级
state，并与 `create_deep_agent(state_schema=...)`、middleware 的公开扩展、Deep Agents
文件字段和 Todo 字段组合。reducer、`Required` / `NotRequired` 和 schema metadata 会保留；
同名字段合同不兼容时在创建 Definition 时失败。运行时 `context_schema` 仍是独立且不进入
checkpoint 的 context 合同。

## 流对象

| API | 用法 |
| --- | --- |
| `GraphRunStream` | 异步迭代普通对象；支持 `aclose()` 和 `to_sse()` |
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
| `schemaVersion` | `1` | `1` | 数据结构版本 |
| `type` | 字符串 | 必填 | `messages`、`tasks`、`values`、`updates`、`checkpoints`、`debug` 或 `custom` |
| `ns` | 字符串元组 | 必填 | Graph namespace；空元组表示根 Graph |
| `data` | JSON 值 | 必填 | 当前模式的数据 |
| `interrupts` | JSON 值元组 | `()` | `values` 携带的 interrupt |

通常由 Runtime 自动创建，不需要手工拼装。

## AG-UI 原生流配置

| API | 参数 | 作用 |
| --- | --- | --- |
| `AgUiNativeStreamConfig` | `extra_modes=()` | 声明 AG-UI 基础模式之外允许的额外模式 |
| `.bind(astream, *args, **options)` | 流函数及其调用参数 | 生成一次绑定好的原生流调用 |
| `AgUiNativeStreamInvocation` | 无需手工创建 | 交给 `TinkerFin.run()` |

配置错误会抛出 `AgUiNativeStreamConfigurationError`。

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
| `RunCoordinator` | 自定义运行互斥边界 |
| `InMemoryRunCoordinator(key_resolver=...)` | 当前进程内按业务 key 串行运行 |

安装 `tinkerfin[redis]` 后还可使用：

| API | 作用 |
| --- | --- |
| `RedisRunCoordinator` | 多进程按 `Identity` 串行运行 |
| `RedisLeaseLock` | 自动续期的通用 Redis 租约锁 |
| `RedisLease` | `hold()` 返回的不可变资源 key 与 fencing token |
| `RedisLeaseLost` | 续期不确定、租约过期或所有权丢失 |

构造参数、默认值和连接池要求见[自定义事件源与并发协调](extensions.md#如果需要通用-redis-租约锁)。

## 恢复和错误

| API | 什么时候遇到 |
| --- | --- |
| `AgUiResumeBinding` | 恢复被 interrupt 暂停的 AG-UI 运行 |
| `tinkerfin.plan.PlanModeConfigurationError` | Plan Definition 缺少具体 saver 或明确模型、state schema 不兼容，或使用了非 sync durability |
| `AgUiSettlementTimeoutError` | 调用方停止等待，但 Runtime 的清理仍未在限定时间内完成 |
| `AgUiNativeStreamConfigurationError` | 原生流配置不符合 AG-UI 转换要求 |

`TinkerFinRun.astream_agui(...)` 和 `NativeTinkerFinRun.astream_agui(...)` 用于把低层 source 转成 AG-UI。

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `timeout` | `None` | 等待原生数据的总时限 |
| `settlement_timeout` | `None` | 调用方等待安全清理的时限 |
| `expose_reasoning_events` | `False` | 是否交付支持的推理事件 |
| `expose_subagent_events` | `True` | 是否交付子 Agent 事件 |
| `prior_tool_call_ids` | `frozenset()` | 恢复前已经发送完成的 scoped Tool ID |
| `on_event` | `None` | AG-UI 事件交付前的观察函数 |

运行身份已经在 `TinkerFin.run(..., identity=...)` 中绑定，`astream_agui()` 不再重复接收 ID。

`AgUiResumeBinding.from_translation(...)` 接收 `identity` 和转换结果；`validate_identity(...)` 与 `validate_command(...)` 可在自定义入口提前检查恢复目标。一般 Deep Agents 使用者直接走 `new_agui()`。

恢复流程见 [interrupt 与恢复](../agui/interrupts-and-resume.md)。
