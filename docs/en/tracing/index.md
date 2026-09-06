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
inspect upstream stream modes or versions. Profile identity remains framework integration
and recovery metadata; it is not a Graph field or an application query dimension.

## Ledger and Graph

Each Turn is a container for one user task, not a Graph node. Events in the Turn's
top-level scope are siblings. Only a Subagent owns a nested scope, whose events are again
siblings in real start order. A possible scope layout is:

```text
Turn
├── HumanMessage
├── Context / Memory / Guardrail / retrieval / custom / Plan / interaction
├── Model
├── Tool
├── Subagent
│   ├── HumanMessage
│   ├── Model / Tool / context events
│   └── AssistantMessage
└── AssistantMessage
```

The current kinds are `human_message`, `assistant_message`, `context`, `model`,
`tool`, `subagent`, `memory`, `guardrail`, `retrieval`, `custom`, `plan`, and
`interaction`. ToolMessage results remain evidence on their logical Tool event. A Tool
proposal, post-review execution, result, and failure are one event. A validated Deep
Agents `task` execution is exactly one Subagent event, with no separate Tool event.
Rejected HITL actions do not fabricate executions.

`parent_subagent_id` names the nearest owning Subagent and is the only display-nesting
relationship. `model_call_id` associates an AssistantMessage, Tool, or Subagent with the
Model call that emitted it; it does not make the Model a display parent. Missing exact
evidence is reported through link issues rather than inferred from time or arrival order.
A non-empty namespace alone does not establish a Subagent. Only validated Subagent
provenance marks a fact as inside that scope; ordinary LangGraph subgraphs remain in the
Turn's top-level scope.

Each provider attempt creates one Context event immediately before its Model event. The
Context starts at the preceding visible execution boundary in the same scope and ends at
the final provider request boundary. Resuming an active Subagent opens a new Context at
the resume input, excluding approval wait time. Its content is projected from the final
SystemMessage content already held by the Model request, so the Ledger does not store a
duplicate payload. Requests without a SystemMessage retain the measured Context interval
with no content. The duration is wall-clock preparation latency, not a CPU profile.

A proven Subagent's parent `task` Tool start establishes its preparation boundary.
The Subagent opens once even when a child Model or Tool callback precedes its first
Native part, using the already observed parent execution time.

Middleware execution is not a Trace event and generic chain or middleware callbacks are
not captured. Middleware may publish an explicit Memory, Guardrail, retrieval, or custom
event through `trace_contribution(...)`. There is no Skill kind or fact. A model reading
`SKILL.md` produces the ordinary `read_file` Tool event and no second event.

Both synchronous and asynchronous Tools can be observed without application wrappers.
Start observation completes before execution, and an observation failure prevents that
call from starting. Run cancellation cannot forcibly stop a synchronous function that
already began; its blocking I/O and resource lifetime must remain bounded.

The first HumanMessage inside a validated Subagent scope projects only the captured
`task.description`. The Subagent request retains the complete captured task arguments;
both views reference the same Ledger fact and do not duplicate stored content.

Within one scope, events use `started_seq`; equal sequences use the stable order user,
context, model, Tool, Subagent, assistant, then event ID. The projection generates the
public sequence with an iterative depth-first traversal, so an expanded Subagent is
immediately followed by its own sequence without relying on the Python call stack.

Graph filtering runs in the Store. Request, result, message, and state content remain in
the Ledger. Content search applies indexed metadata, namespace, and time constraints
first and decodes only a bounded candidate set through the configured Codec; no plaintext
search document is stored. SQL stores one row per node and Run revision so sibling
branches remain isolated. A removal writes a lineage-local tombstone without deleting an
event from another Run branch.

Filtering selects direct matches first. The Store then adds only their owning Subagent
chain through a bounded breadth-first lookup; those containers are excluded from
`matched_node_ids`. Subagent nesting is limited to 64 levels, `max_total_nodes` bounds the
complete page, and SQL Subagent and Ledger-locator reads use batches of 500 keys. One
Graph query may select at most 10,000 Runs in its lineage.

A Tool keeps its proven Model association while execution updates its timing and actual
input. The Graph retains the source fact for that relationship independently of request,
result, and lifecycle facts, including during streaming updates.

An unfinished Assistant without a completed Model result retains the safe content
actually received before Run settlement. Cancellation marks it `cancelled`, interruption
marks it `waiting`, and other missing results are `abandoned`. A proven completed Model
output stays `succeeded`; if no complete message snapshot was captured, its body is
explicitly omitted as `incomplete_message`, even when some fragments were received.
Complete message snapshots and independent approval waits retain their own state.
A terminal Run also closes any still-running Assistant whose Ledger contains no message
settlement; this supplies no missing content. Payload limits and explicit omission
apply to accumulated responses.

## History and live updates

Graph completeness reports Runs whose call history cannot be established. A proven
initialization failure retains its failed Run and input without inventing Model or Tool
calls, and does not make the call history incomplete. Untracked execution failures
still report missing call history, including when no call events were retained.

`Tracer.get()` returns fixed-prefix messages, reasoning, state, interactions, summary,
event pages, and the canonical `TraceThread.graph` for a selected lineage. Its live
updates carry the same Graph through `TraceUpdate.graph`. Multiple heads require
`head_run_id`.

`Tracer.query()` returns the current canonical Graph. Its opaque pagination cursor is
valid only for the exact current tail and filter. `TraceGraphQuery.follow()` is available
only on the current first page and publishes complete event order and direct matches with
every delta.

Following uses local notifications for the exact thread generation. Changes committed
through another Store instance are checked at `TraceStoreOptions.follow_poll_seconds`,
which defaults to 0.5 seconds. Each idle SQL check reads the generation, tail, and active
Run identities in one bounded query without reloading write-side quota aggregates.
Followers return the connection to the pool before waiting. Each MySQL read transaction
provides a consistent snapshot without changing the session's isolation or autocommit
setting.

`TraceThread.follow()` also reports writer close and lease expiry without new events:
the Run becomes `unknown` with `missing_tail=True`, retaining the same `as_of_seq`.
A valid takeover can restore `running` at that sequence. Neither transition creates an
Agent terminal or changes the independent Graph model.

The fixed view exposes `TraceThread.observed_at`, the storage UTC time of its event and
ownership read. Each `TraceUpdate` carries `generation`, `as_of_seq`, and `observed_at`.
Consumers compare `(as_of_seq, observed_at)` within one generation, preserve timestamp
precision, and read a fresh view when equal observations have conflicting contents.
History cursors retain the original observation while expanding the fixed event window.

Custom Stores return `TraceStoreUpdate` from `TraceStore.follow()`, including current
ownership in the first update even with empty events. Custom Ledger Backends return
`StoredTraceEventPage.active_run_ids` and storage-clock `observed_at` with the same
observed tail, excluding expired leases. Common `Tracer` calls need no extra arguments.

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
