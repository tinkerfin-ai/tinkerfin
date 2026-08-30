# tinkerfin-sandbox

## What it is

`tinkerfin-sandbox` adapts OpenSandbox SDK 0.1.14 to asynchronous Deep Agents backend
and lifecycle contracts. It provides stable caller-keyed Sandboxes, reconnection,
health replacement, warm capacity, rooted paths, reset and destroy operations,
cancellation-safe cleanup, and optional multi-worker allocation state.

The package does not depend on `tinkerfin`. Authentication, tenancy policy, key
selection, graph construction, and HTTP behavior belong to the host.

## Installation

Python 3.11 or newer is required.

```bash
pip install tinkerfin-sandbox
```

The default allocation state is process-local. Install one asynchronous database
extra when multiple workers must share bindings and cleanup work:

```bash
pip install "tinkerfin-sandbox[sqlite]"
pip install "tinkerfin-sandbox[mysql]"
```

## Quick Start

`key_resolver` is required and accepts any application key type. Obtain the Sandbox
backend before constructing the graph:

```python
from deepagents import create_deep_agent
from opensandbox.config import ConnectionConfig

from tinkerfin_sandbox import (
    OpenSandboxClient,
    OpenSandboxConfig,
    OpenSandboxManager,
)

manager = OpenSandboxManager[str](
    client=OpenSandboxClient(
        connection_config=ConnectionConfig(domain="127.0.0.1:8091"),
        config=OpenSandboxConfig(workspace_root="/workspace"),
    ),
    key_resolver=lambda owner: owner,
)

async with manager:
    backend = await manager.get("tenant-1/user-7")
    graph = create_deep_agent(
        model=model,
        backend=backend,
        middleware=manager.build_agent_middleware(backend),
    )
```

The manager owns its client and state. The backend returned by `get()` is borrowed by
the graph and remains valid until the manager closes or that key is explicitly
destroyed.

OpenSandbox SDK reads `OPEN_SANDBOX_DOMAIN` and `OPEN_SANDBOX_API_KEY` when a client
uses its environment configuration. Credentials, image policy, volumes, and physical
paths must remain host configuration.

## Caller-defined keys

The manager has no built-in user, agent, thread, or workspace isolation enum. The
host chooses a key type and a stable non-blank string resolver:

```python
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProjectKey:
    organization: str
    project: str


manager = OpenSandboxManager[ProjectKey](
    client=client,
    key_resolver=lambda key: f"{key.organization}/{key.project}",
)
```

Calls resolving to the same string share one stable handle and serialized lifecycle
transitions. Different resolved keys can proceed concurrently. Raw keys are not placed
in remote OpenSandbox owner labels; those labels use stable digests.

## Lifecycle

- `get(key)` creates, reconnects, or reuses a healthy backend;
- `reconnect(key)` is an alias for `get(key)`, including creation when no binding
  exists;
- `recreate(key)` commits a replacement and retires the previous instance;
- `reset(key)` clears the configured `workspace_root` without changing the binding;
- `destroy(key)` destroys known remote instances and removes the binding;
- `get_details(key)` returns a stable owner-aware runtime snapshot;
- `check_ready()` raises when configured warm capacity is degraded;
- `aclose()` waits for active operations and closes owned local resources.

`settlement_timeout=None` keeps the default complete wait. A finite constructor value
limits only each caller's wait: expiry raises `OpenSandboxSettlementTimeoutError`,
leaves the manager unavailable to new work, and does not cancel the owned close task.
Call `aclose()` again to continue waiting for that same task.

Existing handles keep object identity across remote replacement. In-flight operations
finish against their acquired backend before the old instance is retired, and the
replacement call waits for that retirement before returning. Creation, health
checking, replacement, reset, destroy, and shutdown retain cleanup ownership when the
calling task is cancelled.

`OpenSandboxClient.destroy()` uses one retained task per Sandbox ID. Concurrent callers
join the same kill and local-close lifecycle. Caller cancellation waits for that owned
settlement and then propagates; a confirmed remote kill is not reported as failed only
because the SDK close step also fails. `aclose()` waits for every active destroy task
before closing the transport that the client created when `ConnectionConfig` omitted
one. Concurrent close callers join one retained settlement; cancelling a waiter does
not cancel transport closure, and a failed close remains retryable. A transport supplied
by the caller remains borrowed and is never closed by the client.

