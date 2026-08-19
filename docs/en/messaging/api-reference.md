# Messaging usage reference

[Messaging basics](index.md) · [中文](../../zh/messaging/api-reference.md)

## Application entry points

| API | Parameters | Use |
| --- | --- | --- |
| `Messaging(...)` | `backend=None`, `settlement_timeout=None` | Application-scoped Messaging facade |
| `Messaging.channel(...)` | `name`, optional `codec`, optional `renderer` | Concurrently reusable channel |
| `Messaging.aclose()` | none | Settle owned producers and cleanup |

The default backend is `MemoryBackend`.

## Channel methods

`MessageChannel` is the reusable handle returned by `Messaging.channel(...)`.

| Method | Main parameters | Result |
| --- | --- | --- |
| `sse(...)` | source, stream, run, cursor, identity, cancel, observer | SSE byte iterator |
| `wrap(...)` | Same identity controls; cursor is concrete | Decoded subscription |
| `wrap_recoverable(...)` | Recoverable source and run identity | Recoverable subscription |
| `read(...)` | stream, `after=0`, `limit=100` | Ascending history tuple |
| `follow(...)` | stream, run, `after=0` | Continuing subscription |
| `latest_seq(...)` | stream | Current tail sequence |
| `validate_cursor(...)` | stream, after | Cursor validation |
| `cancel(...)` | stream, run | Whether cancellation was requested |
| `delete_stream(...)` | stream | Delete an inactive stream |

## Subscription and messages

| API | Use |
| --- | --- |
| `MessageSubscription` | Iterates `DecodedMessage`; supports `sse()` and `aclose()` |
| `DecodedMessage` | Holds `envelope` and decoded `data` |
| `MessageEnvelope` | Durable identity, sequence, codec, byte payload, and UTC time |

### `MessageEnvelope` fields

| Field | Constraint or meaning |
| --- | --- |
| `schema_version` | Currently 1 |
| `channel` | Non-empty channel name |
| `stream` | Non-empty stream ID |
| `seq` | One-based stream position |
| `message_id` | Stable idempotency ID within the stream |
| `run` | Producing run |
| `codec` | Durable format ID |
| `payload` | Encoded bytes |
| `created_at` | UTC-aware time |

## Source utilities

| API | Use |
| --- | --- |
| `MessageSource` | Single-use async source protocol |
| `CancellableMessageSource` | Source declaring its cancellation callback |
| `ProfiledMessageSource` | Source declaring an immutable codec profile |
| `MessageSourceBinding` | Source and optional callback returned by a deferred opener |
| `DeferredMessageSource` | Creates the real source only for the owner |
| `FiniteMessageSource` | Wraps a finite iterable |
| `map_source(...)` | Applies a synchronous or asynchronous transform in order |
| `RecoverableSource` | Rebuilds a source from a checkpoint |
| `RecoverableMessage` | Stable ID, data, and checkpoint |
| `RecoveryCheckpoint` | Opaque `position` and optional `last_message_id` |

## Codecs, renderers, and backends

| API | Use |
| --- | --- |
| `MessageCodec` | `encode()`, `decode()`, and stable `codec_id` |
| `SseRenderer` | `render(seq=..., payload=...) -> bytes` |
| `MemoryBackend` | Complete one-process backend |
| `RedisBackend` | Redis multi-process backend |
| `MessagingBackend` | Custom persistence protocol |
| `BackendRunHandle` | Backend run ownership handle |
| `PreparedRun` | Handle, cursor, owner flag, checkpoint, and recovery state |
| `AgUiCodec` | Optional AG-UI codec and renderer |
| `NativeStreamPartCodec` | Optional native-v2 codec and renderer |
| `NativeStreamPart` | Decoded native-v2 value |

### Complete custom `MessagingBackend` operations

| Operation | Parameters and result |
| --- | --- |
| `prepare(...)` | Channel, stream, run, codec, identity, cursor, cancellable and recoverable flags; returns `PreparedRun` |
| `append(...)` | Owner handle, message ID, codec, byte payload, optional checkpoint; returns `MessageEnvelope` |
| `begin_settlement(handle)` | Atomically starts settlement and reports accepted cancellation |
| `finish(...)` | Handle, final status, optional error |
| `latest_seq(...)` / `read(...)` | Read the tail or one finite history page |
| `bind_follow(...)` / `follow(...)` | Bind the authoritative generation and continue reading |
| `request_cancel(handle)` | Persist a cancellation request |
| `wait_for_cancel(handle)` | Wait for cancellation or settlement |
| `wait_finished(handle)` | Wait for final run status |
| `failure(handle)` | Read the producer failure |
| `renew(handle)` | Renew the lease and report ownership |
| `delete_stream(...)` | Delete an inactive stream generation |

`BackendRunHandle` carries channel, stream, run, owner token, fence, and generation. `PreparedRun` carries the handle, validated cursor, `is_owner`, optional recovery checkpoint, and `recovered` flag.

## Callback types

| API | Use |
| --- | --- |
| `CancelCallback` | Takes no argument or `CancelContext`; may return a finite tail |
| `CancelContext` | Immutable channel, stream, and run |
| `CommittedCallback` | Receives a complete committed `MessageEnvelope` |

## Errors

| Error | Meaning |
| --- | --- |
| `MessagingError` | Base Messaging failure |
| `MessagingNotStarted` | Messaging has not entered its async lifetime |
| `MessagingClosed` | A closed facade was used |
| `MessagingSettlementTimeout` | Caller wait expired before protected cleanup settled |
| `InvalidCursor` | Negative or beyond-tail cursor |
| `CodecMismatch` | One channel name was used with different durable formats |
| `SourceProfileMismatch` | Source profile is incomplete or contradictory |
| `MessageIdConflict` | One stable message ID has different content |
| `RunAlreadyActive` | Another run owns the stream |
| `RunIdentityConflict` | The same run was attached with a different identity |
| `RunNotFound` | No such run exists |
| `RunProducerFailed` | Production, encoding, append, or cancellation failed |
| `CancellationUnsupported` | Run has no cancellation callback |
| `RecoveryUnsupported` | Source cannot recover after owner loss |
| `SseRenderingUnsupported` | Channel has no SSE renderer |
| `BackendOwnershipLost` | A stale producer lost ownership |
| `StreamDeleted` | Handle belongs to a deleted stream generation |
| `StreamDeleteConflict` | An active producer prevents deletion |
