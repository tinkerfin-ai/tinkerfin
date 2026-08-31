# TinkerFin

[中文](docs/README.zh.md)

## What it is

TinkerFin adds native, AG-UI, SSE, semantic Trace, and durable Messaging delivery to Deep Agents.
Models, tools, backends, checkpoints, stores, and Sandbox resources remain host-owned.

```text
packages/
├── tinkerfin-contracts/       protocol-neutral run and observation contracts
├── tinkerfin-native-stream/   current Deep Agents/LangGraph stream contract
├── tinkerfin/                 Deep Agents runtime, optional AG-UI, SSE, coordination
├── tinkerfin-agui-adapter/    LangGraph v2 StreamPart to AG-UI conversion
├── tinkerfin-tracing/         semantic Ledger, queries, and projections
├── tinkerfin-langgraph-mysql/ asyncmy-only LangGraph MySQL Store
├── tinkerfin-messaging/       durable delivery, replay, cancellation, Redis
└── tinkerfin-sandbox/         OpenSandbox backend and lifecycle management
apps/studio/
├── server/                    Studio server application
└── web/                       Studio web application
```

## Installation

Python 3.11 or newer is required.

```bash
pip install tinkerfin
```

The Quick Start uses LangChain's OpenAI model adapter:

```bash
pip install langchain-openai
```

Optional integrations:

```bash
pip install "tinkerfin[agui]"
pip install "tinkerfin[redis]"
pip install tinkerfin-tracing
pip install "tinkerfin-tracing[mysql]"
pip install tinkerfin-langgraph-mysql
pip install "tinkerfin-messaging[agui,redis]"
pip install "tinkerfin-sandbox[sqlite]"
```

## Quick Start

```python
import asyncio

from tinkerfin import RunIdentity, TinkerFin

tinkerfin = TinkerFin()
agent = tinkerfin.create_deep_agent(
    model="openai:gpt-5.4",
    tools=[],
)


async def main() -> None:
    identity = RunIdentity(threadId="thread-1", runId="run-1")
    parts = await tinkerfin.open_run(
        identity,
        agent=agent,
        input={"messages": [{"role": "user", "content": "Hello"}]},
    )
    async for part in parts:
        print(part)


asyncio.run(main())
```

`open_run()` owns definition resolution, asynchronous Graph construction, observation,
coordination, cancellation, and cleanup. Use `await agent.create_graph()` only when an
advanced integration needs a reusable direct LangGraph-style `Runnable` without managed
run identity, Trace, AG-UI, or Messaging lifecycle.

## Core concepts

- `create_deep_agent(...)` records the installed Deep Agents build call;
  `open_run()` and `open_agui_run()` are the ordinary managed execution paths.
- Native Runtime, Plan Mode, Observation, and SSE are included by default;
  `open_agui_run()` requires `tinkerfin[agui]`.
- `TinkerFin(state_schema=...)` contributes application state to every Deep Agent
  Definition created by that factory; Definition state and middleware state are merged
  without weakening reducers or requiredness.
- `.plan(enabled=True)` adds one stable parent workflow without changing the installed
  `create_deep_agent(...)` signature. Choose `mode="default"` or `mode="plan"` on each
  managed run or direct Graph; selecting Plan requires a concrete checkpointer.
- One explicit Runtime Profile owns graph construction, required stream options,
  Native validation, observations, and canonical replay; conflicting or partial
  upstream options fail before iteration or lifecycle events.
- Runtime and Adapter enforce event ordering, subagent provenance, interrupt/resume,
  reasoning privacy, cancellation, backpressure, and cleanup.
- Object streams provide direct SSE and can be passed unencoded to Messaging for
  persistence, replay, attachment, and remote cancellation.
- AG-UI Runtime uses one `RunIdentity` for public events, Graph execution, checkpoints, and
  durable delivery; optional `parent_run_id` creates a real checkpoint branch.
- `open_agui_run(resume=...)` accepts an `AgUiResumeRequest`, reads the canonical
  checkpoint, and owns native commands, Tool correlation, cancellation, and durable
  checkpoint evidence. Binding APIs remain available only for advanced event-log
  integrations.
- `.observe(Tracer())` records fail-closed Runtime lifecycle and validated Native semantic
  facts without recording AG-UI, Messaging, SSE, or Redis delivery state.
- The framework distribution provides the explicit `deepagents-v2` Runtime Profile. It
  does not provide a Deep Agents v3 Profile or a generic TodoGroups projection/UI.
  Studio derives its product-specific task trace from canonical Trace facts at query
  time; another real Profile must emit the same contract without downstream version
  branches.
- Archive/S3/Blob, payload encryption/KMS, and OpenTelemetry exporters are not provided.
  Active Trace storage implements `TraceLedgerBackend`; advanced integrations may
  replace `TraceStore`, wrap the canonical codec, observe `RuntimeObserver`, or decorate
  Store/Messaging Backend operations.

## Documentation

- [Complete documentation](docs/en/README.md)
- [Runtime](docs/en/runtime/index.md)
- [AG-UI](docs/en/agui/index.md)
- [Messaging](docs/en/messaging/index.md)
- [Tracing](docs/en/tracing/index.md)
- [Sandbox](docs/en/sandbox/index.md)
- [Core package](packages/tinkerfin/README.md)
- [Shared contracts](packages/tinkerfin-contracts/README.md)
- [AG-UI adapter](packages/tinkerfin-agui-adapter/README.md)
- [Tracing package](packages/tinkerfin-tracing/README.md)
- [LangGraph MySQL Store](packages/tinkerfin-langgraph-mysql/README.md)
- [Studio server](apps/studio/server/README.md)
- [Studio web client](apps/studio/web/README.md)

## License

Apache License 2.0 is the repository default; see [LICENSE](LICENSE).
`tinkerfin-langgraph-mysql`, derived from the upstream LangGraph MySQL Store, is
distributed under its packaged [MIT LICENSE](packages/tinkerfin-langgraph-mysql/LICENSE)
and [NOTICE](packages/tinkerfin-langgraph-mysql/NOTICE).
