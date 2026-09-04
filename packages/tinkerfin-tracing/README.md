# TinkerFin Tracing

TinkerFin Tracing records normalized Runtime observations in one replayable semantic
Ledger. It provides canonical execution graphs, fixed-prefix conversation history,
live updates, bounded in-memory storage, and durable SQLite or MySQL storage.

Tracing does not persist AG-UI frames, HTTP state, or Messaging delivery state.

## Installation

```bash
pip install tinkerfin-tracing

# Durable storage
pip install "tinkerfin-tracing[sqlite]"
pip install "tinkerfin-tracing[mysql]"
```

## Quick Start

```python
from tinkerfin import RunIdentity, TinkerFin
from tinkerfin_tracing import TraceGraphFilter, TraceGraphNodeKind, Tracer

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
async for _part in stream:
    pass

history = await tracer.get("thread-1")
print(history.messages)
print(history.state.root)
print(history.summary.status.execution)
print(history.graph.turns, history.graph.nodes)

graph = await tracer.query(
    "thread-1",
    where=TraceGraphFilter(
        kinds={TraceGraphNodeKind.MODEL, TraceGraphNodeKind.TOOL},
    ),
)
print(graph.turns, graph.nodes, graph.matched_node_ids)
```

`TinkerFin.ainvoke()` and managed stream entry points open the same observation
lifecycle. Calling a compiled graph directly remains an unmanaged advanced path and
does not automatically create a complete Trace.

## Canonical Graph

Each `TraceGraphTurn` is a container, not an execution node. Events in the Turn's
top-level scope are siblings in real start order. A Subagent is the only event that owns
a nested scope; events inside that scope are siblings under the nearest owning Subagent.

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

`parent_subagent_id` is the only display-nesting relationship. `model_call_id` links an
AssistantMessage, Tool, or Subagent to the Model call that emitted it without making the
Model a display parent. A non-empty graph `namespace` is queryable metadata, but does not
by itself prove a Subagent scope; the Ledger must carry validated Subagent provenance.

Every provider attempt has one `context` event immediately before its Model event. Its
wall-clock interval starts at the preceding visible execution boundary in the same scope
and ends when the final provider request starts. A resumed active Subagent starts a new
preparation interval at the resume input, so approval wait time is excluded. The Context
content projects the final SystemMessage content from that Model request; it does not
store a second payload. A request without a SystemMessage still has a timed Context event
with no content. This interval describes observable preparation latency, not CPU usage.

One logical Tool event combines proposal, post-review execution, ToolMessage result, and
failure. A validated Deep Agents `task` execution is represented by its Subagent event,
not by a second Tool event. Tracing has no Skill event: reading `SKILL.md` is an ordinary
`read_file` Tool execution. Generic chain and middleware callbacks are not captured;
middleware can publish an explicit semantic context event through
`trace_contribution(...)` when its product behavior needs to be visible.

The first HumanMessage inside a validated Subagent scope projects only the captured
`task.description`. The Subagent request retains the complete captured task arguments;
both views reference the same Ledger fact, so tracing does not store a second payload.

`TraceGraphFilter` supports `kinds`, `statuses`, `model_call_id`, `agent_names`,
`providers`, `models`, `namespaces`, `search`, `started_after`, and `started_before`.
`TraceGraphQuery.matched_node_ids` contains only the current page's direct matches. The
Store automatically adds their owning Subagent chain to `nodes`, but those containers do
not become matches.

Within each scope, events are ordered by `started_seq`, then by the stable product order
user, context, model, Tool, Subagent, assistant, and finally by `node_id`. The public
projection uses an iterative depth-first traversal so each expanded Subagent is followed
by its own event sequence. Store-side owning-Subagent expansion is a bounded
breadth-first lookup: it accepts at most 64 Subagent levels, obeys `max_total_nodes`, and
batches SQL keys in groups of 500.

`follow()` is available only on the current first page. Each `TraceGraphDelta` carries
Turn and node upserts/removals, complete `ordered_node_ids` and `matched_node_ids`, the
current `next_cursor`, `as_of_seq`, and completeness. A pagination cursor is bound to its
namespace, thread, generation, selected head, filter, and Ledger tail; a newer commit
replaces the live cursor and invalidates any previously issued cursor instead of silently
changing its page.

`TraceGraphQueryLimits` independently bounds direct matches, content-search candidates,
Subagent-expanded nodes, and serialized page/update bytes. One query may select at most
10,000 Runs in its lineage. Content search first applies indexed metadata, namespace, and
time predicates, then decodes the bounded Ledger locators through the Store's Codec and
matches only retained, already-redacted public details. When only response details exceed
the byte budget, the framework keeps the graph structure and marks content, request, or
result values as omitted. A structure-only page that still exceeds the budget fails with
`TraceQuotaExceeded`.

`Tracer.get()` exposes the same canonical nodes through `TraceThread.graph` at the
history handle's exact fixed prefix. `TraceThread.follow()` carries a
`TraceUpdate.graph` delta with the same ordering contract. Current-tail history
uses the disposable index; an older fixed prefix replays the same Graph reducer from the
Ledger instead of storing another execution tree or payload copy.

