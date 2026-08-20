# AG-UI basics

[Documentation](../README.md) · [中文](../../zh/agui/index.md)

AG-UI represents agent text, tool calls, state, approvals, and run outcomes as frontend-friendly events. Framework execution needs only `Identity`; whether an HTTP endpoint accepts a complete `RunAgentInput` is an application choice.

## Your first AG-UI Runtime

```python
import asyncio

from tinkerfin import Identity, TinkerFin


agent = TinkerFin().create_deep_agent(
    model="openai:gpt-5.4",
    tools=[],
)


async def main() -> None:
    identity = Identity(threadId="conversation-1", runId="run-1")
    runtime = agent.new_agui(identity=identity)
    events = runtime.astream(
        {"messages": [{"role": "user", "content": "Hello"}]},
    )

    async for event in events:
        print(event.type)


asyncio.run(main())
```

- `Identity.threadId` becomes the Graph checkpoint thread automatically.
- `Identity.runId` identifies this semantic run.
- The first `runtime.astream(...)` argument is the explicit Graph input.
- Framework-owned `RUN_STARTED.input` is always `None`.

When a frontend sends standard `RunAgentInput`, validate it at the HTTP boundary, then create the `Identity` and Graph input in application code:

| `RunAgentInput` field | Protocol | Application responsibility | Automatically sent to the Graph |
| --- | --- | --- | --- |
| `threadId` | Required | Build `Identity` with `runId`; also identifies the checkpoint thread | Only as `configurable.thread_id` |
| `runId` | Required | Identify this semantic run; use a new value for new input and reuse it for retries | No |
| `parentRunId` | Optional | Persist and interpret run lineage when the application needs it | No |
| `state` | Required | Validate, persist, or translate according to the application's trust boundary | No |
| `messages` | Required | Preserve standard roles, multimodal content, and extension fields, then select this Graph invocation's input | No; complete history is not injected |
| `tools` | Required | Preserve client tool descriptions without granting server execution permission | No |
| `context` | Required | Translate to Graph context only when the application chooses to | No |
| `forwardedProps` | Required | Preserve application extensions such as model or UI mode | No |
| `resume` | Optional | Validate pending interrupts, then translate to `Command(resume=...)` | No |

Do not inject a complete frontend history into a Graph that already has checkpoint state; the same message could execute twice.

## `new_agui()` parameters

| Parameter | Default | Purpose |
| --- | --- | --- |
| `identity` | required | Thread and run identity |
| `on_part` | `None` | Observe each LangGraph v2 part before conversion |
| `timeout` | `None` | Total native-stream deadline |
| `settlement_timeout` | `None` | Caller wait limit for protected cancellation cleanup |
| `expose_reasoning_events` | `False` | Emit supported public reasoning events |
| `expose_subagent_events` | `True` | Emit subagent events |
| `resume` | `None` | Validated `AgUiResumeBinding` |
| `on_event` | `None` | Async observer before each AG-UI event is delivered |

The reasoning switch does not expose private provider metadata.

## Fixed stream settings

AG-UI conversion requires:

```python
stream_mode = ("messages", "tasks", "values")
version = "v2"
subgraphs = True
```

Usually omit them. You may add `updates`, `checkpoints`, `debug`, or `custom`. Missing required modes, v1, or `subgraphs=False` fails before Graph iteration.

## Echo a complete application request when needed

The framework cannot decide which frontend fields are trusted, so it does not copy `state`, `messages`, `tools`, `context`, or `forwardedProps` into the start event. An application may enrich the main start event:

```python
if event.type == "RUN_STARTED" and event.run_id == identity.run_id:
    event = event.model_copy(update={"input": canonical_run_input})
```

Use a validated application snapshot—such as one with server-assigned message IDs—not raw untrusted JSON.

## Next steps

- [Understand AG-UI events](events.md)
- [Interrupts and resume](interrupts-and-resume.md)
- [Use only the adapter](adapter-extensions.md)
- [AG-UI usage reference](api-reference.md)
