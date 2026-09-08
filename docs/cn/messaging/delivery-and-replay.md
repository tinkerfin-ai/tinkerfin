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

`runId` 是幂等 key。Messaging 不读取或保存业务请求摘要；同一个 `RunIdentity` 的正文是否一致由应用校验。

## 直接获得 SSE

```python
from tinkerfin_messaging import parse_sse_event_id


body = await channel.sse(
    source,
    identity=identity,
    after=lambda: parse_sse_event_id(request.headers.get("Last-Event-ID")),
    on_source_ready=activate_business_run,
    on_delivery_not_started=cleanup_business_run,
)
```

`after` resolver 只调用一次，并且在 durable prepare 之前执行。`parse_sse_event_id()` 只接受
canonical 非负 ASCII 十进制值。返回的每帧使用提交序号作为 SSE `id`。

`on_source_ready` 在请求 source 就绪后、producer 创建之前为新 owner 调用一次。
`on_delivery_not_started` 只在 source 未就绪且 attachment 未成立时调用；attachment 不调用两者。
返回 body 由调用方拥有，不再消费时必须关闭。

有效 attachment 成立后，Messaging 会关闭未打开的 single-use candidate source，调用方不能复用。

## 运行中主动发布消息

```python
from ag_ui.core import CustomEvent

await channel.publish(
    CustomEvent(name="report.progress", value={"completed": 3, "total": 10}),
    identity=identity,
    message_id="report-progress-3",
)
```

消息与源事件进入同一份持久化日志，已有订阅者实时接收，重连沿用同一个序号游标回放。发布不会打开事件源、取得或续租生产者所有权，也不会推进源消息编号或恢复 checkpoint。目标身份的业务授权由宿主负责。

发布前 channel 必须已绑定 codec。其他 worker 没有打开事件源时，可显式使用 `codec=AgUiCodec()` 创建同名 channel。普通 codec 在首条源消息提交后接受发布；AG-UI 仅在目标主 `RUN_STARTED` 提交后接受 `CustomEvent`，主终态与关闭发布在同一提交中完成。子运行事件不改变主运行的发布状态。取消、结算或生产者所有权失效后，新发布抛出 `PublicationRejected`。

省略 `message_id` 时每次调用生成新 ID。相同 ID、相同内容返回仍在保留期内的原消息，包括运行已经结束的情况；内容不同抛出 `MessageIdConflict`。发布被拒绝不会启动新运行。

具有协议生命周期的 codec 可以实现 `MessagePublicationPolicy`：`validate_publication()` 校验外部消息，`starts_publication()` 与 `ends_publication()` 识别目标运行边界。判定结果与源消息原子提交，存储后端无需识别具体协议。

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

虽然 thread 级 API 只使用 `identity.threadId` 定位日志，接口仍统一接收完整 `RunIdentity`，避免 thread/run 在不同层重复平铺。

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
