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
| `tinkerfin:plan_clarification` | `{"type":"respond","answers":{"question-id":{"status":"answered","answerType":"single_choice","optionId":"option-id"}}}`; each question value follows the exact interrupt response Schema, while an optional skip is `{"status":"skipped"}` |
| `tinkerfin:plan_review` | One decision permitted by the interrupt response Schema, with the current `baseRevision`; the default is `approve`, `respond`, or `reject`, while `edit` requires explicit host configuration |

A Plan interrupt has no `toolCallId`. It carries a declared trusted runtime envelope,
its exact response JSON Schema, and Plan clarification metadata containing the complete
public Form. Every question carries `answerType` and
an explicit `required` flag. Built-in answer types are `single_choice`,
`multiple_choice`, `text`, and `date`.
Question and option attributes are preserved as public, non-authoritative planning
context. Internal schema fingerprints remain in checkpoint state and are not part of
any public AG-UI event. The root `tinkerfin_plan` state is
published before the interrupt terminal. On a resumed request, the synchronized
snapshot may be followed by RFC 6902 state deltas as the Plan moves through
`approved` and publishes `effectiveMode=default` before native execution.

Clients must not return the Form, labels, descriptions, or attributes. The Planning Graph
restores the trusted checkpoint Form and derives selected option labels. The `answers`
object must contain every checkpoint question ID exactly once and no unknown keys.
Supplying fields for a different `answerType`, violating multiple-choice bounds, using an
invalid date, skipping a required question, or using unknown option IDs fails before the
Graph consumes the resume.

The same `DeepAgentDefinition.prepare_agui_resume(...)` flow handles Plan interrupts
without a separate API. The Definition restores the trusted envelope and exact pending
set from the canonical checkpoint; the Planning Graph validates the response contract
and rejects a stale `baseRevision`. Use a new `runId` with the same `threadId` for every
resume.

A pending batch cannot mix Plan and Tool interrupts. A Tool review can still occur
later, after Plan approval, and its original scoped Tool ID remains continuous across
that later resume.

Published Tool reviews carry `metadata.deepagents` with schema
`tinkerfin.deepagents.tool-review`. The required fields are
`nativeInterruptId`, `actionIndex`, `toolName`, `allowedDecisions`, and
`originalArgs`. The high-level Runtime does not require the host to persist or resubmit
that metadata: it restores and validates the native interrupt from the checkpointer.
Direct Adapter integrations that maintain their own trusted event log can validate a
complete published interrupt with `parse_tool_review_interrupt()`. Missing fields,
unknown fields, an invalid decision, or disagreement with
`metadata.langgraphValue` fails closed.

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

## Prepare a resume from the canonical checkpoint

The ordinary host path carries only client decisions into the framework. The Definition
loads the current pending interrupts, messages, lineage, and Runtime Profile from the
same checkpointed thread:

```python
from tinkerfin import AgUiResumeRequest


request = AgUiResumeRequest(entries=tuple(resume_entries))
binding = await agent.prepare_agui_resume(
    identity=resume_identity,
    parent_run_id=parent_run_id,
    request=request,
)
```

`AgUiResumeRequest` rejects duplicate IDs and contains no server interrupt payload,
native command, checkpoint identity, or Runtime Profile selection. Preparation requires
exact pending coverage and proves Tool correlation from complete checkpoint messages.
Unknown, stale, incomplete, or disallowed decisions fail before Graph continuation.

Pass the binding once. The Runtime owns native command construction:

```python
runtime = agent.new_agui(
    identity=identity,
    parent_run_id=parent_run_id,
    resume=binding,
    on_resume_checkpointed=record_checkpoint_idempotently,
    on_resume_initialization_failed=release_unprepared_claim_idempotently,
)
events = runtime.astream(config=config)
```

`AgUiResumeBinding` stores the framework-resolved native groups, scoped Tool IDs,
cancellation, and verified subagent sources. It has a stable JSON round trip but stores
no identity or parent and exposes no `Command`. The application still validates
authorization, atomically claims the public pending set, and validates the complete HTTP
request.

## Advanced Adapter resume inputs

`prepare_agui_resume()` is the high-level checkpoint path. A custom Adapter integration
that already owns complete native checkpoint objects can use the lower-level mapper:

```python
translation = ResumeMapper().map(
    entries=resume_entries,
    interrupts=pending_interrupts,
    messages_by_namespace=messages_by_namespace,
)
```

Resolved reviews need complete messages for safe tool correlation. Do not match parallel or repeated tool names by arrival order.
This is an Adapter-level inspection path; ordinary Runtime callers do not translate or
pass a native command.

If an advanced integration instead owns a trusted, complete AG-UI terminal event log,
`AgUiResumeBinding.from_agui(entries=..., interrupts=...)` can validate those persisted
public facts directly. It must never accept interrupt details resubmitted by a client,
and it is not required by the standard Definition/Checkpointer flow.

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

Before submitting the native decision, the selected Runtime Profile durably writes private
lineage and marker values on the exact interrupted checkpoint. This does not run a Graph
node or replace root, Planning, or subgraph pending work. The framework invokes
`on_resume_checkpointed` only after those writes are readable and before exposing the first
resumed native event. A prepared retry receives the same `AgUiResumeCheckpoint` and invokes
the idempotent callback again; a decision already accepted by the Graph is not resubmitted.
Decision-only and `None` continuation inputs remain internal.

If binding, staging, or stream startup fails or is cancelled before the marker is
readable, the Runtime invokes `on_resume_initialization_failed` from its retained close
task. Hosts use this idempotent callback to release the claimed public interrupt set. The
callback cannot run after prepared or accepted evidence exists; those retries continue
through `on_resume_checkpointed` instead.

## Retry and concurrency

- Use a new `runId` for the resume request and keep the same `threadId`;
- atomically claim the complete pending set in application storage;
- keep the exact client decision request stable across retries until checkpoint
  settlement; do not reconstruct or accept interrupt metadata from the client;
- resolve application approvals only from `on_resume_checkpointed`, never from
  `RUN_STARTED`;
- preserve original tool IDs when results continue after resume.

## Common errors

| Error | Likely cause |
| --- | --- |
| `AgUiResumeBindingError` | Unknown ID, incomplete coverage, disallowed decision, invalid Schema payload, or missing Tool evidence |
| `ValueError` | Invalid binding mode, unsupported mixed runtime cancellation, or incomplete scoped Tool ID |
| State cannot be resumed | Changed `RunIdentity.threadId` or missing checkpointer |
| Action runs twice | Pending interrupts were not claimed atomically or retry data changed |

Next: [Use the converter directly](adapter-extensions.md).
