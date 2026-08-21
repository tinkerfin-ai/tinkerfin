# tinkerfin

## What it is

`tinkerfin` adds request-scoped native and AG-UI streams to Deep Agents while keeping
the installed `create_deep_agent(...)` and `CompiledStateGraph.astream(...)` parameter
shapes. Streams can be sent directly as SSE or persisted by `tinkerfin-messaging`.

Models, tools, backends, checkpointers, stores, Sandbox resources, and request mapping
remain host-owned.

## Installation

Python 3.11 or newer is required.

```bash
pip install tinkerfin
```

Redis coordination is optional:

```bash
pip install "tinkerfin[redis]"
```

This extra provides `RedisRunCoordinator` and the reusable auto-renewing
`RedisLeaseLock` with monotonic fencing tokens.

## Quick Start

```python
import asyncio

from tinkerfin import Identity, TinkerFin

tinkerfin = TinkerFin()
agent = tinkerfin.create_deep_agent(
    model="openai:gpt-5.4",
    tools=[],
)


async def main() -> None:
    runtime = agent.new(identity=Identity(threadId="thread-1", runId="run-1"))
    async for part in runtime.astream(
        {"messages": [{"role": "user", "content": "Hello"}]},
    ):
        print(part)


asyncio.run(main())
```

`create_deep_agent(...)` records the build call. `new()` creates a fresh Graph and a
single-use Runtime; the Graph iterator starts on first pull.

### Plan Mode

Plan Mode adds a conservative Gate, a read-only Planner, and user review before a
complex task reaches the configured Deep Agent. Enable the capability on an immutable
TinkerFin factory; the installed Deep Agents factory signature does not change.

```python
from langgraph.checkpoint.memory import MemorySaver
from tinkerfin import TinkerFin


tinkerfin = TinkerFin()
agent = tinkerfin.plan(
    enabled=True,
    default_mode="default",
    gate_model="openai:gpt-5.4-mini",
    planner_model="openai:gpt-5.4",
).create_deep_agent(
    model="openai:gpt-5.4",
    tools=[],
    checkpointer=MemorySaver(),
)

plan_runtime = agent.new(
    identity=Identity(threadId="thread-1", runId="run-1"),
    mode="plan",
)
default_runtime = agent.new_agui(
    identity=Identity(threadId="thread-1", runId="run-2"),
    mode="default",
)
```

Plan Mode requires an explicit model and a concrete `BaseCheckpointSaver`. The parent
workflow is the only graph configured with the supplied checkpointer, store, and cache;
its Gate, Planner, and Deep Agent subgraphs inherit those runtime resources. The host
still owns the resources and their shutdown lifecycle. Plan streams use synchronous
checkpoint durability, and an explicit non-`sync` durability value is rejected.

Only `ls`, `read_file`, `glob`, and `grep` are available to the Planner. Clarification
questions can contain model-generated single-select options and can optionally allow a
custom answer. Approval freezes a `ConfirmedPlan` and injects it into the Deep Agent
system request without adding hidden conversation messages. The public Plan state and
value models are available from `tinkerfin.plan` and are emitted under the root state
key `tinkerfin_plan`.

`.plan(...)` returns a separate TinkerFin factory while retaining the configured run
coordinator and global state schema. Every Definition freezes the capability options of
its source factory. A Plan-capable Definition always uses one topology and state schema;
`mode="default"` bypasses the Gate, while `mode="plan"` enters it. The same checkpoint
thread can select either mode on later requests. An ordinary Definition accepts only
`mode="default"` and cannot resume a Plan-capable checkpoint.

Application state shared by all Definitions can be supplied once:

```python
tinkerfin = TinkerFin(state_schema=AppState)
agent = tinkerfin.plan().create_deep_agent(
    model="openai:gpt-5.4",
    state_schema=AgentState,
    checkpointer=MemorySaver(),
)
```

TinkerFin combines the application, Definition, middleware, filesystem, Todo, and Plan
state contracts. A conflicting field fails during Definition creation. Runtime context
continues to use Deep Agents `context_schema`; it is not merged into state or stored in
the checkpoint.

## Core concepts

### Native Runtime

```python
runtime = agent.new(identity=identity, mode="plan", on_part=on_part)

parts = runtime.astream(
    graph_input,
    config,
    context=context,
    stream_mode="values",
)
```

Identity is injected into the Graph config and native output is fixed to v2.
`NativeGraphRunStream` preserves ordering,
backpressure, errors, cancellation, coordination, observer ordering, and cleanup.

### AG-UI Runtime

```python
runtime = agent.new_agui(
    identity=identity,
    mode="default",
    on_part=on_part,
    on_event=on_event,
)

events = runtime.astream(graph_input, config, context=context)
```

When omitted, the AG-UI Runtime fixes `stream_mode` to `messages/tasks/values`,
`version` to `v2`, and `subgraphs` to `True`. Explicit values must match; supported
extra modes are `updates`, `checkpoints`, `debug`, and `custom`. Invalid options fail
before stream side effects.

The returned `AgUiEventStream` uses `RUN_STARTED.input=None`, keeps the
event, interrupt/resume, subagent, reasoning privacy, cancellation, and cleanup
semantics, and can be passed directly to SSE or Messaging. Resumed runs use
`AgUiResumeBinding`; high-level callers do not pass Tool IDs separately.

### Ownership

A Definition may create multiple Runtimes; each one owns a separate Graph invocation
and one object stream. Graph construction is synchronous, so async servers should use
their controlled thread boundary around `new()` or `new_agui()` when needed.

A configured coordinator receives the same complete Identity. Coordination does not
replace a LangGraph checkpointer.

### Low-level sources

`TinkerFin.run(...)` handles custom asynchronous sources and low-level integrations.
Deep Agents callers normally use the façade above.

## Documentation

- [Runtime guide](https://github.com/tinkerfin-ai/tinkerfin/blob/main/docs/en/runtime/index.md)
- [AG-UI guide](https://github.com/tinkerfin-ai/tinkerfin/blob/main/docs/en/agui/index.md)
- [Complete documentation](https://github.com/tinkerfin-ai/tinkerfin/blob/main/docs/en/README.md)

## License

Apache License 2.0. See the
[repository license](https://github.com/tinkerfin-ai/tinkerfin/blob/main/LICENSE).
