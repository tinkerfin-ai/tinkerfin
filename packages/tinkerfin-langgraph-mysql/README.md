# TinkerFin LangGraph MySQL

## What it is

`tinkerfin-langgraph-mysql` provides the asyncmy-backed LangGraph `BaseStore`
implementation used by TinkerFin hosts. It keeps the Store connection asynchronous and
does not install or import aiomysql or PyMySQL.

The implementation is derived from `langgraph-checkpoint-mysql` 3.0.0 under the MIT
License. The distribution contains only the shared MySQL Store machinery and the
asyncmy driver required by its public API.

## Installation

```bash
pip install tinkerfin-langgraph-mysql
```

Do not install this distribution together with `langgraph-checkpoint-mysql`. Both
distributions provide the same `langgraph.store.mysql` module paths, so package-manager
installation order would overwrite files and make the imported implementation
indeterminate. TinkerFin hosts install only `tinkerfin-langgraph-mysql`.

## Quick Start

```python
from tinkerfin_langgraph_mysql import AsyncMyStore


async with AsyncMyStore.from_conn_string(
    "mysql://user:password@127.0.0.1:3306/agents"
) as store:
    await store.aput(("users", "42"), "preferences", {"theme": "dark"})
```

## Core concepts

- `AsyncMyStore.from_conn_string()` accepts `mysql://` and `mysql+asyncmy://`, owns
  exactly one asyncmy connection, creates the current Store tables before yielding,
  and settles accepted operations before closing the connection on context exit.
- Direct construction borrows a connection or pool. Call `await store.setup()` before
  use and `await store.aclose()` to stop the Store; the borrowed resource stays owned
  by the caller. Closed Stores raise `LangGraphMySQLStoreClosedError`.
- Namespace labels and document keys use exact Unicode comparison, including case,
  accents, and trailing spaces. `setup()` verifies the `utf8mb4_0900_bin` identity
  collation, its `NO PAD` behavior, and complete identity indexes.
- MySQL 8.0.19 or later is required by the Store queries.
- MariaDB and alternate MySQL SQL dialect branches are not part of this package.
- The public distribution intentionally supports asyncmy only. It does not expose
  aiomysql or PyMySQL driver classes.
- The LangGraph Store remains separate from a graph checkpointer. A host can use Redis
  or another supported checkpointer independently.
- Document namespaces follow LangGraph's constraints: labels are non-empty, contain
  no periods, and do not use the reserved `langgraph` root. These constraints also
  apply to explicit batch document operations; search and listing retain their own
  prefix and wildcard matching rules.
- This Store does not support document TTL; an explicit TTL is rejected before writing,
  including in `batch()` and `abatch()`.
- `setup()` creates and reflects the single current `store` table. The package has no
  Schema version table, numbered migration chain, or old-shape runtime branch.

Schema and ordinary driver failures use `LangGraphMySQLSchemaError` and
`LangGraphMySQLDriverError`, both derived from `LangGraphMySQLError`. Cancellation and
caller exceptions retain their original semantics.

Use `abatch()` for explicit bulk operations. Individual asynchronous Store calls execute
directly and do not create an idle batching worker. Synchronous LangGraph Store methods
may be used from a caller-owned worker thread while the Store's event loop is running;
calling them from that event loop raises `asyncio.InvalidStateError`.

Close waits for accepted operations, including when the close waiter is cancelled.
Use an asynchronous timeout around operations, or configure the borrowed driver's
network timeouts, to bound database work. Cancelling in-flight I/O may invalidate the
connection under the driver's protocol rules; the Store still settles the operation
before releasing its owned connection.

## Documentation

See the [TinkerFin documentation](https://github.com/tinkerfin-ai/tinkerfin/tree/main/docs)
for runtime and persistence composition.

## License

MIT. See the packaged
[LICENSE](https://github.com/tinkerfin-ai/tinkerfin/blob/main/packages/tinkerfin-langgraph-mysql/LICENSE)
and
[NOTICE](https://github.com/tinkerfin-ai/tinkerfin/blob/main/packages/tinkerfin-langgraph-mysql/NOTICE)
for upstream attribution and source provenance.
