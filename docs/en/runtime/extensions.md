# Custom sources and run coordination

[Streams and SSE](streams-and-sse.md) · [中文](../../zh/runtime/extensions.md)

Most Deep Agents applications only need `create_deep_agent()`. Use the lower-level features here when you already have an asynchronous source or must serialize runs for one business identity.

## Run your own asynchronous source

The source factory must return a new asynchronous iterator each time it is called.

```python
import asyncio
from collections.abc import AsyncIterator

from tinkerfin import TinkerFin


async def source() -> AsyncIterator[dict[str, object]]:
    yield {"step": 1, "message": "started"}
    await asyncio.sleep(0.1)
    yield {"step": 2, "message": "finished"}


run = TinkerFin().run(source)
stream = run.astream()

async for item in stream:
    print(item)
```

### `TinkerFin.run()` parameters

| Parameter | Default | Purpose |
| --- | --- | --- |
| `source_factory` | required | Creates an async iterator, or is a bound `AgUiNativeStreamInvocation` |
| `principal` | `None` | Business identity used for coordination |
| `on_part` | `None` | Observer called before each item is delivered |

Do not reuse an async generator that has already started.

## Convert a native v2 source to AG-UI

If your source already emits LangGraph v2 parts, bind it to the required AG-UI stream profile:

```python
from tinkerfin import AgUiNativeStreamConfig, TinkerFin


invocation = AgUiNativeStreamConfig().bind(
    graph.astream,
    graph_input,
    config,
)
run = TinkerFin().run(invocation)
events = run.astream_agui(run_input=run_input)
```

`AgUiNativeStreamConfig` requires `messages`, `tasks`, `values`, `version="v2"`, and `subgraphs=True`. Add supported diagnostic modes when needed:

```python
config = AgUiNativeStreamConfig(extra_modes=("custom",))
```

Conflicting stream options fail before the run starts.

## Serialize runs for one identity

```python
from tinkerfin import InMemoryRunCoordinator, TinkerFin


coordinator = InMemoryRunCoordinator[str](
    key_resolver=lambda principal: principal,
)
tinkerfin = TinkerFin(run_coordinator=coordinator)

run = tinkerfin.run(source, principal="tenant-7/user-42")
```

Every run needs a `principal` when a coordinator is configured. Without a coordinator, it must remain `None`.

The built-in coordinator only covers the current process. If several processes must share locks, implement `RunCoordinator` with a shared lock service:

```python
from contextlib import asynccontextmanager


class RedisRunCoordinator:
    @asynccontextmanager
    async def __call__(self, principal: str):
        lock = await acquire_lock(principal)
        try:
            yield
        finally:
            await lock.release()
```

A custom coordinator must release locks during cancellation and use bounded lock waits. Coordination controls concurrency; it does not replace a checkpointer.

## Good uses for observers

Use `on_part` and AG-UI's `on_event` to:

- record metrics;
- write audit events;
- update progress;
- perform light validation before delivery.

Observers are part of the main stream path. Avoid synchronous network I/O and work that cannot be cancelled.

Next: [Runtime usage reference](api-reference.md).
