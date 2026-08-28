# Runtime basics

[Documentation](../README.md) · [中文](../../zh/runtime/index.md)

The Runtime starts one agent run and exposes its progress as an asynchronous stream. It does not create model accounts or take ownership of your database, checkpointer, store, or Sandbox.

## Choose a Runtime

| Goal | Use |
| --- | --- |
| Consume native LangGraph data | `agent.new()` |
| Send AG-UI events to a frontend | `agent.new_agui()` |

Start with `agent.new()` if you are new to TinkerFin.

## Installation

```bash
pip install tinkerfin
```

This base installation includes Native Runtime, Plan Mode, Observation, and native SSE.
Install `pip install "tinkerfin[agui]"` before using `new_agui()`.

Install and configure your model provider separately, including its credentials. The
OpenAI model string below requires `pip install langchain-openai`.

## Your first Runtime

```python
import asyncio

from tinkerfin import RunIdentity, TinkerFin


tinkerfin = TinkerFin()
agent = tinkerfin.create_deep_agent(
    model="openai:gpt-5.4",
    tools=[],
)


async def main() -> None:
    identity = RunIdentity(threadId="conversation-1", runId="run-1")
    runtime = agent.new(identity=identity)
    stream = runtime.astream(
        {"messages": [{"role": "user", "content": "Describe Beijing in one sentence"}]},
    )

    async for part in stream:
        print(part)


asyncio.run(main())
```

The example has four steps:

1. `TinkerFin()` creates the main entry point.
2. `create_deep_agent(...)` records the model, tools, and other agent settings.
3. `agent.new(identity=...)` creates a fresh Graph and binds one run identity.
4. `runtime.astream(...)` starts the run and yields results as they arrive.

## What `RunIdentity` does

`RunIdentity` contains only `threadId` and `runId`. Runtime injects `threadId` into the Graph config automatically.

```python
identity = RunIdentity(threadId="user-42-support", runId="run-20260820-1")
```

| Field | Requirement | Purpose |
| --- | --- | --- |
| `threadId` | Required, non-empty, no surrounding whitespace | Continuing conversation and Graph checkpoint thread |
| `runId` | Required, non-empty, no surrounding whitespace | Idempotent ID for one semantic run in the thread |

A `RunIdentity` is immutable, limits each identifier to 1,024 characters, and rejects
extra fields. It does not contain parent lineage,
user authentication data, or request content. `new_agui(parent_run_id=...)` accepts
lineage only when needed. The same canonical RunIdentity is used by public events, the
Graph, checkpoints, coordination, and durable delivery.

Reuse `threadId` for one continuing conversation. Use a new `runId` for new semantic input, and reuse it only for retries or attachment.

## A Runtime is single-use

Call `astream()` only once on each Runtime. Create another Runtime for another run:

```python
first = agent.new(identity=RunIdentity(threadId="thread-1", runId="run-1"))
second = agent.new(identity=RunIdentity(threadId="thread-1", runId="run-2"))
```

The agent definition is reusable; a Runtime represents one execution.

## Current capability boundary

The distribution provides the explicit `deepagents-v2` Runtime Profile. It does not
provide a Deep Agents v3 Profile or TodoGroups projection/UI. A separately implemented
Profile must own its concrete upstream build and stream contract while emitting the same
canonical Native observations; downstream Runtime, Trace, AG-UI, and Messaging code does
not branch on upstream versions.

Archive/S3/Blob storage, payload encryption/KMS, and OpenTelemetry exporters are not
provided. Concrete integrations compose the already active `TraceStore`, canonical
payload codec, `RuntimeObserver`, or Store/Messaging Backend decorator boundaries. There
are no placeholder interfaces for unavailable capabilities.

## Next steps

- [Create and run a Deep Agent](deep-agents.md)
- [Streams and SSE](streams-and-sse.md)
- [Run coordination and Redis leases](extensions.md)
- [Record semantic execution history](../tracing/index.md)
- [Runtime usage reference](api-reference.md)
