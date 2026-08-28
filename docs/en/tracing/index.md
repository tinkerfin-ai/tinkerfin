# Semantic tracing

[Documentation](../README.md) · [中文](../../zh/tracing/index.md)

`tinkerfin-tracing` records user-facing execution history from two authoritative
sources:

```text
TinkerFin Runtime lifecycle
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
agent = (
    TinkerFin()
    .observe(tracer)
    .create_deep_agent(
        model="openai:gpt-5.4",
        tools=[],
    )
)

runtime = agent.new(
    identity=RunIdentity(threadId="thread-1", runId="run-1"),
)
async for part in runtime.astream(graph_input):
    consume(part)

thread = await tracer.get("thread-1")
print(thread.messages)
print(thread.tree.roots)
print(thread.state.root)
print(thread.interactions)
print(thread.status.execution)
```

`.observe(...)` returns a separate configured `TinkerFin` factory. Each request opens a
request-scoped Trace session. Runtime validates every Native part before Trace, invokes
Trace before `on_part` and AG-UI conversion, and forces accepted observations at resume,
interrupt, terminal, and close boundaries. A Trace write failure terminates the Agent
Run fail-closed.

## Read one fixed execution slice

`await tracer.get(thread_id)` fixes an immutable global `as_of_seq`. The default history
window contains the latest 100 complete Turns:

- `messages`, `tree`, and `interactions` use the Turn window;
- `state`, `status`, and `completeness` cover the complete selected lineage at the fixed
  as-of sequence;
- `await thread.load_older(limit=100)` expands only the historical window;
- messages remain chronological while top-level Turn nodes are latest-first.

A Turn starts with an ordinary or branch user input. Resume, approval, clarification,
and Plan review create another Run segment in the same Turn. A new branch with new user
input creates a new Turn.

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

async for update in thread.follow():
    apply_message_delta(update.messages)
    apply_node_delta(update.nodes)
```

Event cursors are opaque and bind namespace, thread, generation, selected head, fixed
page as-of, and last global sequence. Concurrent appends do not enter an existing page
session. `follow()` starts after the handle's original as-of and yields semantic entity
upserts/removals. Cancelling or closing the iterator releases its wait.

`thread.delete()` deletes only the inactive generation named by that handle. Active
writers prevent deletion; a cursor or handle from a deleted generation cannot address a
recreated thread with the same ID.

## Public-safe capture

`CapturePolicy.public_safe()` keeps public user and assistant content while enforcing
independent safety rules:

- provider-private `additional_kwargs.reasoning_content` is removed without deleting a
  same-named business field elsewhere;
- exact and vendor-prefixed credential fields are redacted;
- Runtime-declared private top-level state channels are removed from state, task, and
  extra-mode structural facts before tracing;
- Run and Runtime task payloads retain bounded structural metadata rather than repeated
  state or message bodies;
- Tool arguments, results, and approval arguments are metadata-only unless a Tool name
  and JSON Pointer path are explicitly allowlisted;
- oversized safe payloads carry an explicit omitted disposition instead of partial or
  silently truncated content.
- non-finite numbers are rejected before JSON serialization.

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

SQL writers use database-clock leases and monotonically increasing fences. An expired
incomplete writer becomes `missing_tail`, keeps reserved terminal capacity, and can be
taken over; a completed Run cannot be taken over. SQLite lock waits enter a bounded,
cancellation-responsive Store retry, while MySQL retries lock timeout, deadlock, and
unknown commit outcomes using retained event IDs or checkpoint digests. Fixed reads use
one repeatable database snapshot.

Facts and Projection state use canonical finite UTF-8 JSON. Their SHA-256 digest covers
the canonical bytes before storage and is verified on read. Trace persistence
does not include S3/Blob archiving, encryption/KMS, or OpenTelemetry exporters. A
concrete archive or Blob integration implements or decorates `TraceStore`; encryption
wraps the canonical payload codec without changing its pre-transform digest; telemetry
observes `RuntimeObserver` or decorates Store/Messaging Backend operations. These active
boundaries are the extension contract, so tracing exposes no empty capability interface.

Next: [Tracing API reference](api-reference.md).
