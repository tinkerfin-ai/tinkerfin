# 事件流与 SSE

[创建和运行 Deep Agent](deep-agents.md) · [English](../../en/runtime/streams-and-sse.md)

`astream()` 返回的是对象流。你可以在服务端直接处理对象，也可以把它编码成 SSE 发送给浏览器。

## 直接消费对象流

```python
runtime = agent.new(identity=identity)
stream = runtime.astream(graph_input, config)

try:
    async for part in stream:
        await handle(part)
finally:
    await stream.aclose()
```

正常迭代完成时会自动清理。提前退出、客户端断开或外层任务被取消时，显式调用 `aclose()` 更容易保证上游及时关闭。

数据由消费者逐条拉取。消费者处理较慢时，上游也会自然减速，不需要用无限队列缓存全部结果。

## 转成 SSE

```python
sse = stream.to_sse()
await sse.prepare()

try:
    async for chunk in sse:
        await send_to_client(chunk)
finally:
    await sse.aclose()
```

默认编码适合 TinkerFin 的原生流。每个 `chunk` 都是一个已经编码的 SSE 字符串。

### `to_sse()` 参数

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `timeout` | `None` | 整条原生 SSE 流的总时限；`None` 表示不限制 |
| `mapper` | `None` | 把对象转换为 `SsePayload` 的函数 |
| `event_id_resolver` | `None` | 为每条事件生成 SSE `id` 的函数 |

`AgUiEventStream.to_sse()` 不接收 `timeout`，因为 AG-UI 的运行超时在创建 Runtime 时配置。

## 自定义事件名称和数据

```python
from tinkerfin import SsePayload


async def to_payload(part) -> SsePayload:
    return SsePayload(
        event="agent-part",
        data=part.model_dump_json(),
        retry=3000,
    )


async def event_id(part) -> str | None:
    if isinstance(part.data, dict):
        value = part.data.get("event_id")
        return value if isinstance(value, str) else None
    return None


sse = stream.to_sse(mapper=to_payload, event_id_resolver=event_id)
```

### `SsePayload` 字段

| 字段 | 必填 | 作用 |
| --- | --- | --- |
| `data` | 是 | SSE 的 `data:` 内容 |
| `event` | 否 | SSE 事件名称 |
| `retry` | 否 | 建议客户端断线后等待多少毫秒再连接，必须大于等于 0 |

事件 ID 必须能稳定表示事件位置。如果需要断线回放，不要临时生成随机 ID；使用 Messaging 提交后的连续序号更合适。

## 在发送前做检查

`prepare()` 可以先执行异步预检查，再让 HTTP 层确认响应可以开始。

```python
async def authorize() -> None:
    await verify_user_can_read_thread()


await sse.prepare(preflight=authorize)
```

预检查失败时还没有开始发送 SSE，HTTP 层仍可返回普通错误响应。

## 超时、取消和关闭

| 情况 | 结果 |
| --- | --- |
| 正常迭代完成 | 上游关闭，错误正常传播 |
| 消费者提前退出 | 调用 `aclose()` 释放资源 |
| 当前任务被取消 | 取消继续向上传播，并等待必要清理 |
| 原生流超过总时限 | 运行失败并关闭上游 |
| `prepare()` 失败 | 不开始发送响应 |

直接 SSE 不保存历史。客户端重连后想继续读取之前的事件，应使用 [Messaging 的 SSE](../messaging/delivery-and-replay.md)。

下一篇：[运行协调与 Redis 租约](extensions.md)。
