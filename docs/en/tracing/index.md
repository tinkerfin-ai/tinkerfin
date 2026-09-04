# Semantic tracing

[API reference](api-reference.md) · [中文](../../zh/tracing/index.md)

TinkerFin Tracing turns normalized Runtime observations into one append-only semantic
Ledger and one rebuildable execution Graph. The Ledger is authoritative; Graph rows and
projection checkpoints are derived indexes.

## Managed execution

```python
from tinkerfin import RunIdentity, TinkerFin
from tinkerfin_tracing import Tracer

tracer = Tracer()
tinkerfin = TinkerFin().observe(tracer)
agent = tinkerfin.create_deep_agent(model="openai:gpt-5.4", tools=[])

result = await tinkerfin.ainvoke(
    RunIdentity(threadId="thread-1", runId="run-1"),
    agent=agent,
    input=graph_input,
)
```

Managed invocation and streaming open Runtime observations before model output, so the
Run and HumanMessage are queryable even when the provider has not emitted a token.
Calling a compiled graph directly is an unmanaged advanced operation.

The stable v2 and experimental v3 Runtime Profiles normalize their upstream streams
into the same observations. Tracing facts, Graph queries, SQL, and applications do not
inspect upstream stream modes or versions.

## Ledger and Graph

The Graph represents each Turn as:

```text
HumanMessage
└── Run / Agent
    ├── Model
    │   ├── SystemMessage
    │   └── AssistantMessage
    └── Tool
        ├── Skill
        └── Subagent
```

The exact shape follows available evidence. Missing parents, model output IDs, Tool
proposals, or executions are reported as link issues; the framework does not correlate
parallel work by time or arrival order. Tool proposal, post-review execution, and result
are one logical Tool node. Rejected HITL actions do not fabricate executions.

SystemMessage, middleware, Run, and Runtime task nodes are technical and hidden by
default. ToolMessage results remain evidence on their logical Tool nodes. When technical
nodes are hidden, the framework reconnects each visible node to its nearest visible
ancestor before returning authoritative order and roots.

Graph filtering runs in the Store. Request, result, message, and state content remain in
the Ledger. Content search applies indexed structural constraints first and decodes only
a bounded candidate set through the configured Codec; no plaintext search document is
stored. SQL stores one row per node and Run revision so sibling branches remain isolated.
A removal writes a lineage-local tombstone instead of deleting an ancestor or sibling
node.

## History and live updates

`Tracer.get()` returns fixed-prefix messages, reasoning, state, interactions, summary,
event pages, and the canonical `TraceThread.graph` for a selected lineage. Its live
updates carry the same Graph through `TraceUpdate.graph`. Multiple heads require
`head_run_id`.

`Tracer.query()` returns the current canonical Graph. Its opaque pagination cursor is
valid only for the exact current tail and filter. `TraceGraphQuery.follow()` is available
only on the current first page and publishes complete order and roots with every delta.

Every follow handle owns and closes its upstream Store iterator. Normal completion,
failure, cancellation, repeated cancellation, and early consumer exit preserve the
same resource ownership and backpressure rules.

## Safety

The Tracer always:

- converts source values to finite standard JSON;
- removes exact and vendor-prefixed credential fields;
- removes only verified provider-private `additional_kwargs.reasoning_content` paths;
- removes Runtime-declared private top-level state channels;
- applies optional business Redactors;
- repeats mandatory credential and private-reasoning cleanup;
- applies retention and byte limits before creating a Fact.

A same-named business `reasoning_content` field outside the reserved provider path is
preserved. Business Redactors receive only detached JSON plus a functional
`RedactionContext`, never LangChain objects or execution identities. Any extension
failure rejects capture without storing the raw value.

Provider reasoning remains disabled by default and requires both a verified Runtime
extractor and explicit `ReasoningCapturePolicy.content()` authorization.

## Durable storage

`InMemoryTraceStore` is bounded and process-local. `SqlAlchemyTraceStore` supports
SQLite and MySQL through a borrowed asynchronous Engine. Call `setup()` before serving
traffic so stale or partial schemas fail early.

The Store owns six tables: namespace, thread, writer, Ledger event, projection
checkpoint, and Graph node. Hash search keys are 32-byte binary SHA-256 values. The
Graph table maintains one lineage index in addition to its primary key.

Custom storage implements `TraceLedgerBackend`; optional Graph query and rebuild
capabilities have separate protocols. Codecs can encrypt canonical bytes, and Runtime
observers or Backend decorators can export telemetry without changing semantic facts.

Archive/S3/Blob implementations and OpenTelemetry exporters are not provided. Advanced
integrations may implement `TraceLedgerBackend`, replace `TraceStore`, wrap the canonical
codec, observe `RuntimeObserver`, or decorate Store operations. Messaging Backend
extensions remain a separate delivery boundary and do not persist Trace facts.
