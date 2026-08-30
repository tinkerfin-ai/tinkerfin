# Create and run a Deep Agent

[Runtime basics](index.md) · [中文](../../zh/runtime/deep-agents.md)

This guide starts with the settings most applications need, then covers the optional run controls. You do not need to fill every parameter.

## A practical agent definition

```python
from langgraph.checkpoint.memory import MemorySaver
from tinkerfin import RunIdentity, TinkerFin


tinkerfin = TinkerFin()
agent = tinkerfin.create_deep_agent(
    name="support-agent",
    model="openai:gpt-5.4",
    tools=[search_orders],
    system_prompt="Answer questions about customer orders.",
    checkpointer=MemorySaver(),
)
```

| Parameter | Default | Purpose |
| --- | --- | --- |
| `model` | `None` | A model name or an existing chat model; normally required in a real app |
| `tools` | `None` | Tools the agent may call |
| `system_prompt` | `None` | The agent's role, goal, and constraints |
| `name` | `None` | A Graph name for logs and diagnostics |
| `checkpointer` | `None` | Saves thread state; required for resumable approvals and continuing conversations |
| `store` | `None` | Saves long-term data across threads |
| `debug` | `False` | Enables LangGraph debug output |

## Add capabilities when you need them

| If you need to... | Parameter | Value |
| --- | --- | --- |
| Add middleware | `middleware` | A middleware sequence |
| Delegate to subagents | `subagents` | Subagent definitions or compiled subagents |
| Load skill directories | `skills` | A list such as `['./skills/']` |
| Load long-term memory files | `memory` | A list of file paths |
| Restrict file operations | `permissions` | `FilesystemPermission` entries |
| Use another file backend | `backend` | A Deep Agents backend |
| Ask for approval before tools | `interrupt_on` | Per-tool approval settings |
| Return structured output | `response_format` | A data model or structured output strategy |
| Extend run state | `state_schema` | A custom state type |
| Supply middleware context | `context_schema` | A context type |
| Enable Graph caching | `cache` | A LangGraph cache |

Interrupts require a checkpointer. A backend or middleware that relies on a store also needs the corresponding `store` argument.

## Review a Plan before execution

Use Plan Mode when a request may need clarification or a reviewed multi-step Plan. The
capability belongs to TinkerFin, so the Deep Agents factory keeps its installed
parameter list.

```python
from tinkerfin import TinkerFin


tinkerfin = TinkerFin(state_schema=AppState)
agent = tinkerfin.plan(
    enabled=True,
    default_mode="default",
    planner_model="openai:gpt-5.4",
).create_deep_agent(
    model="openai:gpt-5.4",
    tools=[search_orders],
    checkpointer=production_checkpointer,
)
```

`.plan(...)` returns a new TinkerFin factory and does not modify `tinkerfin`. It retains
the same run coordinator and global state schema, and every Definition freezes the
capability options of the factory that created it. `planner_model` is optional and
falls back to the explicit Deep Agent model when omitted.

Plan Mode routes a request through these boundaries:

1. One read-only Planner judges whether intent and constraints are sufficient.
2. The Planner can use only `ls`, `read_file`, `glob`, and `grep`.
3. Each clarification question uses a Planner-selected `single_choice`,
   `multiple_choice`, `text`, `date`, `time`, or `datetime` answer type; clarification and plan review pause
   through LangGraph interrupts.
4. Approval freezes a `ConfirmedPlan`, commits a deterministic handoff using the
   original user message ID, and immediately starts the native Deep Agent.

Plan review defaults to `PlanReviewAction.APPROVE`, `RESPOND`, and `REJECT`.
Pass a non-empty, duplicate-free `allowed_review_actions` sequence to change the accepted
decisions. The response Schema contains exactly that sequence; `EDIT` is available only
when the host explicitly enables it and provides a trusted draft editor.

Approval alone exits Plan mode and starts native execution. Rejection invalidates the
draft, accepts an optional reason, produces one visible Planner reply, and waits for the
next Plan input. A configured `CANCEL` does the same without a reason. Transport-level
AG-UI cancellation remains abandonment and does not invoke the Planner.

Selecting Plan requires a concrete `BaseCheckpointSaver` and one explicit model.
`planner_model` is optional when the Agent definition supplies `model`; omitting both is
rejected. TinkerFin never creates an in-process saver or silently weakens durability.
Production applications must provide a production-grade saver. Planning and the native Deep Agent
borrow the same saver, Store, cache, backend, and runtime context. Keep the same
`RunIdentity.threadId` when resuming. Plan state appears at the root `tinkerfin_plan` key;
`PlanContentModel`, the built-in structured and Markdown content types, `PlanDraft`,
`ConfirmedPlan`, `PlanHandoff`, and `PlanState` are exported from `tinkerfin.plan`.

Plan content defaults to `StructuredPlanContent`. Select one exact Markdown document or
a host-defined content contract on the Plan factory:

```python
from tinkerfin.plan import MarkdownPlanContent


agent = tinkerfin.plan(
    planner_model="openai:gpt-5.4",
    content_schema=MarkdownPlanContent,
).create_deep_agent(
    model="openai:gpt-5.4",
    tools=[search_orders],
    checkpointer=production_checkpointer,
)
```

A custom content type inherits `PlanContentModel` and uses precise Pydantic fields. The
selected schema is immutable and automatically fingerprinted for the Definition; the
same validated content is used for draft review, any explicitly enabled edit,
confirmation, checkpoint recovery, and execution handoff.

`mode="default"` directly runs the native Deep Agent Graph. It does not execute
Planning, add middleware, replace state schema, or create a parent Graph. Native Todo,
Tool/Filesystem HITL, subagent, cancellation, and error semantics remain unchanged.

