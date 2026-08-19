# AG-UI basics

[Documentation](../README.md) · [中文](../../zh/agui/index.md)

AG-UI represents a live agent run as events a frontend can understand. The UI can render text, tool calls, state changes, approvals, and final outcomes without knowing LangGraph message classes.

## When to use it

- Stream an answer into a chat interface;
- show tool names, arguments, and results;
- ask a user to approve sensitive work;
- display root-agent and subagent activity through one protocol.

If no frontend consumes AG-UI, the [native Runtime](../runtime/index.md) is simpler.

## Your first AG-UI Runtime

```python
import asyncio

from ag_ui.core import RunAgentInput
from tinkerfin import TinkerFin


tinkerfin = TinkerFin()
agent = tinkerfin.create_deep_agent(
    model="openai:gpt-5.4",
    tools=[],
)


async def main() -> None:
    run_input = RunAgentInput.model_validate(
        {
            "threadId": "conversation-1",
            "runId": "run-1",
            "state": {},
            "messages": [],
            "tools": [],
            "context": [],
            "forwardedProps": {},
        }
    )

    runtime = agent.new_agui(run_input=run_input)
    events = runtime.astream(
        {"messages": [{"role": "user", "content": "Hello"}]},
        {"configurable": {"thread_id": "conversation-1"}},
    )

    async for event in events:
        print(event.type)


asyncio.run(main())
```

`run_input` is the complete frontend request. The first argument to `runtime.astream(...)` is still the Graph input. They serve different purposes.

## `RunAgentInput` fields

| Field | Required | Purpose |
| --- | --- | --- |
| `threadId` | yes | Conversation ID; normally matches Graph `thread_id` |
| `runId` | yes | This run's ID; use a new one for each new run |
| `parentRunId` | no | Caller-defined run lineage, not LangGraph subgraph nesting |
| `state` | yes | State supplied by the frontend |
| `messages` | yes | AG-UI message history |
| `tools` | yes | Tool descriptions supplied by the frontend |
| `context` | yes | Frontend context entries |
| `forwardedProps` | yes | Application-specific forwarded properties |
| `resume` | no | Decisions for pending interrupts |

Supply the complete shape even when lists are empty. `threadId` and `runId` must be non-empty and have no surrounding whitespace.

## Runtime parameters

```python
runtime = agent.new_agui(
    run_input=run_input,
    principal=None,
    on_part=None,
    timeout=None,
    settlement_timeout=None,
    expose_reasoning_events=False,
    expose_subagent_events=True,
    resume=None,
    on_event=None,
)
```

| Parameter | Default | Purpose |
| --- | --- | --- |
| `run_input` | required | Complete AG-UI request |
| `principal` | `None` | Business identity used by a coordinator |
| `on_part` | `None` | Observes native LangGraph parts before conversion |
| `timeout` | `None` | Total time limit for the native AG-UI stream |
| `settlement_timeout` | `None` | Maximum caller wait for protected cleanup after cancellation |
| `expose_reasoning_events` | `False` | Emits supported public reasoning events |
| `expose_subagent_events` | `True` | Delivers public subagent events |
| `resume` | `None` | `AgUiResumeBinding` for a resume request |
| `on_event` | `None` | Observes every AG-UI event before delivery |

Enabling reasoning does not expose raw provider-private data. Private provider metadata is still removed from public payloads.

## Stream settings used automatically

AG-UI needs messages, tasks, and state together, so the Runtime uses:

```python
stream_mode = ("messages", "tasks", "values")
version = "v2"
subgraphs = True
```

You can omit these values from `runtime.astream(...)`. Explicit values must satisfy the same contract. Additional supported modes are `updates`, `checkpoints`, `debug`, and `custom`.

## Next steps

- [Understand AG-UI events](events.md)
- [Interrupts and resume](interrupts-and-resume.md)
- [Use the converter directly](adapter-extensions.md)
- [AG-UI usage reference](api-reference.md)
