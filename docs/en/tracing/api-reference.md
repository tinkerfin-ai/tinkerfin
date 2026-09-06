# Tracing API reference

[Semantic tracing](index.md) · [中文](../../zh/tracing/api-reference.md)

## `Tracer`

```python
Tracer(
    store=None,
    capture_policy=None,
    reasoning_capture_policy=None,
    redactor=None,
    graph_query_limits=None,
    limits=None,
    write_policy=None,
    projections=(),
)
```

| Member | Meaning |
| --- | --- |
| `store` | Borrowed `TraceStore`; defaults to a new `InMemoryTraceStore` |
| `capture_policy` | Content retention, Tool selection, and error-message policy |
| `reasoning_capture_policy` | Independent authorization for extracted reasoning content |
| `redactor` | Optional additional business `TraceRedactor` |
| `graph_query_limits` | Direct-node, Subagent-expanded total-node, and serialized Graph byte limits |
| `open_run(context)` | `RuntimeObserver` entry used by TinkerFin Runtime |
| `get(thread_id, head_run_id=None, limit=100, history_cursor=None, projections=())` | Fixed-prefix conversation history |
| `query(thread_id, where=None, head_run_id=None, cursor=None, limit=100)` | Current indexed `TraceGraphQuery` |
| `rebuild_graph(thread_id)` | Reconstruct the disposable Graph index from Ledger facts |

Passing both `store` and `limits` requires exact equality with `store.limits`.

## Graph values

`TraceGraphFilter` accepts exactly `kinds`, `statuses`, `model_call_id`, `agent_names`,
`providers`, `models`, `namespaces`, `search`, `started_after`, and `started_before`.
`model_call_id` selects AssistantMessage, Tool, and Subagent events emitted by one Model
call. `namespaces` is an exact-scope filter; an empty tuple selects the top-level Graph.

`search` is a literal substring filter over event name, Agent name, provider, model, and
retained public details: content, request, result, and failure information. JSON keys,
string values, and scalar values are searchable. Omitted values, identifiers, and private
reasoning are not searched. An ASCII-only query folds only ASCII `A-Z`; a query
containing any non-ASCII character is case-sensitive.

The Store first applies metadata, exact namespace, Model-call, and time constraints, then
decodes a bounded candidate set through its Codec. This preserves encrypted Codec
behavior and avoids a plaintext search document or payload copy. Content-search
candidates are bounded by `max_total_nodes`; exceeding that bound fails with
`TraceQuotaExceeded` instead of returning partial results.

`TraceGraphQuery` exposes:

- `snapshot`: defensive `TraceGraphPage` copy;
- `turns`, `nodes`, `ordered_node_ids`, and `matched_node_ids`;
- `next_cursor`, `as_of_seq`, and `completeness`;
- `follow()`: closeable current-first-page `TraceFollow[TraceGraphDelta]`.

`TraceGraphNodeKind` contains `human_message`, `assistant_message`, `context`,
`model`, `tool`, `subagent`, `memory`, `guardrail`, `retrieval`, `custom`, `plan`, and
`interaction`. A Tool event owns its ToolMessage result. Middleware execution has no
dedicated kind or fact. A `SKILL.md` read is represented only by the ordinary
`read_file` Tool event.

`TraceGraphNodeStatus` contains `running`, `waiting`, `succeeded`, `failed`,
`cancelled`, `abandoned`, and `unknown`. `TraceGraphLinkIssue` contains
`missing_subagent`, `missing_model_call`, and `missing_tool_proposal`.

`TraceGraphTurn` is a container. Its top-level events are siblings, and only a Subagent
owns a nested event scope. `TraceGraphNode.parent_subagent_id` identifies the nearest
owning Subagent and is the only display-nesting relationship. A non-empty `namespace`
does not prove that relationship; validated Subagent provenance must establish it.
`TraceGraphNode.model_call_id` links only an AssistantMessage, Tool, or Subagent to the
Model call that emitted it. `TraceGraphNode.tool_call_only` is true only when an observed
AssistantMessage has no user-visible content and its Model result emitted at least one
Tool call; it remains stable when a query filters Tool events out of the page.

A HumanMessage owned by a Subagent exposes the captured `task.description` as `content`.
The owning Subagent exposes the complete captured task arguments as `request`; both are
projected from one Ledger fact.

A `context` node is emitted once for every Model attempt. Its `started_at` is the previous
visible boundary in that execution scope, and its `completed_at` is the Model
`started_at`. `content` projects the final SystemMessage content from the Model request;
the same request fact owns both views, so no second payload is persisted. When the request
contains no SystemMessage, `content` is `None` while the timing remains available.

Within one scope, events are ordered by `started_seq`, then by user, context, model,
Tool, Subagent, assistant, and event ID. `ordered_node_ids` is produced by iterative
depth-first traversal, placing each Subagent's scope immediately after its container.
The traversal does not use Python recursion.

`matched_node_ids` contains only direct filter matches in authoritative order. The Store
adds the owning Subagent chain to `nodes` with a bounded breadth-first lookup, but those
containers are not matches. The lookup accepts at most 64 Subagent levels, obeys
`max_total_nodes`, and reads SQL keys in batches of 500. An unfiltered history Graph
matches every returned event.

`TraceGraphDelta` carries Turn and node upserts/removals plus the current `next_cursor`,
`as_of_seq`, complete `ordered_node_ids`, `matched_node_ids`, and Completeness. Follow is
rejected for a paginated cursor. A cursor is invalid after the current Ledger tail
changes, so every live Delta replaces the previous cursor atomically.

