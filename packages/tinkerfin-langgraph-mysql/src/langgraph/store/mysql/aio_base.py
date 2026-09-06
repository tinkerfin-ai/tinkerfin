"""Asynchronous execution and current-Schema validation for the MySQL Store."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable, Iterable, Sequence
from contextlib import asynccontextmanager
from typing import Any, Generic, cast

import orjson
from langgraph.store.base import (
    BaseStore,
    GetOp,
    ListNamespacesOp,
    Op,
    PutOp,
    Result,
    SearchOp,
)

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
from langgraph.store.mysql.errors import (
    LangGraphMySQLSchemaError,
    LangGraphMySQLStoreClosedError,
)


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
        SELECT c.COLUMN_NAME, c.COLUMN_TYPE, c.IS_NULLABLE, c.COLUMN_DEFAULT,
               c.COLUMN_COMMENT, c.CHARACTER_SET_NAME, c.COLLATION_NAME,
               collation.PAD_ATTRIBUTE
        FROM information_schema.COLUMNS AS c
        LEFT JOIN information_schema.COLLATIONS AS collation
            ON collation.COLLATION_NAME = c.COLLATION_NAME
        WHERE c.TABLE_SCHEMA = DATABASE() AND c.TABLE_NAME = 'store'
        ORDER BY c.ORDINAL_POSITION
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
    for row in column_rows:
        if row["COLUMN_NAME"] in {"prefix", "key"} and (
            row.get("CHARACTER_SET_NAME") != "utf8mb4"
            or row.get("COLLATION_NAME") != "utf8mb4_0900_bin"
            or row.get("PAD_ATTRIBUTE") != "NO PAD"
        ):
            raise LangGraphMySQLSchemaError(reason="identity collation mismatch")

    await cur.execute(
        """
        SELECT INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME, INDEX_TYPE, SUB_PART
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
    # Prefix indexes compare only part of each identity. Matching column names and
    # collation is insufficient: every key column must participate at full length.
    if indexes != _STORE_INDEX_CONTRACTS or any(
        "SUB_PART" not in row or row["SUB_PART"] is not None for row in index_rows
    ):
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
    BaseStore,
    BaseMySQLStore,
    Generic[_ainternal.C, _ainternal.R],
):
    """Execute explicit Store batches without an idle background worker.

    LangGraph BaseStore convenience methods call ``abatch`` directly. Accepted
    operations remain owned by their callers; closing waits for their transactions
    before an owned connection is released. Network deadlines remain configurable
    through the borrowed driver or the caller's asynchronous timeout scope.
    """

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
        self._closed = False
        self._active_operations = 0
        self._operations_done = asyncio.Event()
        self._operations_done.set()

    def batch(self, ops: Iterable[Op]) -> list[Result]:
        """Run a Store batch from a thread outside the creating event loop.

        This bridges LangGraph's synchronous Store methods to native async I/O.
        The calling thread waits for completion; its owner controls thread capacity
        and driver timeouts. Interrupting that wait cancels the submitted operation.

        Args:
            ops: Ordered LangGraph Store operations.

        Returns:
            Results in the original operation order.

        Raises:
            asyncio.InvalidStateError: Called from the Store loop or while it is stopped.
            LangGraphMySQLStoreClosedError: The Store has begun closing.
        """

        if self._closed:
            raise LangGraphMySQLStoreClosedError()
        try:
            caller_loop = asyncio.get_running_loop()
        except RuntimeError:
            caller_loop = None
        if caller_loop is self.loop or not self.loop.is_running():
            raise asyncio.InvalidStateError(
                "Synchronous Store calls require a worker thread and a running "
                "Store event loop; use the asynchronous methods in the Store loop"
            )
        submitted = asyncio.run_coroutine_threadsafe(self.abatch(ops), self.loop)
        try:
            return submitted.result()
        except BaseException:
            submitted.cancel()
            raise

    @asynccontextmanager
    async def _operation(self) -> AsyncGenerator[None, None]:
        if asyncio.get_running_loop() is not self.loop:
            raise RuntimeError("Use the Store from its creating event loop")
        if self._closed:
            raise LangGraphMySQLStoreClosedError()
        self._active_operations += 1
        self._operations_done.clear()
        try:
            yield
        finally:
            self._active_operations -= 1
            if not self._active_operations:
                self._operations_done.set()

    async def aclose(self) -> None:
        """Stop accepting operations and settle every accepted transaction.

        Closing is idempotent and never closes a borrowed connection or pool. Caller
        cancellation is propagated after accepted operations settle. Set operation
        deadlines or cancel the tasks that own those operations when bounded shutdown
        is required; no Store worker or hidden queue continues after close.

        Raises:
            asyncio.CancelledError: The close caller was cancelled during settlement.
            RuntimeError: Called from an event loop other than the creating loop.
        """

        if asyncio.get_running_loop() is not self.loop:
            raise RuntimeError("Close the Store from its creating event loop")
        self._closed = True
        cancellation: asyncio.CancelledError | None = None
        while not self._operations_done.is_set():
            try:
                await self._operations_done.wait()
            except asyncio.CancelledError as error:
                if cancellation is None:
                    cancellation = error
        if cancellation is not None:
            raise cancellation

    @staticmethod
    def _get_cursor_from_connection(conn: _ainternal.C) -> _ainternal.R:
        raise NotImplementedError

    async def abatch(self, ops: Iterable[Op]) -> list[Result]:
        """Execute one ordered LangGraph Store operation batch atomically."""

        async with self._operation():
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
                columns, complete primary key, index, defaults, or comments.
        """

        async with self._operation(), _ainternal.get_connection(self.conn) as conn:
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
