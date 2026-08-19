# Persistent state and extensions

[Rooted files and commands](rooted-filesystem.md) · [中文](../../zh/sandbox/persistence-and-extensions.md)

Default state exists only in the current process. Use SQLAlchemy state when multiple workers share bindings or when a restart must recover them.

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

## Generate schema before deployment

```python
from pathlib import Path
from tinkerfin_sandbox import get_sqlalchemy_opensandbox_state_schema


schema = get_sqlalchemy_opensandbox_state_schema(dialect="mysql")
Path("opensandbox-schema.sql").write_text(schema.ddl, encoding="utf-8")
```

`dialect` is `mysql` or `sqlite`. The returned value also provides `component`, `version`, and `table_names`. Runtime startup still validates the deployed structure.

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

## Initialize newly created instances

```python
async def install_project(backend) -> None:
    await backend.aexecute("git clone https://example.com/project.git /workspace/project")


client = OpenSandboxClient(
    connection_config=connection_config,
    config=config,
    initializers=[install_project],
)
```

Initializers run after a new Sandbox becomes ready. Keep them asynchronous, cancellable, observable, and deterministic for each fresh instance.

## Bring your own state store

Implement `OpenSandboxState` to store bindings and leases in an existing database.

| Capability | Methods |
| --- | --- |
| Lifetime | `start()`, `aclose()` |
| Owner | `acquire_owner()`, `renew_owner()`, `bind_owner()`, `unbind_owner()`, `release_owner()`, `read_binding()` |
| Warm pool | `claim_warm_slot()`, `renew_warm()`, `publish_warm()`, `release_warm()`, `consume_warm()` |
| Cleanup | `enqueue_cleanup()`, `claim_cleanup()`, `renew_cleanup()`, `complete_cleanup()`, `release_cleanup()` |
| Shutdown recovery | `shutdown_sandbox_ids()` |

A custom implementation needs atomic claims, generation fencing, lease renewal, and idempotent release. After an uncertain network result, never destroy a Sandbox that may already be the authoritative binding.

`InMemoryOpenSandboxState(namespace=...)` demonstrates the behavior but does not share state across processes.

Next: [Sandbox usage reference](api-reference.md).
