# Runtime API

[Runtime](index.md) · [中文](../../cn/runtime/api-reference.md)

AG-UI entry points require `pip install "tinkerfin[agui]"`.

## Builder

| API | Purpose |
| --- | --- |
| `TinkerFin(checkpointer=..., run_coordinator=..., state_schema=..., runtime_profile=...)` | Configure shared borrowed resources |
| `.with_namespace(namespace)` | Select the application's isolation scope; required before `build()` |
| `.with_observer(observer)` | Add a Runtime observer |
| `.with_observer(on_terminal=callback)` | Add one asynchronous terminal callback |
| `.with_plan(...)` | Enable Plan and choose its model, forms, content, and review actions |
| `.with_attachments(support)` | Enable host-authorized attachment resolution |
| `.build(model, tools, ...)` | Capture agent configuration and return `AgentRuntime` |

Configuration methods return independent builders. `build()` performs no execution I/O
and borrows supplied resources.

### Main build parameters

| Parameter | Purpose |
| --- | --- |
| `model`, `system_prompt` | Model and instructions |
| `tools`, `middleware` | Agent tools and middleware |
| `subagents` | Declarative, compiled, or remote subagents |
| `skills`, `memory` | Skill directories and memory files |
| `backend`, `permissions` | Filesystem backend or lazy workspace and its file rules |
| `prepare_tools` | Create additional tools after per-run preparation |
| `interrupt_on` | Require decisions before selected tools execute |
| `response_format` | Structured output contract |
| `state_schema`, `context_schema` | Persistent state fields and invocation context type |
| `checkpointer`, `store`, `cache` | Persistence and Graph cache |

`context_schema` determines the type of each execution method's `context` argument.

## AgentRuntime

| API | Result |
| --- | --- |
| `runtime.namespace` | Namespace fixed when the Runtime was built |
| `runtime.thread_identity(thread_id)` | Complete namespaced thread identity |
| `runtime.run_identity(thread_id, run_id)` | Complete namespaced run identity |
| `await runtime.ainvoke(...)` | Final defensive state mapping |
| `runtime.open_run(...)` | Lazy `NativeRunStream` |
| `runtime.open_agui_run(...)` | Lazy `AgUiRunStream` |

All execution methods accept `thread_id`, `run_id`, `input` or AG-UI input, optional
`mode`, Graph `config`, typed `context`, observation callbacks, and supported LangGraph
stream controls.

`open_agui_run()` accepts exactly one of:

| Input | Use |
| --- | --- |
| `messages` | Standard AG-UI user messages with final unique IDs |
| `input` | Advanced native agent state |
| `resume` | `AgUiResumeRequest` for all pending interrupts |

Resume-only callbacks are `on_resume_saved` and `on_resume_not_saved`. They settle host
state around the durable resume marker and are awaited.

## Streams

| API | Purpose |
| --- | --- |
| `NativeRunStream` | Iterate validated native objects; `to_sse()` uses canonical replay values |
| `AgUiRunStream` | Iterate AG-UI events; `abort()` returns the remaining cancellation events |
| `stream.aclose()` | Finish owned cleanup; safe to await again after a cleanup timeout |
| `stream.to_sse()` | Return a single-use `SseBody[bytes]` of UTF-8 SSE frames |
| `SseBody.prepare(preflight=...)` | Run checks before the response starts |

`AgUiSettlementTimeoutError` means the stream still owns cleanup. Await `aclose()` again
before closing shared resources.

`NativeStreamPart` is the finite native replay value. Its Python field
`graph_namespace` identifies Graph position; its third-party wire alias remains `ns`.
Business isolation always comes from `RunIdentity.namespace`.

## Public modules

Common Runtime types, streams, SSE helpers, and errors are exported from `tinkerfin`.
Extension contracts use focused modules:

| Module | API family |
| --- | --- |
| `tinkerfin.runtime_profile` | Deep Agents Runtime profiles |
| `tinkerfin.native_driver` | Native stream drivers and reasoning extractors |
| `tinkerfin.coordination` | Run coordinator protocol and in-memory implementation |
| `tinkerfin.redis` | Redis coordinator, leases, and Redis lease errors |
| `tinkerfin.plan` | Plan forms, content models, decisions, and Plan errors |
| `tinkerfin.deep_agent` | Caller-managed Graph construction |

The application opens and closes models, checkpointers, Stores, caches, database pools,
and supplied service clients. Managed runs close the Graph, prepared workspace, streams,
and per-run resources they create.
