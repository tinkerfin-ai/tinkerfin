# Create and run a Deep Agent

[Runtime basics](index.md) · [中文](../../zh/runtime/deep-agents.md)

This guide starts with the settings most applications need, then covers the optional run controls. You do not need to fill every parameter.

## A practical agent definition

```python
from langgraph.checkpoint.memory import MemorySaver
from tinkerfin import Identity, TinkerFin


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
3. Clarification can provide model-generated single-select options and optional free
   text; clarification and plan review pause through LangGraph interrupts.
4. Approval freezes a `ConfirmedPlan`, commits a deterministic handoff using the
   original user message ID, and immediately starts the native Deep Agent.

Selecting Plan requires a concrete `BaseCheckpointSaver` and an explicit Planner model.
TinkerFin never creates an in-process saver or silently weakens durability. Production
applications must provide a production-grade saver. Planning and the native Deep Agent
borrow the same saver, Store, cache, backend, and runtime context. Keep the same
`Identity.threadId` when resuming. Plan state appears at the root `tinkerfin_plan` key;
`PlanContentModel`, the built-in structured and Markdown content types, `PlanDraft`,
`ConfirmedPlan`, `PlanHandoff`, and `PlanState` are exported from `tinkerfin.plan`.

Plan content defaults to `StructuredPlanContent`. Select one exact Markdown document or
a host-defined content contract on the Plan factory:

```python
from tinkerfin.plan import MarkdownPlanContent


agent = tinkerfin.plan(
    planner_model="openai:gpt-5.4",
    plan_schema=MarkdownPlanContent,
).create_deep_agent(
    model="openai:gpt-5.4",
    tools=[search_orders],
    checkpointer=production_checkpointer,
)
```

A custom content type inherits `PlanContentModel`, declares a stable `schema_id`, and
uses precise Pydantic fields. The selected schema is immutable for the Definition and
the same validated content is used for draft review, authoritative edits, confirmation,
checkpoint recovery, and execution handoff.

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
`clarification_schema`. Hosts that need typed question or option metadata can define a
concrete `ClarificationForm` in application code and pass that type to `.plan(...)`.
The schema is frozen on the returned factory and cannot be replaced by `new()`,
`new_agui()`, or resume calls. Attributes must use concrete `ClarificationModel`
subclasses; they are public, model-generated planning context rather than authoritative
permission, billing, or compliance data.

Each question explicitly sets `required` and uses `allow_free_text` (`allowFreeText` on
the JSON boundary). A round can contain required questions, optional refinements, or only
optional questions, and is submitted as one complete batch. An option answer sends only
`questionId` and `optionId`; a free-text answer sends only `questionId` and `answer`; an
optional skip sends only `questionId` and `skipped: true`. The workflow requires one
explicit result for every checkpointed question, derives trusted option labels, rejects a
skip for a required question, and retains skipped answers as Planner context. A Planner
must not repeat an optional question the user explicitly skipped in the same Plan cycle.

A complete user edit is authoritative. The Planner either asks for missing information
or accepts that exact edit; it cannot silently replace it. Clarification does not change
the revision. The revision increments only when a complete draft becomes reviewable.

Plan Mode fixes checkpoint durability to `sync`. Omitting `durability` is recommended;
passing `sync` is also accepted, while `async` and `exit` fail before streaming.

Choose the current request on Runtime creation:

```python
plan_runtime = agent.new(
    identity=Identity(threadId="project-7", runId="run-1"),
    mode="plan",
)
default_runtime = agent.new_agui(
    identity=Identity(threadId="project-7", runId="run-2"),
    mode="default",
)
```

`default` uses the original native topology and state schema; `plan` uses the standalone
Planning Graph. Approval changes the effective mode to `default` before native execution.
A later request on the same checkpoint thread can select Plan again. Plan resumes are
routed to Planning, while Tool and subagent resumes go directly to the native Graph.

## Create one native Runtime

```python
runtime = agent.new(
    identity=Identity(threadId="project-7", runId="run-1"),
    mode="default",
    on_part=None,
)
```

| Parameter | Default | Purpose |
| --- | --- | --- |
| `identity` | required | Thread and run identity used by checkpointing and coordination |
| `mode` | Definition default | `default` or `plan`; ordinary Definitions accept only `default` |
| `on_part` | `None` | Observer called before each native part reaches the consumer |

## Start the run

```python
stream = runtime.astream(
    {"messages": [{"role": "user", "content": "Review this project"}]},
)
```

### Input and configuration

| Parameter | Default | Purpose |
| --- | --- | --- |
| `input` | required | New state input, a `Command`, or `None` |
| `config` | `None` | Thread, tags, metadata, recursion limits, and other run configuration |
| `context` | `None` | Runtime context matching `context_schema` |

You normally omit `configurable.thread_id`. An explicitly equal value is accepted; a value different from `Identity.threadId` fails before Graph iteration, observers, or coordination begin.

### Output controls

| Parameter | Default | Purpose |
| --- | --- | --- |
| `stream_mode` | `None` | Selects `values`, `updates`, `messages`, `tasks`, or another supported mode |
| `print_mode` | `()` | Prints extra modes without changing yielded data |
| `output_keys` | `None` | Limits state output to selected keys |
| `subgraphs` | `False` | Includes subgraph output when true |
| `version` | forced to `"v2"` | Explicit v1 is rejected before Graph iteration |
| `debug` | `None` | Overrides debugging for this run |

### Interrupt and durability controls

| Parameter | Default | Purpose |
| --- | --- | --- |
| `interrupt_before` | `None` | Pauses before selected nodes |
| `interrupt_after` | `None` | Pauses after selected nodes |
| `durability` | `None` | Controls checkpoint timing; Planning and handoff require `sync` |
| `control` | `None` | Supplies LangGraph run control data |
| Extra keyword arguments | none | Current additional LangGraph run options |

Use `stream_mode="values"` when you mainly need state snapshots. To inspect the complete native v2 run, combine `messages`, `tasks`, and `values` with `subgraphs=True`.

## Observe every part

`on_part` must be asynchronous. It runs before the item reaches your `async for` loop.

```python
async def record_part(part: object) -> None:
    print("received", part)


runtime = agent.new(identity=identity, on_part=record_part)
```

An observer failure ends the run. Use asynchronous clients inside it instead of blocking network or database calls.

## Common problems

### A second `astream()` call fails

The Runtime is single-use. Call `agent.new()` again.

### The conversation does not continue

Make sure the agent has a checkpointer and later runs use the same `Identity.threadId`.

### An asynchronous server pauses while creating the Runtime

`agent.new()` constructs the Graph synchronously. If construction is expensive, call it through a controlled thread boundary.

Next: [Streams and SSE](streams-and-sse.md).
