# Messaging 入门

[文档首页](../README.md) · [English](../../en/messaging/index.md)

Messaging 把一次性对象流变成可以保存、回放、附着和远程取消的生产者。浏览器断开后，Agent 可以继续运行；重新连接时从最后一个 durable 序号继续读取。

## 安装

```bash
pip install tinkerfin-messaging
```

基础安装已经包含 Native 与 AG-UI codec。只有使用 RedisBackend 时才需要：

```bash
pip install "tinkerfin-messaging[redis]"
```

## 把 TinkerFin 流变成可续传 SSE

```python
from tinkerfin import Identity
from tinkerfin_messaging import Messaging


identity = Identity(threadId="thread-42", runId="run-7")
runtime = agent.new_agui(identity=identity)
events = runtime.astream(graph_input)

async with Messaging() as messaging:
    channel = messaging.channel(name="agent-events")
    body = await channel.sse(events, after=0)

    async for chunk in body:
        await send_to_client(chunk)
```

`AgUiEventStream` 和 `NativeGraphRunStream` 会携带不可变 codec profile 与 `Identity`，所以 name-only channel 不需要重复传 codec、thread 或 run。空流也能在拉取第一条数据前完成识别。

`body` 已经是 `bytes` 形式的 SSE，不要再调用 Runtime 的 `to_sse()`，也不要把已经编码过的 `SseBody` 交给 Messaging。

## 自定义 source

自定义 source 没有 Identity profile，需要显式提供一次：

```python
channel = messaging.channel(name="custom", codec=codec)
subscription = await channel.wrap(
    source,
    identity=identity,
    after=0,
)
```

如果 source 自带 Identity，又显式提供了不同值，Messaging 会在 backend prepare 和 source 打开前拒绝。

## 四个核心概念

| 名称 | 作用 |
| --- | --- |
| channel name | 一种稳定的消息格式，例如 AG-UI |
| `Identity.threadId` | thread 级有序日志、generation 和回放游标 |
| `Identity.runId` | 一次语义生产者，也是调用方的幂等 key |
| `seq` | thread 日志内从 1 开始的连续提交位置 |

同一个 `Identity` 永远表示同一次语义运行。网络重试、附着和回放复用它；新输入使用新的 `runId`。Messaging 不比较请求正文，权限、正文一致性和业务幂等由调用方负责。

## `after` 游标

| 值 | 行为 |
| --- | --- |
| `None` | 在 prepare 时捕获当前末尾，只接收之后的数据 |
| `0` | 从第一条保留消息开始 |
| `N` | 返回 `seq > N` 的消息 |

负数或超过当前末尾会抛出 `InvalidCursor`。

## 应用生命周期

通常在应用启动时创建一个 `Messaging`，关闭时等待它拥有的生产者安全结束：

```python
async with Messaging(backend=backend) as messaging:
    channel = messaging.channel(name="agent-events")
    await serve_application(channel)
```

## 下一步

- [投递、回放和 SSE](delivery-and-replay.md)
- [取消、延迟创建与恢复](cancellation-and-recovery.md)
- [Redis、自定义 codec 和 backend](backends-and-codecs.md)
- [Messaging 使用参考](api-reference.md)
