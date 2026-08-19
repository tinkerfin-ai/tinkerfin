# tinkerfin-messaging

## What it is

`tinkerfin-messaging` turns a single-use asynchronous object source into an independent
producer with ordered persistence, replay, cancellation, and optional SSE delivery.
It accepts TinkerFin native streams, TinkerFin AG-UI streams, and custom sources with
an explicit codec.

Messaging does not create or invoke a graph. Its base package remains protocol-neutral;
optional integrations add AG-UI, native TinkerFin, and Redis support.

## Installation

Python 3.11 or newer is required. Install only the integrations used by the host:

```bash
pip install tinkerfin-messaging
pip install "tinkerfin-messaging[agui]"
pip install "tinkerfin-messaging[native]"
pip install "tinkerfin-messaging[redis]"
pip install "tinkerfin-messaging[agui,native,redis]"
```

- `agui` provides `AgUiCodec`.
- `native` provides `NativeStreamPartCodec` and uses the canonical native model from
  `tinkerfin`.
- `redis` provides `RedisBackend`.

## Quick Start

Create `Messaging` and its name-only channel for the application lifetime. Create one
new Runtime source per HTTP request:

```python
import asyncio
from functools import partial

from starlette.responses import StreamingResponse
from tinkerfin import TinkerFin
from tinkerfin_messaging import Messaging

tinkerfin = TinkerFin()
agent = tinkerfin.create_deep_agent(
    model=model,
    tools=tools,
    backend=agent_backend,
    store=store,
)

async with Messaging(backend=messaging_backend) as messaging:
    channel = messaging.channel(name="agent-events")

    runtime = await asyncio.to_thread(
        partial(
            agent.new_agui,
            run_input=agent_input,
        )
    )
    events = runtime.astream(
        graph_input,
        config={"configurable": {"thread_id": agent_input.thread_id}},
    )
    body = await channel.sse(
        events,
        stream=agent_input.thread_id,
        run=agent_input.run_id,
        after=lambda: parse_last_event_id(request),
        attach_identity=agent_input,
    )

    response = StreamingResponse(
        body,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
```

`channel.sse()` completes durable start-or-attach preflight before returning the body.
Its result is already suitable as `StreamingResponse` content; there is no second SSE
wrapper.

## Core concepts

### Channel names and reuse

```python
agui_channel = messaging.channel(name="agent-events")
native_channel = messaging.channel(name="native-parts")
```

`name` is a stable durable protocol namespace, not a display label. Together with
`stream`, it scopes the ordered log; by itself it binds every stream under that name to
one codec family. Use a different name for native, AG-UI, or different custom formats.

A channel handle is application-scoped and concurrently reusable across requests,
streams, and runs. Each supplied source remains single-use. Separate workers may
construct the same name; the shared backend enforces codec agreement atomically at
run preparation and again before every append.

Closing `Messaging` through its context manager or `aclose()` closes owned producers
and invalidates all handles created from that facade. The default
`settlement_timeout=None` waits until preflight work and producer settlement finish.
A finite non-negative timeout limits only each caller's wait: expiry raises
`MessagingSettlementTimeout`, keeps the shared close task running, and lets a later
`aclose()` continue waiting for that same task. It never cancels accepted cancellation
tails, pending commits, source closure, or backend `finish()`. An injected backend
remains subject to its own ownership contract.

### Built-in inference and custom codecs

Name-only channels inspect immutable source metadata before iteration:

| Source | Codec | Durable SSE shape |
| --- | --- | --- |
| `AgUiEventStream` | `agui.event.v1` | `id: <seq>` and AG-UI JSON `data` |
| `NativeGraphRunStream` | `langgraph.stream-part.v2.v1` | `id: <seq>`, `event: stream-part`, versioned native JSON `data` |

Inference requires immutable `messaging_codec_profile`, `messaging_source_type`, and
`messaging_replay_type` metadata. Messaging verifies all three against the built-in
codec before backend preparation and never consumes the first item, so empty streams
still work. Missing or contradictory types raise `SourceProfileMismatch`; mixing
complete profiles under one channel raises `CodecMismatch`. Passing a pre-encoded
Runtime `SseBody` is rejected.

The native codec accepts live `Mapping[str, object]` envelopes and decodes committed
bytes as `NativeStreamPart`. It supports `messages`, `tasks`, `values`, `updates`,
`checkpoints`, `debug`, and `custom`. A generic `GraphRunStream[PartT]` deliberately
has no built-in profile, regardless of whether it came from `TinkerFin`.

For name-only native inference, bind the optional strict source:

```python
from tinkerfin import AgUiNativeStreamConfig

native = tinkerfin.run(
    AgUiNativeStreamConfig(extra_modes=("custom",)).bind(
        graph.astream,
        graph_input,
        config=graph_config,
    )
).astream()
```

Custom sources provide a codec explicitly. A separate renderer is optional when the
codec already implements `SseRenderer`:

```python
channel = messaging.channel(
    name="custom-events",
    codec=CustomCodec(),
    renderer=CustomSseRenderer(),
)
```

