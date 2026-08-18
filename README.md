# TinkerFin

[中文](docs/README.zh.md)

## What it is

TinkerFin binds a caller-owned asynchronous LangGraph v2 source to native, AG-UI,
direct SSE, or durable Messaging delivery. Graph construction, invocation arguments,
models, tools, middleware, checkpoints, stores, and Sandbox ownership remain with the
host.

```text
packages/
├── tinkerfin/                 source binding, AG-UI, direct SSE, coordination
├── tinkerfin-agui-adapter/    LangGraph v2 StreamPart to AG-UI conversion
├── tinkerfin-messaging/       durable delivery, replay, cancellation, Redis
└── tinkerfin-sandbox/         OpenSandbox backend and lifecycle management
apps/studio/
├── server/                    Studio server application
└── web/                       Studio web application
docs/                          runtime and integration documentation
```

## Installation

Python 3.11 or newer is required.

```bash
pip install tinkerfin
```

Install only the integrations used by the host:

```bash
pip install "tinkerfin[redis]"
pip install "tinkerfin-messaging[agui,native,redis]"
pip install "tinkerfin-sandbox[sqlite]"
```

## Quick Start

Bind the caller-owned Graph stream lazily, then choose native or AG-UI objects from the
same run interface.

```python
import asyncio
from typing import TypedDict

from langgraph.graph import START, StateGraph
from tinkerfin import TinkerFin


class State(TypedDict):
    message: str


async def echo(state: State) -> dict[str, str]:
    return {"message": f"Echo: {state['message']}"}


builder = StateGraph(State)
builder.add_node("echo", echo)
builder.add_edge(START, "echo")
graph = builder.compile()
tinkerfin = TinkerFin()


async def main() -> None:
    run = tinkerfin.run(
        lambda: graph.astream(
            {"message": "Hello"},
            config={"configurable": {"thread_id": "thread-1"}},
            stream_mode=("messages", "tasks", "values"),
            version="v2",
            subgraphs=True,
        )
    )
    events = run.astream_agui(thread_id="thread-1", run_id="run-1")
    async for event in events:
        print(event)


asyncio.run(main())
```

Use `run.astream()` for native objects. Use `events.to_sse()` or
`run.astream().to_sse()` for direct HTTP SSE. For replayable delivery, give the
unencoded object stream to a reusable name-only Messaging channel.

## Core concepts

- `TinkerFin` is application-scoped and stateless. `TinkerFin.run(source_factory,
  principal=None, on_part=None)` binds one lazy object source.
- A `TinkerFinRun` claims exactly one object stream through `astream()` or
  `astream_agui(...)`.
- AG-UI conversion consumes v2 `messages`, `tasks`, and `values` with
  `subgraphs=True` and validates every observed part.
- `AgUiNativeStreamConfig(extra_modes=...).bind(...)` is an optional strict source
  that fixes those Graph options and is preflighted by the same `run()` method.
- Ordinary compiled subgraphs and Deep Agents `task` delegates are distinct sources.
  Namespace provenance never changes AG-UI `parentRunId`.
- Direct `to_sse()` supports custom payload mapping and event IDs. Pre-encoded SSE is
  not accepted by Messaging.
- `messaging.channel(name=...)` infers a native codec from a strict native stream and
  an AG-UI codec from `AgUiEventStream`, without pulling the first item. A channel
  handle is reusable; each source is single-use.
- The host maps `RunAgentInput` and checkpoint state to graph input or
  `Command(resume=...)`. TinkerFin does not fabricate protocol input.

## Documentation

- [Runtime and integration guide](docs/runtime.md)
- [Core package](packages/tinkerfin/README.md)
- [AG-UI adapter](packages/tinkerfin-agui-adapter/README.md)
- [Messaging](packages/tinkerfin-messaging/README.md)
- [OpenSandbox integration](packages/tinkerfin-sandbox/README.md)
- [Studio server](apps/studio/server/README.md)
- [Studio web client](apps/studio/web/README.md)

## License

Apache License 2.0. See [LICENSE](LICENSE).
