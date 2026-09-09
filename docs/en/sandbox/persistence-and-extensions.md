# Persistent state and extensions

[Rooted files and commands](rooted-filesystem.md) · [中文](../../cn/sandbox/persistence-and-extensions.md)

Default state exists only in the current process. Use SQLAlchemy state when multiple workers share bindings or when a restart must recover them.

State stores bindings and leases, not container files. For a workspace kept until
explicit cleanup, combine persistent State with `OpenSandboxConfig(ttl=None)`.
Normal manager close then preserves its binding and remote instance. Volumes and a
backup policy are still needed when files must survive instance or storage loss.

## SQLite for multiple local processes

```bash
pip install "tinkerfin-sandbox[sqlite]"
```

```python
from tinkerfin_sandbox import (
    OpenSandboxManager,
    SQLAlchemyOpenSandboxState,
)


state = SQLAlchemyOpenSandboxState(
    url="sqlite+aiosqlite:////var/lib/app/opensandbox.db",
    namespace="production",
    lease_ttl=15.0,
    poll_interval=0.05,
    sqlite_retry_timeout=5.0,
)
manager = OpenSandboxManager(
    client=client,
    key_resolver=key_resolver,
    state=state,
)
```

## MySQL for workers on several hosts

```bash
pip install "tinkerfin-sandbox[mysql]"
```

```python
state = SQLAlchemyOpenSandboxState(
    url="mysql+asyncmy://user:password@db/sandbox_state",
    namespace="production",
)
```

MySQL 5.7 and MySQL 8.x are supported. MariaDB is not currently in the verified boundary.

### State parameters

| Parameter | Default | Purpose |
| --- | --- | --- |
| `url` | required | SQLAlchemy asynchronous connection URL |
| `namespace` | `""` | Isolates deployments in one database |
| `lease_ttl` | `15.0` | Owner, warm-slot, and cleanup lease seconds |
| `poll_interval` | `0.05` | Base wait and retry interval |
| `sqlite_retry_timeout` | `5.0` | Total SQLite lock retry budget |

All workers in one namespace must use the same warm-pool size. The database account needs schema and data permissions during first startup.

Cancellation waits for database results to be consumed and the connection to return
to its pool. A cancellation observed before COMMIT begins rolls back the write;
once COMMIT has been issued, State waits for its confirmed or uncertain outcome.
This necessary settlement can extend the caller's work deadline. It does not
serialize independent transactions or change the SQLite lock retry budget.

## Generate schema before deployment

```python
from pathlib import Path
from tinkerfin_sandbox import get_sqlalchemy_opensandbox_state_schema


schema = get_sqlalchemy_opensandbox_state_schema(dialect="mysql")
Path("opensandbox-schema.sql").write_text(schema.ddl, encoding="utf-8")
```

`dialect` is `mysql` or `sqlite`. The returned value also provides `table_names`.
Runtime startup still validates the complete deployed table, column, primary-key, and
index structure, including the exact index set and uniqueness flags.

## Warm capacity

```python
config = OpenSandboxConfig(warm_pool_size=2)
manager = OpenSandboxManager(
    client=client,
    key_resolver=key_resolver,
    state=state,
    warm_pool_size=2,
)
```

Warm instances are not bound to an application key until `get()` atomically consumes a ready slot.

## Prepare workspaces

```python
async def prepare_project(backend) -> None:
    result = await backend.aexecute(
        "mkdir -p /workspace/project /workspace/output"
    )
    if result.exit_code != 0:
        raise RuntimeError("Could not prepare workspace directories")


client = OpenSandboxClient(
    connection_config=connection_config,
    config=config,
    initializers=[prepare_project],
)
```

Initializers run after creation and each connection to an existing Sandbox. Make
them idempotent and preserve existing workspace contents. Use asynchronous callbacks
for I/O and propagate cancellation; synchronous callbacks must be non-blocking.
Connection and initialization share the earlier client or recovery deadline.
Initializer failure is reported as `OpenSandboxInitializationError` and does not
authorize retries or recreation. See the [usage reference](api-reference.md) for
callback and timeout constraints.

## Bring your own state store

Implement `OpenSandboxState` to store bindings and leases in an existing database.

| Capability | Methods |
| --- | --- |
| Lifetime | `start()`, `aclose()` |
| Owner | `acquire_owner()`, `renew_owner()`, `bind_owner()`, `unbind_owner()`, `release_owner()`, `read_binding()` |
| Warm pool | `claim_warm_slot()`, `claim_ready_warm_slot()`, `renew_warm()`, `publish_warm()`, `discard_ready_warm_slot()`, `release_warm()`, `warm_pool_ready()`, `consume_warm()` |
| Cleanup | `enqueue_cleanup()`, `claim_cleanup()`, `renew_cleanup()`, `complete_cleanup()`, `release_cleanup()` |
| Shutdown recovery | `shutdown_sandbox_ids()` |

A custom implementation needs atomic claims, generation fencing, lease renewal, and
idempotent release. Ready-slot claims must preserve the published ID while it is probed;
discarding a proven unusable ID must clear the slot and enqueue cleanup in one atomic
transition. After an uncertain network result, never destroy a Sandbox that may already
be the authoritative binding.

`InMemoryOpenSandboxState(namespace=...)` demonstrates the behavior but does not share state across processes.

Next: [Sandbox usage reference](api-reference.md).
