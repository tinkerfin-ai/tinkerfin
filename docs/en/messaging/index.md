# Messaging basics

[Documentation](../README.md) · [中文](../../zh/messaging/index.md)

Messaging turns a single-use asynchronous source into an independent producer with persistence, replay, attachment, and cancellation. An agent can continue after a browser disconnects, and the browser can reconnect from its last durable event.

## When to use it

| Requirement | Need Messaging? |
| --- | --- |
| Consume events only inside the current request | Not necessarily |
| Keep the agent running after disconnect | Yes |
| Resume from `Last-Event-ID` | Yes |
| Attach several requests to one run | Yes |
| Cancel a run from another process | Yes, with a shared backend |
| Save events for later reads | Yes |

## Installation

```bash
pip install "tinkerfin-messaging[agui]"
```

The default `MemoryBackend` is enough for one-process development. Add Redis when several processes must share state.

## Turn an AG-UI stream into durable SSE

```python
from tinkerfin_messaging import Messaging


async with Messaging() as messaging:
    channel = messaging.channel(name="agent-events")

    runtime = agent.new_agui(run_input=run_input)
    events = runtime.astream(
        graph_input,
        {"configurable": {"thread_id": run_input.thread_id}},
    )

    body = await channel.sse(
        events,
        stream=run_input.thread_id,
        run=run_input.run_id,
        after=0,
        attach_identity=run_input,
    )

    async for chunk in body:
        await send_to_client(chunk)
```

`channel.sse()` completes durable start-or-attach checks before it returns. `body` already yields SSE bytes; do not encode it again.

## Four important identifiers

| Name | Example | Purpose |
| --- | --- | --- |
| channel name | `agent-events` | Stable message format namespace |
| stream | `thread-42` | One independent ordered log |
| run | `run-7` | One producer attempt inside the stream |
| seq | `1, 2, 3...` | Position of each committed message |

Do not mix AG-UI, native LangGraph, and custom formats under one channel name. A channel handle is reusable across requests, but each supplied source is single-use.

## The `after` cursor

`after` means “the last sequence already received.” Replay is exclusive.

| Value | Behavior |
| --- | --- |
| `None` | Start at the current tail and wait for new messages |
| `0` | Replay from the first retained message |
| `N` | Replay messages with `seq > N` |

Negative or beyond-tail cursors raise `InvalidCursor`.

## Application lifetime

Create one Messaging instance when the application starts and close it during shutdown:

```python
async with Messaging(backend=backend) as messaging:
    channel = messaging.channel(name="agent-events")
    await serve_application(channel)
```

Closing waits for owned producers to settle. An injected backend follows its own resource-ownership contract.

## Next steps

- [Delivery, replay, and SSE](delivery-and-replay.md)
- [Cancellation, deferred sources, and recovery](cancellation-and-recovery.md)
- [Redis, custom codecs, and backends](backends-and-codecs.md)
- [Messaging usage reference](api-reference.md)
