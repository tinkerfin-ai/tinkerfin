# Redis, custom codecs, and backends

[Cancellation, deferred sources, and recovery](cancellation-and-recovery.md) · [中文](../../zh/messaging/backends-and-codecs.md)

The default `MemoryBackend` is for one-process development. Use Redis when multiple processes share events, run state, and cancellation.

## Use Redis

```bash
pip install "tinkerfin-messaging[redis]"
```

```python
from redis.asyncio import Redis
from tinkerfin_messaging import (
    Messaging,
    MessagingLimits,
    MessagingRetentionPolicy,
    RedisBackend,
)


redis = Redis.from_url(
    "redis://localhost:6379/0",
    decode_responses=False,
)
backend = RedisBackend(
    redis,
    key_prefix="my-app:tinkerfin",
    lease_ttl=15.0,
    poll_interval=0.1,
    limits=MessagingLimits(),
    retention_policy=MessagingRetentionPolicy.expire_after(86_400),
)
messaging = Messaging(backend=backend)
```

| Parameter | Default | Purpose |
| --- | --- | --- |
| `client` | required | Async Redis client returning bytes |
| `key_prefix` | `tinkerfin-messaging` | Prefix reserved for this application |
| `lease_ttl` | `15.0` | Producer ownership lease in seconds |
| `poll_interval` | `0.1` | Delete-lease contention interval |
| `limits` | `MessagingLimits()` | Encoded payload, checkpoint, message-count, and thread-byte limits |
| `retention_policy` | disabled | Terminal thread-generation replay window |

The caller owns the Redis client and closes it during application shutdown. Size the connection pool for blocked followers, cancellation waiters, and ordinary commands.

An enabled retention policy starts at terminal settlement. Active producers do not
expire, and a new Run before the deadline clears the timer. An expired generation raises
`StreamExpired`; an explicit `after=0` start creates the next empty generation. Redis
uses its server clock and performs resumable physical cleanup when a backend operation
first observes the deadline. `delete_stream()` remains an explicit, distinct
`StreamDeleted` lifecycle.

## Select a built-in codec explicitly

Install the matching codec extra:

```bash
pip install "tinkerfin-messaging[agui]"
# or: pip install "tinkerfin-messaging[native]"
```

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
| `[agui]` | `AgUiCodec` | AG-UI encoding, decoding, and SSE |
| `[native]` | `NativeStreamPartCodec` | Canonical Native replay encoding, decoding, and SSE |
| `[redis]` | `RedisBackend` | Multi-process durable backend |

Canonical TinkerFin streams include immutable codec and RunIdentity profiles, so a
name-only channel infers both. Native Runtime sources additionally transfer the
Driver-owned `NativeStreamPart` through `MessageCodecInputSource`; the codec never
reparses a live upstream mapping. Custom sources need an explicit codec and RunIdentity.

RedisBackend stores the complete limits fingerprint, per-generation `payload_bytes`,
and the current and immediately previous owner's successful lease-renewal counts and UTC
timestamps for trusted postmortem diagnostics. Workers sharing a channel must use
identical limits and retention policies.
Quota checks and counters are atomic with append after message-ID idempotency. These
fields never enter `MessageEnvelope`.

Default limits are 16 MiB per encoded message, 1 MiB per checkpoint, 100,000 messages
per thread generation, and 1 GiB of encoded payload per thread generation. Custom
backends expose the same immutable `limits` property and must reject quota overflow
before mutation.

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
- producer lease renewal interval, expiry budget, fencing, and ownership-loss detection;
- stream generation isolation and deletion.

All backend operations are asynchronous. Do not block the event loop with synchronous database or network clients. Match the public behavior of `MemoryBackend` when implementing another backend.

Next: [Messaging usage reference](api-reference.md).
