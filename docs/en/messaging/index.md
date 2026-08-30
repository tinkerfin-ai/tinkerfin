# Messaging basics

[Documentation](../README.md) · [中文](../../zh/messaging/index.md)

Messaging turns a single-use object stream into a durable producer that supports replay, attachment, and remote cancellation. An agent can keep running after a browser disconnects, and a later request resumes from the last durable sequence.

## Installation

Core messaging is protocol-neutral:

```bash
pip install tinkerfin-messaging
```

Install the codecs and backend used by the host. The example below needs AG-UI:

```bash
pip install tinkerfin "tinkerfin-messaging[agui]"
pip install "tinkerfin-messaging[native]"
pip install "tinkerfin-messaging[agui,redis]"
```

## Turn a TinkerFin stream into resumable SSE

```python
from tinkerfin import RunIdentity, TinkerFin
from tinkerfin_messaging import Messaging, create_agui_run_source


tinkerfin = TinkerFin()
agent = tinkerfin.create_deep_agent(model=model, tools=tools)
identity = RunIdentity(threadId="thread-42", runId="run-7")
source = create_agui_run_source(
    identity,
    open_events=lambda run_identity: tinkerfin.open_agui_run(
        run_identity,
        agent=agent,
        input=graph_input,
    ),
)

async with Messaging() as messaging:
    channel = messaging.channel(name="agent-events")
    body = await channel.sse(
        source,
        after=0,
        on_source_starting=activate_business_run,
        on_delivery_not_started=cleanup_business_run,
    )

    async for chunk in body:
        await send_to_client(chunk)
```

`create_agui_run_source()` keeps model, Sandbox, and Graph setup behind the owner
decision. Attachments never open the Agent. A name-only channel needs no duplicate codec,
thread, or run parameters, including for an empty source. Its optional transform may add
product metadata or change content, but cannot change an event type or any Run, message,
Tool, snapshot, or interrupt correlation identity. It cannot change any field of an
optional `RUN_STARTED.input`. Protocol-changing transformations belong to the advanced
unprofiled `map_source()` boundary.

`on_source_starting` activates host delivery for a new owner. If neither a producer nor
an attachment is established, `on_delivery_not_started` performs host cleanup. An
attachment invokes neither callback.

An attachment never opens its unused candidate source, but Messaging closes that
single-use candidate before returning. Do not reuse it after `wrap()` or `sse()`.

`AgUiCodec` requires `[agui]`; `NativeStreamPartCodec` requires `[native]`; and
`RedisBackend` requires `[redis]`. Missing extras fail at the relevant lazy import with
the exact installation command instead of loading the Agent Runtime into Messaging Core.

The returned body already contains SSE bytes and is caller-owned; close it if HTTP setup
fails before consumption. Do not call Runtime `to_sse()` first or pass a pre-encoded
`SseBody` into Messaging.

## Custom sources

Custom sources have no identity profile, so provide one explicitly:

```python
channel = messaging.channel(name="custom", codec=codec)
subscription = await channel.wrap(source, identity=identity, after=0)
```

If a source has a RunIdentity profile and an explicit different RunIdentity is supplied, preflight fails before backend preparation or source opening.

## Core concepts

| Name | Purpose |
| --- | --- |
| channel name | Stable payload format, such as AG-UI |
| `RunIdentity.threadId` | Ordered log, generation, and replay cursor scope |
| `RunIdentity.runId` | Semantic producer and caller idempotency key |
| `seq` | One-based committed position in the thread log |

The same RunIdentity always means the same semantic run. Reuse it for retries and attachment; use a new runId for new input. Messaging does not compare request bodies—authorization and business idempotency belong to the caller.

## `after`

| Value | Behavior |
| --- | --- |
| `None` | Capture the current tail during prepare and receive later data |
| `0` | Replay from the first retained message |
| `N` | Return messages where `seq > N` |

Negative cursors and cursors beyond the current generation tail raise `InvalidCursor`.

## Application lifecycle

```python
async with Messaging(backend=backend) as messaging:
    channel = messaging.channel(name="agent-events")
    await serve_application(channel)
```

Closing signals current producers before waiting for cancellation preflights, then waits
for owned producer settlement and cleanup.

## Next steps

- [Delivery, replay, and SSE](delivery-and-replay.md)
- [Cancellation, deferred sources, and recovery](cancellation-and-recovery.md)
- [Redis, codecs, and custom backends](backends-and-codecs.md)
- [Messaging usage reference](api-reference.md)
