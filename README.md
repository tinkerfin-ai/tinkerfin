# TinkerFin

[中文](docs/README.zh.md)

## What it is

TinkerFin adds native, AG-UI, SSE, and durable Messaging delivery to Deep Agents.
Models, tools, backends, checkpoints, stores, and Sandbox resources remain host-owned.

```text
packages/
├── tinkerfin/                 Deep Agents runtime, AG-UI, SSE, coordination
├── tinkerfin-agui-adapter/    LangGraph v2 StreamPart to AG-UI conversion
├── tinkerfin-messaging/       durable delivery, replay, cancellation, Redis
└── tinkerfin-sandbox/         OpenSandbox backend and lifecycle management
apps/studio/
├── server/                    Studio server application
└── web/                       Studio web application
docs/                          Chinese and English usage documentation
```

## Installation

Python 3.11 or newer is required.

```bash
pip install tinkerfin
```

Optional integrations:

```bash
pip install "tinkerfin[redis]"
pip install "tinkerfin-messaging[redis]"
pip install "tinkerfin-sandbox[sqlite]"
```

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
    identity = Identity(threadId="thread-1", runId="run-1")
    runtime = agent.new_agui(identity=identity)
    events = runtime.astream(
        {"messages": [{"role": "user", "content": "Hello"}]},
    )
    async for event in events:
        print(event)


asyncio.run(main())
```

Use `agent.new()` for native LangGraph objects. Both Runtime types expose the installed
`CompiledStateGraph.astream(...)` parameter shape.

## Core concepts

- `create_deep_agent(...)` records the installed Deep Agents build call;
  `new()` / `new_agui()` creates a fresh Graph and a single-use Runtime.
- AG-UI uses v2 `messages`, `tasks`, and `values` with `subgraphs=True`; invalid stream
  options fail before iteration or lifecycle events.
- Runtime and Adapter enforce event ordering, subagent provenance, interrupt/resume,
  reasoning privacy, cancellation, backpressure, and cleanup.
- Object streams provide direct SSE and can be passed unencoded to Messaging for
  persistence, replay, attachment, and remote cancellation.
- Runtime accepts one `Identity` and injects its Graph thread. Framework
  `RUN_STARTED.input` is `None`; applications may add a validated canonical request.
- Resume uses `AgUiResumeBinding` to bind Identity, one native Command, and scoped Tool IDs.
- `TinkerFin.run(...)` runs custom asynchronous sources.

## Documentation

- [Complete documentation](docs/en/README.md)
- [Runtime](docs/en/runtime/index.md)
- [AG-UI](docs/en/agui/index.md)
- [Messaging](docs/en/messaging/index.md)
- [Sandbox](docs/en/sandbox/index.md)
- [Core package](packages/tinkerfin/README.md)
- [AG-UI adapter](packages/tinkerfin-agui-adapter/README.md)
- [Studio server](apps/studio/server/README.md)
- [Studio web client](apps/studio/web/README.md)

## License

Apache License 2.0. See [LICENSE](LICENSE).
