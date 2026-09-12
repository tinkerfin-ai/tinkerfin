# Messaging usage reference

[Messaging basics](index.md) · [中文](../../cn/messaging/api-reference.md)

## Application entry points

| API | Parameters | Purpose |
| --- | --- | --- |
| `Messaging(...)` | `backend=None`, `settlement_timeout=None` | Create one application lifecycle |
| `Messaging.channel(...)` | name, optional codec and renderer | Create a reusable channel |
| `Messaging.aclose()` | none | Settle preflight, producers, and cleanup |
| `create_agui_run_source(...)` | profiled AG-UI source, optional transform | Transform event content or metadata while preserving protocol identity |

The default backend is `MemoryBackend`.

## Channel methods

| Method | Main parameters | Result |
| --- | --- | --- |
| `open_sse(...)` | source, optional identity, after, callbacks | Caller-owned closeable SSE byte iterator |
| `publish(...)` | message, identity, optional message_id | Persist a notification in the existing run; return MessageEnvelope |
| `wrap(...)` | source, optional identity, after, callbacks | `MessageSubscription` |
| `wrap_recoverable(...)` | recoverable source, optional identity, after, callbacks | Recoverable subscription |
| `read(...)` | RunIdentity, `after=0`, `limit=100` | Ascending finite page |
| `follow(...)` | RunIdentity, `after=0` | Follow a run to its terminal state |
| `get_run_status(...)` | RunIdentity | Current authoritative run status |
| `latest_seq(...)` | RunIdentity | Current thread tail |
| `validate_cursor(...)` | RunIdentity, after | Read-only cursor validation |
| `cancel(...)` | RunIdentity | Request cancellation and wait for settlement |
| `delete_stream(...)` | RunIdentity | Delete the current thread generation |

RunIdentity is optional only when the source advertises an immutable profile.

## Subscription and Envelope

| API | Purpose |
| --- | --- |
| `MessageSubscription` | Returned by channel `wrap()`/`follow()`; asynchronously iterates `DecodedMessage` and supports `to_sse()`/`aclose()`; do not construct directly |
| `DecodedMessage` | Committed `envelope` plus codec-decoded `data` |
| `MessageEnvelope` | Immutable committed durable message |

### `MessageEnvelope` fields

| Envelope field | Meaning |
| --- | --- |
| `channel` | Non-empty channel name |
| `identity` | Nested shared RunIdentity |
| `seq` | One-based thread position |
| `message_id` | Stable message idempotency ID |
| `codec` | Persisted format ID |
| `payload` | Encoded bytes |
| `created_at` | Aware UTC timestamp |

## Common helpers

| API | Purpose |
| --- | --- |
| `create_agui_run_source(...)` | Transform a lazy AG-UI source while preserving preparation, cancellation, and cleanup |
| `parse_sse_event_id(value)` | Parse `None` or canonical non-negative ASCII decimal SSE IDs |
| `is_active_run_status(status)` | TypeGuard for `running` and `cancel_requested` |
| `is_final_run_status(status)` | TypeGuard for all terminal durable statuses |
| `is_failed_run_status(status)` | TypeGuard for `failed` and `owner_lost` |

The status helpers are pure and perform no backend I/O.
The common AG-UI source transform can enrich product metadata or content only. It rejects
event-type substitutions and changes to Run, message, Tool, snapshot, or interrupt IDs.
It also preserves every field of an optional `RUN_STARTED.input`, including messages,
tools, context, forwarded props, and resume entries. Use advanced `map_source()` when
changing the output protocol is intentional.

Use a built Runtime and an application-owned channel to add product fields:

```python
from ag_ui.core import BaseEvent
from tinkerfin_messaging import create_agui_run_source


def add_label(event: BaseEvent) -> BaseEvent:
    return event.model_copy(update={"label": "Report"})


source = create_agui_run_source(
    runtime.open_agui_run(
        thread_id="conversation-1",
        run_id="run-1",
        messages=[{"id": "message-1", "role": "user", "content": "Summarize this report"}],
    ),
    transform_event=add_label,
)
body = await channel.open_sse(source, after=0)
```

The HTTP host sends `body` and calls `await body.aclose()` on completion or disconnect.
A failed adapter construction leaves source cleanup with the caller. After a cleanup
waiting limit is exceeded, call `aclose()` again before disposing borrowed resources.

## Advanced source helpers