The Planner has only read-only filesystem tools for optional workspace inspection.
That restricted list is not the execution Deep Agent's capability list: the Planner
can include user-requested execution tools in a draft but never calls or tests them
itself. Approving a Plan starts execution immediately without another general Plan
approval request. Tool-specific human review, such as a configured `write_file`
interrupt, still applies during execution.

The built-in `DefaultClarificationForm` is used when `.plan(...)` omits
`clarification_schema`. `BuiltInClarificationForm[QuestionAttributes, OptionModel]`
applies shared strongly typed metadata to all six built-in types without per-type
subclasses. A host can also provide a concrete `ClarificationForm` union containing only
the answer types its client supports. Attributes are public, model-generated planning
context rather than authoritative permission, billing, or compliance data.

Choice questions use `allow_free_text` (`allowFreeText` on JSON). Multiple choice also
declares `min_selections` and an optional `max_selections`; selected option IDs and one
custom answer may coexist. Text answers are non-blank, and dates use `YYYY-MM-DD` without
a time or time zone. Time answers contain one local wall-clock minute and the question
declares the IANA time zone used to interpret it; optional inclusive bounds must form a
non-wrapping range. A complete response is keyed by checkpoint question ID. Each value
has `status: answered` plus its `answerType`, or `status: skipped` for an optional
question. Planning validates the exact pending response Schema before Graph resume,
derives trusted option labels, canonicalizes answer order, and retains explicit skips as
Planner context.
Datetime questions require an IANA zone and accept one local calendar date plus minute;
their bounds may span dates. Normalization adds the unique UTC `instant`, and rejects
DST gaps or repeated local minutes.


Use `clarification_type(...)` to register a host-defined semantic answer type. One frozen
descriptor supplies its unversioned namespaced ID, model-facing description, Question and
Response models, optional per-question Schema and validator, and a canonical JSON
normalizer. All callbacks are synchronous, deterministic, and free of external I/O. A
host client must provide a renderer for every custom type included in its Form. The
Definition fingerprint fixes the exact current Schema and callbacks; incompatible stored
data is rebuilt instead of introducing another type-ID version.

When `PlanReviewAction.EDIT` is configured, a complete user edit is authoritative. The
Planner either asks for missing information or accepts that exact edit; it cannot
silently replace it. Clarification does not change the revision. The revision increments
only when a complete draft becomes reviewable.

Plan Mode fixes checkpoint durability to `sync`. Omitting `durability` is recommended;
passing `sync` is also accepted, while `async` and `exit` fail before streaming.

Choose the route on each managed run:

```python
plan_stream = await tinkerfin.open_run(
    RunIdentity(threadId="project-7", runId="run-1"),
    agent=agent,
    input=graph_input,
    mode="plan",
)
default_events = await tinkerfin.open_agui_run(
    RunIdentity(threadId="project-7", runId="run-2"),
    agent=agent,
    input=graph_input,
    mode="default",
)
```

`default` uses the native topology; `plan` uses the standalone Planning Graph. Approval
changes the effective mode to `default` before native execution. A later request on the
same checkpoint thread can select Plan again. Plan resumes route to Planning, while Tool
and subagent resumes route to the native Graph.

## Open one managed native run

```python
stream = await tinkerfin.open_run(
    RunIdentity(threadId="project-7", runId="run-1"),
    agent=agent,
    input={"messages": [{"role": "user", "content": "Review this project"}]},
    mode="default",
    config=config,
    context=context,
    on_native_part=record_part,
)
```

| Parameter | Default | Purpose |
| --- | --- | --- |
| `identity` | required | Thread and run identity used by checkpointing and coordination |
| `agent` | required | A Definition or sync/async callable returning one |
| `input` | required | New state, native `Command`, or `None` |
| `mode` | Definition default | `default` or `plan`; ordinary Definitions accept only `default` |
| `config` | `None` | Tags, metadata, recursion limits, and other Graph configuration |
| `context` | `None` | Runtime context matching `context_schema` |
| `on_native_part` | `None` | Async observer before each native part reaches the consumer |
| Extra keywords | none | Current Profile-supported Graph stream options |

You normally omit `configurable.thread_id`. An explicitly equal value is accepted; a
different value fails before Graph iteration, observations, or coordination. The selected
Profile owns required stream modes, version, subgraph scope, and complete state output.

`on_native_part` is awaited in the delivery path. Use asynchronous clients inside it and
avoid blocking network or database calls.

## Reuse a direct Graph

Advanced integrations that do not need managed run lifecycle can create one async
Runnable and reuse it across thread IDs:

```python
graph = await agent.create_graph(mode="plan")
state = await graph.ainvoke(graph_input, config=config)
async for part in graph.astream(graph_input, config=config):
    consume(part)
```

The direct Graph owns Plan/native routing and checkpoint-based resume selection. It does
not create `RunIdentity`, Runtime Observation, Trace, AG-UI, Messaging, or host business
state. Direct resume uses LangGraph `Command(resume=...)`.

## Common problems

### A second iteration of one managed stream fails

Managed streams are single-use. Call `open_run()` again with the next run identity.

### The conversation does not continue

Make sure the Agent has a checkpointer and later runs reuse the same
`RunIdentity.threadId`.

### Graph construction blocks the event loop

The built-in Profile runs its locked synchronous Deep Agents factory through AnyIO's
capacity-limited worker boundary. Do not add another application thread around
`open_run()`, `open_agui_run()`, or `create_graph()`.

A custom Profile can implement `create_agent_graph(factory, args, kwargs)` when its
factory is natively asynchronous. A Profile without that optional method retains its own
synchronous factory, which Core invokes through the same bounded worker boundary.

Next: [Streams and SSE](streams-and-sse.md).
