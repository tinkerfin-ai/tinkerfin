# Runtime usage reference

[Runtime basics](index.md) · [中文](../../zh/runtime/api-reference.md)

AG-UI Runtime entrypoints require `pip install "tinkerfin[agui]"`. Native Runtime,
Plan Mode, Observation, and native SSE are part of the base installation.

This page groups the public Runtime capabilities by how you use them. Most applications only need the first two sections.

## Common entry points

| API | When to use it | Main input or result |
| --- | --- | --- |
| `TinkerFin(checkpointer=None, run_coordinator=None, state_schema=None, runtime_profile=None)` | Create the main entry point | Optional borrowed default saver, shared coordinator, Definition-wide state, and integration Profile |
| `TinkerFin.observe(observer)` | Add one managed Runtime observer immutably | A `tinkerfin-contracts` `RuntimeObserver` |
| `TinkerFin.plan(...)` | Create an immutable Plan-capable factory | Capability and mode defaults, optional Planner model, clarification form, Plan content schema, and review actions |
| `TinkerFin.create_deep_agent(...)` | Create a reusable agent definition | See [Create and run a Deep Agent](deep-agents.md) |
| `TinkerFin.open_run(identity, *, agent, input, ...)` | Open one managed native run | A single-use `NativeGraphRunStream` |
| `TinkerFin.open_agui_run(identity, *, agent, input=... or resume=..., ...)` | Open one managed AG-UI run | A single-use `AgUiEventStream` |
| `DeepAgentDefinition.create_graph(mode=...)` | Reuse a direct async Runnable without managed lifecycle | Complete native or Plan-capable `DeepAgentGraph` |
| `RunIdentity(threadId=..., runId=...)` | Identify one framework run | Thread and run only |

`agent` may be an existing Definition or a synchronous/asynchronous callable returning
one. The managed facade owns Definition resolution, asynchronous Graph construction,
identity binding, Observation, coordination, setup-failure conversion, cancellation,
and cleanup. `open_agui_run()` requires exactly one of `input` and `resume`.

## Advanced integration APIs

| API | When to use it | Main input or result |
| --- | --- | --- |
| `DeepAgentsRuntimeProfile` | Implement a complete upstream integration | Canonical Profile ID, graph factory, Native Stream Driver, and resume checkpoint semantics |
| `DeepAgentsFactoryPreparation` | Return Profile-owned factory overrides | Read-only override mapping and external-subagent cancellation boundary |
| `DeepAgentsV2RuntimeProfile(...)` | Use the current built-in integration | Locked graph construction, invocation, validation, observations, reasoning extractors, and replay |
| `DeepAgentDefinition.new(...)` | Create a native Runtime | Required `identity`, optional request `mode` and `on_part` |
| `DeepAgentDefinition.new_agui(...)` | Create an AG-UI Runtime | Required canonical `identity`; optional parent, mode, resume, checkpoint callback, and observers |
| `TinkerFin.failed_agui_run(...)` | Represent a setup failure after a Run was accepted | Error, identity, and the real available input/config/resume |
| `trace_contribution(kind=..., name=..., input=None)` | Publish explicit Memory, Guardrail, retrieval, or custom semantics | Async context manager with optional `set_result(...)` |

`DeepAgentsV2RuntimeProfile` is the default current Profile. TinkerFin selects one
Profile before Definition creation, persists its `profile_id` with checkpoint lineage,
and rejects branch or resume through another Profile before Graph continuation. It never
detects or negotiates a Profile from stream data. A verified provider reasoning path is
enabled only by passing a `ReasoningExtractor`, such as `DeepSeekReasoningExtractor`, to
the Profile; this does not authorize Trace persistence by itself. A custom Profile also
implements its concrete `stage_resume_intent()`, `pending_resume_values()`, and
`native_resume_submitted()` checkpointer semantics; TinkerFin does not fall back to v2
checkpoint behavior for it.

Reuse `DeepAgentDefinition`. Treat `DeepAgentRuntime`, `DeepAgentAgUiRuntime`, and
`DeepAgentAgUiResumeRuntime` as single-use values returned by the entry points rather
than constructing them directly.

`.plan(enabled=True)` affects only definitions created from the returned factory. It
does not add a parameter to `create_deep_agent(...)`. The returned factory retains its
coordinator and global state schema. Selecting Plan requires an explicit Planner model
or an explicit Agent model, plus a concrete production checkpointer; default runs retain
upstream requirements. Omitting `planner_model` uses the Agent model.
Invalid Plan configuration raises `tinkerfin.plan.PlanModeConfigurationError`.

Choose the current request path with `mode="default"` or `mode="plan"` on a managed run
or direct Graph. Omitting it uses `.plan(default_mode=...)`. Default directly runs the
native Deep Agent; Plan runs the standalone Planning Graph and automatically hands an
approved draft to native execution. The same checkpoint thread can select Plan on a
later request. An ordinary Definition accepts only `default`.

## Plan Mode values

