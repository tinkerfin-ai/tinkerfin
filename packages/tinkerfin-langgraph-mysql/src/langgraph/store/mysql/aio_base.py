"""Asynchronous execution and current-Schema validation for the MySQL Store."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable, Iterable, Sequence
from contextlib import asynccontextmanager
from typing import Any, Generic, cast

import orjson
from langgraph.store.base import (
    GetOp,
    ListNamespacesOp,
    Op,
    PutOp,
    Result,
    SearchOp,
)
from langgraph.store.base.batch import AsyncBatchedBaseStore

from langgraph.store.mysql import _ainternal
from langgraph.store.mysql.base import (
    _STORE_COLUMN_CONTRACTS,
    _STORE_INDEX_CONTRACTS,
    _STORE_SCHEMA_STATEMENT,
    _STORE_TABLE_COMMENT,
    BaseMySQLStore,
    GroupedOps,
    NamespaceRow,
    Row,
    decode_namespace,
    group_ops,
    row_to_item,
    row_to_search_item,
)
from langgraph.store.mysql.errors import LangGraphMySQLSchemaError


def _required_text(row: dict[str, Any], key: str) -> str:
    """Read one required text field from an information-schema row."""

    value = row.get(key)
    if not isinstance(value, str):
        raise LangGraphMySQLSchemaError(reason=f"{key} is not text")
    return value


def _required_int(row: dict[str, Any], key: str) -> int:
    """Read one required integer field from an information-schema row."""

    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise LangGraphMySQLSchemaError(reason=f"{key} is not an integer")
    return value


def _normalized_default(row: dict[str, Any]) -> str | None:
    """Normalize MySQL's equivalent CURRENT_TIMESTAMP renderings."""

    value = row.get("COLUMN_DEFAULT")
    if value is None:
        return None
    if not isinstance(value, str):
        raise LangGraphMySQLSchemaError(reason="COLUMN_DEFAULT is not text or null")
    return value.strip().lower().removesuffix("()")


async def _validate_current_schema(cur: _ainternal.AsyncDictCursor) -> None:
    """Fail closed unless the existing table matches the only current Store Schema."""

    await cur.execute(
        """
        SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, COLUMN_COMMENT
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'store'
        ORDER BY ORDINAL_POSITION
        """
    )
    column_rows = await cur.fetchall()
    columns = tuple(
        (
            _required_text(row, "COLUMN_NAME"),
            _required_text(row, "COLUMN_TYPE").lower(),
            _required_text(row, "IS_NULLABLE").upper(),
            _normalized_default(row),
            _required_text(row, "COLUMN_COMMENT"),
        )
        for row in column_rows
    )
    if columns != _STORE_COLUMN_CONTRACTS:
        raise LangGraphMySQLSchemaError(reason="column contract mismatch")

    await cur.execute(
        """
        SELECT INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME, INDEX_TYPE
        FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'store'
        ORDER BY INDEX_NAME, SEQ_IN_INDEX
        """
    )
    index_rows = await cur.fetchall()
    indexes = tuple(
        (
            _required_text(row, "INDEX_NAME"),
            _required_int(row, "NON_UNIQUE"),
            _required_int(row, "SEQ_IN_INDEX"),
            _required_text(row, "COLUMN_NAME"),
            _required_text(row, "INDEX_TYPE").upper(),
        )
        for row in index_rows
    )
    if indexes != _STORE_INDEX_CONTRACTS:
        raise LangGraphMySQLSchemaError(reason="index contract mismatch")

    await cur.execute(
        """
        SELECT TABLE_COMMENT
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'store'
        """
    )
    table_row = await cur.fetchone()
    if table_row is None or _required_text(table_row, "TABLE_COMMENT") != (
        _STORE_TABLE_COMMENT
    ):
        raise LangGraphMySQLSchemaError(reason="table comment mismatch")


async def _store_table_exists(cur: _ainternal.AsyncDictCursor) -> bool:
    """Return whether the current database already owns the Store table."""

    await cur.execute(
        """
        SELECT COUNT(*) AS table_count
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'store'
        """
    )
    row = await cur.fetchone()
    if row is None:
        raise LangGraphMySQLSchemaError(reason="table existence query returned no row")
    count = row.get("table_count")
    if isinstance(count, bool) or not isinstance(count, int) or count not in {0, 1}:
        raise LangGraphMySQLSchemaError(reason="table existence count is invalid")
    return count == 1


