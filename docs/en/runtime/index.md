# Runtime basics

[Documentation](../README.md) · [中文](../../zh/runtime/index.md)

TinkerFin opens one managed Agent run and exposes its progress as an asynchronous
stream. Models, databases, checkpointers, Stores, and Sandbox resources remain owned by
the host.

## Choose an entry point

| Goal | Use |
| --- | --- |
| Consume validated native LangGraph data | `TinkerFin.open_run()` |
| Return the final state with managed observations and cleanup | `TinkerFin.ainvoke()` |
| Send AG-UI events to a frontend | `TinkerFin.open_agui_run()` |
| Reuse a direct async Runnable without managed lifecycle | `DeepAgentDefinition.create_graph()` |

Start with `ainvoke()` when only the final state is needed, or `open_run()` when the
caller consumes progress. The direct Graph is an advanced boundary and does not create
a run identity, Runtime Observation, Trace, AG-UI, Messaging, or business lifecycle.

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
These methods return after Run start and input observations are committed, but before the
first model output is pulled. Close a returned stream that will not be iterated. The
Agent definition is reusable, including across concurrent thread IDs.

Advanced code can create one reusable async Runnable:

```python
graph = await agent.create_graph(mode="plan")
result = await graph.ainvoke(graph_input, config=config)
```

`DeepAgentGraph` supports `ainvoke()`, `astream()`, `abatch()`, and standard Runnable
composition. It uses native LangGraph `Command(resume=...)` for direct resume. Its
synchronous `invoke()`, `stream()`, and `batch()` paths reject execution so async
checkpointers, Stores, tools, and cancellation remain native.

## Runtime Profile selection

`DeepAgentsV2RuntimeProfile` is the default stable integration. The
`DeepAgentsV3RuntimeProfile` integration for Deep Agents v3 selects LangGraph's
experimental v3 event stream. The Runtime never detects, negotiates, or falls back
between them. Both produce the same canonical Native observations, so Trace, AG-UI,
Messaging, and host code do not branch on the upstream stream API. `TodoGroups` remain
host projections over canonical Trace facts rather than Runtime state or a second
persistence format.

The selected Profile and its `profile_id` form a framework-private integration and
checkpoint-recovery boundary. A host selects that boundary during framework assembly;
the identity is not an application database field or HTTP request/response value.

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
