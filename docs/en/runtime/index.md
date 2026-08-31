# Runtime basics

[Documentation](../README.md) · [中文](../../zh/runtime/index.md)

TinkerFin opens one managed Agent run and exposes its progress as an asynchronous
stream. Models, databases, checkpointers, Stores, and Sandbox resources remain owned by
the host.

## Choose an entry point

| Goal | Use |
| --- | --- |
| Consume validated native LangGraph data | `TinkerFin.open_run()` |
| Send AG-UI events to a frontend | `TinkerFin.open_agui_run()` |
| Reuse a direct async Runnable without managed lifecycle | `DeepAgentDefinition.create_graph()` |

Start with `open_run()`. The direct Graph is an advanced boundary and does not create a
run identity, Runtime Observation, Trace, AG-UI, Messaging, or business lifecycle.

## Installation

```bash
pip install tinkerfin
```

The base installation includes native Runtime, Plan Mode, Observation, and native SSE.
Install `pip install "tinkerfin[agui]"` before using `open_agui_run()`.

Install and configure the selected model provider separately. The OpenAI model string
below requires `pip install langchain-openai`.

## Your first managed run

```python
import asyncio

from tinkerfin import RunIdentity, TinkerFin


tinkerfin = TinkerFin()
agent = tinkerfin.create_deep_agent(
    model="openai:gpt-5.4",
    tools=[],
)


async def main() -> None:
    stream = await tinkerfin.open_run(
        RunIdentity(threadId="conversation-1", runId="run-1"),
        agent=agent,
        input={
            "messages": [
                {"role": "user", "content": "Describe Beijing in one sentence"}
            ]
        },
    )
    async for part in stream:
        print(part)


asyncio.run(main())
```

The common path has three steps:

1. Configure one reusable `TinkerFin` facade.
2. Create a reusable Agent definition.
3. Call `open_run()` with the identity and input, then consume the returned stream.

The facade resolves a lazy Agent callback when needed, constructs the Graph through the
selected Profile's native asynchronous method or its bounded synchronous-factory
boundary, injects identity, opens observations and coordination, and owns cancellation
and cleanup.

## What `RunIdentity` does

`RunIdentity` contains only `threadId` and `runId`. TinkerFin injects `threadId` into
Graph configuration, so callers do not repeat it.

```python
identity = RunIdentity(threadId="user-42-support", runId="run-20260820-1")
```

| Field | Requirement | Purpose |
| --- | --- | --- |
| `threadId` | Required, non-empty, no surrounding whitespace | Continuing conversation and checkpoint thread |
| `runId` | Required, non-empty, no surrounding whitespace | Idempotent ID for one semantic run |

The value is immutable, limits each identifier to 1,024 characters, and rejects extra
fields. Parent lineage, authentication, and request content are separate inputs. Reuse
`threadId` for one continuing conversation. Use a new `runId` for new semantic input and
reuse it only for retry or attachment to that same run.

## Managed streams and direct Graphs

Every stream returned by `open_run()` or `open_agui_run()` is single-use and closeable.
The Agent definition is reusable, including across concurrent thread IDs.

Advanced code can create one reusable async Runnable:

```python
graph = await agent.create_graph(mode="plan")
result = await graph.ainvoke(graph_input, config=config)
```

`DeepAgentGraph` supports `ainvoke()`, `astream()`, `abatch()`, and standard Runnable
composition. It uses native LangGraph `Command(resume=...)` for direct resume. Its
synchronous `invoke()`, `stream()`, and `batch()` paths reject execution so async
checkpointers, Stores, tools, and cancellation remain native.

## Current capability boundary

The framework distribution provides the explicit `deepagents-v2` Runtime Profile. It
does not provide a Deep Agents v3 Profile or a generic TodoGroups projection/UI. Studio
derives its product-specific task trace from canonical Trace facts at query time. Another
concrete Profile must own its upstream construction and stream contract while emitting
the same canonical Native observations; downstream Runtime, Trace, AG-UI, and Messaging
code does not branch on upstream versions.

Archive/S3/Blob storage, payload encryption/KMS, and OpenTelemetry exporters are not
provided. Active Trace storage implements `TraceLedgerBackend`; advanced integrations
may replace `TraceStore`, wrap the canonical payload codec, observe `RuntimeObserver`,
or decorate Store/Messaging Backend boundaries. No placeholder API represents an
unavailable capability.

## Next steps

- [Create and run a Deep Agent](deep-agents.md)
- [Streams and SSE](streams-and-sse.md)
- [Run coordination and Redis leases](extensions.md)
- [Record semantic execution history](../tracing/index.md)
- [Runtime usage reference](api-reference.md)
