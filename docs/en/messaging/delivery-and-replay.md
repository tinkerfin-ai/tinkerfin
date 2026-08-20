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
body = await channel.sse(
    source,
    identity=identity,
    after=lambda: parse_last_event_id(request.headers.get("Last-Event-ID")),
)
```

The resolver runs once before durable preparation. Committed sequence numbers become SSE IDs.

## Read committed data

```python
latest = await channel.latest_seq(identity=identity)
page = await channel.read(identity=identity, after=100, limit=200)
subscription = await channel.follow(identity=identity, after=100)
```

| API | Scope | Result |
| --- | --- | --- |
| `latest_seq()` | whole thread | Current maximum seq, or 0 |
| `read()` | whole thread | Finite ascending page |
| `follow()` | selected run | Replay, then wait for its terminal state |

Thread-level methods use `identity.threadId`, but still accept the complete Identity so extensions never flatten thread and run into separate parameters.

## Envelope v2

| Field | Purpose |
| --- | --- |
| `schemaVersion` | Fixed at 2 |
| `channel` | Codec namespace |
| `identity` | Nested threadId and runId |
| `seq` | Thread-level committed position |
| `messageId` | Stable message idempotency ID |
| `codec` | Persisted format ID |
| `payload` | Encoded bytes |
| `createdAt` | UTC time allocated on first commit |

Only Envelope v2 is readable. Envelope v1 records cannot be loaded; change the storage prefix or remove records you no longer need before switching formats.

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
