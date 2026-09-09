# AG-UI basics

[Documentation](../index.md) · [中文](../../cn/agui/index.md)

AG-UI represents Agent text, tool calls, state, approvals, and outcomes as
frontend-friendly events. TinkerFin uses one canonical `RunIdentity` for public
lifecycle events, Graph execution, checkpoints, coordination, and durable delivery.

## Installation

```bash
pip install "tinkerfin[agui]"
pip install langchain-openai
```

The second command installs the provider adapter used below. Replace it when using
another model vendor.

## Your first managed AG-UI run

```python
import asyncio

from tinkerfin import RunIdentity, TinkerFin


tinkerfin = TinkerFin()
agent = tinkerfin.create_deep_agent(
    model="openai:gpt-5.4",
    tools=[],
)


async def main() -> None:
    events = await tinkerfin.open_agui_run(
        RunIdentity(threadId="conversation-1", runId="run-1"),
        agent=agent,
        input={"messages": [{"role": "user", "content": "Hello"}]},
    )
    async for event in events:
        print(event.type)


asyncio.run(main())
```

- `RunIdentity.threadId` is the canonical checkpoint thread.
- `RunIdentity.runId` identifies one semantic run and is reused only for its retry.
- `input` is the explicit Graph input.
- `RUN_STARTED.input` is omitted; the Runtime does not fabricate or duplicate input.
- The returned `AgUiEventStream` is single-use and owns request cancellation and cleanup.

## HTTP input and Graph input

A frontend can send standard `RunAgentInput`. Validate it at the HTTP boundary, then
map only application-approved facts:

| `RunAgentInput` field | Application responsibility | Framework call |
| --- | --- | --- |
| `threadId` / `runId` | Authenticate, authorize, and select one identity | `RunIdentity(...)` |
| `parentRunId` | Authorize a branch or resume source in the same thread | `parent_run_id=...` |
| `state` / `messages` | Validate and map to the concrete Graph state | `input=graph_input` |
| `tools` | Treat as client descriptions, not execution permission | Not automatic |
| `context` | Translate only when explicitly supported | `context=...` |
| `forwardedProps` | Apply product policy such as model or mode | Host-owned |
| `resume` | Carry client decisions only | `resume=AgUiResumeRequest(...)` |

Do not inject complete frontend history into a Graph that already has checkpoint state;
the same message could execute twice.

## Resume

```python
from tinkerfin import AgUiResumeRequest, RunIdentity


events = await tinkerfin.open_agui_run(
    RunIdentity(threadId="conversation-1", runId="run-resume"),
    agent=agent,
    resume=AgUiResumeRequest(entries=tuple(resume_entries)),
    parent_run_id=parent_run_id,
    config=config,
    on_resume_saved=record_checkpoint_idempotently,
    on_resume_not_saved=release_unprepared_claim_idempotently,
)
```

The facade reads the canonical checkpoint and validates complete coverage, native
grouping, decision order, JSON Schema, Tool correlation, cancellation, Runtime Profile,
and subagent provenance. Client input contains no server interrupt payload or native
`Command`. An entirely cancelled batch emits a finite cancelled lifecycle without
invoking the Graph.

The selected Profile writes private lineage and marker facts without changing pending
root, Planning, or subgraph work. `on_resume_saved` runs after the marker is readable and
may receive the same `AgUiResumeCheckpoint` on retry, so it must be idempotent. If setup
fails before any prepared or accepted marker exists, protected settlement invokes
`on_resume_not_saved`; it is never called after durable marker evidence exists.

## `open_agui_run()` parameters

| Parameter | Default | Purpose |
| --- | --- | --- |
| `identity` | required | Canonical thread and run identity |
| `agent` | required | Definition or sync/async callable returning one |
| `input` / `resume` | exactly one | Ordinary Graph input or client decisions |
| `parent_run_id` | `None` | Optional checkpoint branch or resume source |
| `mode` | Definition default | `default` or `plan` |
| `config` / `context` | `None` | Graph configuration and declared Runtime context |
| `stream_timeout` | `None` | Total native-pull deadline |
| `cleanup_timeout` | `None` | Caller wait for protected cleanup |
| `include_reasoning_events` | `False` | Emit verified public reasoning events |
| `include_subagent_events` | `True` | Emit validated subagent events |
| `on_native_part` | `None` | Observe each validated native object |
| `on_agui_event` | `None` | Observe each event before delivery |
| `on_resume_saved` | `None` | Idempotent callback after marker durability |
| `on_resume_not_saved` | `None` | Idempotent settlement only before marker durability |

The reasoning switch never exposes provider-private metadata.

## Advanced integration

`DeepAgentDefinition.new_agui()`, `prepare_agui_resume()`, `AgUiResumeBinding`, and
`TinkerFin.failed_agui_run()` remain available for trusted event-log integrations and
custom orchestration. Those APIs expose lifecycle ordering that `open_agui_run()` owns
for ordinary applications.

The selected Runtime Profile owns required modes, upstream version, subgraph behavior,
and complete state output. Usually omit those options. Supported diagnostic modes may
be added, but conflicting or incomplete upstream options fail before Graph side effects.

`parent_run_id` selects a checkpoint branch; it is not subagent provenance. Missing,
cross-thread, active, failed, ambiguous, self-referential, or improperly resumed parents
fail before Graph execution.

## Next steps

- [Understand AG-UI events](events.md)
- [Interrupts and resume](interrupts-and-resume.md)
- [Use only the adapter](adapter-extensions.md)
- [AG-UI usage reference](api-reference.md)
