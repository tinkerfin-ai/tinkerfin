# Cancellation, deferred sources, and recovery

[Delivery, replay, and SSE](delivery-and-replay.md) · [中文](../../zh/messaging/cancellation-and-recovery.md)

This guide covers remote cancellation, creating an expensive source only for the producer owner, and rebuilding a source after owner loss.

## Remote cancellation

Register a callback when starting the producer:

```python
async def cancel_agent(context):
    running_task.cancel()
    return cancellation_tail


body = await channel.sse(
    source,
    identity=identity,
    cancel=cancel_agent,
)
```

Cancel from another request:

```python
cancelled = await channel.cancel(identity=identity)
```

The callback may take no arguments or one `CancelContext`. It may return a finite terminal tail for subscribers.

TinkerFin AG-UI streams already declare cancellation. Do not also pass `cancel=` when the source owns that callback.

`cancel()` is a Messaging preflight operation: it cannot outlive the facade and continue
against a closed borrowed backend. During shutdown, Messaging first signals current
producers, then joins cancel preflights and producer settlement so the two paths cannot
form a wait cycle.

| Result or error | Meaning |
| --- | --- |
| `True` | Cancellation was requested from the active producer |
| `False` | The run had already settled |
| `RunNotFound` | No such run exists |
| `CancellationUnsupported` | The run has no cancellation callback |
| `RunProducerFailed` | Producer or cancellation work failed |

## Create the agent only for the owner

For managed TinkerFin AG-UI runs, use the task-oriented helper:

```python
from tinkerfin_messaging import create_agui_run_source


source = create_agui_run_source(
    identity,
    open_events=lambda run_identity: tinkerfin.open_agui_run(
        run_identity,
        agent=create_agent,
        input=graph_input,
        config=graph_config,
    ),
    transform_event=add_product_metadata,
)
```

The helper opens the Agent only after Messaging selects this caller as owner. It hides
Binding, profile constants, source types, mapping, and the first-event cancellation
fence. The exact `RunIdentity` supplied once to the helper is passed to `open_events`.
`transform_event` may enrich product metadata or content, but it must preserve the
concrete event type, every protocol correlation identity, and the complete optional
`RUN_STARTED.input` supplied by the caller.

Use channel callbacks for host delivery state:

```python
body = await channel.sse(
    source,
    on_source_starting=activate_business_run,
    on_delivery_not_started=cleanup_business_run,
)
```

Source-owned `on_owner_preflight` is a separate advanced hook for preparing the source
itself. It is not a second name for host activation.

## Advanced deferred sources

Use `DeferredMessageSource` when a custom protocol source is expensive:

```python
from tinkerfin_messaging import (
    DeferredMessageSource,
    MessageSourceBinding,
    ProfiledDeferredMessageSource,
)


async def open_events():
    runtime = await create_runtime()
    events = runtime.astream(graph_input, graph_config)
    return MessageSourceBinding(source=events)


source = DeferredMessageSource(
    open_events,
    cancellable=True,
    cancel_after_first_item=True,
)
```

| Parameter | Purpose |
| --- | --- |
| `opener` | Asynchronously creates the source and returns `MessageSourceBinding` |
| `cancellable` | Declares whether the opened source supports cancellation |
| `cancel_after_first_item` | Prevents cancellation from overtaking the first protocol event |
| `on_owner_preflight` | Optional async owner-only activation before producer and opener execution |

Attachments and replay-only requests close the deferred wrapper without opening the real source. `cancel_after_first_item=True` is useful for protocols that must emit `RUN_STARTED` first.

Messaging settles `on_owner_preflight` after durable owner selection. A failure releases
that prepared owner and closes the deferred wrapper before its opener runs. Use it only
for source-owned preparation; host activation belongs in `on_source_starting`.

`MessageSourceBinding` holds the source and an optional cancel callback. Leave the callback empty when the source already declares its own.

Use `ProfiledDeferredMessageSource` when a custom opener returns a known AG-UI or Native
source and a name-only channel must know the codec and RunIdentity before opening it:

```python
from ag_ui.core import BaseEvent


source = ProfiledDeferredMessageSource(
    open_events,
    identity=identity,
    codec_profile="agui.event",
    source_type=BaseEvent,
    replay_type=BaseEvent,
    cancellable=True,
    cancel_after_first_item=True,
)
```

| Parameter | Default | Purpose |
| --- | --- | --- |
| `opener` | required | Asynchronously returns `MessageSourceBinding` |
| `identity` | required | Complete run identity available before open |
| `codec_profile` | required | Stable profile ID matching a built-in codec |
| `source_type` | required | Live value type returned by the opener |
| `replay_type` | required | Value type decoded by the codec |
| `cancellable` | required | Whether the source supports remote cancellation |
| `cancel_after_first_item` | `False` | Prevent cancellation from overtaking the first protocol event |
| `on_owner_preflight` | `None` | Owner-only async activation before producer execution |

## Fixed and transformed sources

```python
from tinkerfin_messaging import FiniteMessageSource, map_source


finite = FiniteMessageSource.from_events((started, finished))


async def enrich(event):
    return add_request_metadata(event)


mapped = map_source(source, enrich)
```

`map_source()` accepts a synchronous or asynchronous transform and preserves order, backpressure, cancellation tails, and close behavior.

Closing a mapped source or subscription retains the underlying close task across caller
cancellation. A later `aclose()` joins the same task; the backend iterator is not dropped
while its close is incomplete.

A transform may change the data type, so the mapped source no longer claims the original built-in codec profile. Configure the channel codec explicitly.

## Recover after producer owner loss

Implement `RecoverableSource` and use `wrap_recoverable()` when a source can restart from a stable position:

```python
subscription = await channel.wrap_recoverable(
    recoverable_source,
    identity=identity,
    after=0,
)
```

| Parameter | Default | Purpose |
| --- | --- | --- |
| `source` | required | Recoverable source implementing `open(checkpoint)` |
| `identity` | optional for a profiled source | Custom-source identity or equality check for the advertised RunIdentity |
| `after` | `None` | Exclusive replay cursor; `None` starts at the tail captured during prepare |
| `cancel` | `None` | Stops the source after accepted remote cancellation and may return a finite stable-ID tail |
| `on_committed` | `None` | Async observer after each successful owner commit |

Each item opened by the source is a `RecoverableMessage`:

```python
RecoverableMessage(
    message_id="source-event-17",
    data=event,
    checkpoint=RecoveryCheckpoint(
        position=b"17",
        last_message_id="source-event-17",
    ),
)
```

| Field | Purpose |
| --- | --- |
| `message_id` | Stable ID used for idempotent commits |
| `data` | Value to encode and persist |
| `checkpoint.position` | Opaque restart position understood by the source |
| `checkpoint.last_message_id` | Must match the current message ID |

Stable message IDs make commits idempotent. They do not make model calls, tool calls, or database writes exactly once; external effects still need business idempotency or an outbox.

## Close settlement timeout

`Messaging(settlement_timeout=...)` limits how long the caller waits, not the protected cleanup itself. Timeout raises `MessagingSettlementTimeout` while accepted commits, cancellation tails, source closure, and backend settlement continue. A later `aclose()` waits for that same task.

Next: [Redis, custom codecs, and backends](backends-and-codecs.md).