The SQL Graph index stores only identity, relationship, filter, lifecycle, and Ledger
sequence locators. Fact payloads remain solely in the Ledger and are decoded only for
selected rows or a bounded content-search candidate set. No plaintext search document
or second payload copy is stored. The index is disposable and can be rebuilt with
`await tracer.rebuild_graph(thread_id)`.

## Capture and business redaction

`CapturePolicy` decides whether content is retained, which Tool paths are selected, and
the applicable byte budget. It does not perform redaction. `ReasoningCapturePolicy`
independently authorizes or rejects persistence of explicitly extracted reasoning.

Framework credential redaction and verified provider-private reasoning removal are
mandatory. Applications may add business rules through `Tracer(redactor=...)`:

```python
from pydantic import JsonValue

from tinkerfin_tracing import (
    CompositeRedactor,
    RedactionContext,
    Tracer,
    redact_json_paths,
)


class CustomerRedactor:
    def redact(
        self,
        value: JsonValue,
        *,
        context: RedactionContext,
    ) -> JsonValue:
        if (
            context.content_kind == "tool_arguments"
            and context.component_name == "create_customer"
        ):
            return redact_json_paths(
                value,
                paths=("/id_card_number", "/mobile", "/bank_account"),
            )
        return value


tracer = Tracer(
    redactor=CompositeRedactor(
        CustomerRedactor(),
    )
)
```

Each Redactor receives detached finite JSON after mandatory credential and private
reasoning cleanup. It never receives LangChain messages, provider objects, thread IDs,
Run IDs, or user identities. `RedactionContext.content_kind` identifies message, model
request/response, Tool arguments/result, state, interaction, plan, or custom content;
`component_name` contains only the public component name when one exists.

Redactors are synchronous, deterministic, reentrant, free of I/O, and must not mutate
their input. Every result is validated before the next Redactor runs. The framework
performs credential and private-reasoning cleanup again after the business chain, so an
extension cannot reintroduce those fields. Invalid JSON, an awaitable, input mutation,
an exception, or damage to required state, model-message, or HITL structure rejects the
capture. Raw content is never used as a fallback.

The default `CapturePolicy.public_history()` retains complete sanitized Tool content.
Use per-Tool `ToolTraceCapture.metadata_only()`, `selected_content()`, or `disabled()`
when a Tool needs a narrower policy. `CapturePolicy.public_safe()` makes metadata-only
Tool capture the default and supports explicit RFC 6901 selections.

Provider reasoning still requires two independent opt-ins: a verified Runtime extractor
and `ReasoningCapturePolicy.content()` on the Tracer. The default stores no reasoning
content or digest. A business field named `reasoning_content` outside the reserved
`additional_kwargs` provider path is ordinary data and is not removed automatically.

## Storage and extension boundaries

`InMemoryTraceStore` is bounded and process-local. `SqlAlchemyTraceStore` borrows an
application-owned asynchronous Engine and supports SQLite and MySQL:

```python
from sqlalchemy.ext.asyncio import create_async_engine

from tinkerfin_tracing import SqlAlchemyTraceStore, Tracer

engine = create_async_engine("mysql+asyncmy://user:password@db/tinkerfin")
store = SqlAlchemyTraceStore(engine, namespace="my-application")
await store.setup()
tracer = Tracer(store=store)

# The application remains responsible for closing the borrowed Engine.
await engine.dispose()
```

The Store owns exactly six current tables. Hash search keys use 32-byte SHA-256 binary
values. The Graph table has one lineage index in addition to its primary key, so a new
node revision updates two B-Trees rather than maintaining one index per filter field.
Filtering runs after selected-lineage revisions are merged.
Literal metadata search folds only ASCII `A-Z` for ASCII-only queries and is
case-sensitive when the query contains non-ASCII characters. Unicode characters are
never mapped to ASCII lookalikes, keeping the built-in Stores equivalent without a
duplicate search payload.

Custom shared storage implements the five-operation `TraceLedgerBackend`; optional
indexed Graph queries use `TraceGraphQueryBackend` and rebuilds use
`TraceGraphRebuildBackend`. A codec may encrypt canonical fact and checkpoint bytes
without changing their pre-transform digest. Runtime observers and Backend decorators
remain the integration points for telemetry or archival. The package does not provide
an S3, KMS, or OpenTelemetry implementation.

The low-level `TraceWriter` accepts already captured semantic facts and is a trusted
storage boundary. Applications that need mandatory framework and business redaction use
`Tracer`; direct Fact construction does not provide enough source context for the Store
to infer business rules safely.

## Documentation

- [Semantic tracing](https://github.com/tinkerfin-ai/tinkerfin/blob/main/docs/en/tracing/index.md)
- [API reference](https://github.com/tinkerfin-ai/tinkerfin/blob/main/docs/en/tracing/api-reference.md)

## License

Apache License 2.0. See
[LICENSE](https://github.com/tinkerfin-ai/tinkerfin/blob/main/LICENSE).
