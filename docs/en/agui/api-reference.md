# AG-UI usage reference

[AG-UI basics](index.md) · [中文](../../cn/agui/api-reference.md)

## Common managed APIs

| API | Use |
| --- | --- |
| `TinkerFin.open_agui_run(identity, *, agent, input=... or resume=..., ...)` | Opens one ordinary or resumed AG-UI run |
| `AgUiEventStream` | Iterates, aborts, closes, or renders AG-UI events |
| `AgUiResumeRequest` | Carries only untrusted client decisions into checkpoint resolution |
| `AgUiResumeCheckpoint` | Stable evidence passed to `on_resume_saved` after marker durability |
| `TINKERFIN_HITL_CONTRACT` | Contract declaration for external mixed-cancellation subagents |

`open_agui_run()` owns Agent resolution, asynchronous Graph construction, checkpoint
resolution, failed lifecycle conversion, resume settlement, and stream creation. See
[AG-UI basics](index.md) for its parameters.

## Advanced Runtime APIs

| API | Use |
| --- | --- |
| `DeepAgentDefinition.new_agui(...)` | Creates one AG-UI Runtime |
| `DeepAgentDefinition.prepare_agui_resume(...)` | Resolves client decisions from the canonical Graph checkpoint |
| `DeepAgentAgUiRuntime.astream(graph_input, ...)` | Runs an ordinary Graph request |
| `DeepAgentAgUiResumeRuntime.astream(...)` | Runs a bound resume without caller input |
| `AgUiResumeBinding` | Private framework-resolved resume facts accepted by `new_agui(...)` |

See [Interrupts and resume](interrupts-and-resume.md) for ordinary and advanced resume
boundaries.

## Conversion entry points

| API | Main parameters | When to use it |
| --- | --- | --- |
| `astream_events(...)` | `parts`, `identity`, visibility flags, prior IDs, private state keys | Automatic complete lifecycle around an existing native stream |
| `DeepAgentAgUiAdapter(...)` | `identity`, prior IDs, visibility flags, private state keys | A custom orchestrator owns the main lifecycle |
| `encode_sse(...)` | `event`, optional `event_id` | Render one AG-UI event as SSE |
| `micro_batch(...)` | `events`, optional batcher | Combine adjacent small deltas |

### `DeepAgentAgUiAdapter` methods

| Method | Use |
| --- | --- |
| `process(part)` | Validate and convert one complete native part |
| `finish()` | Close remaining text, reasoning, and tool lifecycles normally |
| `abort()` | Close open child lifecycles; the orchestrator still owns the one main terminal |
| `main_outcome()` | Return success or interrupt outcome |

## `AgUiLifecycleEventFactory`

| Method | Parameters | Result |
| --- | --- | --- |
| `started(...)` | `identity`, optional `parent_run_id` | `RUN_STARTED` with omitted input |
| `finished(...)` | `identity`, `outcome` | `RUN_FINISHED` with canonical identity |
| `failed(...)` | `identity`, `message`, `code`, optional `parent_run_id` | `RUN_ERROR` with canonical identity |
| `is_main_lifecycle(...)` | `event`, `identity` | Whether the event occupies this main lifecycle |
| `event_run_id(event)` | event | Validated run ID when available |
| `validate_identity(...)` | `identity` | Early shared identity validation |

`AgentRunOutcome.type` is `success` or `interrupt`; only the latter carries `interrupts`.

## Resume APIs

| API | Use |
| --- | --- |
| `TinkerFin.open_agui_run(resume=...)` | Restore pending facts and run a managed resume without exposing a binding |
| `AgUiResumeRequest` | Immutable, non-empty, duplicate-free client resume entries |
| `DeepAgentDefinition.prepare_agui_resume(...)` | Advanced path that restores pending facts and returns a binding |
| `AgUiResumeBinding.from_agui(...)` | Advanced path for an integration that owns a complete trusted AG-UI terminal log |
| `AgUiResumeBinding.model_validate(...)` | Restore the complete stable binding JSON model |
| `AgUiResumeBindingError` | High-level binding cannot preserve the supplied resume semantics |
| `ResumeMapper.map(...)` | Map from native interrupts and checkpoint messages |
| `ResumeMapper.map_agui(...)` | Map from trusted persisted AG-UI interrupts |
| `ResumeTranslation` | Holds kind, mode, resume data, cancellations, Tool IDs, sources, and native decisions |
| `ResumeMappingError` | Low-level Adapter data cannot be mapped without losing semantics |

`ResumeTranslation` is the lower-level Adapter result. Ordinary callers pass an
`AgUiResumeRequest` directly to `open_agui_run(resume=...)`; the facade restores native
interrupts and complete messages from the checkpointer. Definition-level binding methods
remain for trusted event-log or custom orchestration and must not consume
client-resubmitted interrupt details.

## Interrupt data

| Model | Fields |
| --- | --- |
| `AgentRuntimeInterrupt` | Non-empty `id`, JSON `value` |
| `RuntimeInterruptEnvelope` | Current `schema`, non-empty `kind`, optional `message`, response JSON Schema, and trusted metadata |
| `HitlActionRequest` | Non-empty `name`, object `args`, optional `description` |
| `HitlReviewConfig` | `actionName`, non-empty `allowedDecisions`, optional `argsSchema` |
| `HitlRequest` | Equally sized, non-empty `actionRequests` and `reviewConfigs` |
| `ToolReviewInterruptMetadata` | Current native group, action position, Tool name, decisions, and original arguments |
| `SubagentProvenance` | Stable invocation ID, full namespaces, graph task, parent Tool, Agent, description, and current request run |

Allowed public decisions are `approve`, `edit`, `reject`, and `respond`. Action and
review entries pair by position. Edited arguments are validated against `argsSchema`
with JSON Schema Draft 2020-12 before translation returns.

`RuntimeInterruptEnvelope` supports non-Tool workflow pauses. It maps `kind` to the
AG-UI interrupt reason. `require_valid_schema(...)` checks Draft 2020-12 contracts before
publication, and `validate_json_schema_instance(...)` validates resolved JSON with format
checking before native translation. The emitting graph retains domain validation.
Extension kinds must be namespaced, and Runtime and Tool interrupts cannot share one
pending batch.

`parse_tool_review_interrupt(interrupt)` validates a complete trusted Tool interrupt
against `tinkerfin.deepagents.tool-review`. `subagent_invocation_id(...)` and
`create_subagent_provenance(...)` implement the fixed
`tinkerfin.subagent-provenance` identity contract.

## IDs and errors

| API | Use |
| --- | --- |
| `ScopedIdCodec.encode(...)` | Builds an ID from kind, full namespace, and raw ID |
| `ScopedIdCodec.decode(...)` | Restores those three components |
| `HitlCorrelationError` | Review action cannot be correlated with tool messages |
| `ToolReviewContractError` | A complete Tool review interrupt violates the current public contract |
| `SseEventId` | String or integer ID accepted by `encode_sse()` |

Parallel tools, subagents, and resumed results require complete scoped IDs. Never correlate them by arrival order.

`ScopedIdCodec.encode()` accepts `message`, `tool`, `reasoning`, or `reasoning-message` as its kind. Namespace must be a tuple of non-empty strings, and the raw ID must be non-empty.
