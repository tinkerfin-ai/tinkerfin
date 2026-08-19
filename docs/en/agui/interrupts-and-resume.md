# Interrupts and resume

[Understand AG-UI events](events.md) · [中文](../../zh/agui/interrupts-and-resume.md)

An agent can pause before a sensitive tool action and return an interrupt. After the user decides, resume the same checkpointed thread.

## Require approval for a tool

```python
from langgraph.checkpoint.memory import MemorySaver
from tinkerfin import TinkerFin


agent = TinkerFin().create_deep_agent(
    model="openai:gpt-5.4",
    tools=[delete_order],
    interrupt_on={
        "delete_order": {
            "allowed_decisions": ["approve", "reject"],
        }
    },
    checkpointer=MemorySaver(),
)
```

Without a checkpointer there is no reliable paused Graph state to resume.

## Resume entries from the frontend

Each entry corresponds to one pending interrupt:

```json
{
  "interruptId": "interrupt-1",
  "status": "resolved",
  "payload": {"decision": "approve"}
}
```

| Field | Required | Purpose |
| --- | --- | --- |
| `interruptId` | yes | ID from the previous terminal event |
| `status` | yes | `resolved` for a decision or `cancelled` to abandon it |
| `payload` | no | Approval, edit, rejection, or response data |

The server should load the complete current pending set and require exact coverage. Never trust interrupt details resubmitted by the client as correlation evidence.

## Resume from persisted AG-UI interrupts

When the server saved the complete interrupts emitted by the earlier terminal, reuse their verified tool correlation:

```python
from tinkerfin import AgUiResumeBinding
from tinkerfin_agui_adapter import ResumeMapper


translation = ResumeMapper().map_agui(
    entries=run_input.resume or (),
    interrupts=persisted_interrupts,
)

if translation.mode == "command":
    binding = AgUiResumeBinding.from_translation(
        run_input=run_input,
        translation=translation,
    )
```

`persisted_interrupts` must come from a trusted server event log, not the client payload.

Then pass both the binding and its command:

```python
runtime = agent.new_agui(
    run_input=run_input,
    resume=binding,
)
events = runtime.astream(
    binding.command,
    {"configurable": {"thread_id": run_input.thread_id}},
)
```

The binding verifies that the complete request, native resume command, and earlier tool IDs belong together.

## Resume from native checkpoint data

If you did not persist AG-UI interrupts but can read the current checkpoint:

```python
translation = ResumeMapper().map(
    entries=run_input.resume or (),
    interrupts=pending_interrupts,
    messages_by_namespace=messages_by_namespace,
)
```

Resolved reviews need complete messages for safe tool correlation. Do not match parallel or repeated tool names by arrival order.

## Three translation modes

| Mode | Meaning | Application action |
| --- | --- | --- |
| `command` | All decisions map to a native resume command | Create a binding and continue the Graph |
| `abandon` | All entries were cancelled | End the business flow without fabricating rejection |
| `custom` | Resolved and cancelled decisions are mixed | Handle explicitly; do not force a lossy native resume |

Cancellation means abandoning this resume attempt. It is not a tool rejection.

## Retry and concurrency

- Use a new `runId` for the resume request and keep the same `threadId`;
- atomically claim the complete pending set in application storage;
- persist translated resume data and full tool IDs for idempotent retries;
- preserve original tool IDs when results continue after resume.

## Common errors

| Error | Likely cause |
| --- | --- |
| `ResumeMappingError` | Unknown ID, incomplete coverage, disallowed decision, or missing tool evidence |
| `ValueError` | Empty resume, impure command, or incomplete scoped tool ID |
| State cannot be resumed | Changed `thread_id` or missing checkpointer |
| Action runs twice | Pending interrupts were not claimed atomically or retry data changed |

Next: [Use the converter directly](adapter-extensions.md).

