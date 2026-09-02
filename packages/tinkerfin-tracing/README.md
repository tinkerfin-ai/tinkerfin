# TinkerFin Tracing

`tinkerfin-tracing` records TinkerFin Runtime lifecycle, provider and Tool call
boundaries, and validated Native semantic facts in one replayable Ledger. It provides a
bounded in-memory Store, directly filtered call entries, fixed-as-of
messages/reasoning/tree/state/interaction views, live semantic deltas, incremental core
checkpoints, deterministic business Projection extensions, and borrowed-Engine SQLite
and MySQL persistence.

It does not record AG-UI events, Messaging settlement, SSE frames, or Redis ownership.

## Installation

```bash
pip install tinkerfin-tracing

# Durable SQLite or MySQL persistence
pip install "tinkerfin-tracing[sqlite]"
pip install "tinkerfin-tracing[mysql]"
```

The Runtime example below also uses the core Runtime and LangChain's OpenAI adapter:

```bash
pip install tinkerfin langchain-openai
```

Tracing itself remains provider-neutral and does not depend on either package.

## Quick Start

```python
from tinkerfin import RunIdentity, TinkerFin
from tinkerfin_tracing import TraceEntryKind, TraceFilter, Tracer

tracer = Tracer()
tinkerfin = TinkerFin().observe(tracer)
definition = tinkerfin.create_deep_agent(
    model="openai:gpt-5.4",
    tools=[],
)

stream = await tinkerfin.open_run(
    RunIdentity(threadId="thread-1", runId="run-1"),
    agent=definition,
    input=graph_input,
)
async for _part in stream:
    pass

thread = await tracer.get("thread-1")
print(thread.messages)
print(thread.reasoning)
print(thread.tree.roots)
print(thread.state.root)
print(thread.status.execution)
print(thread.summary.pending_interactions)

calls = await tracer.query(
    "thread-1",
    where=TraceFilter(
        kinds={
            TraceEntryKind.MODEL,
            TraceEntryKind.PROVIDER,
            TraceEntryKind.TOOL,
        }
    ),
)
print(calls.turns, calls.items)
```

Graph model entries describe the actual Agent step. Provider entries retain the final
middleware-processed System and user messages, provider settings, usage, response
metadata, and time to first output. Tool proposals remain distinct from actual
post-approval Tool executions: `parent_id` retains the execution tree and `proposal_id`
retains their semantic association. `TraceTurn` resolves its HumanMessage from the
existing message fact rather than storing another payload. Native streams remain the
authority for messages, Todo, Plan, HITL, state, checkpoints, and subagents.

Messages, reasoning, nodes, and interactions carry the first authoritative `trace_seq`
that created each entity. Use that sequence—not timestamps or IDs—when merging different
view kinds. Interactions retain the canonical Native `source_id` for public resume ID
derivation and position-aligned `tool_call_ids` proven from the checkpoint message.
Consumers must not re-correlate same-name or parallel review actions by list order.

The default `InMemoryTraceStore` is process-local and is lost when the process exits.

For durable persistence, construct one host-owned asynchronous Engine and lend it to the
Store:

```python
from sqlalchemy.ext.asyncio import create_async_engine

from tinkerfin_tracing import SqlAlchemyTraceStore, Tracer

engine = create_async_engine("mysql+asyncmy://user:password@db/tinkerfin")
store = SqlAlchemyTraceStore(engine, namespace="my-application")
await store.setup()
tracer = Tracer(store=store)

# Configure TinkerFin with `.observe(tracer)`, then consume or close every managed stream.

await engine.dispose()  # The host, not the Store, owns this operation.
```

`setup()` creates and reflects exactly six framework-owned tables. It is idempotent and
safe for concurrent first starts. The same setup runs lazily before the first Store
operation, but an application should call it during startup so an invalid or stale
Schema fails before accepting Agent traffic. Trace tables have no foreign keys or
Schema-version fields and can share a database with host-owned tables.

For another shared storage system, implement the five-operation
`TraceLedgerBackend` and lend it to `DurableTraceStore`:

```python
from my_trace_storage import MyTraceLedgerBackend
from tinkerfin_tracing import DurableTraceStore, Tracer

backend = MyTraceLedgerBackend(database_client)
store = DurableTraceStore(backend, namespace="my-application")
await store.setup()
tracer = Tracer(store=store)

# The host remains responsible for closing database_client.
```

