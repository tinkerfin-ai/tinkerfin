"""Run the shared transaction contract against actual MySQL and PostgreSQL servers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, TypeGuard
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import Column, Integer, MetaData, Table, event, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool
from tests.support.docker_services import MySQLTestService, PostgreSQLTestService

from tinkerfin_sqlalchemy import (
    SqlTransaction,
    SqlTransactionError,
    database_capabilities,
)

pytestmark = pytest.mark.docker_integration


def _is_group(error: BaseException) -> TypeGuard[BaseExceptionGroup[BaseException]]:
    return isinstance(error, BaseExceptionGroup)


@dataclass(frozen=True, slots=True)
class _Database:
    engine: AsyncEngine
    table: Table
    family: str


@pytest_asyncio.fixture(params=["mysql", "mysql57", "postgresql"])
async def database(request: pytest.FixtureRequest) -> AsyncIterator[_Database]:
    family: str = request.param
    service = request.getfixturevalue(f"{family}_test_service")
    if isinstance(service, MySQLTestService):
        url = service.url("tinkerfin_test_admin")
    else:
        assert isinstance(service, PostgreSQLTestService)
        url = service.url()
    engine = create_async_engine(url, pool_size=3, max_overflow=0)
    metadata = MetaData()
    table = Table(
        f"sql_transaction_{uuid4().hex}",
        metadata,
        Column("value", Integer, nullable=False),
    )
    try:
        async with SqlTransaction(engine, schema_lock=table.name) as connection:
            await connection.run_sync(metadata.create_all)
            await connection.execute(table.insert().values(value=0))
        yield _Database(engine, table, family)
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(metadata.drop_all)
        await engine.dispose()


async def test_connected_server_capabilities_and_atomic_rollback(
    database: _Database,
) -> None:
    engine, table = database.engine, database.table
    async with SqlTransaction(engine) as connection:
        features = database_capabilities(connection)
        assert features.row_locks
        assert features.skip_locked is (database.family != "mysql57")
        if database.family == "mysql57":
            assert features.server_version[:2] == (5, 7)
        await connection.execute(table.update().values(value=1))
    with pytest.raises(RuntimeError, match="rollback this write"):
        async with SqlTransaction(engine) as connection:
            await connection.execute(table.update().values(value=2))
            raise RuntimeError("rollback this write")
    async with SqlTransaction(engine, read_only=True) as connection:
        assert await connection.scalar(select(table.c.value)) == 1


async def test_write_batch_can_read_one_snapshot_before_its_writes(
    database: _Database,
) -> None:
    engine, table = database.engine, database.table
    async with SqlTransaction(engine, isolation_level="REPEATABLE READ") as batch:
        first = await batch.scalar(select(table.c.value))
        async with SqlTransaction(engine) as peer:
            await peer.execute(table.update().values(value=1))
        second = await batch.scalar(select(table.c.value))
        await batch.execute(table.insert().values(value=2))
    assert (first, second) == (0, 0)
    async with SqlTransaction(engine, read_only=True) as connection:
        assert (
            await connection.execute(select(table.c.value).order_by(table.c.value))
        ).scalars().all() == [1, 2]


@pytest.mark.parametrize("host_isolation", ["READ COMMITTED", "AUTOCOMMIT"])
async def test_repeatable_read_and_read_only_with_host_isolation_preserved(
    database: _Database,
    host_isolation: str,
) -> None:
    engine, table = database.engine, database.table
    borrowed = engine.execution_options(isolation_level=host_isolation)
    async with borrowed.connect() as connection:
        original_isolation = await connection.get_isolation_level()
    async with SqlTransaction(borrowed, read_only=True) as reader:
        assert await reader.scalar(select(table.c.value)) == 0
        async with SqlTransaction(engine) as writer:
            await writer.execute(table.update().values(value=1))
        assert await reader.scalar(select(table.c.value)) == 0
        with pytest.raises(DBAPIError):
            await reader.execute(table.update().values(value=7))
    async with borrowed.connect() as connection:
        assert await connection.get_isolation_level() == original_isolation
    async with SqlTransaction(borrowed) as writer:
        await writer.execute(table.update().values(value=2))
    async with SqlTransaction(engine, read_only=True) as reader:
        assert await reader.scalar(select(table.c.value)) == 2


async def test_schema_initialization_is_serialized_on_one_connection(
    database: _Database,
) -> None:
    metadata = MetaData()
    table = Table(f"sql_setup_{uuid4().hex}", metadata, Column("value", Integer))
    engine = create_async_engine(database.engine.url, pool_size=1, max_overflow=0)
    peer = create_async_engine(database.engine.url, pool_size=1, max_overflow=0)
    created: list[bool] = []

    async def initialize(target: AsyncEngine) -> None:
        async with SqlTransaction(target, schema_lock=table.name) as connection:
            exists = await connection.run_sync(
                lambda sync: sync.dialect.has_table(sync, table.name)
            )
            created.append(not exists)
            await connection.run_sync(metadata.create_all)

    try:
        await asyncio.gather(initialize(engine), initialize(peer))
        assert sorted(created) == [False, True]
        async with SqlTransaction(engine, schema_lock=table.name) as connection:
            await connection.run_sync(metadata.drop_all)
    finally:
        await engine.dispose()
        await peer.dispose()


async def test_cancelled_body_releases_row_lock_before_caller_returns(
    database: _Database,
) -> None:
    engine, table = database.engine, database.table
    started, hold = asyncio.Event(), asyncio.Event()

    async def write() -> None:
        async with SqlTransaction(engine) as connection:
            await connection.execute(table.update().values(value=5))
            started.set()
            await hold.wait()

    task = asyncio.create_task(write())
    try:
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        async with SqlTransaction(engine) as connection:
            assert await connection.scalar(select(table.c.value)) == 0
            await connection.execute(table.update().values(value=6))
    finally:
        hold.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_zero_schema_timeout_rejects_contended_lock_without_waiting(
    database: _Database,
) -> None:
    lock_name = f"schema-no-wait-{uuid4().hex}"
    async with SqlTransaction(database.engine, schema_lock=lock_name):
        with pytest.raises(SqlTransactionError, match="schema lock"):
            async with SqlTransaction(
                database.engine,
                schema_lock=lock_name,
                schema_lock_timeout_seconds=0,
            ):
                raise AssertionError("the second schema lock was admitted")
    async with SqlTransaction(
        database.engine, schema_lock=lock_name, schema_lock_timeout_seconds=0
    ):
        pass


@pytest.mark.parametrize("operation", ["schema", "body"])
async def test_inflight_cancellation_survives_driver_invalidation_listener_failure(
    database: _Database,
    operation: str,
) -> None:
    engine = create_async_engine(database.engine.url, pool_size=1, max_overflow=0)
    entered = asyncio.Event()
    invalidation = RuntimeError("host invalidation listener failed")
    lock_name = f"cancel-schema-{uuid4().hex}"

    def executing(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if (
            "GET_LOCK(" in statement
            or "pg_advisory_xact_lock(" in statement
            or statement.startswith("UPDATE ")
        ):
            entered.set()

    def fail_invalidation(
        _connection: object, _record: object, _error: BaseException | None
    ) -> None:
        raise invalidation

    event.listen(engine.sync_engine, "before_cursor_execute", executing)
    event.listen(engine.sync_engine, "invalidate", fail_invalidation)

    async def waiting() -> None:
        async with SqlTransaction(
            engine, schema_lock=lock_name if operation == "schema" else None
        ) as connection:
            await connection.execute(database.table.update().values(value=3))
            raise AssertionError("the blocked operation completed")

    task: asyncio.Task[None] | None = None
    try:
        async with SqlTransaction(
            database.engine, schema_lock=lock_name if operation == "schema" else None
        ) as owner:
            await owner.execute(database.table.update().values(value=1))
            task = asyncio.create_task(waiting())
            await entered.wait()
            task.cancel("cancel blocked I/O")
            with pytest.raises(asyncio.CancelledError) as caught:
                await task
            assert task.cancelled()
            assert isinstance(engine.pool, AsyncAdaptedQueuePool)
            assert engine.pool.checkedout() == 0
            pending: list[BaseException] = [caught.value]
            seen: set[int] = set()
            while pending:
                failure = pending.pop()
                if id(failure) in seen:
                    continue
                seen.add(id(failure))
                pending.extend(
                    item
                    for item in (failure.__cause__, failure.__context__)
                    if item is not None
                )
                if _is_group(failure):
                    pending.extend(failure.exceptions)
            assert id(invalidation) in seen
        async with SqlTransaction(engine, schema_lock=lock_name) as connection:
            assert await connection.scalar(select(database.table.c.value)) == 1
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        event.remove(engine.sync_engine, "invalidate", fail_invalidation)
        await engine.dispose()


async def test_mysql_wait_setting_is_restored(database: _Database) -> None:
    if database.family == "postgresql":
        pytest.skip("InnoDB lock wait is a MySQL session setting")
    engine = create_async_engine(database.engine.url, pool_size=1, max_overflow=0)
    try:
        async with engine.connect() as connection:
            before = await connection.scalar(
                text("SELECT @@SESSION.innodb_lock_wait_timeout")
            )
        async with SqlTransaction(
            engine, mysql_lock_wait_timeout_seconds=1
        ) as connection:
            assert (
                await connection.scalar(
                    text("SELECT @@SESSION.innodb_lock_wait_timeout")
                )
                == 1
            )
        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT @@SESSION.innodb_lock_wait_timeout")
                )
                == before
            )
    finally:
        await engine.dispose()


@pytest.mark.parametrize("release_failure", ["error", "unconfirmed"])
async def test_schema_lock_release_failure_discards_physical_session(
    database: _Database,
    monkeypatch: pytest.MonkeyPatch,
    release_failure: str,
) -> None:
    if database.family == "postgresql":
        pytest.skip(
            "PostgreSQL releases its transaction advisory lock at rollback/commit"
        )
    engine = create_async_engine(database.engine.url, pool_size=1, max_overflow=0)
    peer = create_async_engine(database.engine.url, pool_size=1, max_overflow=0)
    original = AsyncConnection.scalar
    lock_name = f"release-fault-{uuid4().hex}"

    async def scalar(
        connection: AsyncConnection, statement: Any, *args: Any, **kwargs: Any
    ) -> Any:
        if "RELEASE_LOCK" in str(statement):
            if release_failure == "error":
                raise SqlTransactionError("injected lock release failure")
            return 0
        return await original(connection, statement, *args, **kwargs)

    try:
        monkeypatch.setattr(AsyncConnection, "scalar", scalar)
        with pytest.raises(SqlTransactionError):
            async with SqlTransaction(engine, schema_lock=lock_name):
                pass
        monkeypatch.setattr(AsyncConnection, "scalar", original)
        # A separate physical session must acquire immediately. Returning the first
        # locked session to its own pool would hide a leaked session advisory lock.
        async with SqlTransaction(
            peer, schema_lock=lock_name, schema_lock_timeout_seconds=0
        ):
            pass
    finally:
        await engine.dispose()
        await peer.dispose()


async def test_autocommit_read_cleanup_ignores_host_skip_rollback_optimization(
    database: _Database,
) -> None:
    engine = create_async_engine(
        database.engine.url,
        isolation_level="AUTOCOMMIT",
        skip_autocommit_rollback=True,
        pool_size=1,
        max_overflow=0,
    )
    try:
        async with SqlTransaction(engine, read_only=True) as connection:
            assert await connection.scalar(select(database.table.c.value)) == 0
        async with engine.connect() as connection:
            # No READ ONLY state or prior snapshot may outlive the framework scope.
            await connection.execute(database.table.update().values(value=3))
        async with SqlTransaction(database.engine, read_only=True) as connection:
            assert await connection.scalar(select(database.table.c.value)) == 3
    finally:
        await engine.dispose()
