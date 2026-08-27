# 投递、回放和 SSE

[Messaging 入门](index.md) · [English](../../en/messaging/delivery-and-replay.md)

## 启动或附着

```python
subscription = await channel.wrap(
    source,
    identity=identity,  # TinkerFin profile source 可省略
    after=0,
)
```

`wrap()` 返回前会原子决定：

- 没有该 run：当前调用成为 owner 并启动 source；
- 已有该 run：关闭未使用的候选 source，附着已有日志；
- 同一 thread 有另一个活跃 run：抛出 `RunAlreadyActive`；
- codec 与 channel 已绑定格式不同：抛出 `CodecMismatch`。

`runId` 是幂等 key。Messaging 不读取或保存业务请求摘要；同一个 `Identity` 的正文是否一致由应用校验。

## 直接获得 SSE

```python
body = await channel.sse(
    source,
    identity=identity,
    after=lambda: parse_last_event_id(request.headers.get("Last-Event-ID")),
)
```

`after` resolver 只调用一次，并且在 durable prepare 之前执行。返回的每帧使用提交序号作为 SSE `id`。

## 已提交数据的三种读取方式

```python
latest = await channel.latest_seq(identity=identity)

page = await channel.read(
    identity=identity,
    after=100,
    limit=200,
)

subscription = await channel.follow(
    identity=identity,
    after=100,
)

status = await channel.get_run_status(identity=identity)
```

| API | 范围 | 结果 |
| --- | --- | --- |
| `latest_seq()` | 整个 thread | 当前最大 seq，空日志为 0 |
| `read()` | 整个 thread | 一页 `DecodedMessage`，不会等待新消息 |
| `follow()` | 指定 run | 先回放，再等待该 run 的权威终止 |
| `get_run_status()` | 指定 run | 不取得 owner 的当前 durable 状态 |

虽然 thread 级 API 只使用 `identity.threadId` 定位日志，接口仍统一接收完整 `Identity`，避免 thread/run 在不同层重复平铺。

`get_run_status()` 可原子把过期 producer lease 归档为 `owner_lost`。没有 durable
run 时抛出 `RunNotFound`，且不会创建或恢复 producer。

## Envelope

每条提交结果是 `MessageEnvelope`：

| 字段 | 作用 |
| --- | --- |
| `channel` | codec 命名空间 |
| `identity` | 嵌套的 `threadId` 与 `runId` |
| `seq` | thread 内连续位置 |
| `messageId` | thread 内稳定的消息幂等 ID |
| `codec` | 持久化格式 ID |
| `payload` | 编码后的 bytes |
| `createdAt` | 首次提交时分配的 UTC 时间 |

Messaging 只存储一种当前 Envelope 结构。应用自有格式发生不兼容变化时应先重建记录，
运行时不会协商或识别多种格式。

## 提交观察函数

如果需要在 owner 新提交后更新投影：

```python
async def on_committed(envelope) -> None:
    await projection_queue.put(envelope)


subscription = await channel.wrap(
    source,
    identity=identity,
    on_committed=on_committed,
)
```

观察函数只对 owner 的新提交调用，附着回放不会重复调用。观察失败会记录日志，但不会改变已经提交的 run 结果。

## 删除 thread 日志

```python
await channel.delete_stream(identity=identity)
```

删除范围是整个 `identity.threadId`。活跃生产者会触发 `StreamDeleteConflict`；缺失或已经删除的 thread 是成功的幂等 no-op。删除后用相同 threadId 创建新 run 会进入新的 generation，旧 handle 不能再读写。

下一篇：[取消、延迟创建与恢复](cancellation-and-recovery.md)。
