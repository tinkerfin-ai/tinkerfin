# Messaging usage reference

[Messaging basics](index.md) · [中文](../../zh/messaging/api-reference.md)

## Application entry points

| API | Parameters | Purpose |
| --- | --- | --- |
| `Messaging(...)` | `backend=None`, `settlement_timeout=None` | Create one application lifecycle |
| `Messaging.channel(...)` | name, optional codec and renderer | Create a reusable channel |
| `Messaging.aclose()` | none | Settle preflight, producers, and cleanup |

## Channel methods

| Method | Main parameters | Result |
| --- | --- | --- |
| `sse(...)` | source, optional Identity, after, cancel, on_committed | SSE byte iterator |
| `wrap(...)` | source, optional Identity, after, cancel, on_committed | `MessageSubscription` |
| `wrap_recoverable(...)` | recoverable source, optional Identity, after, cancel, on_committed | Recoverable subscription |
| `read(...)` | Identity, `after=0`, `limit=100` | Ascending finite page |
| `follow(...)` | Identity, `after=0` | Follow a run to its terminal state |
| `latest_seq(...)` | Identity | Current thread tail |
| `validate_cursor(...)` | Identity, after | Read-only cursor validation |
| `cancel(...)` | Identity | Request cancellation and wait for settlement |
| `delete_stream(...)` | Identity | Delete the current thread generation |

Identity is optional only when the source advertises an immutable profile.

## Subscription and Envelope

`MessageSubscription` yields `DecodedMessage` and supports `sse()` and `aclose()`.

| Envelope v2 field | Meaning |
| --- | --- |
| `schema_version` | Fixed at 2 |
| `channel` | Codec namespace |
| `identity` | Nested shared Identity |
| `seq` | One-based thread position |
| `message_id` | Stable message idempotency ID |
| `codec` | Persisted format ID |
| `payload` | Encoded bytes |
| `created_at` | Aware UTC timestamp |

## Source helpers

| API | Purpose |
| --- | --- |
| `MessageSource` | Single-use asynchronous source protocol |
| `CancellableMessageSource` | Source-owned cancellation callback |
| `ProfiledMessageSource` | Codec, Identity, live type, and replay type profile |
| `MessageSourceBinding` | Opened source and optional cancellation callback |
| `DeferredMessageSource` | Open an ordinary source only for the owner |
| `ProfiledDeferredMessageSource` | Deferred source whose profile is known before open |
| `FiniteMessageSource` | Adapt a finite iterable |
| `map_source(...)` | Ordered synchronous or asynchronous transform |
| `RecoverableSource` | Rebuild from a checkpoint |
| `RecoverableMessage` | Stable ID, data, and checkpoint |
| `RecoveryCheckpoint` | Opaque position; schema 1 |

## Codecs and backends

| API | Purpose |
| --- | --- |
| `AgUiCodec` | Base-install AG-UI codec and renderer |
| `NativeStreamPartCodec` | Base-install native v2 codec and renderer |
| `NativeStreamPart` | Native v2 replay value |
| `MemoryBackend` | In-process implementation |
| `RedisBackend` | `[redis]` multi-process implementation; persistent schema 5 |
| `MessagingBackend` | Custom backend protocol |

### Backend operations

| Operation | Contract |
| --- | --- |
| `prepare(...)` | Channel, Identity, codec, cursor, cancellation/recovery flags; returns `PreparedRun` |
| `append(...)` | Handle, message ID, codec, bytes, optional checkpoint; returns Envelope |
| `begin_settlement()` / `finish()` | Atomically claim and record finalization |
| `latest_seq()` / `read()` | Channel, Identity, and pagination |
| `bind_follow()` / `follow()` | Bind an authoritative generation and read it |
| cancellation methods | Request, wait, and retrieve failure |
| `lease_renew_interval` / `lease_timeout` / `renew(handle)` | Describe and renew producer lease ownership |
| `delete_stream(...)` | Delete an inactive thread generation |

`BackendRunHandle` contains channel, Identity, owner token, fence, and generation. `PreparedRun` adds the cursor, owner decision, checkpoint, and recovery flag.

## Callbacks

| API | Purpose |
| --- | --- |
| `CancelCallback` | Zero arguments or one `CancelContext`; may return a finite tail |
| `CancelContext` | Immutable channel and Identity |
| `CommittedCallback` | Receives owner commits after append |

## Common errors

| Error | Meaning |
| --- | --- |
| `MessagingNotStarted` / `MessagingClosed` | Lifecycle does not allow the operation |
| `MessagingSettlementTimeout` | Caller wait for protected cleanup expired |
| `InvalidCursor` | Cursor is invalid or beyond the tail |
| `CodecMismatch` | Channel codec differs |
| `SourceProfileMismatch` | Source profile is incomplete or contradictory |
| `MessageIdConflict` | One message ID maps to different content |
| `RunAlreadyActive` | Another run owns the thread |
| `RunNotFound` | Run does not exist |
| `RunProducerFailed` | Production, encoding, commit, or cancellation failed |
| `CancellationUnsupported` | No cancellation callback exists |
| `BackendOwnershipLost` | A stale producer lost ownership |
| `StreamDeleted` / `StreamDeleteConflict` | Generation is deleted or still active |

Messaging does not read or compare request bodies. Callers own body consistency for a reused runId.
