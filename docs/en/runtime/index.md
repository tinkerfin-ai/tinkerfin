# Runtime basics

[Documentation](../README.md) · [中文](../../zh/runtime/index.md)

The Runtime starts one agent run and exposes its progress as an asynchronous stream. It does not create model accounts or take ownership of your database, checkpointer, store, or Sandbox.

## Choose a Runtime

| Goal | Use |
| --- | --- |
| Consume native LangGraph data | `agent.new()` |
| Send AG-UI events to a frontend | `agent.new_agui()` |
| Run your own asynchronous source | `TinkerFin.run()` |

Start with `agent.new()` if you are new to TinkerFin.

## Installation

```bash
pip install tinkerfin
```

Install and configure your model provider separately, including its credentials.

## Your first Runtime

```python
import asyncio

from tinkerfin import TinkerFin


tinkerfin = TinkerFin()
agent = tinkerfin.create_deep_agent(
    model="openai:gpt-5.4",
    tools=[],
)


async def main() -> None:
    runtime = agent.new()
    stream = runtime.astream(
        {"messages": [{"role": "user", "content": "Describe Beijing in one sentence"}]},
        {"configurable": {"thread_id": "conversation-1"}},
    )

    async for part in stream:
        print(part)


asyncio.run(main())
```

The example has four steps:

1. `TinkerFin()` creates the main entry point.
2. `create_deep_agent(...)` records the model, tools, and other agent settings.
3. `agent.new()` creates a fresh Graph and a single-use Runtime.
4. `runtime.astream(...)` starts the run and yields results as they arrive.

## What `thread_id` does

`thread_id` identifies a conversation. When the agent has a checkpointer, later runs with the same value can continue its saved state.

```python
config = {"configurable": {"thread_id": "user-42-support"}}
```

Keep the same ID for one continuing conversation. Do not share it across different users.

## A Runtime is single-use

Call `astream()` only once on each Runtime. Create another Runtime for another run:

```python
first = agent.new()
second = agent.new()
```

The agent definition is reusable; a Runtime represents one execution.

## Next steps

- [Create and run a Deep Agent](deep-agents.md)
- [Streams and SSE](streams-and-sse.md)
- [Custom sources and run coordination](extensions.md)
- [Runtime usage reference](api-reference.md)

