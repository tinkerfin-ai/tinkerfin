# Configure an agent

[Runtime](index.md) · [中文](../../cn/runtime/deep-agents.md)

`build()` accepts the Deep Agents capabilities used by the Runtime and returns one
reusable `AgentRuntime`.

```python
from tinkerfin import TinkerFin

runtime = (
    TinkerFin(checkpointer=checkpointer)
    .with_namespace("support")
    .build(
        name="support-agent",
        model="openai:gpt-5.4",
        tools=[search_orders],
        system_prompt="Answer questions about customer orders.",
        store=store,
    )
)
```

## Build options

| Capability | Parameter |
| --- | --- |
| Model and instructions | `model`, `system_prompt` |
| Tools and middleware | `tools`, `middleware` |
| Delegation | `subagents` |
| Skills and memory files | `skills`, `memory` |
| Filesystem access | `backend`, `permissions` |
| Per-run tools | `prepare_tools` |
| Tool approval | `interrupt_on` |
| Structured output | `response_format` |
| State and invocation context | `state_schema`, `context_schema` |
| Persistence and cache | `checkpointer`, `store`, `cache` |

Supplied resources are borrowed. `build()` validates and captures configuration but
performs no execution I/O.

Store-backed file, memory, skill, and summarization middleware must omit the
`StoreBackend(store=...)` argument and use the Store passed to `build(store=...)`.
The Runtime applies namespace isolation and asynchronous file access to these
built-in declarations. Their caller-owned configuration remains unchanged.

## Persistence and approval

A concrete checkpointer is required for resumable tool approval and Plan. Submit only
new user messages when the checkpointer already contains the thread history.

```python
runtime = (
    TinkerFin(checkpointer=checkpointer)
    .with_namespace(namespace)
    .build(
        model=model,
        tools=[send_report],
        interrupt_on={"send_report": True},
    )
)
```

Checkpointed approval uses `durability="sync"`. This waits for asynchronous checkpoint
writes; it does not select a synchronous database driver.

## Plan before execution

Enable Plan on the builder, then select `mode="plan"` for a request:

```python
runtime = (
    TinkerFin(checkpointer=checkpointer)
    .with_namespace(namespace)
    .with_plan(planner_model=model)
    .build(model=model, tools=tools)
)

result = await runtime.ainvoke(
    thread_id=thread_id,
    run_id=run_id,
    input=graph_input,
    mode="plan",
)
```

Plan can clarify requirements and ask the user to approve a draft before agent
execution. Its built-in filesystem access is read-only. `mode="default"` executes the
agent directly. Plan models and review actions are exported from `tinkerfin.plan`.

## Lazy workspaces and tools

Pass a `Workspace` declaration as the backend when a run needs a Sandbox or another
prepared filesystem:

```python
runtime = (
    TinkerFin()
    .with_namespace(namespace)
    .build(
        model=model,
        backend=sandboxes.workspace(workspace_key),
    )
)
```

The application chooses `workspace_key`; user, session, and project keys are all valid
policies. The Runtime combines its namespace with that key, prepares the workspace only
for an admitted run, and releases the run's handle during cleanup.

`prepare_tools` can create additional asynchronous tools from the admitted run identity
and prepared workspace. It runs once per run. Built-in file, Todo, and task tool names
are reserved.

## Subagents

Use Deep Agents compiled or remote subagents, or declare typed subagents with
`tinkerfin.subagents.SubAgent`. Declarative subagents can define their own tools and
workspace preparation. Registered compiled subagents inherit the Runtime checkpointer
and Store; do not bind separate checkpoint coordinates to them.

## Direct Graph access

Advanced callers can build an asynchronous Graph:

```python
from tinkerfin.deep_agent import create_graph

graph = await create_graph(runtime)
result = await graph.ainvoke(graph_input, config=config)
```

The caller owns direct Graph execution and cleanup. Direct Graphs do not create managed
Runtime observations, AG-UI events, Messaging delivery, or per-run workspace
preparation. A Runtime using `workspace(...)` or `prepare_tools` must use managed
execution methods.

Next: [Streams and SSE](streams-and-sse.md).
