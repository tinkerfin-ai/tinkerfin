# Semantic tracing

[Documentation](../README.md) · [中文](../../zh/tracing/index.md)

`tinkerfin-tracing` records user-facing execution history from complementary
authoritative sources:

```text
TinkerFin Runtime lifecycle
+ LangChain provider and Tool callbacks
+ validated LangGraph Native messages/tasks/values
```

It does not record AG-UI events, Messaging commits or settlement, SSE frames, Redis
leases, or delivery ownership. Those signals describe presentation and transport rather
than what the Agent did.

## Installation

```bash
pip install tinkerfin tinkerfin-tracing

# Select one durable SQL dialect when needed.
pip install "tinkerfin-tracing[sqlite]"
pip install "tinkerfin-tracing[mysql]"
```

`tinkerfin-tracing` itself depends only on `tinkerfin-contracts` and Pydantic. Importing
it does not load Deep Agents, LangGraph, AG-UI, Messaging, SQL, Redis, Sandbox, or a host
application. SQLAlchemy and its drivers remain optional extras and load only when a SQL
symbol is requested.

## Record a Runtime

```python
from tinkerfin import RunIdentity, TinkerFin
from tinkerfin_tracing import Tracer


tracer = Tracer()
tinkerfin = TinkerFin().observe(tracer)
agent = tinkerfin.create_deep_agent(
    model="openai:gpt-5.4",
    tools=[],
)

stream = await tinkerfin.open_run(
    RunIdentity(threadId="thread-1", runId="run-1"),
    agent=agent,
    input=graph_input,
)
async for part in stream:
    consume(part)

thread = await tracer.get("thread-1")
print(thread.messages)
print(thread.tree.roots)
print(thread.state.root)
print(thread.interactions)
print(thread.status.execution)
print(thread.summary.pending_interactions)
```

`.observe(...)` returns a separate configured `TinkerFin` factory. Each request opens a
request-scoped Trace session. The callback plane records the final middleware-processed
model request before provider execution, first output, usage, failures, cancellation,
and actual post-approval Tool execution. Runtime validates every Native part before
Trace; that Native plane remains authoritative for messages, Todo, Plan, HITL, state,
checkpoints, and subagents. Accepted observations are forced at call start, resume,
interrupt, terminal, and close boundaries. A Trace write failure terminates the Agent
Run fail-closed.

## Filter call entries in the Store

```python
from tinkerfin_tracing import TraceEntryKind, TraceFilter

query = await tracer.query(
    "thread-1",
    where=TraceFilter(
        kinds={
            TraceEntryKind.MODEL,
            TraceEntryKind.PROVIDER,
            TraceEntryKind.TOOL,
            TraceEntryKind.SKILL,
        },
        include_ancestors=True,
    ),
    limit=100,
)
print(query.turns, query.items, query.facets, query.next_cursor)
```

The Store applies kind, status, parent, Agent, middleware, Skill, provider, model,
namespace, time, and text filters before returning rows. `TraceQuery.follow()` tracks
the same filtered page, including its cursor. The derived SQL entry table contains no request, result,
message, or state payload; selected details are loaded by Ledger sequence and decoded
through the configured codec. `await tracer.rebuild_entries(thread_id)` reconstructs
those disposable entries without rewriting Ledger events.

Each returned entry has one Turn owner. `parent_id` describes only the verified call
tree; actual Tool executions use a separate `proposal_id` to refer to their model Tool
proposal. User messages are resolved from existing message facts into `query.turns`.
Live followers publish Turn and entry changes together, including before a provider
produces output.

## Read one fixed execution slice

`await tracer.get(thread_id)` fixes an immutable global `as_of_seq`. The default history
window contains the latest 100 complete Turns:

- `messages`, `tree`, and `interactions` use the Turn window;
- `state` and `summary` cover the complete selected lineage at the fixed
  as-of sequence;
- `await thread.load_older(limit=100)` expands only the historical window;
- messages remain chronological while top-level Turn nodes are latest-first.

A Turn starts with an ordinary or branch user input. Resume, approval, clarification,
and Plan review create another Run segment in the same Turn. A new branch with new user
input creates a new Turn.

`thread.summary` contains cumulative status, completeness, message and Tool counts,
pending interactions, and the maximum source `last_occurred_at`. These values do not
depend on the visible Turn window. Every live `update.summary` is the complete cumulative
value after that update, not a delta.

If a thread has multiple branch heads, `get()` raises `AmbiguousTraceHead` and exposes
the selectable Run IDs. Select one explicitly:

```python
thread = await tracer.get("thread-1", head_run_id="run-branch-a")
```

## Page and follow

```python
page = await thread.events(limit=100)
while page.next_cursor is not None:
    page = await thread.events(cursor=page.next_cursor, limit=100)

updates = thread.follow()
async with updates:
    async for update in updates:
        apply_message_delta(update.messages)
        apply_node_delta(update.nodes)
```

Event cursors are opaque and bind namespace, thread, generation, selected head, fixed
page as-of, and last global sequence. Concurrent appends do not enter an existing page
session. `follow()` starts after the handle's original as-of and yields semantic entity
upserts/removals. Cancellation, exhaustion, or the context manager closes its wait;
consumers that do not use `async with` must call `aclose()` after an early break.

`thread.delete()` deletes only the inactive generation named by that handle. Active
writers prevent deletion; a cursor or handle from a deleted generation cannot address a
recreated thread with the same ID.

## Public history and safety

`Tracer()` uses `CapturePolicy.public_history()`. It keeps public user, assistant, and
complete Tool content without requiring a second Tool-name registry, while enforcing
independent safety rules:

