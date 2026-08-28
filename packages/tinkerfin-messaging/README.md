# tinkerfin-messaging

## What it is

`tinkerfin-messaging` persists and replays asynchronous object streams. Its Core is
protocol-neutral; optional integrations add Redis, AG-UI, and TinkerFin Native codecs.

## Installation

```bash
pip install tinkerfin-messaging
```

Install only the integrations used by the host:

```bash
pip install "tinkerfin-messaging[agui]"
pip install "tinkerfin-messaging[native]"
pip install "tinkerfin-messaging[redis]"
pip install "tinkerfin-messaging[agui,redis]"
```

The TinkerFin AG-UI quick start below also requires the Runtime package:

```bash
pip install "tinkerfin[agui]" "tinkerfin-messaging[agui]"
```

## Quick Start

```python
from tinkerfin_contracts import RunIdentity
from tinkerfin_messaging import Messaging


identity = RunIdentity(threadId="thread-1", runId="run-1")
events = agent.new_agui(identity=identity).astream(graph_input)

async with Messaging() as messaging:
    channel = messaging.channel(name="agent-events")
    body = await channel.sse(events, after=0)

    async for frame in body:
        await send(frame)
```

This example requires the `agui` extra. TinkerFin streams carry their codec and
RunIdentity profiles. Do not repeat thread or run parameters, and do not pre-encode the
stream with Runtime `to_sse()`.

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

A custom source must receive one explicit RunIdentity. A profiled source may omit it; an explicitly conflicting RunIdentity fails before backend preparation or source opening.

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

`RunIdentity.runId` is the caller's idempotency key. Messaging does not store or compare business request bodies. Reuse a RunIdentity only for retry, replay, or attachment to the same semantic run.

`get_run_status()` returns `running`, `cancel_requested`, `completed`, `cancelled`,
`failed`, or `owner_lost`. A leased backend can atomically classify an expired owner as
`owner_lost` during this lookup; the method never grants producer ownership.

## Durable values

`MessageEnvelope` contains channel, nested RunIdentity, sequence, message ID, codec,
payload bytes, and UTC creation time.

`RecoveryCheckpoint` contains an opaque source position plus the last stable message ID.

## Deferred and recoverable sources

Use `DeferredMessageSource` when only the durable owner should build an expensive Graph or Sandbox. Use `ProfiledDeferredMessageSource` when the codec and RunIdentity must be visible before opening.

Set `on_owner_preflight` when a host must atomically activate business state after durable owner selection but before the producer task or opener starts. Attachments never invoke it; failure releases the prepared owner and leaves the source unopened.

Use `RecoverableSource` and `RecoverableMessage` when a producer can rebuild from the last atomically committed checkpoint. Stable message IDs make commits idempotent; external side effects still require application-level idempotency.

## Backends

`MemoryBackend` is complete but process-local. `RedisBackend` shares logs, run state, cancellation, leases, fencing, and generation deletion across workers.

```python
from redis.asyncio import Redis
from tinkerfin_messaging import (
    Messaging,
    MessagingRetentionPolicy,
    RedisBackend,
)


redis = Redis.from_url("redis://localhost:6379/0", decode_responses=False)
backend = RedisBackend(
    redis,
    key_prefix="my-app:messaging",
    retention_policy=MessagingRetentionPolicy.expire_after(86_400),
)
messaging = Messaging(backend=backend)
```

Redis keeps trusted per-owner lease renewal counts and timestamps for postmortem
diagnostics; these fields never enter envelopes or client output.

Retention is disabled by default. `MessagingRetentionPolicy.expire_after(seconds)`
starts a thread-generation deadline after terminal settlement. Active producers never
expire, and a new Run admitted before the deadline clears the timer. After expiry,
replay/status/cursor operations raise `StreamExpired`; an explicit `after=0` start opens
the next empty generation. Old bound handles retain an `expired` tombstone even after a
replacement generation starts. Memory uses a monotonic clock. Redis uses its server
clock and performs generation-fenced, resumable physical cleanup when the deadline is
first observed by a backend operation.

Explicit `delete_stream()` is separate: it rejects an active producer and records a
`deleted` generation tombstone. Old handles raise `StreamDeleted`, not `StreamExpired`.

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

Redis also stores the retention fingerprint. Workers sharing a key prefix and channel
must use the same retention policy; a mismatch fails before mutation.

## Built-in codecs

| API | Live input | Replay output |
| --- | --- | --- |
| `AgUiCodec` | `BaseEvent` | `BaseEvent` |
| `NativeStreamPartCodec` | `NativeStreamPart` | `NativeStreamPart` |

`AgUiCodec` requires the `agui` extra. `NativeStreamPartCodec` requires the `native`
extra. A profiled Runtime source may implement `MessageCodecInputSource` to transfer an
already normalized finite value to its codec. TinkerFin Native streams use that hook to
hand off the selected Runtime Profile's canonical frame; Messaging never parses the live
Deep Agents or LangGraph mapping. Both codecs render durable SSE with the committed
sequence as the event ID.

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
