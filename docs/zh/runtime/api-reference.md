# Runtime 使用参考

[Runtime 入门](index.md) · [English](../../en/runtime/api-reference.md)

这一页按实际使用顺序汇总 Runtime 的公开能力。常见项目通常只会用到前两组。

## 创建入口

| API | 什么时候用 | 主要参数或结果 |
| --- | --- | --- |
| `TinkerFin(run_coordinator=None)` | 创建统一入口 | 可选共享 coordinator |
| `TinkerFin.create_deep_agent(...)` | 创建可重复生成 Runtime 的 Agent 定义 | 参数见[创建和运行 Deep Agent](deep-agents.md) |
| `TinkerFin.run(...)` | 运行自己的异步事件源 | `source_factory`、`principal`、`on_part` |
| `DeepAgentDefinition.new(...)` | 创建原生 Runtime | `principal`、`on_part` |
| `DeepAgentDefinition.new_agui(...)` | 创建 AG-UI Runtime | 详见 [AG-UI 入门](../agui/index.md) |

`DeepAgentDefinition` 可以重复使用。`DeepAgentRuntime`、`DeepAgentAgUiRuntime`、`TinkerFinRun` 和 `NativeTinkerFinRun` 都是一次性运行对象，不要自行构造。

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

## 恢复和错误

| API | 什么时候遇到 |
| --- | --- |
| `AgUiResumeBinding` | 恢复被 interrupt 暂停的 AG-UI 运行 |
| `AgUiSettlementTimeoutError` | 调用方停止等待，但 Runtime 的清理仍未在限定时间内完成 |
| `AgUiNativeStreamConfigurationError` | 原生流配置不符合 AG-UI 转换要求 |

`TinkerFinRun.astream_agui(...)` 和 `NativeTinkerFinRun.astream_agui(...)` 用于把低层 source 转成 AG-UI。

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `run_input` | 必填 | 完整 AG-UI 请求 |
| `timeout` | `None` | 等待原生数据的总时限 |
| `settlement_timeout` | `None` | 调用方等待安全清理的时限 |
| `expose_reasoning_events` | `False` | 是否交付支持的推理事件 |
| `expose_subagent_events` | `True` | 是否交付子 Agent 事件 |
| `prior_tool_call_ids` | `frozenset()` | 恢复前已经发送完成的 scoped Tool ID |
| `on_event` | `None` | AG-UI 事件交付前的观察函数 |

`AgUiResumeBinding.from_translation(...)` 从恢复转换结果创建 binding；`validate_run_input(...)` 和 `validate_command(...)` 可在自定义入口提前检查请求是否属于该 binding。一般 Deep Agents 使用者直接走 `new_agui()`。

恢复流程见 [interrupt 与恢复](../agui/interrupts-and-resume.md)。