`MessageCodec[SourceT, ReplayT]` validates and encodes source values, decodes durable
bytes, and exposes a stable `codec_id`. A channel without an SSE-capable renderer can
still use asynchronous replay; `.sse()` raises `SseRenderingUnsupported`.

### Source adapters

Use `FiniteMessageSource` for a fixed in-memory sequence, `map_source()` to transform
an existing source, and `DeferredMessageSource` when source construction should happen
only for the producer owner:

```python
from tinkerfin_messaging import (
    DeferredMessageSource,
    FiniteMessageSource,
    MessageSourceBinding,
    map_source,
)

finite = FiniteMessageSource.from_events((started_event, finished_event))


async def add_metadata(event: Event) -> Event:
    return enrich(event)


mapped = map_source(agent_events, add_metadata)


async def open_events():
    events = await create_agent_events()
    return MessageSourceBinding(source=events)


deferred = DeferredMessageSource(open_events, cancellable=True)
```

All adapters are single-use and close idempotently. `map_source()` accepts a
synchronous or asynchronous transform, waits for each result before pulling the next
source item, applies the same transform to the complete cancellation tail, and forwards
`aclose()` to its upstream source exactly once. A failed tail transform returns no
partial tail. Iteration
does not close the upstream source independently because the owner may need to settle
a concurrent cancellation callback first. The adapter deliberately returns an
unprofiled source because a transform can invalidate the original codec type; pass it
to a channel with an explicit codec.

`DeferredMessageSource` invokes its asynchronous opener exactly once on the first
producer pull. A run attachment or replay closes it without opening the underlying
source. Opening, cancellation, and ordinary iteration share the same binding, so a
remote cancellation that arrives during opening waits for that source and then invokes
its callback. When `MessageSourceBinding.cancel` is absent, the deferred source derives
the callback from the opened source. An explicit binding callback remains authoritative.
The deferred source declares that callback to Messaging; callers omit the `cancel=`
argument. Supplying a different callback is rejected before durable ownership is
claimed; the same callback, including its pre-map equivalent, is reused.
Natural exhaustion retains the binding until the owner calls `aclose()`, so an accepted
cancellation cannot lose its callback race. Deferred sources are also unprofiled and
therefore require an explicit channel codec.

Set `cancel_after_first_item=True` when the first item establishes a protocol lifecycle
that cancellation must not overtake, such as AG-UI `RUN_STARTED`. The default remains
`False` for sources whose first pull may need the cancellation callback to unblock it.

### High-level and low-level delivery

The high-level path returns the subscription's SSE iterator:

```python
body = await channel.sse(
    source,
    stream=stream_id,
    run=run_id,
    after=lambda: parse_last_event_id(request),
    attach_identity=identity,
    cancel=cancel_callback,
    on_committed=committed_callback,
)
```

The equivalent low-level path exposes decoded committed messages:

```python
subscription = await channel.wrap(
    source,
    stream=stream_id,
    run=run_id,
    after=after,
    attach_identity=identity,
    cancel=cancel_callback,
    on_committed=committed_callback,
)

async for message in subscription:
    print(message.envelope.seq, message.data)

# Or render the same subscription:
body = subscription.sse()
```

Committed values can also be consumed without starting or attaching a producer:

```python
latest = await channel.latest_seq(stream=stream_id)
page = await channel.read(stream=stream_id, after=after, limit=1000)
subscription = await channel.follow(stream=stream_id, run=run_id, after=after)
```

`read()` returns an ascending tuple of `DecodedMessage` values after the exclusive
cursor. `follow()` asynchronously binds the current durable stream generation before
returning a closeable run-bounded `MessageSubscription`; a delete/recreate race cannot
retarget that subscription to the new generation. It preserves completion,
cancellation, producer failure, stream deletion, and backpressure. The
Channel validates every envelope codec before decoding. Reading requires an explicit
codec or a built-in profile already inferred in the current process.

The high-level `sse()` method accepts a concrete `int`, `None`, or a zero-argument
synchronous callback returning `int | None`. It invokes the callback exactly once
before durable preparation. Resolver failures close the unclaimed source and propagate
before `sse()` returns. `validate_cursor()`, `wrap()`, and `wrap_recoverable()` accept
only a concrete `int | None`; backend preparation performs the authoritative atomic
cursor validation.

### Identity and replay

The application defines producer concurrency with `(channel.name, stream)`. Within one
stream:

- one active run owns the producer;
- the same run and attachment identity attach to it;
- a changed identity raises `RunIdentityConflict`;
- a different run while active raises `RunAlreadyActive`;
- a completed run is replay-only and never executes again.

`attach_identity` accepts finite JSON or a Pydantic model. Only a domain-separated
SHA-256 digest is stored. Include every request fact that can change output.

`after` is an exclusive durable cursor:

- `None` starts at the current tail and follows new commits;
- `0` replays from the retained beginning;
- `N` replays records with `seq > N`;
- negative and beyond-tail values raise `InvalidCursor`.

The callback form has the same cursor semantics and is intended for request-local
lookups such as `Last-Event-ID`; it is synchronous and must not perform blocking I/O.