The required operations are `prepare_storage()`, `commit_ledger_change()`,
`load_ledger_state()`, `read_event_page()`, and
`load_projection_checkpoint()`. The framework owns `TraceWriter`, semantic state
transitions, canonical validation, heartbeats, and following. A shared Backend must
provide atomic conditional changes and storage-clock lease checks; ordinary CRUD or
Blob storage cannot provide this contract. Backend authors can run
`verify_trace_ledger_backend(primary, peer)` against two independent clients.
Backends that support directly filtered entries additionally implement
`TraceQueryBackend`; backends that can atomically reconstruct those derived entries
implement `TraceEntryRebuildBackend`. These optional capabilities do not change the
five-operation Ledger contract.

## Query semantics

`Tracer.query()` resolves the selected Run lineage, applies kind, status, parent,
Agent, middleware, Skill, provider, model, namespace, time, and text filters in the
Store, and returns a stable cursor, server-computed Facets, and optional ancestors.
Each Facet applies every active filter except its own dimension, so a caller can switch
kind or status directly without clearing that filter first. Failure ownership comes
from the Ledger fact and therefore remains stable across filters and pagination.
`TraceQuery.follow()` refreshes that same filtered page after indexed commits and
emits Turn and entry upserts/removals through a `TraceFollow` handle. The handle owns
each Store pull and waits for its upstream follower to close before cancellation escapes,
including repeated caller cancellation. `TraceFollow` is an asynchronous context
manager; use that scope when iteration may stop early, or call `aclose()` explicitly.
Entering a closed handle or starting another pull raises `TraceFollowLifecycleError`
with code `tracing.follow_lifecycle`.
It never loads the complete Ledger into the caller or browser to perform filtering.

The SQL entry table contains only searchable identity, relationship, status, time, and
Ledger-sequence columns. Request, result, message, and state payloads remain solely in
the Ledger and are decoded through the configured `CanonicalTracePayloadCodec` only for
selected rows. `await tracer.rebuild_entries(thread_id)` atomically reconstructs the
disposable entries from the current Ledger generation without rewriting authoritative
events.

`Tracer.get()` fixes one global `as_of_seq`. The default window contains the latest 100
complete Turns. Messages remain chronological; top-level Turn nodes are latest-first;
state and summary cover the full selected lineage. Multiple branch heads
require an explicit `head_run_id`.
Requesting a Run that has not entered the current generation raises
`TraceRunNotFound`; it is not reported as stored-fact corruption.

`TraceThread.summary` is the complete cumulative value for the selected lineage at the
fixed as-of boundary. Its status, completeness, message and Tool counts, pending
interactions, and maximum source `last_occurred_at` do not shrink with the visible Turn
window. Every `TraceUpdate.summary` is the complete value after that update. Existing
flat status and count accessors are computed from this one summary.

`Tracer()` captures every observed Tool's complete sanitized arguments, results, and
public review description. `TraceNode.input` and `TraceNode.result` expose those retained
values, while their omission flags distinguish policy or size omission from JSON null.
Per-Tool overrides describe whether the Tool remains fully visible, metadata-only,
selected-content, or absent from Trace:

```python
from tinkerfin_tracing import (
    CapturePolicy,
    MiddlewareTraceCapture,
    ToolTraceCapture,
)

CapturePolicy.public_history(
    tool_overrides={
        "execute": ToolTraceCapture.metadata_only(),
        "private_audit": ToolTraceCapture.disabled(),
        "web_search": ToolTraceCapture.selected_content(
            argument_paths=("/query",),
            result_paths=("/answer", "/sources"),
        ),
    },
    middleware_overrides={
        InternalMetricsMiddleware: MiddlewareTraceCapture.disabled(),
        PromptCacheMiddleware: MiddlewareTraceCapture.configuration_only(),
    },
)
```