`Tracer.get()` returns a `TraceThread` whose `graph` is a complete `TraceGraph` for the
same fixed prefix and loaded Turn window as its messages and state. `TraceThread.follow()`
publishes `TraceUpdate.graph` as a `TraceGraphDelta`; history and query paths share this
flat, Subagent-scoped timeline model. Current-tail history reads the disposable Graph
index, while an older fixed prefix replays the same reducer from Ledger facts.

`TraceGraphCompleteness` distinguishes unknown call history, missing relationship
evidence, and details omitted by capture or response limits. `call_tracking_missing`
excludes pre-execution initialization failures proven by a `runtime_initialization_error`
terminal. Other untracked Runs remain incomplete; failure or absent call events alone
does not establish complete history.

## Query limits

```python
TraceGraphQueryLimits(
    max_direct_nodes=1000,
    max_total_nodes=4000,
    max_page_bytes=8 * 1024 * 1024,
)
```

Direct matches are limited before owning-Subagent expansion. `max_total_nodes` also
bounds both the complete expanded page and the decoded candidate set needed for exact
content search. One query may select at most 10,000 Runs in its lineage. When a page
exceeds its byte budget, content, request, result, usage, and response metadata are
omitted first while structure remains authoritative. A structure-only page that remains
too large raises `TraceQuotaExceeded`.

## Capture policy

| API | Purpose |
| --- | --- |
| `CapturePolicy.public_history(...)` | Retain complete sanitized Tool content by default |
| `CapturePolicy.public_safe(...)` | Make Tool content metadata-only unless selected |
| `ToolTraceCapture.full_content()` | Retain Tool lifecycle, arguments, result, and public review description |
| `ToolTraceCapture.metadata_only()` | Retain lifecycle without content |
| `ToolTraceCapture.selected_content(...)` | Retain selected RFC 6901 paths |
| `ToolTraceCapture.disabled()` | Suppress that Tool's Trace facts |
| `ReasoningCapturePolicy.omitted()` | Retain no extracted reasoning content or digest |
| `ReasoningCapturePolicy.content()` | Authorize bounded extracted reasoning content |

`CapturePolicy` does not expose raw-value capture methods. Runtime values enter the
mandatory pipeline owned by `Tracer`.

## Business redaction

```python
class TraceRedactor(Protocol):
    def redact(
        self,
        value: JsonValue,
        *,
        context: RedactionContext,
    ) -> JsonValue: ...
```

`RedactionContext.content_kind` is one of `message`, `model_request`,
`model_response`, `tool_arguments`, `tool_result`, `state`,
`interaction`, `plan`, or `custom`. `component_name` is an optional public component
name. Execution and user identities are not provided.

`CompositeRedactor(*redactors)` applies results in declaration order.
`redact_json_paths(value, paths=(...))` returns a detached copy with matching RFC 6901
locations replaced by `{"$type": "redacted"}`.

Redactors must be synchronous, deterministic, reentrant, free of I/O, and must not
mutate input. An exception, awaitable, mutation, invalid JSON, non-finite number, or
damage to required state/model/HITL structure raises `TraceCaptureRejected`. The raw
value is never used as a fallback.

Credential and verified private-reasoning cleanup runs before and after the business
chain. The final safety pass cannot be disabled.

## Storage interfaces

`RunFact` describes the whole Runtime invocation. Its `namespace` must be empty and
`in_subagent_scope` must be false; child execution uses `SubagentFact`. Construction
rejects another scope. A writer also rejects invalid Run scope atomically before
committing any fact in a batch, including input made through unchecked model copying.

`MessageFact.phase` is `started`, `content`, `completed`, `reconciled`, `removed`,
`cancelled`, `interrupted`, or `abandoned`. The last three describe an unfinished
Assistant delivery at Run settlement and may carry its retained partial content;
they cannot represent state snapshots or another message role. `TraceMessage.status`
uses `completed` when delivery ends or pauses. The Graph separately records its
success, cancellation, waiting, or abandonment. A resumed delivery can become
`streaming` again without duplicating already retained content.

`SubagentFact` uses `started/running`, `updated/waiting`, and `completed` with
`succeeded`, `failed`, `cancelled`, or `abandoned`. Only `started` carries `input`,
`parent_tool_call_id`, `parent_execution_id`, and `model_call_id`. Later facts keep
the same `subagent_id` and namespace and inherit the opening request and relationships.

| Interface | Responsibility |
| --- | --- |
| `TraceStore` | Ledger generations, fixed reads, follow, checkpoints, and deletion |
| `TraceGraphStore` | Bounded direct Graph queries |
| `TraceGraphRebuildStore` | Disposable Graph-index rebuild |
| `TraceLedgerBackend` | Five-operation durable Ledger storage boundary |
| `TraceGraphQueryBackend` | Optional durable indexed Graph query |
| `TraceGraphRebuildBackend` | Optional durable Graph rebuild |
| `CanonicalTracePayloadCodec` | Canonical encoding or reversible encryption transform |

The low-level writer persists already captured facts. Mandatory framework and business
redaction is guaranteed on the `Tracer` path, where source context is available.

`TraceGraphNodeMutation` supplies `model_call_seq` together with `model_call_id` when it
establishes a Model association. `StoredTraceGraphNode` and `TraceGraphNodeRecord` retain
that sequence and its `model_call_event` independently of lifecycle and payload locators.
The referenced fact must establish the same node's association in the selected scope and
Run lineage; an unrelated Model fact or a later input update is not sufficient evidence.
