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
| `astream_events(...)` | `parts`, `identity`, visibility flags, prior IDs | Automatic complete lifecycle around an existing native stream |
| `DeepAgentAgUiAdapter(...)` | `identity`, prior IDs, visibility flags | A custom orchestrator owns the main lifecycle |
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
| `HitlActionRequest` | Non-empty `name`, object `args`, optional `description` |
| `HitlReviewConfig` | `actionName`, non-empty `allowedDecisions`, optional `argsSchema` |
| `HitlRequest` | Equally sized, non-empty `actionRequests` and `reviewConfigs` |

Allowed decisions are `approve`, `edit`, `reject`, and `respond`. Action and review entries pair by position.

## IDs and errors

| API | Use |
| --- | --- |
| `ScopedIdCodec.encode(...)` | Builds an ID from kind, full namespace, and raw ID |
| `ScopedIdCodec.decode(...)` | Restores those three components |
| `InterruptCorrelationError` | Native interrupt cannot be correlated safely |
| `HitlCorrelationError` | Review action cannot be correlated with tool messages |
| `SseEventId` | String or integer ID accepted by `encode_sse()` |

Parallel tools, subagents, and resumed results require complete scoped IDs. Never correlate them by arrival order.

`ScopedIdCodec.encode()` accepts `message`, `tool`, `reasoning`, or `reasoning-message` as its kind. Namespace must be a tuple of non-empty strings, and the raw ID must be non-empty.
