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
| `graph_query_limits` | Direct-node, total-node, and serialized Graph byte limits |
| `open_run(context)` | `RuntimeObserver` entry used by TinkerFin Runtime |
| `get(thread_id, head_run_id=None, limit=100, history_cursor=None, projections=())` | Fixed-prefix conversation history |
| `query(thread_id, where=None, head_run_id=None, cursor=None, limit=100)` | Current indexed `TraceGraphQuery` |
| `rebuild_graph(thread_id)` | Reconstruct the disposable Graph index from Ledger facts |

Passing both `store` and `limits` requires exact equality with `store.limits`.

## Graph values

`TraceGraphFilter` accepts `kinds`, `statuses`, `parent_id`, `agent_names`,
`middleware_names`, `skill_names`, `providers`, `models`, `namespaces`, `search`,
`started_after`, `started_before`, `include_technical_nodes`, and
`include_ancestor_nodes`.

`search` is a literal substring filter over node metadata. An ASCII-only query folds
only ASCII `A-Z` in both the query and metadata. It does not map Unicode characters to
ASCII lookalikes. A query containing any non-ASCII character is case-sensitive, which
keeps SQLite, MySQL, and in-memory results identical without storing a duplicate search
document.

`TraceGraphQuery` exposes:

- `snapshot`: defensive `TraceGraphPage` copy;
- `turns`, `nodes`, `ordered_node_ids`, and `root_node_ids`;
- `next_cursor`, `as_of_seq`, `facets`, and `completeness`;
- `follow()`: closeable current-first-page `TraceFollow[TraceGraphDelta]`.

`TraceGraphNodeKind` contains HumanMessage, AssistantMessage, SystemMessage,
ToolMessage, Agent, Model, Tool, Subagent, Skill, middleware, Memory, Guardrail,
retrieval, custom, Plan, interaction, Run, and Runtime task nodes. SystemMessage,
ToolMessage, middleware, Run, and Runtime task are technical nodes.

`TraceGraphDelta` carries Turn and node upserts/removals plus the current
`next_cursor`, complete `ordered_node_ids`, `root_node_ids`, Facets, and Completeness.
Follow is rejected for a paginated cursor. A cursor is invalid after the current Ledger
tail changes, so every live Delta replaces the previous cursor atomically.

`Tracer.get()` returns a `TraceThread` whose `graph` is a complete `TraceGraph` for the
same fixed prefix and loaded Turn window as its messages and state. `TraceThread.follow()`
publishes `TraceUpdate.graph` as a `TraceGraphDelta`; it does not expose a separate tree
node model. Current-tail history reads the disposable Graph index, while an older fixed
prefix replays the same reducer from Ledger facts.

`TraceGraphCompleteness` distinguishes missing callback evidence, missing relationship
evidence, and details omitted by capture or response limits.

## Query limits

```python
TraceGraphQueryLimits(
    max_direct_nodes=1000,
    max_total_nodes=4000,
    max_page_bytes=8 * 1024 * 1024,
)
```

Direct matches are limited before ancestor expansion. When a page exceeds its byte
budget, content, request, result, usage, and response metadata are omitted first while
structure remains authoritative. A structure-only page that remains too large raises
`TraceQuotaExceeded`.

## Capture policy

| API | Purpose |
| --- | --- |
| `CapturePolicy.public_history(...)` | Retain complete sanitized Tool content by default |
| `CapturePolicy.public_safe(...)` | Make Tool content metadata-only unless selected |
| `ToolTraceCapture.full_content()` | Retain Tool lifecycle, arguments, result, and public review description |
| `ToolTraceCapture.metadata_only()` | Retain lifecycle without content |
| `ToolTraceCapture.selected_content(...)` | Retain selected RFC 6901 paths |
| `ToolTraceCapture.disabled()` | Suppress that Tool's Trace facts |
| `MiddlewareTraceCapture.visible()` | Retain callback-proven execution lifecycle |
| `MiddlewareTraceCapture.disabled()` | Suppress middleware-specific facts |
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
