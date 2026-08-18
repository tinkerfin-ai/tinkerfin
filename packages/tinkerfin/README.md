# tinkerfin

## What it is

`tinkerfin` binds caller-owned asynchronous Graph invocations to native object streams
or validated AG-UI events. Either object stream can render direct SSE. The package
does not construct graphs, copy LangGraph invocation signatures, map HTTP requests,
query checkpoints, or own models, tools, stores, and backends.

## Installation

Python 3.11 or newer is required.

```bash
pip install tinkerfin
```

Redis-backed principal coordination is optional:

```bash
pip install "tinkerfin[redis]"
```

## Quick Start

```python
import asyncio
from typing import TypedDict

from langgraph.graph import START, StateGraph
from tinkerfin import TinkerFin


class State(TypedDict):
    value: int


async def increment(state: State) -> dict[str, int]:
    return {"value": state["value"] + 1}


builder = StateGraph(State)
builder.add_node("increment", increment)
builder.add_edge(START, "increment")
graph = builder.compile()
tinkerfin = TinkerFin()


async def main() -> None:
    run = tinkerfin.run(
        lambda: graph.astream(
            {"value": 1},
            stream_mode=("messages", "tasks", "values"),
            version="v2",
            subgraphs=True,
        )
    )
    async for part in run.astream():
        print(part)


asyncio.run(main())
```

The zero-argument factory must return a fresh, unconsumed `AsyncIterator`. Invocation
is lazy. TinkerFin owns and closes that iterator but does not own resources captured by
the closure.

## Core concepts

### Run binding

```python
tinkerfin = TinkerFin(run_coordinator=coordinator)

run = tinkerfin.run(
    lambda: graph.astream(
        graph_input,
        config,
        context=context,
        stream_mode=("messages", "tasks", "values"),
        print_mode=(),
        output_keys=None,
        interrupt_before=None,
        interrupt_after=None,
        durability=None,
        control=None,
        subgraphs=True,
        debug=None,
        version="v2",
    ),
    principal=principal,
    on_part=on_part,
)
```

- `run_coordinator=None` disables principal coordination and rejects a non-`None`
  principal.
- A configured coordinator requires `principal` for every run.
- `on_part(part)` is awaited before native delivery or AG-UI conversion and cannot
  replace the part.
- One `TinkerFinRun` creates exactly one object stream. Create another run binding for
  another consumer or request.

### Native objects

```python
parts = run.astream()
async for part in parts:
    ...
```

`astream()` accepts no graph parameters. Ordering, pull-based backpressure, exceptions,
cancellation, coordinator release, and upstream `aclose()` are preserved.

### AG-UI objects

```python
events = run.astream_agui(
    thread_id=thread_id,
    run_id=run_id,
    parent_run_id=parent_run_id,
    timeout=None,
    settlement_timeout=None,
    expose_reasoning_events=False,
    expose_subagent_events=True,
    prior_tool_call_ids=frozenset(),
    on_event=on_event,
)
```

`on_event(event)` is awaited before delivery and cannot replace the event. The stream
emits one `RUN_STARTED` and exactly one main terminal. `abort()` returns the remaining
AG-UI cancellation tail, while `aclose()` stops consumption without fabricating a
successful terminal. `timeout` is the total native-part pull deadline.
`settlement_timeout` limits only how long one caller waits after close settlement
starts. Its default `None` waits without a deadline. A finite deadline raises
`AgUiSettlementTimeoutError` without cancelling the owned close task; a later
`aclose()` continues waiting for that same task.

AG-UI sources must provide LangGraph v2 `messages`, `tasks`, and `values` with
`subgraphs=True`. The default `run(source_factory).astream_agui(...)` path validates
each observed envelope without inspecting the source factory's closure.

### Optional strict native source

Use a strict source when Graph options must be validated before any Graph, iterator,
coordinator, observer, or AG-UI lifecycle work begins:

```python
from tinkerfin import AgUiNativeStreamConfig

run = tinkerfin.run(
    AgUiNativeStreamConfig(extra_modes=("custom",)).bind(
        graph.astream,
        graph_input,
        config,
        context=context,
    ),
    principal=principal,
    on_part=on_part,
)
```

The strict source always injects `messages`, `tasks`, and `values`, `version="v2"`,
and `subgraphs=True`. `extra_modes` accepts `updates`, `checkpoints`, `debug`, and
`custom`. Reserved options cannot be overridden after binding, and Graph invocation
remains lazy after successful preflight.

Its `run.astream()` returns `NativeGraphRunStream`, whose Messaging profile declares
live `Mapping[str, object]` envelopes and decoded `NativeStreamPart` replay. All seven
supported modes share the same native model, codec, and SSE representation. A generic
`run(...).astream()` remains profile-free; persist arbitrary objects with an explicit
matching Messaging codec.

Ordinary compiled subgraphs use `compiled_subgraph` provenance. Only a compiled scope
also correlated with a Deep Agents `task` Tool call uses `deep_agent_subagent`
provenance. Child state never replaces root state, and subagent nesting never uses
AG-UI `parentRunId`.

`prior_tool_call_ids` accepts only complete scoped `tf:tool:...` IDs. This lets a
resumed child Tool result reuse its published identity without replaying Tool
start/args/end events.

### Direct SSE

```python
from starlette.responses import StreamingResponse

body = events.to_sse(
    mapper=event_mapper,
    event_id_resolver=event_id_resolver,
)
await body.prepare(preflight=authorize_request)

return StreamingResponse(
    body,
    media_type="text/event-stream",
    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
)
```

Native streams expose the same method with an additional total `timeout` parameter:

```python
body = run.astream().to_sse(
    timeout=None,
    mapper=native_mapper,
    event_id_resolver=native_event_id_resolver,
)
```

A mapper is an async callable returning `SsePayload` or `None`; `None` filters the
item. `SsePayload` controls `data`, optional `event`, and optional non-negative
`retry`. The separate async ID resolver returns `str`, `int`, or `None`. Runtime
validates line breaks and null bytes before framing. Custom data normalizes CRLF and
CR to LF, preserves a trailing LF through an empty final `data:` field, and leaves
other Unicode separators untouched.

`prepare()` is optional and never pulls the source or calls the mapper. It runs host
preflight before response construction and closes the body if preflight fails. Errors
raised after HTTP headers are committed terminate the body and cannot change the
status code.

### Coordination

`InMemoryRunCoordinator` and `RedisRunCoordinator` serialize runs for the same
principal key. They do not store graph state or replace a LangGraph checkpointer. A
coordinator is application-owned and supplied once to `TinkerFin`.

### Durable delivery

Direct SSE is single-connection delivery. For persistence, replay, remote cancellation,
and multi-worker Redis ownership, pass an unencoded `NativeGraphRunStream`, an
`AgUiEventStream`, or a generic object stream with an explicit codec to
`tinkerfin-messaging`. Do not pass an `SseBody` through a Messaging codec.

## Documentation

- [Runtime guide](https://github.com/tinkerfin-ai/tinkerfin/blob/main/docs/runtime.md)
- [AG-UI adapter](https://github.com/tinkerfin-ai/tinkerfin/tree/main/packages/tinkerfin-agui-adapter)

## License

Apache License 2.0. See the
[repository license](https://github.com/tinkerfin-ai/tinkerfin/blob/main/LICENSE).
