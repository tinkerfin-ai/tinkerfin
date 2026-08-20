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

## Core concepts

### Native Runtime

```python
runtime = agent.new(identity=identity, on_part=on_part)

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
