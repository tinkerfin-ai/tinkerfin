# AG-UI usage reference

[AG-UI basics](index.md) · [中文](../../zh/agui/api-reference.md)

## High-level Runtime APIs

| API | Use |
| --- | --- |
| `DeepAgentDefinition.new_agui(...)` | Creates one AG-UI Runtime |
| `DeepAgentAgUiRuntime.astream(...)` | Runs the Graph and returns `AgUiEventStream` |
| `AgUiEventStream` | Iterates, aborts, closes, or renders AG-UI events |
| `AgUiResumeBinding` | Binds a resume request to its native command and previous tool IDs |

See [AG-UI basics](index.md) for Runtime parameters and [Interrupts and resume](interrupts-and-resume.md) for binding use.

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
| `abort(code=...)` | Close open child lifecycles after failure or cancellation |
| `main_outcome()` | Return success or interrupt outcome |

## `AgUiLifecycleEventFactory`

| Method | Parameters | Result |
| --- | --- | --- |
| `started(...)` | `identity` | `RUN_STARTED` with `input=None` |
| `finished(...)` | `identity`, `outcome` | `RUN_FINISHED` |
| `failed(...)` | `identity`, `message`, `code` | `RUN_ERROR` |
| `is_main_lifecycle(...)` | `event`, `identity` | Whether the event occupies this main lifecycle |
| `event_run_id(event)` | event | Validated run ID when available |
| `validate_identity(...)` | `identity` | Early shared identity validation |

`AgentRunOutcome.type` is `success` or `interrupt`; only the latter carries `interrupts`.

## Resume APIs

| API | Use |
| --- | --- |
| `ResumeMapper.map(...)` | Map from native interrupts and checkpoint messages |
| `ResumeMapper.map_agui(...)` | Map from trusted persisted AG-UI interrupts |
| `ResumeTranslation` | Holds mode, resume data, cancelled IDs, prior tool IDs, and per-interrupt decisions |
| `ResumeMappingError` | Resume data cannot be mapped without losing semantics |
| `ResumeMappingFailure` | Stable failure category |

`ResumeTranslation.mode` is `command`, `abandon`, or `custom`.

## Interrupt data

| Model | Fields |
| --- | --- |
| `AgentRuntimeInterrupt` | Non-empty `id`, JSON `value` |
| `RuntimeInterruptEnvelope` | Versioned `schema`, non-empty `kind`, optional `message`, response JSON Schema, and trusted metadata |
| `HitlActionRequest` | Non-empty `name`, object `args`, optional `description` |
| `HitlReviewConfig` | `actionName`, non-empty `allowedDecisions`, optional `argsSchema` |
| `HitlRequest` | Equally sized, non-empty `actionRequests` and `reviewConfigs` |
| `ToolReviewInterruptMetadata` | Versioned native group, action position, Tool name, decisions, and original arguments |
| `SubagentProvenance` | Stable invocation ID, full namespaces, graph task, parent Tool, Agent, description, and current request run |
| `ToolResultCorrelation` | Original scoped Tool and parent-message IDs for a parent-completed child Tool |

Allowed decisions are `approve`, `edit`, `reject`, and `respond`. Action and review entries pair by position.

`RuntimeInterruptEnvelope` supports non-Tool workflow pauses. It maps `kind` to the
AG-UI interrupt reason and leaves domain validation of the resolved JSON object to the
emitting graph. Runtime and Tool interrupts cannot share one pending batch.

`parse_tool_review_interrupt(interrupt)` validates a complete trusted Tool interrupt
against `tinkerfin.deepagents.tool-review.v1`. `subagent_invocation_id(...)` and
`create_subagent_provenance(...)` implement the fixed
`tinkerfin.subagent-provenance.v1` identity contract.

## IDs and errors

| API | Use |
| --- | --- |
| `ScopedIdCodec.encode(...)` | Builds an ID from kind, full namespace, and raw ID |
| `ScopedIdCodec.decode(...)` | Restores those three components |
| `HitlCorrelationError` | Review action cannot be correlated with tool messages |
| `ToolReviewContractError` | A complete Tool review interrupt violates the versioned public contract |
| `SseEventId` | String or integer ID accepted by `encode_sse()` |

Parallel tools, subagents, and resumed results require complete scoped IDs. Never correlate them by arrival order.

`ScopedIdCodec.encode()` accepts `message`, `tool`, `reasoning`, or `reasoning-message` as its kind. Namespace must be a tuple of non-empty strings, and the raw ID must be non-empty.