Warm-pool capacities are strict integers, and command timeouts are strict finite numeric
values; booleans are rejected before State startup or task creation. State and
SQLAlchemy constructors apply the same boundary so invalid capacity cannot fail later
inside warmup.

Published warm slots are fenced, reconnected, health-checked, and renewed before they
count as ready. Missing or expired instances are replaced atomically. The manager keeps
ready instances renewed while it is open and retries failed background replenishment;
`check_ready()` exposes degraded capacity to a host without leaking provider details.
With `fail_on_startup_warmup_error=True`, authentication, reconnect, health, renewal,
creation, or publication failure prevents startup.

With `InMemoryOpenSandboxState`, shutdown destroys remote Sandboxes owned only by that
process because no later worker can recover them. Persistent state keeps committed
bindings, warm slots, and durable cleanup work available to other workers.
It stores lifecycle identity rather than container contents. An expired owner binding
is replaced on the next `get()`; durable workspace contents require an OpenSandbox
volume or snapshot policy selected by the host.

## Rooted backend

When `OpenSandboxConfig.workspace_root` is set, `get()` returns a rooted view. Deep
Agents file-tool `/` maps to that physical directory and shell commands start there.
`reset()` refuses to delete when no safe root is configured.

Rooted file tools resolve and access each target in one isolated in-Sandbox helper.
The helper pins the configured root directory, resolves stable links whose targets
remain under that root, and reopens every canonical component without following
links. Links to external paths and components replaced while an operation is running
are rejected or cause that operation to stop without accessing the external target.
Read, edit, delete, list, glob, grep, capture offload, and reset use descriptor-relative
operations. Reset retains the configured root and removes its children without
following child links.

Native upload and download hold the validated file descriptor in a finite background
helper while the OpenSandbox filesystem service transfers through
`/proc/<helper-pid>/fd/<fd>`. The Sandbox image must provide Python 3, Linux procfs,
and shared process visibility between the command and filesystem services. A runtime
that cannot satisfy this descriptor path contract fails the operation; it does not
fall back to reopening the requested pathname. All remote OpenSandbox I/O is
asynchronous-only. Valid synchronous upload, download, write, and capture-offload
calls raise the backend's explicit async-only error before file I/O; empty and locally
invalid batches can still return without remote work.

Batch transfers process valid items in input order and retain one response per input.
A confirmed invalid path affects only that item. Transport failures and uncertain
mutation results propagate and are never retried, so a completed write, edit, delete,
offload, or reset is not replayed after cancellation or response loss. Once remote
work starts, the Handle lease remains owned until helper and transfer cleanup settle.
Raw Shell execution is not confined by the file-tool root and remains a separate,
unrestricted Sandbox capability governed by the host's tool and approval policy.

Pass the final backend to `manager.build_agent_middleware()`. This replaces Deep
Agents' default filesystem middleware with matching virtual-file and Shell-path
guidance. When a `CompositeBackend` adds virtual routes, pass that composed backend
rather than its OpenSandbox default.

Filesystem permissions on an executable CompositeBackend must be fully scoped to
non-Shell routes. Pass the same rules to Deep Agents and the rooted middleware:

```python
from deepagents.backends import CompositeBackend, StoreBackend
from deepagents.middleware.filesystem import FilesystemPermission
from langgraph.store.memory import InMemoryStore

permissions = [
    FilesystemPermission(
        operations=["write"],
        paths=["/policies/private/**"],
        mode="deny",
    )
]
store = InMemoryStore()
sandbox_backend = await manager.get("tenant-1/user-7")
backend = CompositeBackend(
    default=sandbox_backend,
    routes={
        "/policies/": StoreBackend(
            namespace=lambda _runtime: ("tenant-1", "policies"),
        ),
    },
)
graph = create_deep_agent(
    model=model,
    backend=backend,
    middleware=manager.build_agent_middleware(
        backend,
        permissions=permissions,
    ),
    permissions=permissions,
    store=store,
)
```

