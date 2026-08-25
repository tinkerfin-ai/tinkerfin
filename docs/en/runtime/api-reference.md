# Runtime usage reference

[Runtime basics](index.md) · [中文](../../zh/runtime/api-reference.md)

This page groups the public Runtime capabilities by how you use them. Most applications only need the first two sections.

## Entry points

| API | When to use it | Main input or result |
| --- | --- | --- |
| `TinkerFin(run_coordinator=None, state_schema=None)` | Create the main entry point | Optional shared coordinator and Definition-wide state |
| `TinkerFin.plan(...)` | Create an immutable Plan-capable factory | Capability and mode defaults, optional Planner model, clarification form, and Plan content schema |
| `TinkerFin.create_deep_agent(...)` | Create a reusable agent definition | See [Create and run a Deep Agent](deep-agents.md) |
| `Identity(threadId=..., runId=...)` | Identify one framework run | Thread and run only |
| `TinkerFin.run(...)` | Run a custom async source | `source_factory`, `identity`, `on_part` |
| `DeepAgentDefinition.new(...)` | Create a native Runtime | Required `identity`, optional request `mode` and `on_part` |
| `DeepAgentDefinition.new_agui(...)` | Create an AG-UI Runtime | Required canonical `identity`; optional parent, mode, resume, checkpoint callback, and observers |

Reuse `DeepAgentDefinition`. Treat `DeepAgentRuntime`, `DeepAgentAgUiRuntime`,
`DeepAgentAgUiResumeRuntime`, `TinkerFinRun`, and `NativeTinkerFinRun` as single-use
values returned by the entry points rather than constructing them directly.

`.plan(enabled=True)` affects only definitions created from the returned factory. It
does not add a parameter to `create_deep_agent(...)`. The returned factory retains its
coordinator and global state schema. Selecting Plan requires an explicit Planner model
and a concrete production checkpointer; default runs retain upstream requirements.
Invalid Plan configuration raises `tinkerfin.plan.PlanModeConfigurationError`.

Choose the current request path with `mode="default"` or `mode="plan"` on `new()` or
`new_agui()`. Omitting it uses `.plan(default_mode=...)`. Default directly runs the
native Deep Agent; Plan runs the standalone Planning Graph and automatically hands an
approved draft to native execution. The same checkpoint thread can select Plan on a
later request. An ordinary Definition accepts only `default`.

## Plan Mode values

The top-level package exports `AgentMode`. The `tinkerfin.plan` package exports
`ClarificationModel`, the `ClarificationOption` / `ClarificationQuestion` /
`ClarificationForm` base and generic types, `DefaultClarificationForm`,
`PlanContentModel`, `StructuredPlanStep`, `StructuredPlanContent`,
`MarkdownPlanContent`, `PlanSchemaReference`, `PlanDraft`, `ConfirmedPlan`,
`RequirementAnswer`, `PendingClarification`, `ClarificationExchange`, `PlanState`,
`PlanStatus`, `PlanHandoff`, `PlanHandoffPhase`, `PlanReviewAction`, and the Plan error
types. The models are frozen. Planning state uses the camel-case JSON representation at `tinkerfin_plan`;
`effectiveMode` becomes `default` when approval commits the handoff.

`PlanHandoffPhase` advances `pending → accepted → completed`. Accepted records include
the native checkpoint that contains the deterministic handoff message; completed records
also include terminal native checkpoint evidence. Retries reconcile these checkpoints
instead of redispatching the approved Plan.

`.plan(plan_schema=...)` accepts one concrete `PlanContentModel` subclass. Omitting it
uses `StructuredPlanContent`; `MarkdownPlanContent` preserves one non-blank Markdown
string without whitespace rewriting. A host schema declares a stable `schema_id`; the
runtime validates and fingerprints its JSON Schema and rejects schema drift on resume.
The reviewed draft and `ConfirmedPlan` always use the same frozen content schema.

`.plan(clarification_schema=...)` accepts one fully concrete `ClarificationFormBase`
subclass defined by the host. Omitting it uses `DefaultClarificationForm`. Python uses
`allow_free_text`; the JSON contract uses `allowFreeText`. Every question explicitly
sets `required`. Option answers contain only `questionId` and `optionId`; free-text
answers contain only `questionId` and `answer`; an optional skip contains
`questionId` and `skipped: true`.
Host models can add typed attributes and discriminants but cannot redefine the
framework-owned core fields or the tuple shape of questions and options.

`TinkerFin(state_schema=...)` contributes application state to every native Deep Agent
Definition created by that factory. The standalone Planning Graph composes its own
required view without changing native default topology or middleware. Reducers,
`Required` / `NotRequired`, and schema metadata are retained; incompatible same-name
fields fail before a Plan run. Runtime `context_schema` remains a separate
non-checkpointed context contract.

## Stream objects

