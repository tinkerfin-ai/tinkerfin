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
  and closes the connection when its context exits.
- MySQL 8.0.19 or later is required by the Store queries.
- MariaDB and alternate MySQL SQL dialect branches are not part of this package.
- The public distribution intentionally supports asyncmy only. It does not expose
  aiomysql or PyMySQL driver classes.
- The LangGraph Store remains separate from a graph checkpointer. A host can use Redis
  or another supported checkpointer independently.
- `setup()` creates and reflects the single current `store` table. The package has no
  Schema version table, numbered migration chain, or old-shape runtime branch.

Schema and ordinary driver failures use `LangGraphMySQLSchemaError` and
`LangGraphMySQLDriverError`, both derived from `LangGraphMySQLError`. Cancellation and
caller exceptions retain their original semantics.

## Documentation

See the [TinkerFin documentation](https://github.com/tinkerfin-ai/tinkerfin/tree/main/docs)
for runtime and persistence composition.

## License

MIT. See the packaged
[LICENSE](https://github.com/tinkerfin-ai/tinkerfin/blob/main/packages/tinkerfin-langgraph-mysql/LICENSE)
and
[NOTICE](https://github.com/tinkerfin-ai/tinkerfin/blob/main/packages/tinkerfin-langgraph-mysql/NOTICE)
for upstream attribution and source provenance.