The top-level package exports `AgentMode`. The `tinkerfin.plan` package exports
`ClarificationModel`, `ClarificationOption`, `ClarificationForm`,
`BuiltInClarificationForm`, the six built-in Question types,
`ClarificationType`, `clarification_type`, `DefaultClarificationForm`,
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

`.plan(content_schema=...)` accepts one concrete `PlanContentModel` subclass. Omitting it
uses `StructuredPlanContent`; `MarkdownPlanContent` preserves one non-blank Markdown
string without whitespace rewriting. The runtime validates and fingerprints the selected
JSON Schema automatically and rejects Definition drift on resume.
The reviewed draft and `ConfirmedPlan` always use the same frozen content schema.

`.plan(allowed_review_actions=...)` accepts an ordered, non-empty sequence of distinct
`PlanReviewAction` members. The default is `APPROVE`, `RESPOND`, and `REJECT`; the
interrupt response Schema contains exactly the configured decisions. Include `EDIT`
explicitly only when the host provides a trusted draft editor.

Only `APPROVE` exits Planning and hands the confirmed draft to native execution.
`REJECT` invalidates the reviewed draft, accepts an optional reason, returns one visible
Planner reply, and enters `awaiting_input` while keeping effective mode `plan`. An
explicitly configured `CANCEL` follows the same continuation without a reason. AG-UI
`status="cancelled"` remains abandonment and does not become a Plan decision.

`.plan(clarification_schema=...)` accepts one concrete `ClarificationFormBase` subclass;
omitting it uses all six built-in answer types. Shared typed metadata uses
`BuiltInClarificationForm[QuestionAttributes, OptionModel]`. A host-defined semantic type
is registered once through `.plan(clarification_types=(clarification_type(...),))`.
Its namespaced type ID is unversioned. The Definition fingerprint fixes the exact current
Schema and callbacks; incompatible stored data must be rebuilt instead of assigning a
second type-ID version.

Clarification responses use an `answers` object keyed by checkpoint question ID. An
answered value contains `status: answered`, its `answerType`, and type-specific fields;
an optional skip contains only `status: skipped`. The exact pending JSON Schema validates
complete coverage, choice IDs, selection bounds, non-blank text, and calendar dates before
Graph resume. Time responses additionally validate local minute precision, the question's
IANA time zone, and any inclusive non-wrapping range. Host models cannot redefine
framework-owned IDs, required/skip semantics, or checkpoint ownership.
Datetime responses contain one local calendar date and minute in a required IANA zone.
Their bounds may span dates; normalization emits `localDateTime`, `timeZone`, and a
unique UTC `instant`. DST gaps and repeated local minutes are rejected.


`TinkerFin(state_schema=...)` contributes application state to every native Deep Agent
Definition created by that factory. The standalone Planning Graph composes its own
required view without changing native default topology or middleware. Reducers,
`Required` / `NotRequired`, and schema metadata are retained; incompatible same-name
fields fail before a Plan run. Runtime `context_schema` remains a separate
non-checkpointed context contract.

## Stream objects

| API | Use |
| --- | --- |
| `NativeGraphRunStream` | Iterates original upstream objects while transferring the same Driver-owned canonical frame to Messaging or Native SSE |
| `AgUiEventStream` | Iterates AG-UI events; supports `abort()`, `aclose()`, and `to_sse()` |
| `SseBody` | Single-use SSE body that owns each upstream pull and close task; call `prepare()` before handing it to a host response |

### `AgUiEventStream.abort()`

Requests cancellation and returns the remaining events needed to close open lifecycles correctly. Repeated calls do not create duplicate terminal tails.

### `TinkerFin.failed_agui_run(...)`

Creates an observed failed AG-UI stream when an advanced host accepted a semantic Run
before entering the managed facade. `open_agui_run()` already uses this behavior for
ordinary model, Sandbox, Definition, or Graph setup failures.

## Native stream data

`NativeStreamPart` is the current finite canonical replay representation. It is produced
by the selected Profile and contains no upstream version selector.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `type` | string | required | `messages`, `tasks`, `values`, `updates`, `checkpoints`, `debug`, or `custom` |
| `ns` | tuple of strings | required | Graph namespace; empty means root |
| `data` | JSON value | required | Mode-specific data |
| `interrupts` | tuple of JSON values | `()` | Interrupts carried by `values` |

The Runtime normally creates these objects for you.

## Native stream preflight

The selected Runtime Profile owns its complete upstream invocation and checkpoint contract,
including required modes, upstream version, subgraph behavior, complete state output, and
stable build/bound stream signatures plus durable resume-intent writes that preserve
interrupted control state. The bound signature permits lazy resume Graph construction
inside retained async settlement. A custom Profile may expose
`create_agent_graph(factory, args, kwargs)` for native asynchronous construction;
otherwise Core invokes that Profile's selected synchronous factory in AnyIO's bounded
worker pool. The built-in Profile
lets callers add supported diagnostic modes through
`astream(stream_mode=...)`; removing a required mode or supplying a conflicting upstream
option fails before Graph or coordinator side effects. Native and AG-UI paths use the
same Profile boundary and expose the same parameter-validation errors.

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
| `RuntimeObserver` | Opens one managed run/native observation session per admitted Runtime request |
| `RunObservationSession` | Receives ordered observations, hard-boundary force calls, failure notification, and close |
| `join_task(task, cancel=False, suppress_task_cancellation=False)` | Settle a host-owned task before propagating caller cancellation |
| `RunCoordinator` | Extension boundary for run exclusion |
| `InMemoryRunCoordinator(key_resolver=...)` | Serializes business keys within one process |

