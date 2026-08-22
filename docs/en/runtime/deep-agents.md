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
from langgraph.checkpoint.memory import MemorySaver
from tinkerfin import TinkerFin


tinkerfin = TinkerFin(state_schema=AppState)
agent = tinkerfin.plan(
    enabled=True,
    default_mode="default",
    gate_model="openai:gpt-5.4-mini",
    planner_model="openai:gpt-5.4",
).create_deep_agent(
    model="openai:gpt-5.4",
    tools=[search_orders],
    checkpointer=MemorySaver(),
)
```

`.plan(...)` returns a new TinkerFin factory and does not modify `tinkerfin`. It retains
the same run coordinator and global state schema, and every Definition freezes the
capability options of the factory that created it. `gate_model` and `planner_model` are
optional; each falls back to the Deep Agent model when omitted.

Plan Mode routes a request through these boundaries:

1. A conservative Gate chooses direct execution, clarification, or planning.
2. The Planner can use only `ls`, `read_file`, `glob`, and `grep`.
3. Clarification can provide model-generated single-select options and optional free
   text; clarification and plan review pause through LangGraph interrupts.
4. Approval freezes a `ConfirmedPlan` and passes it to the configured Deep Agent in its
   system request.

A concrete `BaseCheckpointSaver` and an explicit model are required. The parent Plan
workflow owns the supplied durable checkpointer. Gate and Planner disable child
checkpointing and return JSON-validated state to the parent; the Deep Agent inherits the
parent saver for Tool/Filesystem HITL. Child graphs continue to use the parent runtime
store and cache. Keep the same `Identity.threadId` when resuming. Plan state appears at the root
`tinkerfin_plan` key, and public models such as `PlanDraft`, `ConfirmedPlan`, and
`PlanState` are exported from `tinkerfin.plan`.

When the configured main execution middleware includes `TodoListMiddleware`, each
`write_todos` call commits the current Todo list to the authoritative parent state and
parent checkpoint before later execution Tools run. The normal Tool Start/Args/End and
Result lifecycle remains intact. A Tool interrupt snapshot and the first resumed
snapshot therefore contain the same latest Todos. Todo telemetry is not a replacement
for `ConfirmedPlan` steps or acceptance criteria. Ordinary `task` subagents and unknown
compiled subgraphs cannot overwrite this root projection. Files remain part of the
root-visible Deep Agent state and are synchronized when execution returns or crosses a
Todo parent boundary.

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

Each question uses `allow_free_text` (`allowFreeText` on the JSON boundary). An option
answer sends only `questionId` and `optionId`; a free-text answer sends only
`questionId` and `answer`. The workflow restores the checkpointed form, derives an
option's trusted label, and rejects mixed, incomplete, unknown, or stale answers.

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

Both requests use the same Plan-capable topology and state schema. `default` bypasses
the Gate, while `plan` enters it. A later request on the same checkpoint thread can
choose either mode. Mode does not become an application state field. Do not resume a
Plan-capable checkpoint with an ordinary Definition.

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
| `durability` | `None` | Controls checkpoint persistence timing; Plan Mode requires `sync` |
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
