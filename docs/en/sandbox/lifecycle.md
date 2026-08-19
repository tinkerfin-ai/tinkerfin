# Sandbox lifecycle

[Sandbox basics](index.md) · [中文](../../zh/sandbox/lifecycle.md)

`OpenSandboxManager` keeps one stable handle for each business key. The same handle remains usable when its remote Sandbox reconnects or is replaced.

## Manager configuration

```python
manager = OpenSandboxManager(
    client=client,
    key_resolver=lambda key: str(key),
    state=None,
    warm_pool_size=None,
    fail_on_startup_warmup_error=False,
    settlement_timeout=None,
)
```

| Parameter | Default | Purpose |
| --- | --- | --- |
| `client` | required | Async creation, connection, inspection, and destruction |
| `key_resolver` | required | Converts application keys to stable non-empty strings |
| `state` | `None` | Binding and lease state; defaults to in-memory |
| `warm_pool_size` | `None` | Overrides the configured warm capacity |
| `fail_on_startup_warmup_error` | `False` | Makes `start()` fail if initial warmup fails |
| `settlement_timeout` | `None` | Maximum caller wait for manager close |

## Common operations

| Method | Behavior | Does the remote ID normally change? |
| --- | --- | --- |
| `get(key)` | Create, reconnect, or reuse a healthy Sandbox | Only when needed |
| `reconnect(key)` | Same behavior as `get()`, with explicit reconnect intent | Only when needed |
| `recreate(key)` | Commit a replacement and retire the old instance | Yes |
| `reset(key)` | Clear workspace-root contents | No |
| `destroy(key)` | Destroy known instances and remove the binding | Binding is removed |
| `delete(key)` | Alias for `destroy()` | Binding is removed |
| `is_healthy(key)` | Probe the current instance | No |
| `get_details(key)` | Return runtime and owner information | No |

```python
backend = await manager.get(project_key)

if not await manager.is_healthy(project_key):
    backend = await manager.recreate(project_key)

details = await manager.get_details(project_key)
```

Most requests only need `get()`. Recreating on every request defeats reuse and warm capacity.

## Start and close

`async with manager` calls `start()` and `aclose()` for you. Manual control looks like this:

```python
await manager.start()
try:
    backend = await manager.get(key)
finally:
    await manager.aclose()
```

`start()` is idempotent. A closed manager cannot be restarted.

Close waits for active creation, replacement, reset, and cleanup to settle safely. A finite `settlement_timeout` only limits this caller's wait. It raises `OpenSandboxSettlementTimeoutError` without cancelling owned cleanup; call `aclose()` later to continue waiting.

## Health checks and replacement

`OpenSandboxConfig.health_command` probes the data plane and defaults to `printf ok`. When `get()` finds an unhealthy binding, it creates a replacement and updates the stable handle.

In-flight operations finish against the backend they acquired. The old instance is not closed underneath them, and replacement waits for safe retirement before returning.

## Cancellation safety

After creation, health checking, replacement, reset, destroy, or close begins, the manager retains cleanup responsibility even if the requesting task is cancelled. A caller receiving cancellation does not mean remote cleanup has finished.

## Inspect details

```python
details = await manager.get_details(key)
if details is not None:
    print(details.sandbox_id)
    print(details.available, details.healthy)
    print(details.owner_key, details.cached)
```

`None` means no known binding. When `available=False`, `unavailable_reason` is `not_found` or `unreachable`.

Next: [Rooted files and commands](rooted-filesystem.md).

