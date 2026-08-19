# Delivery, replay, and SSE

[Messaging basics](index.md) · [中文](../../zh/messaging/delivery-and-replay.md)

Use `sse()` when the HTTP response needs SSE immediately. Use `wrap()` when server code needs decoded committed messages.

## Return SSE directly

```python
body = await channel.sse(
    source,
    stream="thread-42",
    run="run-7",
    after=lambda: parse_last_event_id(request),
    attach_identity={"user_id": 42, "request": request_payload},
    on_committed=notify_projection,
)
```

### Parameters

| Parameter | Default | Purpose |
| --- | --- | --- |
| `source` | required | Single-use asynchronous message source |
| `stream` | required | Ordering and producer-concurrency scope |
| `run` | required | Producer run identity |
| `after` | `None` | Concrete cursor or a no-argument synchronous resolver |
| `attach_identity` | `None` | Request identity and input that affect output |
| `cancel` | `None` | Callback used by remote cancellation |
| `on_committed` | `None` | Async observer called after each durable append |

The cursor resolver runs once and is suitable for request-local `Last-Event-ID` parsing. It must not perform blocking I/O.

`attach_identity` accepts finite JSON data or a Pydantic model. Messaging stores only its digest. Include every request fact that can change output; attaching the same run with a different identity fails.

## Consume decoded messages

```python
subscription = await channel.wrap(
    source,
    stream="thread-42",
    run="run-7",
    after=0,
    attach_identity=request_payload,
)

try:
    async for message in subscription:
        print(message.envelope.seq, message.data)
finally:
    await subscription.aclose()
```

`message.data` is codec-decoded. `message.envelope` contains the durable identity and sequence.

The same subscription can render SSE:

```python
body = subscription.sse()
async for chunk in body:
    await send_to_client(chunk)
```

A channel without an SSE renderer raises `SseRenderingUnsupported`.

## Read without starting a producer

```python
latest = await channel.latest_seq(stream="thread-42")

page = await channel.read(
    stream="thread-42",
    after=100,
    limit=50,
)

subscription = await channel.follow(
    stream="thread-42",
    run="run-7",
    after=latest,
)
```

| Method | Use |
| --- | --- |
| `latest_seq()` | Read the current tail; an empty stream returns 0 |
| `read()` | Read one finite ascending page |
| `follow()` | Replay and then wait for new messages from that run |
| `validate_cursor()` | Check a cursor before starting a response |

Reads require a known codec. Configure one explicitly, or first use a built-in TinkerFin source whose immutable profile establishes it in the current process.

## Observe committed messages

```python
async def notify_projection(envelope) -> None:
    await queue.put(
        (envelope.channel, envelope.stream, envelope.seq, envelope.message_id)
    )
```

`on_committed` runs after persistence. Its failure does not roll back the message or fail the run. Recovery may notify the same envelope again, so use channel, stream, seq, and message ID for idempotency.

Messaging waits for each observer before pulling the next source item, preserving order and bounded backpressure.

## Attach to an existing run

Only one active producer owns a `(channel, stream)`:

- same run and identity attaches without executing the source again;
- same run with a different identity raises `RunIdentityConflict`;
- another run while active raises `RunAlreadyActive`;
- a completed run is replay-only and never executes again.

Run IDs must be stable for retries and unique across different inputs.

## Delete history

```python
await channel.delete_stream(stream="thread-42")
```

Deletion removes messages, runs, recovery checkpoints, and deduplication data. An active producer raises `StreamDeleteConflict`; cancel or settle it first.

Recreating the same stream name starts sequence numbers at 1. Existing subscriptions remain bound to the deleted generation and raise `StreamDeleted`.

Next: [Cancellation, deferred sources, and recovery](cancellation-and-recovery.md).
