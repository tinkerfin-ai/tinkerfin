# Messaging 入门

[文档首页](../README.md) · [English](../../en/messaging/index.md)

Messaging 把一次性的异步事件流变成可以独立运行、保存、回放和取消的生产者。浏览器断开后，Agent 可以继续运行；浏览器重新连接时，可以从上次收到的位置继续读取。

## 什么时候使用

| 需求 | 是否需要 Messaging |
| --- | --- |
| 当前请求中直接读取 Agent 事件 | 不一定 |
| 浏览器断线后继续运行 | 需要 |
| 使用 `Last-Event-ID` 续传 | 需要 |
| 多个请求附着到同一次运行 | 需要 |
| 从另一个进程取消运行 | 使用共享 backend 时需要 |
| 保存事件供稍后读取 | 需要 |

## 安装

```bash
pip install "tinkerfin-messaging[agui]"
```

如果只在一个进程内试用，默认的 `MemoryBackend` 就够了。多个进程共享数据时再安装 Redis extra。

## 把 AG-UI 流变成可续传 SSE

```python
from tinkerfin_messaging import Messaging


async with Messaging() as messaging:
    channel = messaging.channel(name="agent-events")

    runtime = agent.new_agui(run_input=run_input)
    events = runtime.astream(
        graph_input,
        {"configurable": {"thread_id": run_input.thread_id}},
    )

    body = await channel.sse(
        events,
        stream=run_input.thread_id,
        run=run_input.run_id,
        after=0,
        attach_identity=run_input,
    )

    async for chunk in body:
        await send_to_client(chunk)
```

`channel.sse()` 返回前已经完成“启动新生产者还是附着已有生产者”的检查。返回的 `body` 已经是 `bytes` 形式的 SSE，不要再套一层 SSE 编码。

## 四个最重要的名称

| 名称 | 示例 | 作用 |
| --- | --- | --- |
| channel name | `agent-events` | 表示一种稳定的消息格式 |
| stream | `thread-42` | 一条独立、有序的消息日志 |
| run | `run-7` | stream 中的一次生产过程 |
| seq | `1, 2, 3...` | 每条已提交消息在 stream 中的位置 |

同一个 channel name 下不要混用 AG-UI、原生 LangGraph 和自定义格式。一个 channel 对象可以被多个并发请求重复使用，但传入的每个事件源只能消费一次。

## `after` 游标

`after` 表示“已经收到的最后一个序号”，回放从它之后开始。

| 值 | 行为 |
| --- | --- |
| `None` | 从当前末尾开始，只等待新消息 |
| `0` | 从第一条保留消息开始 |
| `N` | 返回 `seq > N` 的消息 |

负数或超过当前末尾的值会抛出 `InvalidCursor`。

## 应用生命周期

通常在应用启动时创建一个 `Messaging`，在应用关闭时关闭它：

```python
async with Messaging(backend=backend) as messaging:
    channel = messaging.channel(name="agent-events")
    await serve_application(channel)
```

关闭 Messaging 会等待它拥有的生产者正确结束；注入的 backend 是否需要额外关闭，取决于 backend 自己的资源约定。

## 下一步

- [投递、回放和 SSE](delivery-and-replay.md)
- [取消、延迟创建与恢复](cancellation-and-recovery.md)
- [Redis、自定义 codec 和 backend](backends-and-codecs.md)
- [Messaging 使用参考](api-reference.md)
