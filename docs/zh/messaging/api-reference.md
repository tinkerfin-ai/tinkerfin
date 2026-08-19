# Messaging 使用参考

[Messaging 入门](index.md) · [English](../../en/messaging/api-reference.md)

## 应用入口

| API | 参数 | 用途 |
| --- | --- | --- |
| `Messaging(...)` | `backend=None`、`settlement_timeout=None` | 创建应用级 Messaging |
| `Messaging.channel(...)` | `name`、可选 `codec`、可选 `renderer` | 创建可并发复用的 channel |
| `Messaging.aclose()` | 无 | 等待拥有的生产者和清理任务完成 |

未提供 backend 时使用 `MemoryBackend`。

## Channel 方法

`MessageChannel` 是 `Messaging.channel(...)` 返回的可复用 channel 句柄。

| 方法 | 关键参数 | 结果 |
| --- | --- | --- |
| `sse(...)` | source、stream、run、after、identity、cancel、observer | SSE bytes 迭代器 |
| `wrap(...)` | 与 `sse()` 相同，但 `after` 只能是具体值 | 解码后的 subscription |
| `wrap_recoverable(...)` | recoverable source 和运行身份 | 可恢复的 subscription |
| `read(...)` | stream、`after=0`、`limit=100` | 升序历史元组 |
| `follow(...)` | stream、run、`after=0` | 持续 subscription |
| `latest_seq(...)` | stream | 当前最后序号 |
| `validate_cursor(...)` | stream、after | 校验游标 |
| `cancel(...)` | stream、run | 是否请求了取消 |
| `delete_stream(...)` | stream | 删除不活跃 stream |

## Subscription 与消息

| API | 用途 |
| --- | --- |
| `MessageSubscription` | 异步迭代 `DecodedMessage`；支持 `sse()` 和 `aclose()` |
| `DecodedMessage` | 包含 `envelope` 和解码后的 `data` |
| `MessageEnvelope` | 持久消息的身份、序号、codec、bytes payload 和 UTC 时间 |

### `MessageEnvelope` 字段

| 字段 | 约束或含义 |
| --- | --- |
| `schema_version` | 当前为 1 |
| `channel` | 非空 channel name |
| `stream` | 非空 stream ID |
| `seq` | 从 1 开始的连续位置 |
| `message_id` | stream 内稳定幂等 ID |
| `run` | 生产这条消息的 run |
| `codec` | 持久格式 ID |
| `payload` | 编码后的 bytes |
| `created_at` | UTC 时区时间 |

## Source 工具

| API | 用途 |
| --- | --- |
| `MessageSource` | 单次异步 source 协议 |
| `CancellableMessageSource` | 自己声明取消 callback 的 source |
| `ProfiledMessageSource` | 自己声明稳定 codec profile 的 source |
| `MessageSourceBinding` | deferred opener 返回的 source 与可选取消函数 |
| `DeferredMessageSource` | owner 确定后才创建 source |
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
| `MemoryBackend` | 单进程完整实现 |
| `RedisBackend` | Redis 多进程实现 |
| `MessagingBackend` | 自定义持久 backend 协议 |
| `BackendRunHandle` | backend 使用的运行所有权句柄 |
| `PreparedRun` | 准备结果：句柄、游标、owner、checkpoint 和恢复状态 |
| `AgUiCodec` | AG-UI 可选 codec 与 renderer |
| `NativeStreamPartCodec` | 原生 v2 可选 codec 与 renderer |
| `NativeStreamPart` | 原生 v2 解码结果 |

### 自定义 `MessagingBackend` 的完整操作

| 操作 | 参数与结果 |
| --- | --- |
| `prepare(...)` | channel、stream、run、codec、identity、after、是否可取消、是否可恢复；返回 `PreparedRun` |
| `append(...)` | owner handle、message ID、codec、bytes payload、可选 checkpoint；返回 `MessageEnvelope` |
| `begin_settlement(handle)` | 原子进入收尾，返回是否已有取消请求 |
| `finish(...)` | handle、最终状态、可选 error |
| `latest_seq(...)` / `read(...)` | 读取尾部序号或有限历史页 |
| `bind_follow(...)` / `follow(...)` | 绑定权威 stream 代际并持续读取 |
| `request_cancel(handle)` | 记录取消请求 |
| `wait_for_cancel(handle)` | 等待取消或 run 收尾 |
| `wait_finished(handle)` | 等待最终 run 状态 |
| `failure(handle)` | 读取 producer 失败原因 |
| `renew(handle)` | 续租并返回是否仍有所有权 |
| `delete_stream(...)` | 删除不活跃 stream 的当前代际 |

`BackendRunHandle` 包含 channel、stream、run、owner token、fence 和 generation。`PreparedRun` 包含该 handle、已验证游标、`is_owner`、可选 recovery checkpoint 和 `recovered` 标记。

## Callback 类型

| API | 作用 |
| --- | --- |
| `CancelCallback` | 无参数或接收 `CancelContext`，可返回有限取消尾部 |
| `CancelContext` | 不可变的 channel、stream、run 三元组 |
| `CommittedCallback` | 消息提交后接收完整 `MessageEnvelope` |

## 常见错误

| 错误 | 含义 |
| --- | --- |
| `MessagingError` | Messaging 错误基类 |
| `MessagingNotStarted` | Messaging 尚未进入异步生命周期 |
| `MessagingClosed` | 已关闭的 Messaging 被再次使用 |
| `MessagingSettlementTimeout` | 等待安全清理超过调用方时限 |
| `InvalidCursor` | 游标为负数或超出保留范围 |
| `CodecMismatch` | 同一 channel 使用了不同持久格式 |
| `SourceProfileMismatch` | source 声明的 profile 不完整或矛盾 |
| `MessageIdConflict` | 同一消息 ID 对应了不同内容 |
| `RunAlreadyActive` | 另一个 run 已占用 stream |
| `RunIdentityConflict` | 同一个 run 使用了不同 attach identity |
| `RunNotFound` | 找不到 run |
| `RunProducerFailed` | 生产、编码、提交或取消过程失败 |
| `CancellationUnsupported` | run 没有取消 callback |
| `RecoveryUnsupported` | source 不能从 owner 丢失中恢复 |
| `SseRenderingUnsupported` | channel 没有 SSE renderer |
| `BackendOwnershipLost` | 过期 producer 失去所有权 |
| `StreamDeleted` | 句柄所属的 stream 代际已删除 |
| `StreamDeleteConflict` | 活跃 producer 阻止删除 |
