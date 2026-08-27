# tinkerfin-messaging

## What it is

`tinkerfin-messaging` persists and replays asynchronous object streams. It supports TinkerFin Native and AG-UI streams out of the box while keeping custom source, codec, renderer, and backend extension points.

## Installation

```bash
pip install tinkerfin-messaging
```

Native and AG-UI codecs are included. Redis remains optional:

```bash
pip install "tinkerfin-messaging[redis]"
```

## Quick Start

```python
from tinkerfin import Identity
from tinkerfin_messaging import Messaging


identity = Identity(threadId="thread-1", runId="run-1")
events = agent.new_agui(identity=identity).astream(graph_input)

async with Messaging() as messaging:
    channel = messaging.channel(name="agent-events")
    body = await channel.sse(events, after=0)

    async for frame in body:
        await send(frame)
```

TinkerFin streams carry their codec and Identity profiles. Do not repeat thread or run parameters, and do not pre-encode the stream with Runtime `to_sse()`.

## Custom sources

```python
channel = messaging.channel(
    name="custom-events",
    codec=codec,
    renderer=renderer,
)
subscription = await channel.wrap(
    custom_source,
    identity=identity,
    after=0,
)
```

A custom source must receive one explicit Identity. A profiled source may omit it; an explicitly conflicting Identity fails before backend preparation or source opening.

## Channel operations

| Operation | Purpose |
| --- | --- |
| `wrap(source, identity=..., after=...)` | Start or attach and return decoded messages |
| `sse(source, identity=..., after=...)` | Start or attach and return SSE bytes |
| `wrap_recoverable(...)` | Rebuild a lost owner from a committed checkpoint |
| `get_run_status(identity=...)` | Read one run's authoritative durable status |
| `latest_seq(identity=...)` | Read the thread tail |
| `read(identity=..., after=..., limit=...)` | Read a finite thread page |
| `follow(identity=..., after=...)` | Follow one run to its terminal state |
| `validate_cursor(identity=..., after=...)` | Validate without creating a run |
| `cancel(identity=...)` | Request cancellation and wait for settlement |
| `delete_stream(identity=...)` | Delete an inactive thread generation |

`Identity.runId` is the caller's idempotency key. Messaging does not store or compare business request bodies. Reuse an Identity only for retry, replay, or attachment to the same semantic run.

`get_run_status()` returns `running`, `cancel_requested`, `completed`, `cancelled`,
`failed`, or `owner_lost`. A leased backend can atomically classify an expired owner as
`owner_lost` during this lookup; the method never grants producer ownership.

## Durable values

`MessageEnvelope` contains channel, nested Identity, sequence, message ID, codec,
payload bytes, and UTC creation time.

`RecoveryCheckpoint` contains an opaque source position plus the last stable message ID.

## Deferred and recoverable sources

Use `DeferredMessageSource` when only the durable owner should build an expensive Graph or Sandbox. Use `ProfiledDeferredMessageSource` when the codec and Identity must be visible before opening.

Use `RecoverableSource` and `RecoverableMessage` when a producer can rebuild from the last atomically committed checkpoint. Stable message IDs make commits idempotent; external side effects still require application-level idempotency.

## Backends

`MemoryBackend` is complete but process-local. `RedisBackend` shares logs, run state, cancellation, leases, fencing, and generation deletion across workers.

```python
from redis.asyncio import Redis
from tinkerfin_messaging import Messaging, RedisBackend


redis = Redis.from_url("redis://localhost:6379/0", decode_responses=False)
backend = RedisBackend(redis, key_prefix="my-app:messaging")
messaging = Messaging(backend=backend)
```

Redis keeps trusted per-owner lease renewal counts and timestamps for postmortem
diagnostics; these fields never enter envelopes or client output.

## Capacity limits

`MessagingLimits` is immutable and enforced by both built-in backends before durable
mutation. Defaults are 16 MiB per encoded message, 1 MiB per checkpoint, 100,000
messages per thread generation, and 1 GiB of encoded payload per thread generation.
`delete_stream()` starts a new generation with empty counters.

Redis stores the complete limits fingerprint in channel metadata and tracks
`payload_bytes` atomically with append and idempotency. Every worker sharing a key prefix
and channel must use the same limits. A mismatch fails without changing existing state;
an idempotent retry of an already committed message succeeds even when the thread is now
at its quota.

## Built-in codecs

| API | Live input | Replay output |
| --- | --- | --- |
| `AgUiCodec` | `BaseEvent` | `BaseEvent` |
| `NativeStreamPartCodec` | LangGraph v2 mapping | `NativeStreamPart` |

Both codecs also render durable SSE with the committed sequence as the event ID.

## Cancellation and cleanup

A cancel callback accepts zero arguments or one `CancelContext(channel, identity)`. It may return a finite terminal tail. TinkerFin AG-UI streams already own their cancellation callback.

`Messaging(settlement_timeout=...)` limits only the caller's wait. Protected producer, commit, callback, and close tasks remain owned and can be awaited again with `aclose()`.

`cancel()` participates in the Messaging preflight lifecycle. Shutdown signals current
producers before waiting for cancellation preflights, then settles producers and closes
the borrowed backend boundary without leaving a control task to outlive the facade.
Closing mapped and subscribed sources retains the underlying close task across caller
cancellation.

`read()` and `follow()` reject a cursor greater than the current generation tail with
`InvalidCursor`; they never reinterpret it as an empty page or a future wait.

## Documentation

- [Messaging basics](https://github.com/tinkerfin-ai/tinkerfin/blob/main/docs/en/messaging/index.md)
- [Delivery and replay](https://github.com/tinkerfin-ai/tinkerfin/blob/main/docs/en/messaging/delivery-and-replay.md)
- [Backends and codecs](https://github.com/tinkerfin-ai/tinkerfin/blob/main/docs/en/messaging/backends-and-codecs.md)

## License

Apache License 2.0. See the
[repository license](https://github.com/tinkerfin-ai/tinkerfin/blob/main/LICENSE).
