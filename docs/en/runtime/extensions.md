# Custom sources and run coordination

[Streams and SSE](streams-and-sse.md) · [中文](../../zh/runtime/extensions.md)

Most Deep Agents applications only need `create_deep_agent()`. Use the lower-level features here when you already have an asynchronous source or must serialize runs for one business identity.

## Run your own asynchronous source

The source factory must return a new asynchronous iterator each time it is called.

```python
import asyncio
from collections.abc import AsyncIterator

from tinkerfin import Identity, TinkerFin


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
| `identity` | `None` | Optional for stateless custom sources; required for coordination, AG-UI, or Messaging |
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
identity = Identity(threadId="thread-1", runId="run-1")
run = TinkerFin().run(invocation, identity=identity)
events = run.astream_agui()
```

`AgUiNativeStreamConfig` requires `messages`, `tasks`, `values`, `version="v2"`, and `subgraphs=True`. Add supported diagnostic modes when needed:

```python
config = AgUiNativeStreamConfig(extra_modes=("custom",))
```

Conflicting stream options fail before the run starts.

## Serialize runs for one identity

```python
from tinkerfin import InMemoryRunCoordinator, TinkerFin


coordinator = InMemoryRunCoordinator(
    key_resolver=lambda identity: identity.thread_id,
)
tinkerfin = TinkerFin(run_coordinator=coordinator)

identity = Identity(threadId="tenant-7/user-42", runId="run-1")
run = tinkerfin.run(source, identity=identity)
```

Every coordinated run needs an `Identity`. A custom source without a coordinator may omit it, but then it has no durable identity profile for Messaging.

The built-in coordinator only covers the current process. If several processes must share locks, implement `RunCoordinator` with a shared lock service:

```python
from contextlib import asynccontextmanager


class CustomRunCoordinator:
    @asynccontextmanager
    async def __call__(self, identity: Identity):
        lock = await acquire_lock(identity.thread_id)
        try:
            yield
        finally:
            await lock.release()
```

A custom coordinator must release locks during cancellation and use bounded lock waits. Coordination controls concurrency; it does not replace a checkpointer.

## Use a renewable Redis lease when needed

```bash
pip install "tinkerfin[redis]"
```

```python
from tinkerfin.redis import RedisLeaseLock


lock = RedisLeaseLock.from_client(redis, key_prefix="my-app:locks")

async with lock:
    async with lock.hold("invoice-42") as lease:
        print(lease.fencing_token)
        await update_invoice()
```

The lock can also create and own its Redis client:

```python
lock = RedisLeaseLock.from_url(
    "redis://localhost:6379/0",
    key_prefix="my-app:locks",
)
```

| Constructor | Required input | Who closes the Redis client |
| --- | --- | --- |
| `from_client(client, ...)` | Async Redis client | Caller; the lock borrows it |
| `from_url(url, ...)` | Redis URL | `RedisLeaseLock` |

Both constructors accept the same options:

| Parameter | Default | Purpose |
| --- | --- | --- |
| `key_prefix` | `"tinkerfin:lease:v1:"` | Isolates this application's lock keys; must be non-blank without surrounding whitespace |
| `lease_ttl_seconds` | `30.0` | Redis TTL for one lease; must be positive |
| `renew_interval_seconds` | `None` | `None` means one third of the TTL; an explicit value must be less than half the TTL |
| `wait_poll_seconds` | `0.1` | Delay before retrying an occupied resource; must be positive |

`hold(resource_key)` waits for that resource and yields an immutable `RedisLease` containing `resource_key` and `fencing_token`. Waiting is cancellable, and one lock instance can manage different resource keys concurrently.

The lock renews automatically. An uncertain result, timeout, connection failure, or owner-token mismatch cancels the owner task. Exit waits for renewal cleanup and releases only when the owner token still matches. Redis failure is fail-closed and never falls back to unlocked execution.

After a lease expires, a new holder may enter while a paused old holder later resumes and attempts an external write. The fencing token increases for each successful acquisition of the same resource; use it in conditional writes when the external store must reject stale holders. Applications that only need Redis exclusion may ignore fencing and decide whether database locking or another consistency mechanism is necessary.

Closing `RedisLeaseLock` rejects new scopes and waits for active scopes to clean up. Fencing counters remain in Redis to prevent rollback during normal operation. After Redis data loss or restoration, the application must decide how external fencing monotonicity is preserved.

Active leases, waiters, and releases issue short commands through the shared Redis pool; they do not reserve a connection between polling or renewal attempts. Size the pool for peak concurrent acquire, renew, release, and application Redis work. Redis Cluster is unsupported.

## Good uses for observers

Use `on_part` and AG-UI's `on_event` to:

- record metrics;
- write audit events;
- update progress;
- perform light validation before delivery.

Observers are part of the main stream path. Avoid synchronous network I/O and work that cannot be cancelled.

Next: [Runtime usage reference](api-reference.md).
