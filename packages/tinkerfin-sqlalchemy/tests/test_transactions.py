"""Observable transaction, cancellation, and pool-reuse guarantees on real SQLite."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import event, text
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool

from tinkerfin_sqlalchemy import (
    SqlTransaction,
    SqlTransactionError,
    database_capabilities,
    sqlite_lock_error,
)


@pytest_asyncio.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    instance = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'transactions.db'}",
        pool_size=3,
        max_overflow=0,
    )
    try:
        async with instance.begin() as connection:
            await connection.exec_driver_sql("PRAGMA journal_mode = WAL")
            await connection.exec_driver_sql(
                "CREATE TABLE records (value INTEGER NOT NULL)"
            )
            await connection.exec_driver_sql("INSERT INTO records VALUES (0)")
        yield instance
    finally:
        await instance.dispose()


async def _value(engine: AsyncEngine) -> object:
    async with SqlTransaction(engine, read_only=True) as connection:
        return await connection.scalar(text("SELECT value FROM records"))


async def test_write_commit_rollback_and_engine_borrowing(engine: AsyncEngine) -> None:
    committed = SqlTransaction(engine)
    async with committed as connection:
        features = database_capabilities(connection)
        assert features.dialect == "sqlite" and not features.row_locks
        await connection.exec_driver_sql("UPDATE records SET value = 1")
    assert committed.committed and not committed.rolled_back
    failed = SqlTransaction(engine)
    failure = RuntimeError("body failure")
    with pytest.raises(RuntimeError) as caught:
        async with failed as connection:
            await connection.exec_driver_sql("UPDATE records SET value = 2")
            raise failure
    assert caught.value is failure
    assert failed.rolled_back and not failed.committed and not failed.commit_uncertain
    assert await _value(engine) == 1


async def test_read_transaction_keeps_a_snapshot_across_peer_commit(
    engine: AsyncEngine,
) -> None:
    async with SqlTransaction(engine, read_only=True) as reader:
        first = await reader.scalar(text("SELECT value FROM records"))
        async with SqlTransaction(engine) as writer:
            await writer.exec_driver_sql("UPDATE records SET value = 1")
        second = await reader.scalar(text("SELECT value FROM records"))
    assert (first, second) == (0, 0)
    assert await _value(engine) == 1


async def test_sqlite_writer_lock_is_real_and_released(engine: AsyncEngine) -> None:
    async with SqlTransaction(engine, sqlite_busy_timeout_ms=0):
        with pytest.raises(OperationalError) as caught:
            async with SqlTransaction(engine, sqlite_busy_timeout_ms=0):
                raise AssertionError("a second SQLite writer was admitted")
        assert sqlite_lock_error(caught.value)
    async with SqlTransaction(engine, sqlite_busy_timeout_ms=0) as connection:
        await connection.exec_driver_sql("UPDATE records SET value = 3")
    assert await _value(engine) == 3


@pytest.mark.parametrize("invalidate_listener_failure", [False, True])
async def test_connection_settings_restore_or_discard_after_failure(
    tmp_path: Path,
    invalidate_listener_failure: bool,
) -> None:
    instance = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'settings.db'}", pool_size=1, max_overflow=0
    )
    settings = {"fail_restore": False}
    checkouts: list[object] = []

    def configure(dbapi_connection: Any, _record: object) -> None:
        dbapi_connection.execute("PRAGMA busy_timeout = 4321")
        checkouts.append(dbapi_connection)

    def fail_restore(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if statement == "PRAGMA busy_timeout = 4321" and settings["fail_restore"]:
            raise OperationalError(
                statement, None, OSError("restore failed"), connection_invalidated=False
            )

    event.listen(instance.sync_engine, "connect", configure)
    event.listen(instance.sync_engine, "before_cursor_execute", fail_restore)
    if invalidate_listener_failure:
        _fail_invalidation_event(instance)
    try:
        async with SqlTransaction(instance, sqlite_busy_timeout_ms=0) as connection:
            assert await connection.scalar(text("PRAGMA busy_timeout")) == 0
        async with instance.connect() as connection:
            assert await connection.scalar(text("PRAGMA busy_timeout")) == 4321
        assert len(checkouts) == 1
        settings["fail_restore"] = True
        with pytest.raises(OperationalError, match="restore failed"):
            async with SqlTransaction(instance, sqlite_busy_timeout_ms=0):
                pass
        settings["fail_restore"] = False
        async with instance.connect() as connection:
            assert await connection.scalar(text("PRAGMA busy_timeout")) == 4321
        assert len(checkouts) == 2
    finally:
        await instance.dispose()


async def test_busy_commit_can_retry_same_transaction_without_replaying_body(
    engine: AsyncEngine,
) -> None:
    async with engine.connect() as connection:
        await connection.exec_driver_sql("PRAGMA journal_mode = DELETE")
    reader = SqlTransaction(engine, read_only=True)
    async with reader as read_connection:
        assert await read_connection.scalar(text("SELECT value FROM records")) == 0
        transaction = SqlTransaction(engine, sqlite_busy_timeout_ms=0)
        async with transaction as writer:
            await writer.exec_driver_sql("UPDATE records SET value = value + 1")
            with pytest.raises(OperationalError) as caught:
                await transaction.commit()
            assert sqlite_lock_error(caught.value) and not transaction.commit_uncertain
            assert not transaction.committed and not transaction.rolled_back
            await reader.rollback()
            await transaction.commit()
        assert transaction.committed
    assert await _value(engine) == 1


async def test_read_only_scope_rejects_writes_and_restores_host_setting(
    engine: AsyncEngine,
) -> None:
    async with SqlTransaction(engine, read_only=True) as connection:
        with pytest.raises(OperationalError):
            await connection.exec_driver_sql("UPDATE records SET value = 1")
    async with SqlTransaction(engine) as connection:
        await connection.exec_driver_sql("UPDATE records SET value = 2")
    assert await _value(engine) == 2


async def test_shared_live_connection_pool_is_rejected_before_io() -> None:
    instance = create_async_engine("sqlite+aiosqlite://")
    try:
        with pytest.raises(TypeError, match="exclusive connection checkouts"):
            SqlTransaction(instance)
    finally:
        await instance.dispose()


@pytest.mark.parametrize("invalidate_listener_failure", [False, True])
async def test_cancel_before_commit_rolls_back_and_releases_connection(
    engine: AsyncEngine,
    invalidate_listener_failure: bool,
) -> None:
    if invalidate_listener_failure:
        _fail_invalidation_event(engine)
    entered, hold = asyncio.Event(), asyncio.Event()
    transaction = SqlTransaction(engine)
    original_driver: object | None = None

    async def write() -> None:
        nonlocal original_driver
        async with transaction as connection:
            original_driver = (await connection.get_raw_connection()).driver_connection
            await connection.exec_driver_sql("UPDATE records SET value = 7")
            entered.set()
            await hold.wait()

    writer = asyncio.create_task(write())
    try:
        await entered.wait()
        writer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await writer
    finally:
        hold.set()
        await asyncio.gather(writer, return_exceptions=True)
    assert not transaction.committed
    assert await _value(engine) == 0
    async with engine.connect() as connection:
        assert (
            await connection.get_raw_connection()
        ).driver_connection is not original_driver


async def test_cleanup_error_cannot_retain_a_public_cause_back_to_body_failure(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = RuntimeError("body failure")
    cleanup = SqlTransactionError("connection close failed", cause=primary)
    original = AsyncConnection.close

    async def close(connection: AsyncConnection) -> None:
        await original(connection)
        raise cleanup

    monkeypatch.setattr(AsyncConnection, "close", close)
    with pytest.raises(RuntimeError) as caught:
        async with SqlTransaction(engine):
            raise primary
    monkeypatch.setattr(AsyncConnection, "close", original)
    assert caught.value is primary and primary.__cause__ is cleanup
    assert cleanup.__cause__ is None and cleanup.cause is None
    assert isinstance(engine.pool, AsyncAdaptedQueuePool)
    assert engine.pool.checkedout() == 0


async def test_schema_lock_requires_a_finite_timeout_before_io(
    engine: AsyncEngine,
) -> None:
    invalid_options: dict[str, Any] = {"schema_lock_timeout_seconds": None}
    with pytest.raises(TypeError, match="schema_lock_timeout_seconds"):
        SqlTransaction(engine, schema_lock="test", **invalid_options)
    assert isinstance(engine.pool, AsyncAdaptedQueuePool)
    assert engine.pool.checkedout() == 0


async def test_historical_cancellation_does_not_replace_a_current_body_failure(
    engine: AsyncEngine,
) -> None:
    primary = RuntimeError("current body failure")
    historical = asyncio.CancelledError("another operation")
    with pytest.raises(RuntimeError) as caught:
        async with SqlTransaction(engine):
            raise primary from historical
    assert caught.value is primary and primary.__cause__ is historical


@pytest.mark.parametrize("failure_after_commit", [False, True])
async def test_commit_settles_under_repeated_cancellation_and_records_uncertainty(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    failure_after_commit: bool,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    original = AsyncConnection.exec_driver_sql
    transaction = SqlTransaction(engine)

    async def execute(
        connection: AsyncConnection,
        statement: str,
        parameters: Sequence[Mapping[str, Any] | tuple[Any, ...]]
        | Mapping[str, Any]
        | tuple[Any, ...]
        | None = None,
        execution_options: Mapping[str, object] | None = None,
    ) -> CursorResult[Any]:
        if statement == "COMMIT":
            entered.set()
            await release.wait()
        result = await original(connection, statement, parameters, execution_options)
        if statement == "COMMIT" and failure_after_commit:
            raise OperationalError(
                statement, None, OSError("commit acknowledgement lost")
            )
        return result

    monkeypatch.setattr(AsyncConnection, "exec_driver_sql", execute)

    async def write() -> None:
        async with transaction as connection:
            await connection.exec_driver_sql("UPDATE records SET value = value + 1")

    writer = asyncio.create_task(write())
    try:
        await entered.wait()
        writer.cancel("cancel commit waiter")
        writer.cancel("repeat cancellation")
        release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await writer
    finally:
        release.set()
        await asyncio.gather(writer, return_exceptions=True)
    assert transaction.committed is not failure_after_commit
    assert transaction.commit_uncertain is failure_after_commit
    assert await _value(engine) == 1
    if failure_after_commit:
        assert caught.value.__cause__ is not None


async def test_rollback_failure_retains_original_and_discards_connection(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = AsyncConnection.exec_driver_sql
    rollback_failure = OperationalError("ROLLBACK", None, OSError("rollback failed"))
    body_failure = RuntimeError("body failed")
    transaction = SqlTransaction(engine)

    async def execute(
        connection: AsyncConnection, statement: str, *args: Any, **kwargs: Any
    ) -> CursorResult[Any]:
        if statement == "ROLLBACK":
            raise rollback_failure
        return await original(connection, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncConnection, "exec_driver_sql", execute)
    with pytest.raises(RuntimeError) as caught:
        async with transaction as connection:
            await connection.exec_driver_sql("UPDATE records SET value = 9")
            raise body_failure
    monkeypatch.setattr(AsyncConnection, "exec_driver_sql", original)
    assert caught.value is body_failure and caught.value.__cause__ is rollback_failure
    assert not transaction.rolled_back and not transaction.committed
    assert await _value(engine) == 0


@pytest.mark.parametrize("invalidate_listener_failure", [False, True])
async def test_failed_connection_close_still_returns_its_pool_checkout(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    invalidate_listener_failure: bool,
) -> None:
    if invalidate_listener_failure:
        _fail_invalidation_event(engine)
    original = AsyncConnection.close
    failure = RuntimeError("connection close failed")

    async def close(connection: AsyncConnection) -> None:
        raise failure

    monkeypatch.setattr(AsyncConnection, "close", close)
    with pytest.raises(RuntimeError) as caught:
        async with SqlTransaction(engine) as connection:
            await connection.exec_driver_sql("UPDATE records SET value = 1")
    assert caught.value is failure
    monkeypatch.setattr(AsyncConnection, "close", original)
    assert isinstance(engine.pool, AsyncAdaptedQueuePool)
    assert engine.pool.checkedout() == 0
    assert await _value(engine) == 1


def _fail_invalidation_event(engine: AsyncEngine) -> None:
    def listener(
        _connection: object, _record: object, _error: BaseException | None
    ) -> None:
        raise RuntimeError("host invalidation listener failed")

    event.listen(engine.sync_engine.pool, "invalidate", listener)


async def test_catching_uncertain_commit_cannot_make_scope_commit_again(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = AsyncConnection.exec_driver_sql
    commits = 0
    failure = OperationalError("COMMIT", None, OSError("commit outcome uncertain"))

    async def execute(
        connection: AsyncConnection, statement: str, *args: Any, **kwargs: Any
    ) -> CursorResult[Any]:
        nonlocal commits
        if statement == "COMMIT":
            commits += 1
            if commits == 1:
                raise failure
        return await original(connection, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncConnection, "exec_driver_sql", execute)
    transaction = SqlTransaction(engine)
    with pytest.raises(OperationalError) as caught:
        async with transaction as connection:
            await connection.exec_driver_sql("UPDATE records SET value = 1")
            try:
                await transaction.commit()
            except OperationalError:
                pass
    assert caught.value is failure
    assert commits == 1 and transaction.commit_uncertain and not transaction.committed
    assert await _value(engine) == 0
