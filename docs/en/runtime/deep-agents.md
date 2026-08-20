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

## Create one native Runtime

```python
runtime = agent.new(
    identity=Identity(threadId="project-7", runId="run-1"),
    on_part=None,
)
```

| Parameter | Default | Purpose |
| --- | --- | --- |
| `identity` | required | Thread and run identity used by checkpointing and coordination |
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
| `durability` | `None` | Controls checkpoint persistence timing |
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
