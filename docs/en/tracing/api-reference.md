# Tracing API reference

[Semantic tracing](index.md) · [中文](../../zh/tracing/api-reference.md)

## `Tracer`

```python
Tracer(
    store=None,
    capture_policy=None,
    reasoning_capture_policy=None,
    limits=None,
    write_policy=None,
    projections=(),
)
```

| Member | Meaning |
| --- | --- |
| `store` | Borrowed `TraceStore`; defaults to a new `InMemoryTraceStore` |
| `open_run(context)` | `RuntimeObserver` entry used by TinkerFin Runtime |
| `get(thread_id, head_run_id=None, limit=100, history_cursor=None, projections=())` | Latest or cursor-bound fixed-as-of `TraceThread` |

Passing both `store` and `limits` requires exact equality with `store.limits`. Projection
names are canonical, unique, and registered immutably at construction.

## Capture and limits

| API | Purpose |
| --- | --- |
| `CapturePolicy.public_history(tool_overrides=None)` | Default policy; retain complete sanitized content for every Tool with optional exact-name overrides |
| `CapturePolicy.public_safe(tool_rules=())` | Metadata-only Tool policy with optional low-level RFC 6901 selections |
| `ToolTraceCapture.full_content()` | Retain Tool lifecycle, complete sanitized content, and public review text |
| `ToolTraceCapture.metadata_only()` | Retain Tool lifecycle without arguments, results, or review text |
| `ToolTraceCapture.selected_content(...)` | Retain Tool lifecycle and selected argument/result paths |
| `ToolTraceCapture.disabled()` | Suppress Tool lifecycle and result-message facts |
| `ReasoningCapturePolicy.omitted()` | Default reasoning policy; retain no content or digest |
| `ReasoningCapturePolicy.content()` | Explicitly retain bounded content received from a configured Runtime extractor |
| `ToolCaptureRule(toolName=..., argumentPaths=..., resultPaths=..., includeReviewDescription=False)` | Allow selected RFC 6901 values and an optional review description for one Tool |
| `CapturedValue` | Explicit `inline` or `omitted` capture envelope |
| `TraceLimits` | Immutable event, thread, Tracer, reserve, and follow limits |
| `TraceWritePolicy` | Immutable batch, 64 MiB pending, backpressure, and delay policy |

Default `TraceLimits`:

| Field | Default |
| --- | ---: |
| `maxEventBytes` | 1 MiB |
| `maxThreadEvents` | 100,000 |
| `maxThreadBytes` | 256 MiB |
| `maxTracerThreads` | 10,000 |
| `maxTracerBytes` | 1 GiB |
| `terminalReserveEventsPerRun` | 4 |
| `terminalReserveBytesPerRun` | 4 MiB |
| `followBatchSize` | 256 |

Regular appends cannot consume active Runs' terminal reserves. Mandatory Store appends
accept only `run.terminal` and `run.closed` facts. The byte reserve must cover
`terminalReserveEventsPerRun * maxEventBytes`, so every reserved event can reach its
declared maximum. A session that already failed remains an incomplete Trace; reserve
capacity does not synthesize facts after Observer ownership is lost.
An interaction payload that cannot fit its reconstruction boundary fails observation
before a durable pause is published; it is never silently reduced to a blind approval.

## `TraceThread`

| Member | Meaning |
| --- | --- |
| `key` | Namespace, thread ID, and non-reusable generation |
| `as_of_seq` | Immutable global Ledger prefix |
| `head_run_id` | Selected current branch head |
| `available_heads` | Every selectable head at this prefix |
| `messages` | Chronological `TraceMessage` values in the loaded Turn window |
| `reasoning` | Explicitly extracted `TraceReasoning` values in the loaded Turn window |
| `tree` | Flat authoritative `TraceTree`; `roots` and `children(id)` are convenience views |
| `state` | Complete root and collision-safe subgraph states for the selected lineage |
| `interactions` | Approval, clarification, review, and input interactions in the window |
| `summary` | Complete cumulative status, completeness, counts, pending interactions, and maximum source time for the selected lineage |
| `status` | `running`, `waiting`, `succeeded`, `failed`, `cancelled`, `abandoned`, or `unknown` |
| `completeness` | Independent missing-prefix, missing-tail, and payload-omitted signals |
| `message_count` / `tool_call_count` | Compatibility views computed from `summary` |
| `projections` | Defensive copies of explicitly requested business results |
| `has_older` | Whether earlier complete Turns can be loaded |
| `history_cursor` | Opaque generation/head/as-of cursor for another `Tracer.get(...)` history request |
| `load_older(limit=100)` | Expand history without changing as-of or follow start |
| `events(cursor=None, limit=100)` | Ascending selected-lineage event page |
| `follow()` | Live `TraceUpdate` iterator after the original as-of |
| `delete()` | Delete this exact inactive Store generation |

