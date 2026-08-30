# Messaging 入门

[文档首页](../README.md) · [English](../../en/messaging/index.md)

Messaging 把一次性对象流变成可以保存、回放、附着和远程取消的生产者。浏览器断开后，Agent 可以继续运行；重新连接时从最后一个 durable 序号继续读取。

## 安装

基础 Messaging 与具体协议无关：

```bash
pip install tinkerfin-messaging
```

按宿主实际使用的 codec 和 backend 安装 extra。下面的示例需要 AG-UI：

```bash
pip install tinkerfin "tinkerfin-messaging[agui]"
pip install "tinkerfin-messaging[native]"
pip install "tinkerfin-messaging[agui,redis]"
```

## 把 TinkerFin 流变成可续传 SSE

```python
from tinkerfin import RunIdentity, TinkerFin
from tinkerfin_messaging import Messaging, create_agui_run_source


tinkerfin = TinkerFin()
agent = tinkerfin.create_deep_agent(model=model, tools=tools)
identity = RunIdentity(threadId="thread-42", runId="run-7")
source = create_agui_run_source(
    identity,
    open_events=lambda run_identity: tinkerfin.open_agui_run(
        run_identity,
        agent=agent,
        input=graph_input,
    ),
)

async with Messaging() as messaging:
    channel = messaging.channel(name="agent-events")
    body = await channel.sse(
        source,
        after=0,
        on_source_starting=activate_business_run,
        on_delivery_not_started=cleanup_business_run,
    )

    async for chunk in body:
        await send_to_client(chunk)
```

`create_agui_run_source()` 会把模型、Sandbox 与 Graph setup 留到 owner 确定后；attachment 不会
打开 Agent。Name-only channel 不需要重复传 codec、thread 或 run，空流也能在第一条数据前识别。
可选 transform 可以补充产品 metadata 或调整内容，但不得改变事件类型以及 Run、消息、Tool、快照或
interrupt 的关联身份，也不得改变可选 `RUN_STARTED.input` 的任何字段。需要改变输出协议时，应使用
高级、无 profile 的 `map_source()` 边界。

`on_source_starting` 只为新 owner 激活宿主投递。既没有 producer、也没有 attachment 时，
`on_delivery_not_started` 负责宿主清理；attachment 不调用这两个 callback。

Attachment 不会打开未使用的候选 source，但 Messaging 会在返回前关闭这个 single-use
candidate。`wrap()` 或 `sse()` 返回后不得复用它。

`AgUiCodec` 需要 `[agui]`，`NativeStreamPartCodec` 需要 `[native]`，`RedisBackend`
需要 `[redis]`。缺少 extra 时，相应懒加载入口会给出准确安装命令；Messaging Core 不会因此
加载完整 Agent Runtime。

`body` 已经是调用方拥有的 SSE bytes 流；HTTP setup 在消费前失败时应关闭它。不要再调用
Runtime 的 `to_sse()`，也不要把已经编码过的 `SseBody` 交给 Messaging。

## 自定义 source

自定义 source 没有 RunIdentity profile，需要显式提供一次：

```python
channel = messaging.channel(name="custom", codec=codec)
subscription = await channel.wrap(
    source,
    identity=identity,
    after=0,
)
```

如果 source 自带 RunIdentity，又显式提供了不同值，Messaging 会在 backend prepare 和 source 打开前拒绝。

## 四个核心概念

| 名称 | 作用 |
| --- | --- |
| channel name | 一种稳定的消息格式，例如 AG-UI |
| `RunIdentity.threadId` | thread 级有序日志、generation 和回放游标 |
| `RunIdentity.runId` | 一次语义生产者，也是调用方的幂等 key |
| `seq` | thread 日志内从 1 开始的连续提交位置 |

同一个 `RunIdentity` 永远表示同一次语义运行。网络重试、附着和回放复用它；新输入使用新的 `runId`。Messaging 不比较请求正文，权限、正文一致性和业务幂等由调用方负责。

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

关闭时会先通知当前生产者，再等待取消 preflight、producer settlement 和清理完成。

## 下一步

- [投递、回放和 SSE](delivery-and-replay.md)
- [取消、延迟创建与恢复](cancellation-and-recovery.md)
- [Redis、自定义 codec 和 backend](backends-and-codecs.md)
- [Messaging 使用参考](api-reference.md)