Deep Agents rejects permission patterns for the executable default Sandbox because
Shell commands can bypass file-tool enforcement. Interrupt-mode permissions also
require a checkpointer. Hosts constructing rooted backends without a manager can call
`build_rooted_filesystem_middleware()` with the same backend and permissions.

## Persistent multi-worker state

`SQLAlchemyOpenSandboxState` uses SQLAlchemy Core with asynchronous drivers:

```python
from tinkerfin_sandbox import (
    OpenSandboxManager,
    SQLAlchemyOpenSandboxState,
)

manager = OpenSandboxManager[ProjectKey](
    client=client,
    key_resolver=lambda key: f"{key.organization}/{key.project}",
    state=SQLAlchemyOpenSandboxState(
        url="sqlite+aiosqlite:////var/lib/app/opensandbox.db",
        namespace="production",
        sqlite_retry_timeout=5.0,
    ),
)
```

SQLite write transactions use `BEGIN IMMEDIATE` with package-controlled retry. Each
attempt permits at most 10 milliseconds of SQLite busy waiting;
`sqlite_retry_timeout` is the total monotonic budget for replaying `SQLITE_BUSY` and
`SQLITE_LOCKED` attempts that either never began or completed rollback and connection
release. Backoff starts at `poll_interval` and is capped at 0.5 seconds. Cancellation
during backoff propagates immediately. A failed or
cancelled `COMMIT` never replays the transaction body. SQLite `SQLITE_BUSY` retries
only `COMMIT` on the same open transaction because that result is known to be
uncommitted; every other commit error is treated as an uncertain database outcome.

Use `mysql+asyncmy://...` for workers on different hosts. The built-in State supports
SQLite, MySQL 5.7, and MySQL 8.x. MariaDB and other MySQL server versions are rejected
until their transaction behavior is verified. MySQL 8.x uses `SKIP LOCKED`; MySQL 5.7
uses a bounded row-lock wait with the same claim, fencing, and lease semantics.

`start()` creates the complete schema in an empty database and otherwise validates the
existing TinkerFin-owned tables against the current exact structure. The
database account needs DDL and DML permissions when workers initialize the database.
Workers in one database namespace must use the same warm-pool size.

Infrastructure-managed deployments can generate a complete empty-database script
without creating an engine or opening a connection:

```python
from pathlib import Path

from tinkerfin_sandbox import get_sqlalchemy_opensandbox_state_schema

schema = get_sqlalchemy_opensandbox_state_schema(dialect="mysql")
Path("schema.sql").write_text(schema.ddl, encoding="utf-8")
```

Use `dialect="sqlite"` for SQLite. One MySQL script targets both MySQL 5.7 and 8.x and
contains every current table, explicit index, and comment. After that script is applied,
`start()` validates the deployed structure before it registers a worker. The descriptor
is immutable and exposes `dialect`, `table_names`, and `ddl`. Non-current owned tables or
columns, missing or extra indexes, and changed index uniqueness fail startup and must be
rebuilt before a DML-only runtime account is started.

Persistent state coordinates allocation, binding, warm slots, owner fencing, and
cleanup. It does not serialize complete graph runs; use an application run coordinator
when graph execution also requires per-RunIdentity exclusion.

Consuming a ready warm slot atomically commits that Sandbox as the owner binding; the
manager does not bind it a second time. An on-demand bind whose response fails or is
cancelled is reconciled through `read_binding()`. An exact Sandbox ID and generation is
authoritative and is never destroyed. A confirmed missing or superseded candidate can
be destroyed idempotently; an unreadable result closes only the current worker's local
connection so an uncertain authoritative remote is preserved.

## Documentation

- [Sandbox guide](https://github.com/tinkerfin-ai/tinkerfin/blob/main/docs/en/sandbox/index.md)
- [Persistent state and extensions](https://github.com/tinkerfin-ai/tinkerfin/blob/main/docs/en/sandbox/persistence-and-extensions.md)
- [Complete documentation](https://github.com/tinkerfin-ai/tinkerfin/blob/main/docs/en/README.md)

## License

Apache License 2.0. See the
[repository license](https://github.com/tinkerfin-ai/tinkerfin/blob/main/LICENSE).
