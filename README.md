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
docs/                          runtime and integration documentation
```

## Installation

Python 3.11 or newer is required.

```bash
pip install tinkerfin
```

Optional integrations:

```bash
pip install "tinkerfin[redis]"
pip install "tinkerfin-messaging[agui,native,redis]"
pip install "tinkerfin-sandbox[sqlite]"
```

## Quick Start

```python
import asyncio

from ag_ui.core import RunAgentInput
from tinkerfin import TinkerFin

tinkerfin = TinkerFin()
agent = tinkerfin.create_deep_agent(
    model="openai:gpt-5.4",
    tools=[],
)


async def main() -> None:
    run_input = RunAgentInput.model_validate(
        {
            "threadId": "thread-1",
            "runId": "run-1",
            "state": {},
            "messages": [],
            "tools": [],
            "context": [],
            "forwardedProps": {},
        }
    )
    runtime = agent.new_agui(run_input=run_input)
    events = runtime.astream(
        {"messages": [{"role": "user", "content": "Hello"}]},
        {"configurable": {"thread_id": "thread-1"}},
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
- Existing event ordering, subagent provenance, interrupt/resume, reasoning privacy,
  cancellation, backpressure, and cleanup are preserved.
- Object streams provide direct SSE and can be passed unencoded to Messaging for
  persistence, replay, attachment, and remote cancellation.
- `RUN_STARTED.input` carries the complete caller `RunAgentInput`; resumed requests use
  `AgUiResumeBinding` to bind that input to one native Command and its scoped Tool IDs.
- `TinkerFin.run(...)` remains available for custom asynchronous sources.

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
