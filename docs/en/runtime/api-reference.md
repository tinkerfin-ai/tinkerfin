# Runtime usage reference

[Runtime basics](index.md) · [中文](../../zh/runtime/api-reference.md)

This page groups the public Runtime capabilities by how you use them. Most applications only need the first two sections.

## Entry points

| API | When to use it | Main input or result |
| --- | --- | --- |
| `TinkerFin(run_coordinator=None)` | Create the main entry point | Optional shared coordinator |
| `TinkerFin.create_deep_agent(...)` | Create a reusable agent definition | See [Create and run a Deep Agent](deep-agents.md) |
| `TinkerFin.run(...)` | Run a custom async source | `source_factory`, `principal`, `on_part` |
| `DeepAgentDefinition.new(...)` | Create a native Runtime | `principal`, `on_part` |
| `DeepAgentDefinition.new_agui(...)` | Create an AG-UI Runtime | See [AG-UI basics](../agui/index.md) |

Reuse `DeepAgentDefinition`. Treat `DeepAgentRuntime`, `DeepAgentAgUiRuntime`, `TinkerFinRun`, and `NativeTinkerFinRun` as single-use values returned by the entry points rather than constructing them directly.

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

## Resume and errors

| API | When it appears |
| --- | --- |
| `AgUiResumeBinding` | Resuming an interrupted AG-UI run |
| `AgUiSettlementTimeoutError` | Caller wait ended before protected Runtime cleanup settled |
| `AgUiNativeStreamConfigurationError` | Native options do not satisfy the AG-UI profile |

`TinkerFinRun.astream_agui(...)` and `NativeTinkerFinRun.astream_agui(...)` convert lower-level sources to AG-UI.

| Parameter | Default | Purpose |
| --- | --- | --- |
| `run_input` | required | Complete AG-UI request |
| `timeout` | `None` | Overall native-stream wait limit |
| `settlement_timeout` | `None` | Caller wait limit for protected cleanup |
| `expose_reasoning_events` | `False` | Deliver supported reasoning events |
| `expose_subagent_events` | `True` | Deliver subagent events |
| `prior_tool_call_ids` | `frozenset()` | Complete scoped tool IDs emitted before resume |
| `on_event` | `None` | Observer called before AG-UI event delivery |

`AgUiResumeBinding.from_translation(...)` creates a binding; `validate_run_input(...)` and `validate_command(...)` let a custom host check it early. Deep Agents callers normally use `new_agui()` instead.

See [Interrupts and resume](../agui/interrupts-and-resume.md) for the complete resume flow.