`TraceUpdate` contains committed selected-lineage events and facts, entity
upserts/removals for messages, reasoning, nodes, and interactions, the current complete
state, and `summary`. `update.summary` is the cumulative value after the update, not a
delta. Flat status, completeness, and count fields are computed from that same value.

`TraceSummary.pending_interactions` and `last_occurred_at` cover the complete selected
lineage at the fixed as-of boundary, independent of the loaded Turn window. Pending
items are ordered by their first `trace_seq`; resolved or cancelled items are removed.

Every `TraceMessage`, `TraceReasoning`, `TraceNode`, and `TraceInteraction` exposes
`trace_seq`, the first authoritative Ledger sequence that created that entity. Consumers
must use it when merging different entity kinds; timestamps and IDs are not ordering
substitutes. `TraceInteraction.source_id` preserves the canonical Native interrupt ID so
protocol clients can derive their documented per-action public IDs without an AG-UI copy.
For Tool review, `tool_call_ids` preserves the exact checkpoint correlation in action
position order. Consumers must not rebuild that relation from Tool names or node order.

`TraceNode.input` and `result` contain only policy-retained values. `inputOmitted` and
`resultOmitted` distinguish omission from JSON null. `Tracer()` uses
`CapturePolicy.public_history()`, so newly observed Tools automatically retain complete
sanitized content and public review descriptions. Exact-name `toolOverrides` can select
`fullContent`, `metadataOnly`, `selectedContent`, or `disabled`. The low-level
`ToolCaptureRule` and empty RFC 6901 root path remain available through
`CapturePolicy.public_safe()` for explicit selected-path retention.

## Semantic facts

`TraceSemanticFact` is a strict discriminated union:

| Fact | Stable meaning |
| --- | --- |
| `TurnFact` | New ordinary or branch user task with authoritative user message ID |
| `RunFact` | Start, input/resume, resume checkpoint, Observer failure, terminal, or close |
| `MessageFact` | Message start/content/completion/reconciliation/removal |
| `ReasoningFact` | Explicit extractor content/reconciliation/completion under an independent capture policy |
| `ToolFact` | Tool start, argument snapshot, proposal end, or result |
| `RuntimeTaskFact` | LangGraph task start/result, interrupt IDs, and structural payload metadata |
| `StateRevisionFact` | Changed non-message state keys in one exact namespace |
| `InteractionFact` | Pending to resolved/cancelled human interaction |
| `SubagentFact` | Validated non-root graph scope start and parent-task completion |
| `PlanRevisionFact` | Public `tinkerfin_plan` revision and status |
| `NativeExtraFact` | Structural metadata for an allowed extra Native mode |

`TraceEvent` adds a Store event ID, global `traceSeq`, generation, and measured persisted
bytes. There are no project-owned schema or protocol version fields.

## Business Projections

```python
from pydantic import BaseModel

from tinkerfin_tracing import TraceSemanticFact


class State(BaseModel):
    count: int = 0


class Result(BaseModel):
    count: int


class CountTools:
    name = "count_tools"
    state_type = State
    result_type = Result

    def initial_state(self) -> State:
        return State()

    def apply(self, state: State, fact: TraceSemanticFact) -> State:
        return State(count=state.count + int(fact.kind == "tool"))

    def finish(self, state: State) -> Result:
        return Result(count=state.count)
```

Register it with `Tracer(projections=(CountTools(),))`, then request it explicitly with
`tracer.get(..., projections=("count_tools",))`. Every state transition and result is
validated through the declared Pydantic type. A requested Projection failure raises
`TraceProjectionFailed`, leaves the Ledger unchanged, and does not terminate the Agent
Run. Validated state is cached per Run and as-of; a child Run derives from the nearest
ancestor checkpoint instead of refolding its prefix. Projection code must be
deterministic and side-effect free.

## Store contracts

