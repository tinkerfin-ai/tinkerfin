# Delivery, replay, and SSE

[Messaging basics](index.md) · [中文](../../zh/messaging/delivery-and-replay.md)

## Start or attach

```python
subscription = await channel.wrap(
    source,
    identity=identity,  # omit for a profiled TinkerFin source
    after=0,
)
```

Before returning, `wrap()` atomically decides whether this caller owns a new producer or attaches an existing run. A different active run in the same thread raises `RunAlreadyActive`; a different persisted format raises `CodecMismatch`.

`runId` is the idempotency key. Messaging neither reads nor stores a business request digest.

## Return SSE directly

```python
from tinkerfin_messaging import parse_sse_event_id


body = await channel.sse(
    source,
    identity=identity,
    after=lambda: parse_sse_event_id(request.headers.get("Last-Event-ID")),
    on_source_ready=activate_business_run,
    on_delivery_not_started=cleanup_business_run,
)
```

The resolver runs once before durable preparation. `parse_sse_event_id()` accepts only
canonical non-negative ASCII decimal values. Committed sequence numbers become SSE IDs.

`on_source_ready` runs once for a new owner after the request-owned source is ready and
before producer creation. `on_delivery_not_started` runs only when readiness was not
reached and no attachment was established. Attachments invoke neither callback. The
returned body is caller-owned and must be closed when it will not be consumed.

When a valid attachment is established, Messaging closes the unused single-use
candidate source without opening it. The candidate cannot be reused.

## Read committed data

```python
latest = await channel.latest_seq(identity=identity)
page = await channel.read(identity=identity, after=100, limit=200)
subscription = await channel.follow(identity=identity, after=100)
status = await channel.get_run_status(identity=identity)
```

| API | Scope | Result |
| --- | --- | --- |
| `latest_seq()` | whole thread | Current maximum seq, or 0 |
| `read()` | whole thread | Finite ascending page |
| `follow()` | selected run | Replay, then wait for its terminal state |
| `get_run_status()` | selected run | Current durable run status without ownership |

Thread-level methods use `identity.threadId`, but still accept the complete RunIdentity so extensions never flatten thread and run into separate parameters.

`get_run_status()` can atomically archive an expired producer lease as `owner_lost`.
It raises `RunNotFound` when no durable record exists and never creates or recovers a
producer.

## Envelope

| Field | Purpose |
| --- | --- |
| `channel` | Codec namespace |
| `identity` | Nested threadId and runId |
| `seq` | Thread-level committed position |
| `messageId` | Stable message idempotency ID |
| `codec` | Persisted format ID |
| `payload` | Encoded bytes |
| `createdAt` | UTC time allocated on first commit |

Messaging stores one current envelope shape. Rebuild records before deploying an
incompatible application-owned format; runtime decoding does not negotiate formats.

## Observe owner commits

```python
async def on_committed(envelope) -> None:
    await projection_queue.put(envelope)


subscription = await channel.wrap(
    source,
    identity=identity,
    on_committed=on_committed,
)
```

Attachments do not re-notify old commits. Observer failures are logged without changing an already committed run outcome.

## Delete a thread log

```python
await channel.delete_stream(identity=identity)
```

Deletion covers the entire threadId. An active producer raises `StreamDeleteConflict`; a missing stream is an idempotent success. Recreating the thread uses a new generation, and old handles can no longer read or mutate it.

Next: [Cancellation, deferred sources, and recovery](cancellation-and-recovery.md).