class BaseAsyncMySQLStore(
    AsyncBatchedBaseStore,
    BaseMySQLStore,
    Generic[_ainternal.C, _ainternal.R],
):
    """Execute the shared Store query plan through an async MySQL connection."""

    __slots__ = ("_deserializer", "lock")

    def __init__(
        self,
        conn: _ainternal.Conn[_ainternal.C],
        *,
        deserializer: Callable[[bytes | orjson.Fragment], dict[str, Any]] | None = None,
    ) -> None:
        """Borrow one connection or pool without assuming ownership.

        Args:
            conn: Caller-owned asynchronous connection or pool.
            deserializer: Optional strict JSON object decoder.
        """

        super().__init__()
        self._deserializer = deserializer
        self.conn = conn
        self.lock = asyncio.Lock()
        self.loop = asyncio.get_running_loop()

    @staticmethod
    def _get_cursor_from_connection(conn: _ainternal.C) -> _ainternal.R:
        raise NotImplementedError

    async def abatch(self, ops: Iterable[Op]) -> list[Result]:
        """Execute one ordered LangGraph Store operation batch atomically."""

        grouped_ops, num_ops = group_ops(ops)
        results: list[Result] = [None] * num_ops

        async with _ainternal.get_connection(self.conn) as conn:
            await self._execute_batch(grouped_ops, results, conn)

        return results

    async def setup(self) -> None:
        """Create and validate the only current Store table.

        ``AsyncMyStore.from_conn_string()`` calls this method before yielding. Direct
        construction borrows a connection, so its caller must invoke ``setup()`` once
        before use. Concurrent empty-database calls are safe because the index is part
        of the atomic ``CREATE TABLE IF NOT EXISTS`` statement. An existing incompatible
        table is never migrated or interpreted as an older shape; validation fails.

        Raises:
            LangGraphMySQLSchemaError: The existing table differs from the current
                columns, primary key, index, defaults, or comments.
        """

        async with _ainternal.get_connection(self.conn) as conn:
            async with self._cursor(conn) as cur:
                # Avoid the driver's noisy TABLE_EXISTS warning on every normal startup.
                # The DDL still uses IF NOT EXISTS so two empty-database starters remain
                # safe when they race between this read and the atomic CREATE.
                if not await _store_table_exists(cur):
                    await cur.execute(_STORE_SCHEMA_STATEMENT)
                await _validate_current_schema(cur)

    async def _execute_batch(
        self,
        grouped_ops: GroupedOps,
        results: list[Result],
        conn: _ainternal.C,
    ) -> None:
        async with self._cursor(conn, pipeline=True) as cur:
            if GetOp in grouped_ops:
                await self._batch_get_ops(
                    cast(Sequence[tuple[int, GetOp]], grouped_ops[GetOp]),
                    results,
                    cur,
                )

            if SearchOp in grouped_ops:
                await self._batch_search_ops(
                    cast(Sequence[tuple[int, SearchOp]], grouped_ops[SearchOp]),
                    results,
                    cur,
                )

            if ListNamespacesOp in grouped_ops:
                await self._batch_list_namespaces_ops(
                    cast(
                        Sequence[tuple[int, ListNamespacesOp]],
                        grouped_ops[ListNamespacesOp],
                    ),
                    results,
                    cur,
                )

            if PutOp in grouped_ops:
                await self._batch_put_ops(
                    cast(Sequence[tuple[int, PutOp]], grouped_ops[PutOp]),
                    cur,
                )

    async def _batch_get_ops(
        self,
        get_ops: Sequence[tuple[int, GetOp]],
        results: list[Result],
        cur: _ainternal.R,
    ) -> None:
        for query, params, namespace, items in self._get_batch_GET_ops_queries(get_ops):
            await cur.execute(query, params)
            rows = cast(list[Row], await cur.fetchall())
            key_to_row = {row["key"]: row for row in rows}
            for idx, key in items:
                row = key_to_row.get(key)
                if row:
                    results[idx] = row_to_item(
                        namespace, row, loader=self._deserializer
                    )
                else:
                    results[idx] = None

    async def _batch_put_ops(
        self,
        put_ops: Sequence[tuple[int, PutOp]],
        cur: _ainternal.R,
    ) -> None:
        queries = self._prepare_batch_PUT_queries(put_ops)
        for query, params in queries:
            await cur.execute(query, params)

    async def _batch_search_ops(
        self,
        search_ops: Sequence[tuple[int, SearchOp]],
        results: list[Result],
        cur: _ainternal.R,
    ) -> None:
        queries = self._prepare_batch_search_queries(search_ops)
        for (idx, _), (query, params) in zip(search_ops, queries):
            await cur.execute(query, params)
            rows = cast(list[Row], await cur.fetchall())
            items = [
                row_to_search_item(
                    decode_namespace(row["prefix"]), row, loader=self._deserializer
                )
                for row in rows
            ]
            results[idx] = items

    async def _batch_list_namespaces_ops(
        self,
        list_ops: Sequence[tuple[int, ListNamespacesOp]],
        results: list[Result],
        cur: _ainternal.R,
    ) -> None:
        queries = self._get_batch_list_namespaces_queries(list_ops)
        for (query, params), (idx, _) in zip(queries, list_ops):
            await cur.execute(query, params)
            rows = cast(list[NamespaceRow], await cur.fetchall())
            namespaces = [decode_namespace(row["truncated_prefix"]) for row in rows]
            results[idx] = namespaces

    @asynccontextmanager
    async def _cursor(
        self, conn: _ainternal.C, *, pipeline: bool = False
    ) -> AsyncGenerator[_ainternal.R, None]:
        """Create a database cursor as a context manager.

        Args:
            conn: The database connection to use
            pipeline: whether to use transaction context manager and handle concurrency
        """
        if pipeline:
            # a connection can only be used by one
            # thread/coroutine at a time, so we acquire a lock
            async with self.lock:
                await conn.begin()
                try:
                    async with self._get_cursor_from_connection(conn) as cur:
                        yield cur
                    await conn.commit()
                except BaseException as primary:  # Preserve transaction outcome
                    try:
                        await conn.rollback()
                    except BaseException as rollback_error:  # Retain secondary failure
                        if isinstance(primary, Exception) and not isinstance(
                            rollback_error,
                            Exception,
                        ):
                            rollback_error.add_note(
                                "MySQL transaction body also failed: "
                                f"{type(primary).__name__}"
                            )
                            raise
                        primary.add_note(
                            "MySQL rollback also failed: "
                            f"{type(rollback_error).__name__}"
                        )
                        raise primary.with_traceback(
                            primary.__traceback__
                        ) from rollback_error
                    raise
        else:
            async with self.lock, self._get_cursor_from_connection(conn) as cur:
                yield cur