| API | Use |
| --- | --- |
| `GraphRunStream` | Iterates ordinary objects; supports `aclose()` and `to_sse()` |
| `NativeGraphRunStream` | Iterates normalized LangGraph v2 parts and can go directly to Messaging |
| `AgUiEventStream` | Iterates AG-UI events; supports `abort()`, `aclose()`, and `to_sse()` |
| `SseBody` | Iterates SSE strings; call `prepare()` first and `aclose()` when done |

### `AgUiEventStream.abort()`

Requests cancellation and returns the remaining events needed to close open lifecycles correctly. Repeated calls do not create duplicate terminal tails.

### `AgUiEventStream.from_initialization_error(...)`

Creates a valid failed AG-UI stream when initialization failed before Graph iteration. Normal `new_agui()` callers do not need it.

## Native stream data

`NativeStreamPart` is the durable native v2 representation.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `schemaVersion` | `1` | `1` | Data format version |
| `type` | string | required | `messages`, `tasks`, `values`, `updates`, `checkpoints`, `debug`, or `custom` |
| `ns` | tuple of strings | required | Graph namespace; empty means root |
| `data` | JSON value | required | Mode-specific data |
| `interrupts` | tuple of JSON values | `()` | Interrupts carried by `values` |

The Runtime normally creates these objects for you.

## Native AG-UI stream binding

| API | Parameter | Use |
| --- | --- | --- |
| `AgUiNativeStreamConfig` | `extra_modes=()` | Declares additional modes beyond the AG-UI base profile |
| `.bind(astream, *args, **options)` | Stream callable and invocation arguments | Produces one bound native invocation |
| `AgUiNativeStreamInvocation` | Do not construct directly | Pass to `TinkerFin.run()` |

Invalid combinations raise `AgUiNativeStreamConfigurationError`.

## SSE types

| API | Use |
| --- | --- |
| `SsePayload` | Holds required `data`, optional `event`, and optional `retry` |
| `SseMapper` | Asynchronously maps an object to `SsePayload` or `None` |
| `SseEventIdResolver` | Asynchronously resolves an event ID |
| `SsePreflight` | Asynchronous preflight callable used by `prepare()` |

## Observers and coordination

| API | Use |
| --- | --- |
| `PartObserver` | Asynchronously observes native parts |
| `EventObserver` | Asynchronously observes AG-UI events |
| `RunCoordinator` | Extension boundary for run exclusion |
| `InMemoryRunCoordinator(key_resolver=...)` | Serializes business keys within one process |

After installing `tinkerfin[redis]`:

| API | Use |
| --- | --- |
| `RedisRunCoordinator` | Serialize matching `Identity` values across processes |
| `RedisLeaseLock` | Manage renewable Redis resource leases |
| `RedisLease` | Immutable resource key and fencing token yielded by `hold()` |
| `RedisLeaseLost` | Report uncertain, expired, or lost ownership |

See [Custom sources and run coordination](extensions.md#use-a-renewable-redis-lease-when-needed) for constructor defaults and connection-pool guidance.

## Resume and errors

| API | When it appears |
| --- | --- |
| `AgUiResumeBinding` | Resuming an interrupted AG-UI run |
| `AgUiResumeCheckpoint` | Stable callback value after the resume marker is durable |
| `tinkerfin.plan.PlanModeConfigurationError` | A Plan definition lacks a concrete saver or explicit model, has an incompatible state schema, or requests non-sync durability |
| `AgUiSettlementTimeoutError` | Caller wait ended before protected Runtime cleanup settled |
| `AgUiNativeStreamConfigurationError` | Native options do not satisfy the AG-UI profile |

`TinkerFinRun.astream_agui(...)` and `NativeTinkerFinRun.astream_agui(...)` convert lower-level sources to AG-UI.

| Parameter | Default | Purpose |
| --- | --- | --- |
| `timeout` | `None` | Overall native-stream wait limit |
| `settlement_timeout` | `None` | Caller wait limit for protected cleanup |
| `expose_reasoning_events` | `False` | Deliver supported reasoning events |
| `expose_subagent_events` | `True` | Deliver subagent events |
| `prior_tool_call_ids` | `frozenset()` | Complete scoped tool IDs emitted before resume |
| `private_state_keys` | `frozenset()` | Host-owned top-level state channels omitted from public projection |
| `on_event` | `None` | Observer called before AG-UI event delivery |
| `parent_run_id` | `None` | Optional checkpoint lineage exposed on `RUN_STARTED` |

Identity is already bound by `TinkerFin.run(..., identity=...)`; `astream_agui()` does not accept duplicate IDs.

`AgUiResumeBinding.from_agui(...)` validates complete trusted AG-UI interrupts and
entries, including native groups, cancellation mode, Tool IDs, and source agents. The
binding stores no identity or parent and exposes no native command. It can be persisted
and restored as one complete Pydantic model.

See [Interrupts and resume](../agui/interrupts-and-resume.md) for the complete resume flow.
