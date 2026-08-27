# Messaging 使用参考

[Messaging 入门](index.md) · [English](../../en/messaging/api-reference.md)

## 应用入口

| API | 参数 | 用途 |
| --- | --- | --- |
| `Messaging(...)` | `backend=None`、`settlement_timeout=None` | 创建单次应用生命周期 |
| `Messaging.channel(...)` | `name`、可选 `codec`、可选 `renderer` | 创建可并发复用的 channel |
| `Messaging.aclose()` | 无 | 等待 preflight、producer 和清理任务完成 |

默认 backend 是 `MemoryBackend`。

## Channel 方法

| 方法 | 关键参数 | 结果 |
| --- | --- | --- |
| `sse(...)` | source、可选 `identity`、after、cancel、on_committed | SSE bytes 迭代器 |
| `wrap(...)` | source、可选 `identity`、after、cancel、on_committed | `MessageSubscription` |
| `wrap_recoverable(...)` | recoverable source、可选 `identity`、after、cancel、on_committed | 可恢复 subscription |
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
| `MessageSubscription` | 异步迭代 `DecodedMessage`；支持 `sse()` 和 `aclose()` |
| `DecodedMessage` | `envelope` 与 codec 解码后的 `data` |
| `MessageEnvelope` | 已提交的不可变持久消息 |

### `MessageEnvelope` 字段

| 字段 | 约束或含义 |
| --- | --- |
| `channel` | 非空 channel name |
| `identity` | 嵌套的共享 `Identity` |
| `seq` | thread 内从 1 开始的连续位置 |
| `message_id` | thread 内稳定幂等 ID |
| `codec` | 持久格式 ID |
| `payload` | 编码后的 bytes |
| `created_at` | aware UTC 时间 |

## Source 工具

| API | 用途 |
| --- | --- |
| `MessageSource` | 单次异步 source 协议 |
| `CancellableMessageSource` | 自己声明取消 callback 的 source |
| `ProfiledMessageSource` | 声明 codec、Identity、live type 和 replay type |
| `MessageSourceBinding` | deferred opener 返回的 source 与可选取消函数 |
| `DeferredMessageSource` | owner 确定后才创建普通 source |
| `ProfiledDeferredMessageSource` | 延迟创建且在打开前可推断 codec 与 Identity |
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
| `AgUiCodec` | 默认安装可用的 AG-UI codec 与 renderer |
| `NativeStreamPartCodec` | 默认安装可用的 Native v2 codec 与 renderer |
| `NativeStreamPart` | Native v2 解码结果 |
| `MemoryBackend` | 单进程实现 |
| `RedisBackend` | 安装 `[redis]` 后可用的多进程实现 |
| `MessagingBackend` | 自定义 backend 协议 |

### `MessagingBackend` 操作

| 操作 | 参数与结果 |
| --- | --- |
| `prepare(...)` | channel、Identity、codec、after、是否可取消/恢复；返回 `PreparedRun` |
| `append(...)` | handle、message ID、codec、bytes、可选 checkpoint；返回 Envelope |
| `begin_settlement(handle)` | 原子进入收尾，返回是否已有取消请求 |
| `finish(...)` | handle、最终状态、可选 error |
| `get_run_status(...)` | channel 与 Identity；可原子把过期 lease 判定为 `owner_lost` |
| `latest_seq(...)` / `read(...)` | channel、Identity 与分页参数 |
| `bind_follow(...)` / `follow(...)` | 绑定权威 generation 并持续读取 |
| `request_cancel()` / `wait_for_cancel()` | 记录或等待取消 |
| `wait_finished()` / `failure()` | 等待最终状态或读取失败 |
| `lease_renew_interval` / `lease_timeout` / `renew(handle)` | 描述续租周期、过期预算并确认所有权 |
| `delete_stream(...)` | 删除不活跃 thread generation |

`BackendRunHandle` 包含 channel、Identity、owner token、fence 和 generation。`PreparedRun` 还包含游标、`is_owner`、可选 checkpoint 与 `recovered`。

## Callback

| API | 作用 |
| --- | --- |
| `CancelCallback` | 无参数或接收 `CancelContext`，可返回有限取消尾部 |
| `CancelContext` | 不可变的 channel 与 Identity |
| `CommittedCallback` | owner 提交后接收完整 Envelope |

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
| `StreamDeleted` / `StreamDeleteConflict` | generation 已删除或有活跃 producer |

Messaging 不读取或比较请求正文。同一个 `runId` 的请求事实一致性由调用方负责。
