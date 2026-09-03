# Messaging 使用参考

[Messaging 入门](index.md) · [English](../../en/messaging/api-reference.md)

## 应用入口

| API | 参数 | 用途 |
| --- | --- | --- |
| `Messaging(...)` | `backend=None`、`settlement_timeout=None` | 创建单次应用生命周期 |
| `Messaging.channel(...)` | `name`、可选 `codec`、可选 `renderer` | 创建可并发复用的 channel |
| `Messaging.aclose()` | 无 | 等待 preflight、producer 和清理任务完成 |
| `create_agui_run_source(...)` | identity、`open_events`、可选 transform | 创建普通惰性 TinkerFin AG-UI source |

默认 backend 是 `MemoryBackend`。

## Channel 方法

| 方法 | 关键参数 | 结果 |
| --- | --- | --- |
| `sse(...)` | source、可选 identity、after、callback | 调用方拥有且可关闭的 SSE bytes 迭代器 |
| `wrap(...)` | source、可选 identity、after、callback | `MessageSubscription` |
| `wrap_recoverable(...)` | recoverable source、可选 identity、after、callback | 可恢复 subscription |
| `read(...)` | `identity`、`after=0`、`limit=100` | 升序历史元组 |
| `follow(...)` | `identity`、`after=0` | 跟随该 run 到终止 |
| `get_run_status(...)` | `identity` | 当前权威 run 状态 |
| `latest_seq(...)` | `identity` | thread 当前最后序号 |
| `validate_cursor(...)` | `identity`、after | 只读校验游标 |
| `cancel(...)` | `identity` | 请求取消并等待最终状态 |
| `delete_stream(...)` | `identity` | 删除 thread 当前 generation |

TinkerFin profile source 的 `identity` 可省略；普通自定义 source 必须显式提供。

## Subscription 与 Envelope

| API | 用途 |
| --- | --- |
| `MessageSubscription` | 仅由 channel `wrap()`/`follow()` 返回，不直接构造；异步迭代 `DecodedMessage`，并支持 `sse()`/`aclose()` |
| `DecodedMessage` | `envelope` 与 codec 解码后的 `data` |
| `MessageEnvelope` | 已提交的不可变持久消息 |

### `MessageEnvelope` 字段

| 字段 | 约束或含义 |
| --- | --- |
| `channel` | 非空 channel name |
| `identity` | 嵌套的共享 `RunIdentity` |
| `seq` | thread 内从 1 开始的连续位置 |
| `message_id` | thread 内稳定幂等 ID |
| `codec` | 持久格式 ID |
| `payload` | 编码后的 bytes |
| `created_at` | aware UTC 时间 |

## 常用 helper

| API | 用途 |
| --- | --- |
| `create_agui_run_source(...)` | 隐藏 Binding、codec/type profile、转换与首事件取消，只为 owner 打开 managed AG-UI run |
| `parse_sse_event_id(value)` | 解析 `None` 或 canonical 非负 ASCII 十进制 SSE ID |
| `is_active_run_status(status)` | 收窄 `running` 与 `cancel_requested` 的 TypeGuard |
| `is_final_run_status(status)` | 收窄全部 durable 终态的 TypeGuard |
| `is_failed_run_status(status)` | 收窄 `failed` 与 `owner_lost` 的 TypeGuard |

状态 helper 是纯函数，不访问 backend。
普通 AG-UI source 的 transform 只用于补充产品 metadata 或内容；事件类型以及 Run、消息、Tool、快照或
interrupt ID 发生变化时会被拒绝。可选 `RUN_STARTED.input` 中的消息、工具、上下文、forwarded props
与 resume 等全部字段也保持调用方原始输入。需要主动改变输出协议时，应使用高级 `map_source()`。

## 高级 source 工具

| API | 用途 |
| --- | --- |
| `MessageSource` | 单次异步 source 协议 |
| `CancellableMessageSource` | 自己声明取消 callback 的 source |
| `ProfiledMessageSource` | 声明 codec、RunIdentity、live type 和 replay type |
| `MessageSourceBinding` | deferred opener 返回的 source 与可选取消函数 |
| `DeferredMessageSource` | owner 确定后才创建普通 source，可在 producer 前执行 owner preflight |
| `ProfiledDeferredMessageSource` | 延迟创建且在打开前可推断 codec 与 RunIdentity，并使用相同 owner-preflight fence |
| `MessageCodecInputSource` | 由 source 把一条 live item 转成推断 codec 接受的有限输入 |
| `FiniteMessageSource` | 把有限 iterable 变成 source |
| `map_source(...)` | 顺序执行同步或异步转换 |
| `RecoverableSource` | 根据 checkpoint 重建 source |
| `RecoverableMessage` | 稳定消息 ID、数据和 checkpoint |
| `RecoveryCheckpoint` | `position` 与可选 `last_message_id` |

