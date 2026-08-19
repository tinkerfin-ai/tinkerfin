# Redis、自定义 codec 和 backend

[取消、延迟创建与恢复](cancellation-and-recovery.md) · [English](../../en/messaging/backends-and-codecs.md)

默认 `MemoryBackend` 适合单进程开发。如果多个进程要共享事件、运行状态和取消请求，使用 Redis。

## 使用 Redis

```bash
pip install "tinkerfin-messaging[redis]"
```

```python
from redis.asyncio import Redis
from tinkerfin_messaging import Messaging, RedisBackend


redis = Redis.from_url(
    "redis://localhost:6379/0",
    decode_responses=False,
)
backend = RedisBackend(
    redis,
    key_prefix="my-app:tinkerfin",
    lease_ttl=15.0,
    poll_interval=0.1,
)
messaging = Messaging(backend=backend)
```

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `client` | 必填 | 异步 Redis client，必须返回 bytes |
| `key_prefix` | `tinkerfin-messaging` | 当前应用独占的 Redis key 前缀 |
| `lease_ttl` | `15.0` | 生产者所有权租约秒数 |
| `poll_interval` | `0.1` | 删除时等待活跃租约的轮询间隔 |

Redis client 是调用方提供的资源，应用关闭时自行关闭。连接池容量要覆盖同时等待消息、等待取消和普通命令的连接数。

Messaging 不自动压缩或过期历史消息。应用需要根据自己的保留策略调用 `delete_stream()`。

## 显式使用内置 codec

```python
from tinkerfin_messaging import AgUiCodec


codec = AgUiCodec()
channel = messaging.channel(
    name="agent-events",
    codec=codec,
    renderer=codec,
)
```

| extra | API | 用途 |
| --- | --- | --- |
| `agui` | `AgUiCodec` | AG-UI 事件编码、解码和 SSE |
| `native` | `NativeStreamPartCodec` | LangGraph v2 数据编码、解码和 SSE |
| `redis` | `RedisBackend` | 多进程持久 backend |

TinkerFin 的规范事件流带有内置格式信息，因此 name-only channel 通常可以自动选择 codec。自定义 source 必须显式配置。

## 自定义消息格式

如果要保存自己的对象，实现 `MessageCodec`：

```python
import json


class JsonEventCodec:
    codec_id = "my-app.event.v1"

    def encode(self, item: dict[str, object]) -> bytes:
        return json.dumps(item, separators=(",", ":")).encode()

    def decode(self, payload: bytes) -> dict[str, object]:
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise ValueError("event must be an object")
        return value
```

`codec_id` 是持久格式标识。已经有数据后不要在不兼容的情况下复用同一个 ID。

如果还要输出 SSE，实现 renderer：

```python
class JsonEventRenderer:
    def render(self, *, seq: int, payload: dict[str, object]) -> bytes:
        data = json.dumps(payload, separators=(",", ":"))
        return f"id: {seq}\nevent: custom\ndata: {data}\n\n".encode()
```

```python
channel = messaging.channel(
    name="custom-events",
    codec=JsonEventCodec(),
    renderer=JsonEventRenderer(),
)
```

## 自定义 source

一个 `MessageSource` 需要支持异步迭代和幂等关闭：

```python
class QueueSource:
    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.queue.get()
        if item is STOP:
            raise StopAsyncIteration
        return item

    async def aclose(self) -> None:
        self.closed = True
```

如果 source 能稳定声明自己的 codec profile，可以实现 `ProfiledMessageSource`；普通业务 source 通常直接给 channel 传 codec 更简单。

## 自定义 backend

只有需要接入其他共享存储时才实现 `MessagingBackend`。它需要完整支持：

- 原子准备、owner/attachment 判定和 codec 校验；
- 有序追加、稳定序号和 message ID 去重；
- 历史读取和持续 follow；
- run 完成、失败和取消信号；
- producer lease、fencing 和所有权丢失；
- stream 代际隔离与删除。

backend 的方法都是异步协议。不要用同步数据库或同步网络客户端阻塞事件循环。自定义实现应与 `MemoryBackend` 的公开行为保持一致。

下一篇：[Messaging 使用参考](api-reference.md)。
