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

## Plan clarification and review

An Agent created through `TinkerFin().plan(enabled=True)` can pause for two
runtime-owned reasons. Select `mode="plan"` when creating that request's Runtime:

```python
runtime = agent.new_agui(identity=identity, mode="plan")
```

| Reason | Expected resolved payload |
| --- | --- |
| `plan_clarification` | `{"type":"respond","answers":[{"questionId":"...","optionId":"..."}]}` for an option, or `{"type":"respond","answers":[{"questionId":"...","answer":"..."}]}` for free text |
| `plan_review` | `approve`, `edit`, `respond`, or `reject`, each with the current `baseRevision` |

A Plan interrupt has no `toolCallId`. It carries a versioned trusted runtime envelope,
its response JSON Schema, and `tinkerfin.plan-clarification.v1` metadata containing the
complete public Form. Python `allow_free_text` is serialized as `allowFreeText`.
Question and option attributes are preserved as public, non-authoritative planning
context. Internal schema fingerprints remain in checkpoint state and are not part of
any public AG-UI event. The root `tinkerfin_plan` state is
published before the interrupt terminal. On a resumed request, the synchronized
snapshot may be followed by RFC 6902 state deltas as the Plan moves through
`approved` and publishes `effectiveMode=default` before native execution.

Clients must not return the Form, labels, descriptions, or attributes. The Planning Graph
restores the trusted checkpoint Form and derives the selected option label. Supplying
both `optionId` and `answer`, omitting required answers, or using unknown IDs fails the
resume.

The same `ResumeMapper.map_agui(...)` and `AgUiResumeBinding` flow handles Plan
interrupts without a separate API. `ResumeMapper` verifies the persisted envelope and
exact pending coverage; the Planning Graph validates the response contract and rejects a
stale `baseRevision`. Use a new `runId` with the same `threadId` for every resume.

A pending batch cannot mix Plan and Tool interrupts. A Tool review can still occur
later, after Plan approval, and its original scoped Tool ID remains continuous across
that later resume.

Tool reviews carry `metadata.deepagents` with schema
`tinkerfin.deepagents.tool-review.v1`. The required fields are
`nativeInterruptId`, `actionIndex`, `toolName`, `allowedDecisions`, and
`originalArgs`. Hosts should parse the complete trusted interrupt with
`parse_tool_review_interrupt()` and persist it unchanged. Missing fields, unknown
fields, an invalid decision, or disagreement with `metadata.langgraphValue` fails
closed; there is no unversioned metadata shape.

Cancelling a Plan clarification or review abandons that Plan request without fabricating
`reject`. A later ordinary input can use `mode="default"` on the same Plan-capable
Definition and checkpoint thread. Changing the future mode never approves, rejects, or
cancels a pending Tool/Filesystem review.

## Resume entries from the frontend

Each entry corresponds to one pending interrupt:

```json
{
  "interruptId": "interrupt-1",
  "status": "resolved",
  "payload": {"type": "approve"}
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
    entries=resume_entries,
    interrupts=persisted_interrupts,
)

if translation.mode == "command":
    binding = AgUiResumeBinding.from_translation(
        identity=identity,
        translation=translation,
    )
```

`persisted_interrupts` must come from a trusted server event log, not the client payload.
`ResumeMapper.map_agui()` applies the same Tool review v1 parser used at the public
inspection boundary.

Then pass both the binding and its command:

```python
runtime = agent.new_agui(
    identity=identity,
    resume=binding,
)
events = runtime.astream(binding.command)
```

`AgUiResumeBinding` binds the `Identity`, a pure `Command(resume=...)`, and the scoped Tool IDs emitted before the interrupt. The application still validates authorization and the complete HTTP request.

## Resume from native checkpoint data

If you did not persist AG-UI interrupts but can read the current checkpoint:

```python
translation = ResumeMapper().map(
    entries=resume_entries,
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
The same rule applies to Plan review: cancellation does not fabricate a Plan rejection.

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
| State cannot be resumed | Changed `Identity.threadId` or missing checkpointer |
| Action runs twice | Pending interrupts were not claimed atomically or retry data changed |

Next: [Use the converter directly](adapter-extensions.md).
