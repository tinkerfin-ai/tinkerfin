# Redis, custom codecs, and backends

[Cancellation, deferred sources, and recovery](cancellation-and-recovery.md) · [中文](../../zh/messaging/backends-and-codecs.md)

The default `MemoryBackend` is for one-process development. Use Redis when multiple processes share events, run state, and cancellation.

## Use Redis

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

| Parameter | Default | Purpose |
| --- | --- | --- |
| `client` | required | Async Redis client returning bytes |
| `key_prefix` | `tinkerfin-messaging` | Prefix reserved for this application |
| `lease_ttl` | `15.0` | Producer ownership lease in seconds |
| `poll_interval` | `0.1` | Delete-lease contention interval |

The caller owns the Redis client and closes it during application shutdown. Size the connection pool for blocked followers, cancellation waiters, and ordinary commands.

Messaging does not impose retention or compaction. Apply your own policy through `delete_stream()`.

## Select a built-in codec explicitly

```python
from tinkerfin_messaging import AgUiCodec


codec = AgUiCodec()
channel = messaging.channel(
    name="agent-events",
    codec=codec,
    renderer=codec,
)
```

| Installation | API | Use |
| --- | --- | --- |
| base | `AgUiCodec` | AG-UI encoding, decoding, and SSE |
| base | `NativeStreamPartCodec` | Native v2 encoding, decoding, and SSE |
| `[redis]` | `RedisBackend` | Multi-process durable backend |

Canonical TinkerFin streams include immutable codec and Identity profiles, so a name-only channel infers both. Custom sources need an explicit codec and Identity.

RedisBackend reads persistent schema 4 only. Schema 3 records are incompatible; use a new `key_prefix` or remove records you no longer need before switching.

## Define a custom message format

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

`codec_id` identifies the durable format. Do not reuse it for an incompatible payload after data exists.

Add an SSE renderer when needed:

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

## Define a custom source

`MessageSource` supports async iteration and idempotent close:

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

Implement `ProfiledMessageSource` only when the source can declare a complete immutable codec profile. For most application sources, an explicit channel codec is simpler.

## Define a custom backend

Implement `MessagingBackend` only when another shared store is required. A complete implementation must provide:

- atomic preparation, owner/attachment selection, and codec validation;
- ordered appends, stable sequence numbers, and message ID deduplication;
- history reads and continuing follow;
- run completion, failure, and cancellation signals;
- producer leases, fencing, and ownership-loss detection;
- stream generation isolation and deletion.

All backend operations are asynchronous. Do not block the event loop with synchronous database or network clients. Match the public behavior of `MemoryBackend` when implementing another backend.

Next: [Messaging usage reference](api-reference.md).