`TraceStore` exposes namespace, limits, writer creation, metadata-only generation
snapshots, bounded forward/reverse event reads, Projection checkpoint load/CAS, follow,
and deletion. `TraceWriter` binds exactly one Run in one exact generation and supports
atomic ordered fact batches plus idempotent close. `StoreThreadSnapshot` contains
`asOfSeq`, bytes, and `StoreWriterSnapshot` metadata, never the Ledger prefix.

`InMemoryTraceStore` assigns one contiguous per-thread sequence across concurrent Runs,
rejects duplicate or active Run IDs, reserves terminal capacity per active writer,
prevents active deletion, wakes followers on deletion, and returns defensive copies.

### `TraceLedgerBackend` and `DurableTraceStore`

`DurableTraceStore(backend, namespace="default", limits=None, options=None, codec=None)`
provides the complete Store and writer lifecycle over a borrowed Backend. A Backend
implements exactly these storage operations:

| Operation | Required result |
| --- | --- |
| `prepare_storage()` | Idempotently prepare and validate owned storage structures |
| `commit_ledger_change(change)` | Atomically resolve and apply one framework-owned Ledger change |
| `load_ledger_state(request)` | Return consistent namespace, thread, writer, and storage-clock state |
| `read_event_page(request)` | Return one bounded exact-generation raw event page |
| `load_projection_checkpoint(request)` | Return the newest raw checkpoint at or below one prefix |

`commit_ledger_change()` obtains current state inside its transaction or conditional
write loop, calls `resolve_ledger_change()`, and applies the returned storage effect as
one unit. The resolver may be called again after an optimistic conflict and performs no
I/O. A Backend must use storage time for lease guards and must prove an uncertain commit
before returning or raising a Store error. Backend clients are borrowed and are never
closed by `DurableTraceStore`.

`TraceStoreOptions` configures `writer_lease_seconds`,
`writer_heartbeat_interval_seconds`, `follow_poll_seconds`,
`commit_retry_attempts`, and `commit_retry_delay_seconds`.

`verify_trace_ledger_backend(primary_backend, peer_backend)` runs the shared ordering,
replay, checkpoint, follow, and deletion contract against two independent clients.
Provider-specific transaction interruption and storage-clock failures still require
Backend fault-injection tests.

### `SqlAlchemyTraceStore`

```python
SqlAlchemyTraceStore(
    engine,
    namespace="default",
    limits=None,
    options=None,
    codec=None,
)
```

The Store borrows a SQLite or MySQL asynchronous Engine. `setup()` creates and reflects
the exact current Trace Schema and never disposes the Engine. It accepts the same
`TraceStoreOptions` used by `DurableTraceStore`.

| Extra | Engine URL |
| --- | --- |
| `tinkerfin-tracing[sqlite]` | `sqlite+aiosqlite:///...` |
| `tinkerfin-tracing[mysql]` | `mysql+asyncmy://...` |

MySQL writers use `READ COMMITTED` and row locks; fixed reads use `REPEATABLE READ`.
SQLite writers use `BEGIN IMMEDIATE`, temporarily disable the driver's blocking busy
wait for Store-owned bounded retries, and restore the borrowed connection setting
before returning it to the pool. Both dialects allocate one contiguous per-generation
sequence, enforce terminal reserve, keep expired incomplete ownership as missing-tail
evidence, support fenced takeover, and poll cross-instance follow in bounded pages.

Projection checkpoint CAS is strictly forward-only, except that repeating the exact
current prefix and canonical state proves an unknown commit. A different state at the
same identity and prefix raises `TraceStoreProtocolError`.

### Canonical payloads and SQL Schema

`CanonicalTracePayloadCodec` encodes sorted-key, whitespace-free, finite UTF-8 JSON.
`EncodedTracePayload.digest` is SHA-256 of those bytes before storage. SQL event and
Projection payloads are opaque bytes; searchable Run, fact kind, timestamp, sequence,
and digest metadata is checked against decoded content.

`get_trace_store_schema(dialect="sqlite" | "mysql")` returns deterministic empty-
database DDL compiled from the same metadata used by `setup()`. Trace owns exactly the
namespace, thread, writer, event, and Projection-checkpoint tables. They contain no
foreign keys or project-owned Schema-version fields.

## Errors

All tracing-owned failures inherit `TracingError` and expose a stable `tracing.*` code.
The public family includes invalid cursor, thread not found, ambiguous head, Run
not found, Run conflict, corruption, Store unavailable/timeout/protocol error, Projection checkpoint
conflict, quota exceeded, capture rejected, Observer failed, and Projection failed. Public `context` is client-safe;
trusted diagnostics and causes are separate.