Middleware types select every instance of that implementation; an exact public name can
override one named instance. `visible()` is the default and retains only lifecycles that
standard callbacks can prove. Configuration-only wrap hooks never receive fabricated
timing. A type selector also covers framework-injected middleware whose first evidence is
a standard callback class name. Exact failures alone carry `failure`; interrupted work
waits, cancelled work is cancelled, and unmatched terminal work is abandoned instead of
remaining `running` or inheriting a Run-level error. New Tools require no second Trace registry. `CapturePolicy.public_safe(tool_rules=...)`
remains the explicit metadata-only and RFC 6901 selection boundary for hosts that do not
want the public-history default. Every mode still applies credential and provider
reasoning sanitization, event-size limits, and explicit omission semantics.

`TraceThread.history_cursor` expands the same fixed prefix through
`Tracer.get(history_cursor=...)`, even when newer events have committed.
`TraceThread.events()` uses a separate opaque generation-aware event cursor. `follow()`
starts after the handle's original as-of and emits selected-lineage entity
upserts/removals. `delete()` accepts only the exact inactive generation held by that
handle.

Store snapshots contain generation, as-of, byte, and active-writer metadata rather than
the Ledger prefix. Events are available only through bounded forward or reverse reads.
The framework-owned core Projection advances with committed batches; custom Projections
cache validated Run-scoped state on query, so a business Projection failure never
terminates the Agent writer.

`TraceWritePolicy` controls asynchronous batch size, delay, and backpressure. Its default
pending budget is 64 MiB. Runtime force, terminal, and close boundaries wait for every
accepted transaction and checkpoint; caller cancellation never cancels the owned commit.

`SqlAlchemyTraceStore` uses a database-clock lease and monotonic fence for each Run
writer. MySQL transactions use `READ COMMITTED` row locks; SQLite writes use
`BEGIN IMMEDIATE` and a bounded, cancellation-responsive busy retry. Fixed reads use one
repeatable snapshot. An expired incomplete writer remains visible as a missing tail and
keeps its terminal reserve until takeover, close, or explicit generation deletion.

By default, event facts and Projection state are stored as opaque canonical UTF-8 JSON
bytes. A codec may apply a reversible storage transform to those bytes. Their SHA-256
digest is calculated from canonical JSON before that transform and checked on reads and
unknown-commit retries. The package does not provide S3/Blob archiving, payload
encryption or KMS integration, or OpenTelemetry exporters. Active shared storage
implements `TraceLedgerBackend`; complete `TraceStore` replacement remains an advanced
extension. Encryption wraps the canonical payload codec without changing its
pre-transform digest; telemetry consumes `RuntimeObserver` or decorates Store/Messaging
Backend operations. No placeholder interface represents an unavailable capability.

## Safety

The default `CapturePolicy.public_history()` preserves public user, assistant, and Tool
content while removing exact or vendor-prefixed credential fields, Runtime private
state, and the verified provider-private reasoning path. A same-named business field
elsewhere remains intact. Run and Runtime task payloads retain public structural
metadata. `ToolTraceCapture.metadata_only()` preserves Tool lifecycle without content;
`disabled()` suppresses Tool and result-message facts; `selected_content()` retains only
the named RFC 6901 paths. Oversized safe values use an explicit omitted disposition, and
non-finite numbers are rejected before serialization.
An interaction that cannot fit its required reconstruction payload fails observation
before the Runtime publishes an unrecoverable pause; ordinary optional payloads retain
their explicit omission signal.

Provider reasoning has an independent retention gate. Runtime must first receive a
verified `ReasoningExtractor`; Tracing then defaults to
`ReasoningCapturePolicy.omitted()`. Use
`Tracer(reasoning_capture_policy=ReasoningCapturePolicy.content())` only when the host
explicitly authorizes bounded reasoning retention. Omitted reasoning stores no content
or digest. A business field named `reasoning_content` outside reserved provider metadata
is unaffected.

Observer and Store failures terminate the Agent Run fail-closed. Requested business
Projection failures leave the Ledger unchanged and do not terminate the Agent Run.

## Documentation

- [Semantic tracing](https://github.com/tinkerfin-ai/tinkerfin/blob/main/docs/en/tracing/index.md)
- [API reference](https://github.com/tinkerfin-ai/tinkerfin/blob/main/docs/en/tracing/api-reference.md)

## License

Apache License 2.0. See
[LICENSE](https://github.com/tinkerfin-ai/tinkerfin/blob/main/LICENSE).
