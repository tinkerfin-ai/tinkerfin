# AG-UI basics

[Documentation](../README.md) · [中文](../../zh/agui/index.md)

AG-UI represents agent text, tool calls, state, approvals, and outcomes as
frontend-friendly events. TinkerFin uses one canonical `Identity` for public lifecycle
events, Graph execution, checkpoints, coordination, and durable delivery.

## Your first AG-UI Runtime

```python
import asyncio

from tinkerfin import Identity, TinkerFin


agent = TinkerFin().create_deep_agent(
    model="openai:gpt-5.4",
    tools=[],
)


async def main() -> None:
    runtime = agent.new_agui(
        identity=Identity(threadId="conversation-1", runId="run-1"),
    )
    events = runtime.astream(
        {"messages": [{"role": "user", "content": "Hello"}]},
    )
    async for event in events:
        print(event.type)


asyncio.run(main())
```

- `Identity.threadId` is the canonical thread used by every runtime and durable boundary.
- `Identity.runId` identifies one semantic run and is reused only for an idempotent retry.
- The first `runtime.astream(...)` argument is the explicit Graph input.
- `RUN_STARTED.input` is omitted; the Runtime does not fabricate or duplicate Graph input.

## HTTP input and Graph input

A frontend can still send standard `RunAgentInput`. Validate it at the HTTP boundary,
then map only the application-approved facts:

| `RunAgentInput` field | Application responsibility | Framework call |
| --- | --- | --- |
| `threadId` / `runId` | Authenticate, authorize, and select one canonical identity | `Identity(...)` |
| `parentRunId` | Authorize a branch or resume source in the same thread | `parent_run_id=...` |
| `state` / `messages` | Validate and map to the concrete Graph state schema | `astream(graph_input)` |
| `tools` | Treat as client descriptions, not server execution permission | Not automatic |
| `context` | Translate only when the host explicitly supports it | Graph `context=...` |
| `forwardedProps` | Apply product policy such as model or mode selection | Host-owned |
| `resume` | Pair with trusted server-persisted interrupts | `AgUiResumeBinding.from_agui(...)` |

Do not inject a complete frontend history into a Graph that already has checkpoint state;
the same message could execute twice.

## Resume

```python
from tinkerfin import AgUiResumeBinding, Identity


binding = AgUiResumeBinding.from_agui(
    entries=resume_entries,
    interrupts=trusted_persisted_interrupts,
)
runtime = agent.new_agui(
    identity=Identity(threadId="conversation-1", runId="run-resume"),
    parent_run_id=parent_run_id,
    resume=binding,
    on_resume_checkpointed=record_checkpoint_idempotently,
)
events = runtime.astream(config=config)
```

The binding validates complete coverage, native grouping, decision order, JSON Schema,
Tool correlation, cancellation, and subagent provenance. It has a stable JSON round trip
but owns no identity, parent, Graph, or I/O resource. Native `Command` construction stays
inside the Runtime. Entirely cancelled batches emit a finite cancelled lifecycle without
invoking a Graph.

`on_resume_checkpointed` runs after the private marker is readable and before resumed
native output. A retry can deliver the same `AgUiResumeCheckpoint` again, so the callback
must be idempotent. It does not mean a Tool or run has completed.

## `new_agui()` parameters

| Parameter | Default | Purpose |
| --- | --- | --- |
| `identity` | required | Canonical thread and run identity |
| `parent_run_id` | `None` | Optional checkpoint branch or resume source |
| `mode` | Definition default | `default` or `plan` for this Runtime only |
| `on_part` | `None` | Observe each validated LangGraph v2 part |
| `timeout` | `None` | Total native-stream deadline |
| `settlement_timeout` | `None` | Caller wait limit for protected stream cleanup |
| `expose_reasoning_events` | `False` | Emit supported public reasoning events |
| `expose_subagent_events` | `True` | Emit validated subagent events |
| `resume` | `None` | Validated `AgUiResumeBinding` |
| `on_resume_checkpointed` | `None` | Idempotent callback after durable resume acceptance |
| `on_event` | `None` | Async observer before each AG-UI event is delivered |

The reasoning switch never exposes private provider metadata.

## Fixed stream settings

AG-UI conversion requires `messages/tasks/values`, `version="v2"`, and
`subgraphs=True`. Usually omit them. You may add `updates`, `checkpoints`, `debug`, or
`custom`. Missing required modes, v1, or `subgraphs=False` fails before Graph iteration.

`parent_run_id` is not subagent provenance. It selects the unique valid checkpoint leaf
for that run in the same canonical thread. Missing, cross-thread, active, failed,
ambiguous, self-referential, and interrupted-without-matching-resume parents fail before
Graph execution.

## Next steps

- [Understand AG-UI events](events.md)
- [Interrupts and resume](interrupts-and-resume.md)
- [Use only the adapter](adapter-extensions.md)
- [AG-UI usage reference](api-reference.md)