After installing `tinkerfin[redis]`:

| API | Use |
| --- | --- |
| `RedisRunCoordinator` | Serialize matching `RunIdentity` values across processes |
| `RedisLeaseLock` | Manage renewable Redis resource leases |
| `RedisLease` | Immutable resource key and fencing token yielded by `hold()` |
| `RedisLeaseLost` | Report uncertain, expired, or lost ownership |

See [Run coordination and Redis leases](extensions.md#use-a-renewable-redis-lease-when-needed) for constructor defaults and connection-pool guidance.

`TinkerFin.observe(...)` returns a separate factory and preserves Observer registration
through `.plan(...)`. Managed Deep Agent requests install one request-scoped LangChain
callback that records the final middleware-processed model request before provider
execution and records actual Tool execution after review or argument editing. Runtime
also validates each Native part once, then publishes Trace-safe Native observations
before `on_part` and AG-UI conversion. An Observer failure terminates the Run
fail-closed; Runtime still closes all opened sessions. `tinkerfin-tracing.Tracer` is the
provided semantic implementation. AG-UI events, Messaging commits, SSE frames, and
Redis ownership are not Runtime observations.

An Agent terminal is selected before Observer broadcast. A terminal-broadcast Observer
failure fails the caller and is reported to healthy Observers without rewriting that
already selected execution outcome.

## Resume and errors

| API | When it appears |
| --- | --- |
| `AgUiResumeRequest` | Untrusted client decisions for an interrupted AG-UI run |
| `AgUiResumeBinding` | Advanced checkpoint-resolved facts accepted by `new_agui()` |
| `AgUiResumeCheckpoint` | Stable callback value after the resume marker is durable |
| `tinkerfin.plan.PlanModeConfigurationError` | A Plan definition lacks a concrete saver or explicit model, has an incompatible state schema, or requests non-sync durability |
| `AgUiSettlementTimeoutError` | Caller wait ended before protected Runtime cleanup settled |

`TinkerFin.open_agui_run(...)` is the ordinary entry point:

| Parameter | Default | Purpose |
| --- | --- | --- |
| `identity` | required | Canonical thread and run identity |
| `agent` | required | Definition or sync/async callable returning one |
| `input` / `resume` | exactly one | Ordinary Graph input or client-only `AgUiResumeRequest` |
| `parent_run_id` | `None` | Optional checkpoint lineage exposed on `RUN_STARTED` |
| `mode` | Definition default | Native default or Plan route |
| `config` / `context` | `None` | Graph configuration and declared Runtime context |
| `stream_timeout` / `cleanup_timeout` | `None` | Native pull deadline and protected cleanup wait |
| `include_reasoning_events` | `False` | Deliver verified public reasoning events |
| `include_subagent_events` | `True` | Deliver validated subagent events |
| `on_native_part` / `on_agui_event` | `None` | Async observers before delivery |
| `on_resume_saved` / `on_resume_not_saved` | `None` | Idempotent host settlement around durable marker evidence |
| `resume_checkpointer` | TinkerFin default | Advanced saver authority when a lazy resumed Definition intentionally overrides the factory default; it must be the exact same object |

Advanced orchestration may use `DeepAgentDefinition.new_agui(...)` directly:

| Parameter | Default | Purpose |
| --- | --- | --- |
| `identity` | required | Canonical thread and run identity |
| `parent_run_id` | `None` | Optional checkpoint lineage exposed on `RUN_STARTED` |
| `mode` | Definition default | Select native default or Plan routing for this request |
| `timeout` | `None` | Overall native-stream wait limit |
| `settlement_timeout` | `None` | Caller wait limit for protected cleanup |
| `expose_reasoning_events` | `False` | Deliver supported reasoning events |
| `expose_subagent_events` | `True` | Deliver subagent events |
| `resume` | `None` | Complete trusted `AgUiResumeBinding` for a resumed request |
| `on_resume_checkpointed` | `None` | Idempotent callback after the exact resume marker is readable |
| `on_resume_initialization_failed` | `None` | Idempotent settlement if failure, cancellation, or close occurs before marker durability |
| `on_event` | `None` | Observer called before AG-UI event delivery |

The returned Runtime preserves the installed Graph `astream(...)` parameter shape. It
injects the same `RunIdentity` into Graph configuration and does not accept another Runtime
identity at `astream()`.

`DeepAgentDefinition.prepare_agui_resume(...)` accepts an `AgUiResumeRequest`, reads the
canonical checkpoint through the selected Profile, validates native groups, cancellation,
Tool IDs, source agents, and Profile lineage, and returns the private binding. The client
request contains no server interrupt payload or native command.

See [Interrupts and resume](../agui/interrupts-and-resume.md) for the complete resume flow.
