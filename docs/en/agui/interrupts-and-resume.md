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
| `tinkerfin:plan_clarification` | `{"type":"respond","answers":[{"questionId":"...","optionId":"..."}]}` for an option, `{"type":"respond","answers":[{"questionId":"...","answer":"..."}]}` for free text, or `{"type":"respond","answers":[{"questionId":"...","skipped":true}]}` for an optional skip |
| `tinkerfin:plan_review` | One decision permitted by the interrupt response Schema, with the current `baseRevision`; the default is `approve`, `respond`, or `reject`, while `edit` requires explicit host configuration |

A Plan interrupt has no `toolCallId`. It carries a versioned trusted runtime envelope,
its response JSON Schema, and `tinkerfin.plan-clarification.v2` metadata containing the
complete public Form with `schemaVersion: 2`. Python `allow_free_text` is serialized as
`allowFreeText`; every question also carries an explicit `required` flag.
Question and option attributes are preserved as public, non-authoritative planning
context. Internal schema fingerprints remain in checkpoint state and are not part of
any public AG-UI event. The root `tinkerfin_plan` state is
published before the interrupt terminal. On a resumed request, the synchronized
snapshot may be followed by RFC 6902 state deltas as the Plan moves through
`approved` and publishes `effectiveMode=default` before native execution.

Clients must not return the Form, labels, descriptions, or attributes. The Planning Graph
restores the trusted checkpoint Form and derives the selected option label. Resume must
cover every question exactly once. Supplying mixed answer fields, skipping a required
question, omitting a question result, or using unknown IDs fails the resume.

The same `AgUiResumeBinding.from_agui(...)` flow handles Plan interrupts without a
separate API. The binding verifies the persisted envelope and exact pending coverage;
the Planning Graph validates the response contract and rejects a stale `baseRevision`.
Use a new `runId` with the same `threadId` for every resume.

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


binding = AgUiResumeBinding.from_agui(
    entries=resume_entries,
    interrupts=persisted_interrupts,
)
```

`persisted_interrupts` must come from a trusted server event log, not the client payload.
`from_agui()` applies the same Tool review v1 parser used at the public inspection
boundary and returns a binding for full resume, mixed cancellation, or abandonment.

Pass the binding once. The Runtime owns native command construction:

```python
runtime = agent.new_agui(
    identity=identity,
    parent_run_id=parent_run_id,
    resume=binding,
    on_resume_checkpointed=record_checkpoint_idempotently,
)
events = runtime.astream(config=config)
```

`AgUiResumeBinding` stores native interrupt groups, scoped Tool IDs, cancellation, and
verified subagent sources. It has a stable JSON round trip but stores no identity or
parent and exposes no `Command`. The application still validates authorization and the
complete HTTP request.

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
This is an adapter-level inspection path. The high-level Runtime uses persisted AG-UI
interrupts with `AgUiResumeBinding.from_agui(...)` so callers do not translate or pass a
native command.

## Three resume modes

| Mode | Meaning | Runtime action |
| --- | --- | --- |
| fully resolved | All entries map to native resume data | Checkpoint once and continue the Graph |
| all cancelled | Every entry was abandoned | Emit a finite cancelled lifecycle without invoking the Graph |
| mixed | Resolved and cancelled entries share a Tool batch | Execute resolved Tools and settle cancelled slots without executing them |

Cancellation is never converted to rejection. In a mixed Tool batch, TinkerFin executes
resolved calls and creates a deterministic, non-executed error `ToolMessage` for each
cancelled call. Main, general-purpose, declarative subagents, and permission-generated
reviews receive the same adapter. Mixed generic runtime interrupts are not Tool decisions
and are rejected by `AgUiResumeBinding`.

The framework checkpoints the native resume and a private marker atomically. It invokes
`on_resume_checkpointed` only after that marker is readable and before exposing the first
resumed native event. A retry may receive the same `AgUiResumeCheckpoint`; consume it
idempotently. Marker retry and `None` continuation remain internal and never require the
caller to pass a second input.

## Retry and concurrency

- Use a new `runId` for the resume request and keep the same `threadId`;
- atomically claim the complete pending set in application storage;
- persist the original AG-UI entries and complete trusted interrupts, or the binding's
  complete stable JSON model; never persist selected internal fields separately;
- resolve application approvals only from `on_resume_checkpointed`, never from
  `RUN_STARTED`;
- preserve original tool IDs when results continue after resume.

## Common errors

| Error | Likely cause |
| --- | --- |
| `AgUiResumeBindingError` | Unknown ID, incomplete coverage, disallowed decision, invalid Schema payload, or missing Tool evidence |
| `ValueError` | Invalid binding mode, unsupported mixed runtime cancellation, or incomplete scoped Tool ID |
| State cannot be resumed | Changed `Identity.threadId` or missing checkpointer |
| Action runs twice | Pending interrupts were not claimed atomically or retry data changed |

Next: [Use the converter directly](adapter-extensions.md).