## Codec、renderer 与 backend

| API | 用途 |
| --- | --- |
| `MessageCodec` | `encode()`、`decode()` 和稳定 `codec_id` |
| `SseRenderer` | `render(seq=..., payload=...) -> bytes` |
| `AgUiCodec` | 安装 `[agui]` 后可用的 AG-UI codec 与 renderer |
| `NativeStreamPartCodec` | 安装 `[native]` 后可用的 canonical Native replay codec 与 renderer |
| `NativeStreamPart` | 安装 `[native]` 后可用的有限 canonical Native replay 值 |
| `MemoryBackend` | 单进程实现 |
| `RedisBackend` | 安装 `[redis]` 后可用的多进程实现 |
| `MessagingBackend` | 六操作自定义存储协议 |
| `MessagingBackendSettings` | 不可变 limits、retention、lease、续租与等待设置 |
| `MessagingTransition` | 框架定义的原子生命周期意图 |
| `MessagingStateSnapshot` | 有界且与存储时钟一致的 transition 证据 |
| `MessagingStorageEffect` | `resolve_messaging_transition()` 产生的完整替换效果 |
| `MessagingRetentionPolicy` | 关闭或配置正数秒的终态重播窗口 |

### `MessagingBackend` 操作

| 操作 | 参数与结果 |
| --- | --- |
| `messaging_settings` | 返回所有协作 Backend 实例共享的不可变设置 |
| `prepare_messaging_storage()` | 幂等创建或校验当前存储形态 |
| `commit_messaging_transition(...)` | 原子提交一个框架 transition |
| `load_messaging_state(...)` | 加载一个有界一致状态快照 |
| `read_committed_messages(...)` | 读取精确 generation 的升序消息页 |
| `wait_for_messaging_change(...)` | 可响应取消地等待消息或控制状态可能变化 |
| `purge_stream_generation(...)` | 对已封闭 generation 幂等删除一个有界批次 |

`resolve_messaging_transition()` 提供可复用的存储中立状态机。
`tinkerfin_messaging.testing.verify_messaging_backend()` 使用隔离 Backend factory 验证受支持行为。

清理 begin 可以返回一个不透明且会过期的 `cleanup_token`。Messaging 不解析、不持久化该 token，
只会将其原样、串行传给返回 generation 的有界 purge 与 finish 调用。

## Callback

| API | 作用 |
| --- | --- |
| `CancelCallback` | 无参数或接收 `CancelContext`，可返回有限取消尾部 |
| `CancelContext` | 不可变的 channel 与 RunIdentity |
| `CommittedCallback` | owner 提交后接收完整 Envelope |
| `on_source_ready` | source 就绪后、producer 创建前的 owner 专用异步 callback |
| `on_delivery_not_started` | source 未就绪且 attachment 未成立时执行的异步清理 |

Attachment 不调用两个 delivery callback。`on_owner_preflight` 属于 source，在 deferred opener
之前执行；`on_source_ready` 在 opener 完成后执行。

## 常见错误

| 错误 | 含义 |
| --- | --- |
| `MessagingNotStarted` / `MessagingClosed` | 生命周期状态不允许当前操作 |
| `MessagingSettlementTimeout` | 调用方等待安全清理超时 |
| `InvalidCursor` | 游标非法或超出末尾 |
| `CodecMismatch` | channel codec 不一致 |
| `SourceProfileMismatch` | source profile 缺失或矛盾 |
| `MessageIdConflict` | 同一消息 ID 对应不同内容 |
| `RunAlreadyActive` | 同一 thread 有另一个活跃 run |
| `RunNotFound` | 找不到 run |
| `RunProducerFailed` | 生产、编码、提交或取消失败 |
| `CancellationUnsupported` | run 没有取消 callback |
| `BackendOwnershipLost` | 过期 producer 失去所有权 |
| `StreamExpired` | 终态 generation 已超出重播保留窗口 |
| `StreamDeleted` / `StreamDeleteConflict` | generation 已删除或有活跃 producer |

Messaging 不读取或比较请求正文。同一个 `runId` 的请求事实一致性由调用方负责。
