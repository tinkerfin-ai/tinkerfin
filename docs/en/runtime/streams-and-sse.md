# Streams and SSE

[Create and run a Deep Agent](deep-agents.md) · [中文](../../zh/runtime/streams-and-sse.md)

`astream()` returns an object stream. You can process its objects on the server or encode them as SSE for a browser.

## Consume the object stream

```python
runtime = agent.new()
stream = runtime.astream(graph_input, config)

try:
    async for part in stream:
        await handle(part)
finally:
    await stream.aclose()
```

Normal exhaustion cleans up automatically. Calling `aclose()` explicitly is useful when the client disconnects, the consumer stops early, or its task is cancelled.

The consumer pulls one item at a time. A slow consumer naturally slows the producer, so you do not need an unbounded queue.

## Convert the stream to SSE

```python
sse = stream.to_sse()
await sse.prepare()

try:
    async for chunk in sse:
        await send_to_client(chunk)
finally:
    await sse.aclose()
```

Each `chunk` is an encoded SSE string. The default mapper supports TinkerFin's native stream format.

### `to_sse()` parameters

| Parameter | Default | Purpose |
| --- | --- | --- |
| `timeout` | `None` | Total time limit for the native SSE stream |
| `mapper` | `None` | Converts a stream object into `SsePayload` |
| `event_id_resolver` | `None` | Produces the SSE `id` for each event |

`AgUiEventStream.to_sse()` does not accept `timeout`; configure its run timeout when creating the AG-UI Runtime.

## Customize event names and data

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

### `SsePayload` fields

| Field | Required | Purpose |
| --- | --- | --- |
| `data` | yes | The SSE `data:` value |
| `event` | no | An SSE event name |
| `retry` | no | Suggested reconnect delay in milliseconds; must be non-negative |

Use stable event IDs if clients need replay. For durable reconnection, committed Messaging sequence numbers are a better choice than random IDs.

## Check the request before streaming

`prepare()` can run an asynchronous preflight before the HTTP response starts.

```python
async def authorize() -> None:
    await verify_user_can_read_thread()


await sse.prepare(preflight=authorize)
```

If preflight fails, the HTTP layer can still return a normal error response because no SSE bytes have been sent.

## Timeout, cancellation, and close behavior

| Situation | Behavior |
| --- | --- |
| Normal exhaustion | Closes upstream and propagates errors |
| Consumer stops early | Call `aclose()` to release resources |
| Current task is cancelled | Cancellation propagates while required cleanup settles |
| Native stream exceeds its total timeout | Fails the run and closes upstream |
| `prepare()` fails | Does not start the response |

Direct SSE does not save history. Use [Messaging SSE](../messaging/delivery-and-replay.md) when a client must reconnect and replay events.

Next: [Custom sources and run coordination](extensions.md).
