# 投递、回放和 SSE

[Messaging 入门](index.md) · [English](../../en/messaging/delivery-and-replay.md)

Messaging 有两条常用路径：`sse()` 直接返回 SSE；`wrap()` 返回解码后的消息，方便服务端自己处理。

## 直接返回 SSE

```python
body = await channel.sse(
    source,
    stream="thread-42",
    run="run-7",
    after=lambda: parse_last_event_id(request),
    attach_identity={"user_id": 42, "request": request_payload},
    on_committed=notify_projection,
)
```

### 参数

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `source` | 必填 | 一次性异步消息源 |
| `stream` | 必填 | 排序和并发范围 |
| `run` | 必填 | 本次生产者身份 |
| `after` | `None` | 具体游标，或返回游标的无参数同步函数 |
| `attach_identity` | `None` | 能影响输出的请求身份和输入 |
| `cancel` | `None` | 远程取消时调用的函数 |
| `on_committed` | `None` | 每条消息持久提交后调用的异步观察函数 |

`after` 的函数只调用一次，适合读取当前 HTTP 请求的 `Last-Event-ID`。它不能执行阻塞 I/O。

`attach_identity` 可以是有限 JSON 数据或 Pydantic 模型。Messaging 只保存它的摘要；同一个 `run` 用不同 identity 再次附着会失败。应包含所有可能改变输出的请求字段。

## 读取解码后的消息

```python
subscription = await channel.wrap(
    source,
    stream="thread-42",
    run="run-7",
    after=0,
    attach_identity=request_payload,
)

try:
    async for message in subscription:
        print(message.envelope.seq, message.data)
finally:
    await subscription.aclose()
```

`message.data` 是 codec 解码后的对象；`message.envelope` 是持久记录的元数据。

如果随后仍要输出 SSE：

```python
body = subscription.sse()
async for chunk in body:
    await send_to_client(chunk)
```

channel 必须有 SSE renderer，否则会抛出 `SseRenderingUnsupported`。

## 不启动生产者，只读取历史

```python
latest = await channel.latest_seq(stream="thread-42")

page = await channel.read(
    stream="thread-42",
    after=100,
    limit=50,
)

subscription = await channel.follow(
    stream="thread-42",
    run="run-7",
    after=latest,
)
```

| 方法 | 用途 |
| --- | --- |
| `latest_seq()` | 读取当前最后序号；空 stream 返回 0 |
| `read()` | 读取有限、升序的一页历史消息 |
| `follow()` | 先回放再持续等待该 run 的新消息 |
| `validate_cursor()` | 在开始响应前检查游标范围 |

`read()` 和 `follow()` 需要 channel 已经知道 codec。最稳妥的方式是创建 channel 时显式传入 codec；内置 TinkerFin 事件源也可以在当前进程首次使用时提供格式信息。

## 提交后的观察函数

```python
async def notify_projection(envelope) -> None:
    await queue.put(
        (envelope.channel, envelope.stream, envelope.seq, envelope.message_id)
    )
```

`on_committed` 在消息已经持久化后执行。它失败不会回滚消息，也不会把 run 标记为失败。恢复重试时，同一 envelope 可能再次通知，因此观察函数要用 channel、stream、seq 和 message ID 做幂等处理。

观察函数执行完之前，生产者不会拉取下一条 source 数据，这能保持顺序和背压。

## 附着同一次运行

同一个 `(channel, stream)` 同时只有一个活跃生产者：

- 相同 `run` 和相同 identity 会附着已有运行，不再执行 source；
- 相同 `run` 但 identity 不同会抛出 `RunIdentityConflict`；
- 另一个 `run` 在前一个仍活跃时会抛出 `RunAlreadyActive`；
- 已完成的 `run` 只能回放，不会再次执行。

因此 run ID 既要稳定，又不能被不同输入重复使用。

## 删除历史

```python
await channel.delete_stream(stream="thread-42")
```

它会删除这个 stream 的消息、运行记录、恢复 checkpoint 和去重信息。活跃生产者存在时会抛出 `StreamDeleteConflict`；先取消或等待运行结束。

删除后重新使用同名 stream，序号从 1 开始。旧 subscription 不会自动切换到新一代 stream，而是抛出 `StreamDeleted`。

下一篇：[取消、延迟创建与恢复](cancellation-and-recovery.md)。

