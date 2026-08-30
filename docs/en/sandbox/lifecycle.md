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

Warm-pool sizes are strict integers. Command and lifecycle timeouts are finite numeric
values; booleans are rejected before State startup or task creation.

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
| `check_ready()` | Raise unless configured warm capacity is verified | No |

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

Startup fences every published warm slot, reconnects it, runs the data-plane health
command, and renews its remote expiry. Missing instances are replaced before startup
returns. With `fail_on_startup_warmup_error=True`, any authentication, reconnect,
health, renewal, creation, or State publication failure propagates and the host must
not report ready.

While open, the manager periodically renews or replaces warm instances. A failed
background refill does not invalidate an owner backend already handed to a request,
but `check_ready()` raises `OpenSandboxWarmPoolUnavailableError` until capacity is
restored. Hosts should include that method in their readiness check.

Close waits for active creation, replacement, reset, and cleanup to settle safely. A finite `settlement_timeout` only limits this caller's wait. It raises `OpenSandboxSettlementTimeoutError` without cancelling owned cleanup; call `aclose()` later to continue waiting.

## Health checks and replacement

`OpenSandboxConfig.health_command` probes the data plane and defaults to `printf ok`. When `get()` finds an unhealthy binding, it creates a replacement and updates the stable handle.

Persistent State stores binding and fencing identity, not container files. When an
owner Sandbox expires, the next `get()` creates a replacement. Persisting workspace
contents across remote expiry requires a volume or snapshot policy configured by the
host.

In-flight operations finish against the backend they acquired. The old instance is not closed underneath them, and replacement waits for safe retirement before returning.

## Cancellation safety

After creation, health checking, replacement, reset, destroy, or close begins, the manager retains cleanup responsibility even if the requesting task is cancelled. A caller receiving cancellation does not mean remote cleanup has finished.

`OpenSandboxClient.destroy()` retains one task per Sandbox ID. Concurrent callers join
that same remote kill and local close. Caller cancellation waits for settlement and then
propagates. Once kill succeeds, an SDK close failure is logged as cleanup evidence rather
than reported as a remote destruction failure. Client close waits for all active destroy
tasks before closing the shared transport it created when `ConnectionConfig` omitted
one. Concurrent client-close callers join one retained settlement. Cancelling a waiter
does not cancel transport closure, and a close task that fails can be retried without
losing ownership. A caller-supplied transport remains borrowed and is never closed by
the client.

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