| API | Purpose |
| --- | --- |
| `MessageSource` | Single-use asynchronous source protocol |
| `CancellableMessageSource` | Source-owned cancellation callback |
| `ProfiledMessageSource` | Codec, RunIdentity, live type, and replay type profile |
| `MessageCodecInputSource` | Optional source-owned conversion from one live item to the inferred codec's finite input |
| `MessageSourceBinding` | Opened source and optional cancellation callback |
| `DeferredMessageSource` | Open an ordinary source only for the owner; optional owner preflight runs before producer execution |
| `ProfiledDeferredMessageSource` | Deferred source whose profile is known before open, with the same owner-preflight fence |
| `FiniteMessageSource` | Adapt a finite iterable |
| `map_source(...)` | Ordered synchronous or asynchronous transform |
| `RecoverableSource` | Rebuild from a checkpoint |
| `RecoverableMessage` | Stable ID, data, and checkpoint |
| `RecoveryCheckpoint` | Opaque position and optional last message ID |

## Codecs and backends

| API | Purpose |
| --- | --- |
| `MessagePublicationPolicy` | Optional codec validation and main-run publication boundaries |
| `MessageCodec` | Stable `codec_id` plus `encode()` and `decode()` |
| `SseRenderer` | `render(seq=..., payload=...) -> bytes` |
| `AgUiCodec` | `[agui]` AG-UI codec and renderer |
| `NativeStreamPartCodec` | `[native]` canonical Native replay codec and renderer |
| `NativeStreamPart` | `[native]` finite canonical Native replay value |
| `MemoryBackend` | In-process implementation |
| `SqlAlchemyBackend(engine)` | `[sqlalchemy]` SQLite, MySQL and PostgreSQL; borrowed AsyncEngine |
| `RedisBackend` | `[redis]` multi-process implementation |
| `MessagingRetentionPolicy` | Disabled or positive terminal replay deadline |

Backend-author contracts are exported from `tinkerfin_messaging.backend_contract`:

| API | Purpose |
| --- | --- |
| `MessagingBackend` | Six-operation custom storage protocol |
| `MessagingBackendSettings` | Immutable limits, retention, lease, renewal, and wait settings |
| `MessagingTransition` | Framework-defined atomic lifecycle intent |
| `MessagingStateSnapshot` | Bounded storage-clock-consistent transition evidence |
| `MessagingStorageEffect` | Complete replacements produced by `resolve_messaging_transition()` |

### Backend operations

| Operation | Contract |
| --- | --- |
| `messaging_settings` | Return immutable settings shared by cooperating Backend instances |
| `prepare_messaging_storage()` | Idempotently create or validate the current storage shape |
| `commit_messaging_transition(...)` | Atomically commit a framework transition |
| `load_messaging_state(...)` | Load one bounded consistent state snapshot |
| `read_committed_messages(...)` | Read one ascending exact-generation message page |
| `wait_for_messaging_change(...)` | Wait cancellation-responsively for a possible message or control change |
| `purge_stream_generation(...)` | Idempotently remove one bounded batch from a sealed generation |

`resolve_messaging_transition()` contains the reusable storage-neutral state machine.
`tinkerfin_messaging.testing.verify_messaging_backend()` verifies supported behavior
against an isolated Backend factory.

Cleanup begin can return an opaque, expiring `cleanup_token`. Messaging passes it
unchanged and serially to bounded purge and finish calls for the returned generation;
it never interprets or persists the token.

## Callbacks

| API | Purpose |
| --- | --- |
| `CancelCallback` | Zero arguments or one `CancelContext`; may return a finite tail |
| `CancelContext` | Immutable channel and RunIdentity |
| `CommittedCallback` | Receives owner commits after append |
| `on_source_ready` | Owner-only async callback after source readiness and before producer creation |
| `on_delivery_not_started` | Async cleanup when readiness and attachment were both absent |

Attachments invoke neither delivery callback. `on_owner_preflight` belongs to the source
and runs before a deferred opener; `on_source_ready` runs after that opener completes.

Cancellation waits for a delivery callback that has already started to finish.
Use database and network timeouts inside these callbacks to bound that wait.

## Common errors

| Error | Meaning |
| --- | --- |
| `MessagingNotStarted` / `MessagingClosed` | Lifecycle does not allow the operation |
| `MessagingSettlementTimeout` | Caller wait for protected cleanup expired |
| `InvalidCursor` | Cursor is invalid or beyond the tail |
| `PublicationRejected` | Run or protocol does not accept a new external message |
| `CodecMismatch` | Channel codec differs |
| `SourceProfileMismatch` | Source profile is incomplete or contradictory |
| `MessageIdConflict` | One message ID maps to different content |
| `RunAlreadyActive` | Another run owns the thread |
| `RunNotFound` | Run does not exist |
| `RunProducerFailed` | Production, encoding, commit, or cancellation failed |
| `CancellationUnsupported` | No cancellation callback exists |
| `BackendOwnershipLost` | A stale producer lost ownership |
| `StreamExpired` | Terminal generation is outside its replay retention window |
| `StreamDeleted` / `StreamDeleteConflict` | Generation is deleted or still active |

Messaging does not read or compare request bodies. Callers own body consistency for a reused run_id.
