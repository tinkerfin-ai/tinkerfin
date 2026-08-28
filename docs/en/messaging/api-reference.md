# Messaging usage reference

[Messaging basics](index.md) · [中文](../../zh/messaging/api-reference.md)

## Application entry points

| API | Parameters | Purpose |
| --- | --- | --- |
| `Messaging(...)` | `backend=None`, `settlement_timeout=None` | Create one application lifecycle |
| `Messaging.channel(...)` | name, optional codec and renderer | Create a reusable channel |
| `Messaging.aclose()` | none | Settle preflight, producers, and cleanup |

The default backend is `MemoryBackend`.

## Channel methods

| Method | Main parameters | Result |
| --- | --- | --- |
| `sse(...)` | source, optional RunIdentity, after, cancel, on_committed | SSE byte iterator |
| `wrap(...)` | source, optional RunIdentity, after, cancel, on_committed | `MessageSubscription` |
| `wrap_recoverable(...)` | recoverable source, optional RunIdentity, after, cancel, on_committed | Recoverable subscription |
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
| `MessageSubscription` | Asynchronously iterate `DecodedMessage`; supports `sse()` and `aclose()` |
| `DecodedMessage` | Committed `envelope` plus codec-decoded `data` |
| `MessageEnvelope` | Immutable committed durable message |

### `MessageEnvelope` fields

| Envelope field | Meaning |
| --- | --- |
| `channel` | Codec namespace |
| `identity` | Nested shared RunIdentity |
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
| `MessageCodec` | Stable `codec_id` plus `encode()` and `decode()` |
| `SseRenderer` | `render(seq=..., payload=...) -> bytes` |
| `AgUiCodec` | `[agui]` AG-UI codec and renderer |
| `NativeStreamPartCodec` | `[native]` canonical Native replay codec and renderer |
| `NativeStreamPart` | `[native]` finite canonical Native replay value |
| `MemoryBackend` | In-process implementation |
| `RedisBackend` | `[redis]` multi-process implementation |
| `MessagingBackend` | Custom backend protocol |
| `MessagingRetentionPolicy` | Disabled or positive terminal replay deadline |

### Backend operations

| Operation | Contract |
| --- | --- |
| `prepare(...)` | Channel, RunIdentity, codec, cursor, cancellation/recovery flags; returns `PreparedRun` |
| `append(...)` | Handle, message ID, codec, bytes, optional checkpoint; returns Envelope |
| `begin_settlement()` / `finish()` | Atomically claim and record finalization |
| `get_run_status()` | Channel and RunIdentity; may atomically classify an expired lease as `owner_lost` |
| `latest_seq()` / `read()` | Channel, RunIdentity, and pagination |
| `bind_follow()` / `follow()` | Bind an authoritative generation and read it |
| cancellation methods | Request, wait, and retrieve failure |
| `lease_renew_interval` / `lease_timeout` / `renew(handle)` | Describe and renew producer lease ownership |
| `retention_policy` | Immutable terminal replay policy shared by backend workers |
| `delete_stream(...)` | Delete an inactive thread generation |

`BackendRunHandle` contains channel, RunIdentity, owner token, fence, and generation. `PreparedRun` adds the cursor, owner decision, checkpoint, and recovery flag.

## Callbacks

| API | Purpose |
| --- | --- |
| `CancelCallback` | Zero arguments or one `CancelContext`; may return a finite tail |
| `CancelContext` | Immutable channel and RunIdentity |
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
| `StreamExpired` | Terminal generation is outside its replay retention window |
| `StreamDeleted` / `StreamDeleteConflict` | Generation is deleted or still active |

Messaging does not read or compare request bodies. Callers own body consistency for a reused runId.