### Post-commit observation

`wrap()`, `sse()`, and `wrap_recoverable()` accept an asynchronous `on_committed`
callback. The owning producer invokes it with the complete `MessageEnvelope` after a
successful durable append. Replay-only attachments never invoke it. Idempotent
recovery appends may notify the same envelope again, so observers must use its
`channel`, `stream`, `seq`, and `message_id` as idempotency facts.

Observer failures are logged without rolling back the committed message or changing
the run outcome. Messaging waits for each observer before requesting the next source
item, preserving bounded backpressure and notification order. Subscribers can already
consume the committed record while the observer is awaiting because backend
publication precedes the callback. A durable consumer and replay cursor remain the
authoritative recovery path; this hook is a best-effort wake-up signal.

Durable SSE uses the committed decimal sequence as `id`, so it maps directly to
`Last-Event-ID`. Closing a subscription detaches only that subscriber; the producer
continues and awaits each append for backpressure.

### Stream deletion

Delete all messages, run records, checkpoints, leases, and deduplication data for an
inactive stream through its channel:

```python
await channel.delete_stream(stream=thread_id)
```

Deleting a missing or previously deleted stream succeeds. An active producer raises
`StreamDeleteConflict`; deletion never requests cancellation implicitly. Settle or
cancel the run first, then delete the stream. Existing subscriptions and low-level
handles for the deleted generation raise `StreamDeleted`, including after another run
recreates the same stream name. A recreated stream starts at sequence `1` under a new
generation. The channel's codec binding and every other stream remain unchanged.

### Backends

`MemoryBackend` implements the complete contract in one event-loop process.
`RedisBackend` uses Redis Streams, Lua transactions, leases, and monotonically
increasing fencing tokens for multi-worker ownership.

Codec metadata belongs to the channel name, while ordering and run state belong to
each stream. Redis keeps every channel key in one cluster hash slot and stores separate
channel metadata, generation-fenced stream metadata, message logs, run hashes, leases,
message deduplication hashes, and a stream-scoped lifecycle signal log. The signal log
is retained across stream generations, uses a monotonic cursor from the control hash,
and is trimmed exactly to its latest 256 entries. Signals only wake waiters; an atomic
Lua snapshot remains authoritative for generation, run status, terminal `end_seq`,
producer failure, lease expiry, and each bounded replay page.

Idle `follow()`, `wait_for_cancel()`, and `wait_finished()` calls block on message and
lifecycle streams with `XREAD`. The block duration is bounded by the current producer
lease, a five-second progress fallback, and a safety budget below the Redis client's
positive `socket_timeout`. `poll_interval` controls only delete-lease contention.
Every concurrently blocked waiter occupies one Redis connection, so the injected
client pool must have capacity for peak subscribers plus ordinary commands. Cancelling
a waiter cancels its blocking read and releases that connection.

Deletion first fences the generation and writes a durable wake signal, then clears its
private keys in leased `UNLINK` batches; another worker can take over after a delete
lease expires. The control tombstone and bounded signal log remain available to reject
old handles and wake blocked followers. The backend does not impose message retention
or compaction. The host must use an exclusive key prefix and call `delete_stream()`
according to its retention policy. An injected Redis client is borrowed and must use
`decode_responses=False`.

Redis data under the configured prefix must use Messaging schema version `3`. Runtime
startup does not migrate incompatible development data; clear a development prefix
before using it with this package version.

### Cancellation and failures

Register a synchronous or asynchronous callback with `wrap()` or `sse()`, or use a
source that declares `messaging_cancel_callback`, then request remote cancellation:

```python
cancelled = await channel.cancel(stream="thread-1", run="run-1")
```

The callback accepts no arguments or one `CancelContext`. It must stop the active
source and may return a finite cancellation tail. A TinkerFin AG-UI stream supplies
`events.abort`, whose tail contains the unique `RUN_ERROR(code="cancelled")`. An
explicit callback and a source-owned callback are mutually exclusive.

The backend orders cancellation against settlement atomically and invokes the owner
callback at most once. Runs without a callback raise `CancellationUnsupported`.
Callback, encoding, append, producer, and ownership failures drain the committed
prefix before subscribers receive `RunProducerFailed`.

### Recoverable sources

`wrap_recoverable()` rebuilds a source from its last committed
`RecoveryCheckpoint`. Stable caller-supplied message IDs make retried commits
idempotent; they do not make models, tools, database writes, or other external effects
exactly once. External effects still need business idempotency keys or an outbox.

## Documentation

- [Messaging guide](https://github.com/tinkerfin-ai/tinkerfin/blob/main/docs/en/messaging/index.md)
- [Runtime guide](https://github.com/tinkerfin-ai/tinkerfin/blob/main/docs/en/runtime/index.md)
- [Complete documentation](https://github.com/tinkerfin-ai/tinkerfin/blob/main/docs/en/README.md)

## License

Apache License 2.0. See the
[repository license](https://github.com/tinkerfin-ai/tinkerfin/blob/main/LICENSE).
