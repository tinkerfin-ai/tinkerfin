# Sandbox

[Documentation](../index.md) · [中文](../../cn/sandbox/index.md)

`tinkerfin-sandbox` provides asynchronous files, commands, reusable isolated
environments, persistent bindings, warm capacity, pause, resume, and cleanup.

## Installation

```bash
pip install tinkerfin-sandbox
```

For persistent bindings, install the SQLAlchemy extra and one asynchronous driver:

```bash
pip install "tinkerfin-sandbox[sqlalchemy]" aiosqlite
```

Use `asyncpg` for PostgreSQL or `asyncmy` for MySQL.

## Use a Sandbox with AgentRuntime

The application chooses both scopes:

- Runtime `namespace` selects the business isolation scope.
- `workspace_key` selects which runs share a Sandbox inside that scope.

The key may represent a user, session, project, or another application policy.

```python
from opensandbox.config import ConnectionConfig
from tinkerfin import TinkerFin
from tinkerfin_sandbox import OpenSandboxClient, OpenSandboxConfig, OpenSandboxManager

client = OpenSandboxClient(
    connection_config=ConnectionConfig(domain="127.0.0.1:8091"),
    config=OpenSandboxConfig(workspace_root="/workspace"),
)

async with OpenSandboxManager(client=client) as sandboxes:
    runtime = (
        TinkerFin()
        .with_namespace("company-a")
        .build(
            model=model,
            backend=sandboxes.workspace("users/user-7"),
        )
    )
    result = await runtime.ainvoke(
        thread_id=thread_id,
        run_id=run_id,
        input=graph_input,
    )
```

`workspace(...)` performs no I/O. The Runtime opens or reconnects the Sandbox only for
an admitted run and releases the run's handle during cleanup. Finishing a run does not
destroy a persistent Sandbox.

## Manage a Sandbox directly

Use direct manager methods when application code needs the environment outside an
agent run:

```python
backend = await sandboxes.get("projects/project-1", namespace="company-a")
await backend.awrite("/notes.txt", "hello")
result = await backend.aexecute("python -m pytest", timeout=300)
```

| Task | Method |
| --- | --- |
| Open or reuse | `get(key)` |
| Reconnect | `reconnect(key)` |
| Replace | `recreate(key)` |
| Clear workspace files | `reset(key)` |
| Pause or resume | `pause(key)`, `resume(key)` |
| Destroy | `destroy(key)` |
| Inspect | `get_details(key)` |
| Close local resources | `aclose()` |

## Persistence and ownership

`SQLAlchemyOpenSandboxState` supports SQLite, MySQL, and PostgreSQL through a borrowed
SQLAlchemy `AsyncEngine`. The application creates and disposes the Engine. State stores
bindings and lifecycle coordination; files remain in the Sandbox or attached volumes.

The manager owns its OpenSandbox client and State. A caller-supplied HTTP transport
remains caller-owned. Persistent State retains remote Sandboxes when the manager closes;
in-memory State destroys the instances it created.

File tools are confined to `workspace_root` and reject escaping paths and links. Shell
commands are a separate Sandbox capability and are not restricted by the file root.

## Next steps

- [Lifecycle](lifecycle.md)
- [Files and commands](rooted-filesystem.md)
- [Persistent state and extensions](persistence-and-extensions.md)
- [Sandbox API](api-reference.md)
