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
from tinkerfin import RunIdentity, TinkerFin
from tinkerfin_messaging import Messaging, create_agui_run_source


tinkerfin = TinkerFin()
agent = tinkerfin.create_deep_agent(model=model, tools=tools)
identity = RunIdentity(threadId="thread-1", runId="run-1")
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
        on_source_ready=activate_business_run,
        on_delivery_not_started=cleanup_business_run,
    )

    async for frame in body:
        await send(frame)
```

This example requires the `agui` extra. Messaging selects owner or attachment before
opening the model, Sandbox, or Graph. The helper supplies the exact same `RunIdentity`
object to `open_events` and verifies the opened stream. Do not repeat thread or run
parameters, and do not pre-encode the stream with Runtime `to_sse()`.

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

## Publish while a run is active

```python
from ag_ui.core import CustomEvent

await channel.publish(
    CustomEvent(name="report.progress", value={"completed": 3, "total": 10}),
    identity=identity,
    message_id="report-progress-3",
)
```

The message enters the same durable log as source events. Existing subscribers receive it, and reconnecting subscribers replay it using the same sequence cursor. Publication does not open a source, acquire or renew its lease, or advance its message ordinal or recovery checkpoint. The host authorizes the target identity.

The codec must be bound, including on a separate worker. Use `codec=AgUiCodec()` when that worker does not open the source. Ordinary codecs accept publication after the first source commit. AG-UI accepts only `CustomEvent` after the target main `RUN_STARTED`; the main terminal atomically closes publication. Child run events do not open or close the main run. Cancellation, settlement, or producer ownership loss reject new publications with `PublicationRejected`.

Omitting `message_id` generates a fresh key per call. Repeating the same key and content returns the retained envelope, including after completion; different content raises `MessageIdConflict`. A rejected publication never starts a new run.

Codecs with protocol lifecycles can implement `MessagePublicationPolicy`: `validate_publication()` checks external values, while `starts_publication()` and `ends_publication()` identify the target run boundaries. These decisions are committed atomically with source messages; storage backends remain protocol-neutral.

## Durable values

`MessageEnvelope` contains channel, nested RunIdentity, sequence, message ID, codec,
payload bytes, and UTC creation time.

`RecoveryCheckpoint` contains an opaque source position plus the last stable message ID.

## Managed AG-UI and advanced sources

Use `create_agui_run_source()` for the common TinkerFin AG-UI path. It hides Binding,
codec profile, source types, transforms, and the cancellation fence while retaining lazy
owner-only Agent creation. Its optional transform may add product metadata or change
content, but must preserve the concrete event type and all Run, message, Tool, activity,
reasoning, snapshot, and interrupt correlation identities. A `RUN_STARTED` event's
optional `RunAgentInput` is the caller's complete authoritative input and cannot be
changed. Use the advanced unprofiled `map_source()` boundary when the output protocol
itself must change.

`on_source_ready` belongs to the channel and activates host delivery only for a new
owner after the request-owned source is open and ready. `on_delivery_not_started` cleans
up only when readiness was never reached and no attachment was established. Attachments
invoke neither callback. Source-owned `on_owner_preflight` remains a separate advanced
hook that runs before a deferred source is opened.

An attachment does not open the unused candidate source, but Messaging does close that
single-use candidate before returning the attached subscription. Callers must not reuse
it after `wrap()` or `sse()` returns.

The lower-level source classes remain available to custom protocol authors.

Use `DeferredMessageSource` when only the durable owner should build an expensive Graph or Sandbox. Use `ProfiledDeferredMessageSource` when the codec and RunIdentity must be visible before opening.

Set `on_owner_preflight` only when a custom source must prepare its own state after
durable owner selection but before the producer task or opener starts. Host delivery
activation belongs in the channel's `on_source_ready` callback.

Messaging renews distributed ownership from acquisition through source preparation,
ready callbacks, source streaming, and settlement. Source factories do not manage leases.
Cancelling preparation interrupts the pending factory and waits for owned cleanup;
lease loss prevents further source commits.

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

Custom storage implements `MessagingBackend`. Messaging owns the producer, follow,
cancellation, settlement, retention, and cleanup lifecycle; a Backend supplies immutable
`messaging_settings` plus six asynchronous storage operations:

| Operation | Storage responsibility |
| --- | --- |
| `prepare_messaging_storage()` | Idempotently create or validate the current storage shape |
| `commit_messaging_transition()` | Atomically commit one framework-defined transition |
| `load_messaging_state()` | Load one bounded storage-clock-consistent state snapshot |
| `read_committed_messages()` | Read an ascending exact-generation page |
| `wait_for_messaging_change()` | Wait cancellation-responsively for a possible message or control change |
| `purge_stream_generation()` | Remove one bounded batch from an already sealed generation |

Transactional implementations call `resolve_messaging_transition()` inside their atomic
boundary and apply the returned `MessagingStorageEffect`. Use
`tinkerfin_messaging.testing.verify_messaging_backend()` against an empty isolated
namespace before shipping an implementation. The complete atomicity, cancellation,
resource ownership, and cleanup requirements are documented in
[Backends and codecs](https://github.com/tinkerfin-ai/tinkerfin/blob/main/docs/en/messaging/backends-and-codecs.md).

A cleanup begin result may include an opaque, bounded-lifetime `cleanup_token`.
Messaging never inspects or persists it and passes it unchanged, serially, to purge and
finish for the same generation. Backends that return a token must let its external lease
expire so another cleanup attempt can resume after cancellation or process loss.

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

`MessagingLimits` is immutable and enforced by both built-in backends before mutation.
Defaults are 16 MiB per encoded message, 1 MiB per checkpoint position, 100,000 messages
and 1 GiB of payload per thread generation, plus **1 GiB and 100,000 records in total**.
The total scope is one `MemoryBackend` instance or one Redis `key_prefix`, including all
channels. Set `max_total_bytes` and `max_total_records` to change these totals.

Total bytes include encoded payloads and retained checkpoint positions and message IDs
as UTF-8. Each message retains its checkpoint evidence, and each Run also retains its
latest checkpoint; replacing that latest checkpoint charges only the size difference.
Total records count each channel, thread, live generation, Run, message, and generation
tombstone once. These logical limits do not measure allocator or Redis key overhead.

`MessagingQuotaExceeded.resource` identifies the exhausted limit. An idempotent retry
of an existing message is not charged again. Exhaustion allows cancellation, settlement,
and deletion to complete. Cleanup releases message and Run capacity and converts the
generation record to its tombstone. Channel, thread, and tombstone records remain
counted so empty Runs and repeated generations cannot grow without a bound.

Retention is disabled by default. With `MessagingRetentionPolicy.expire_after(...)`,
new Run and message writes reclaim a bounded batch of other threads' expired generations
through the shared expiry index. No background task is required, and active or unexpired
data is never evicted to make room. `delete_stream()` resets the thread counters while
retaining its charged identity and tombstone records.

Redis workers sharing a prefix must use the same total limits; workers sharing a channel
must also agree on all other limits and retention. All channels under the prefix occupy
one Redis Cluster hash slot so admissions and counters commit atomically. A mismatch
fails before admission. These settings and counters never enter `MessageEnvelope`.

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

`subscription.aclose()` detaches one subscriber without stopping its producer. It
cancels and settles any active pull before closing backend resources. The waiting
consumer receives `CancelledError`; repeated close calls await the same cleanup.

A cancel callback accepts zero arguments or one `CancelContext(channel, identity)`. It may return a finite terminal tail. TinkerFin AG-UI streams already own their cancellation callback.

`Messaging(settlement_timeout=...)` limits only the caller's wait. Protected producer, commit, callback, and close tasks remain owned and can be awaited again with `aclose()`.

`cancel()` participates in the Messaging preflight lifecycle. Shutdown signals current
producers before waiting for cancellation preflights, then settles producers and closes
the borrowed backend boundary without leaving a control task to outlive the facade.
Closing mapped and subscribed sources retains the underlying close task across caller
cancellation.

`read()` and `follow()` reject a cursor greater than the current generation tail with
`InvalidCursor`; they never reinterpret it as an empty page or a future wait.

Use `parse_sse_event_id()` for canonical non-negative decimal `Last-Event-ID` values.
Use `is_active_run_status()`, `is_final_run_status()`, and `is_failed_run_status()` when
branching on the durable status returned by Messaging; these pure TypeGuards do no I/O.

## Documentation

- [Messaging basics](https://github.com/tinkerfin-ai/tinkerfin/blob/main/docs/en/messaging/index.md)
- [Delivery and replay](https://github.com/tinkerfin-ai/tinkerfin/blob/main/docs/en/messaging/delivery-and-replay.md)
- [Backends and codecs](https://github.com/tinkerfin-ai/tinkerfin/blob/main/docs/en/messaging/backends-and-codecs.md)

## License

Apache License 2.0. See the
[repository license](https://github.com/tinkerfin-ai/tinkerfin/blob/main/LICENSE).