- provider-private `additional_kwargs.reasoning_content` is removed without deleting a
  same-named business field elsewhere;
- exact and vendor-prefixed credential fields are redacted;
- Runtime-declared private top-level state channels are removed from state, task, and
  extra-mode structural facts before tracing;
- Run and Runtime task payloads retain bounded structural metadata rather than repeated
  state or message bodies;
- each newly observed Tool retains complete sanitized arguments, results, and public
  review descriptions by default;
- middleware configuration and standard-callback lifecycles are visible by default;
  `configuration_only()` and `disabled()` can narrow individual implementations without
  changing middleware execution;
- `ToolTraceCapture.metadata_only()` retains lifecycle without content,
  `selected_content()` retains explicit RFC 6901 paths, and `disabled()` suppresses the
  Tool facts;
- oversized safe payloads carry an explicit omitted disposition instead of partial or
  silently truncated content.
- non-finite numbers are rejected before JSON serialization.

Hosts that require metadata-only Tool retention can select
`CapturePolicy.public_safe(tool_rules=...)`. Its low-level `ToolCaptureRule` preserves
the existing exact-name and JSON Pointer boundary without becoming a second Agent Tool
registry on the ordinary path.

`middleware_overrides` accepts an implementation type for every instance or an exact
public name for one named instance. Exact names take precedence. Wrap-only middleware is
configuration evidence because LangChain does not publish those hooks through standard
callbacks; Trace never fabricates execution or timing. Type selectors also apply when
framework-injected middleware first appears through its standard callback class name.

Only directly failed work owns `failure`. Interrupts remain waiting, cancellations remain
cancelled, and unmatched work at a terminal boundary becomes abandoned. Runtime terminal
settlement updates Native tasks, Tool proposals, Tool executions, subagents, and callback
steps so a completed Run cannot leave query entries running.

Provider reasoning requires two independent opt-ins. Configure a verified extractor on
`DeepAgentsV2RuntimeProfile`, then pass `ReasoningCapturePolicy.content()` to `Tracer`
only when retention is authorized. The default `ReasoningCapturePolicy.omitted()` stores
an explicit omission without content or digest. Enabling AG-UI reasoning events does not
authorize Trace persistence, and a business field named `reasoning_content` outside the
reserved provider metadata path remains ordinary business data.

Trace views return defensive copies. Changing a returned dictionary or event cannot
mutate the Ledger or later queries.

## Storage boundary

`Tracer()` uses `InMemoryTraceStore`. It is bounded, asynchronous, process-local, and
lost when the process exits.

`SqlAlchemyTraceStore` implements the same `TraceStore`/`TraceWriter` contract for
SQLite and MySQL 8.x. It borrows a host-owned `AsyncEngine`; it never changes pool size
or disposes the Engine. Call `await store.setup()` during application startup. Setup is
idempotent, coordinates concurrent first starts, creates only Trace-owned tables, and
reflects their current columns, types, nullability, primary keys, indexes, comments, and
absence of foreign keys before traffic is accepted.

```python
from sqlalchemy.ext.asyncio import create_async_engine

from tinkerfin_tracing import SqlAlchemyTraceStore, Tracer

engine = create_async_engine("mysql+asyncmy://user:password@db/tinkerfin")
store = SqlAlchemyTraceStore(engine, namespace="my-application")
await store.setup()
tracer = Tracer(store=store)
```

An unsupported shared database integrates through `TraceLedgerBackend`, not by
reimplementing `TraceStore` and `TraceWriter`:

```python
from my_trace_storage import MyTraceLedgerBackend
from tinkerfin_tracing import DurableTraceStore, Tracer

backend = MyTraceLedgerBackend(database_client)
store = DurableTraceStore(backend, namespace="my-application")
await store.setup()
tracer = Tracer(store=store)
```

The Backend implements exactly five storage operations: prepare storage, commit one
atomic Ledger change, load Ledger state, read one event page, and load one Projection
checkpoint. The framework owns writer lifecycle, terminal reserve, sequence allocation,
checkpoint decisions, canonical validation, follow polling, backpressure, and
cancellation. `verify_trace_ledger_backend()` checks the shared observable contract
using two independent Backend clients.

Direct entry filtering is an optional `TraceQueryBackend` capability. Atomic
reconstruction is a separate `TraceEntryRebuildBackend` capability, so archival or
telemetry integrations do not need to implement query storage. The ordinary
`TraceLedgerBackend` contract and custom payload codec remain unchanged.

SQL writers use database-clock leases and monotonically increasing fences. An expired
incomplete writer becomes `missing_tail`, keeps reserved terminal capacity, and can be
taken over; a completed Run cannot be taken over. SQLite lock waits enter a bounded,
cancellation-responsive Store retry, while MySQL retries lock timeout, deadlock, and
unknown commit outcomes using retained event IDs or checkpoint digests. Fixed reads use
one repeatable database snapshot.

Facts and Projection state use canonical finite UTF-8 JSON. Their SHA-256 digest covers
the canonical bytes before storage and is verified on read. Trace persistence
does not include S3/Blob archiving, encryption/KMS, or OpenTelemetry exporters. Active
shared storage implements `TraceLedgerBackend`; a complete `TraceStore` replacement is
the advanced extension boundary. Encryption wraps the canonical payload codec without
changing its pre-transform digest; telemetry observes `RuntimeObserver` or decorates
Store/Messaging Backend operations. Blob storage without atomic conditional changes is
an archive target, not an active Ledger.

Next: [Tracing API reference](api-reference.md).
