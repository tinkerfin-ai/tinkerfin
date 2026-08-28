# AG-UI basics

[Documentation](../README.md) · [中文](../../zh/agui/index.md)

AG-UI represents agent text, tool calls, state, approvals, and outcomes as
frontend-friendly events. TinkerFin uses one canonical `RunIdentity` for public lifecycle
events, Graph execution, checkpoints, coordination, and durable delivery.

## Installation

```bash
pip install "tinkerfin[agui]"
pip install langchain-openai
```

The second command installs the provider adapter used by the example; replace it when
using another model vendor.

## Your first AG-UI Runtime

```python
import asyncio

from tinkerfin import RunIdentity, TinkerFin


agent = TinkerFin().create_deep_agent(
    model="openai:gpt-5.4",
    tools=[],
)


async def main() -> None:
    runtime = agent.new_agui(
        identity=RunIdentity(threadId="conversation-1", runId="run-1"),
    )
    events = runtime.astream(
        {"messages": [{"role": "user", "content": "Hello"}]},
    )
    async for event in events:
        print(event.type)


asyncio.run(main())
```

- `RunIdentity.threadId` is the canonical thread used by every runtime and durable boundary.
- `RunIdentity.runId` identifies one semantic run and is reused only for an idempotent retry.
- The first `runtime.astream(...)` argument is the explicit Graph input.
- `RUN_STARTED.input` is omitted; the Runtime does not fabricate or duplicate Graph input.

## HTTP input and Graph input

A frontend can still send standard `RunAgentInput`. Validate it at the HTTP boundary,
then map only the application-approved facts:

| `RunAgentInput` field | Application responsibility | Framework call |
| --- | --- | --- |
| `threadId` / `runId` | Authenticate, authorize, and select one canonical identity | `RunIdentity(...)` |
| `parentRunId` | Authorize a branch or resume source in the same thread | `parent_run_id=...` |
| `state` / `messages` | Validate and map to the concrete Graph state schema | `astream(graph_input)` |
| `tools` | Treat as client descriptions, not server execution permission | Not automatic |
| `context` | Translate only when the host explicitly supports it | Graph `context=...` |
| `forwardedProps` | Apply product policy such as model or mode selection | Host-owned |
| `resume` | Carry client decisions only | `AgUiResumeRequest(entries=...)` |

Do not inject a complete frontend history into a Graph that already has checkpoint state;
the same message could execute twice.

## Resume

```python
from tinkerfin import AgUiResumeRequest, RunIdentity


resume_identity = RunIdentity(threadId="conversation-1", runId="run-resume")
binding = await agent.prepare_agui_resume(
    identity=resume_identity,
    parent_run_id=parent_run_id,
    request=AgUiResumeRequest(entries=tuple(resume_entries)),
)
runtime = agent.new_agui(
    identity=resume_identity,
    parent_run_id=parent_run_id,
    resume=binding,
    on_resume_checkpointed=record_checkpoint_idempotently,
    on_resume_initialization_failed=release_unprepared_claim_idempotently,
)
events = runtime.astream(config=config)
```

The Definition reads the canonical checkpoint and validates complete coverage, native
grouping, decision order, JSON Schema, Tool correlation, cancellation, Runtime Profile,
and subagent provenance before returning the private binding. The client request contains
no server interrupt payload or native `Command`. Entirely cancelled batches emit a finite
cancelled lifecycle without invoking a Graph.

The selected Profile first writes private lineage and marker values without changing the
interrupted Graph's pending work. `on_resume_checkpointed` runs after those values are
readable and before resumed native output. A prepared retry can deliver the same
`AgUiResumeCheckpoint` again, so the callback must be idempotent. It does not mean a Tool
or run has completed.
If the Runtime fails, is cancelled, or closes before that marker becomes readable,
`on_resume_initialization_failed` runs from protected settlement so the host can release
its claim. It never runs after prepared or accepted marker evidence exists.

## `new_agui()` parameters

| Parameter | Default | Purpose |
| --- | --- | --- |
| `identity` | required | Canonical thread and run identity |
| `parent_run_id` | `None` | Optional checkpoint branch or resume source |
| `mode` | Definition default | `default` or `plan` for this Runtime only |
| `on_part` | `None` | Observe each validated upstream Native object |
| `timeout` | `None` | Total native-stream deadline |
| `settlement_timeout` | `None` | Caller wait limit for protected stream cleanup |
| `expose_reasoning_events` | `False` | Emit supported public reasoning events |
| `expose_subagent_events` | `True` | Emit validated subagent events |
| `resume` | `None` | Validated `AgUiResumeBinding` |
| `on_resume_checkpointed` | `None` | Idempotent callback after the exact resume-intent marker is readable |
| `on_resume_initialization_failed` | `None` | Idempotent host settlement only before marker durability |
| `on_event` | `None` | Async observer before each AG-UI event is delivered |

The reasoning switch never exposes private provider metadata.

## Profile-owned stream settings

The selected Runtime Profile owns required semantic modes, the upstream version,
subgraph behavior, and complete state output. Usually omit those options. The built-in
Profile allows supported diagnostic modes to be added; removing required semantics or
supplying a conflicting upstream option fails before Graph iteration. AG-UI conversion
consumes the same canonical frame already produced for Runtime Observation.

`parent_run_id` is not subagent provenance. It selects the unique valid checkpoint leaf
for that run in the same canonical thread. Missing, cross-thread, active, failed,
ambiguous, self-referential, and interrupted-without-matching-resume parents fail before
Graph execution.

## Next steps

- [Understand AG-UI events](events.md)
- [Interrupts and resume](interrupts-and-resume.md)
- [Use only the adapter](adapter-extensions.md)
- [AG-UI usage reference](api-reference.md)
