"""SQLAlchemy dialect capabilities, write transactions, startup, and retries."""

from __future__ import annotations

__all__ = [
    "_apply_claim_lock",
    "_begin_write_transaction",
    "_commit_write_transaction",
    "_is_retryable_mysql_conflict",
    "_is_retryable_sqlite_lock",
    "_is_retryable_start_failure",
    "_renew_worker_loop",
    "_run_claim_transaction",
    "_run_write_transaction",
    "_start_once",
    "_write_transaction_once",
]

import asyncio
import math
import sqlite3
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Literal, TypeVar

from sqlalchemy import delete, insert, select, update
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql import Select

from ..errors import (
    OpenSandboxStateCommitUncertainError,
    OpenSandboxStateConfigurationError,
    OpenSandboxStateError,
)
from ._sql_schema import (
    _initialize_schema,
    _warm_slots,
    _workers,
)

if TYPE_CHECKING:
    from .sqlalchemy import SQLAlchemyOpenSandboxState

_ResultT = TypeVar("_ResultT")


_MYSQL_STARTUP_LOCK_TIMEOUT_SECONDS = 30


_MYSQL_57_LOCK_WAIT_TIMEOUT_SECONDS = 1


_MYSQL_RETRYABLE_CONFLICT_CODES = frozenset({1062, 1205, 1213})


_SQLITE_RETRY_MAX_DELAY_SECONDS = 0.5


_SQLITE_BUSY_WAIT_MAX_SECONDS = 0.01


class _RetryableSQLiteWriteError(Exception):
    """Carry a rolled-back and closed SQLite lock failure to the retry loop."""

    def __init__(self, error: DBAPIError) -> None:
        super().__init__(str(error))
        self.error = error


@dataclass(frozen=True, slots=True)
class _SQLDialectCapabilities:
    """Hold the verified transaction features of one live SQL server."""

    name: Literal["mysql", "sqlite"]
    server_version: tuple[int, ...]
    supports_skip_locked: bool


@dataclass(frozen=True, slots=True)
class _WriteConnectionSetting:
    """Remember one connection-local lock wait value changed by a write attempt."""

    name: Literal["mysql_lock_wait_timeout", "sqlite_busy_timeout"]
    value: int


@dataclass(slots=True)
class _WriteConnectionDisposition:
    """Carry connection reuse safety independently from the primary exception."""

    discard_connection: bool = False
    cancellation: asyncio.CancelledError | None = None


_SelectRowT = TypeVar("_SelectRowT", bound=tuple[object, ...])


def _resolve_dialect_capabilities(
    *,
    dialect_name: str,
    server_version: tuple[int, ...],
    is_mariadb: bool,
) -> _SQLDialectCapabilities:
    """Validate a live SQL server and derive its private locking capabilities."""
    if not server_version:
        raise OpenSandboxStateConfigurationError(
            "OpenSandbox State could not determine the database server version"
        )
    if dialect_name == "sqlite":
        return _SQLDialectCapabilities(
            name="sqlite",
            server_version=server_version,
            supports_skip_locked=False,
        )
    if dialect_name != "mysql":
        raise OpenSandboxStateConfigurationError(
            "SQLAlchemyOpenSandboxState supports only SQLite and MySQL"
        )
    if is_mariadb:
        raise OpenSandboxStateConfigurationError(
            "MariaDB is not a verified OpenSandbox State database"
        )
    if server_version[:2] == (5, 7):
        supports_skip_locked = False
    elif server_version[0] == 8:
        supports_skip_locked = True
    else:
        raise OpenSandboxStateConfigurationError(
            "OpenSandbox State supports MySQL 5.7 and MySQL 8.x"
        )
    return _SQLDialectCapabilities(
        name="mysql",
        server_version=server_version,
        supports_skip_locked=supports_skip_locked,
    )


def _apply_claim_lock(
    statement: Select[_SelectRowT],
    *,
    capabilities: _SQLDialectCapabilities,
) -> Select[_SelectRowT]:
    """Apply the strongest verified non-blocking claim lock when available."""
    if capabilities.name != "mysql":
        return statement
    return statement.with_for_update(skip_locked=capabilities.supports_skip_locked)


def _capabilities_from_connection(
    sync_connection: Connection,
) -> _SQLDialectCapabilities:
    """Read the initialized SQLAlchemy dialect attached to a live connection."""
    dialect = sync_connection.dialect
    server_version = dialect.server_version_info
    if server_version is None or not all(
        isinstance(component, int) for component in server_version
    ):
        normalized_version: tuple[int, ...] = ()
    else:
        normalized_version = tuple(server_version)
    return _resolve_dialect_capabilities(
        dialect_name=dialect.name,
        server_version=normalized_version,
        is_mariadb=bool(getattr(dialect, "is_mariadb", False)),
    )


def _is_retryable_mysql_conflict(
    self: SQLAlchemyOpenSandboxState, error: DBAPIError
) -> bool:
    """Recognize only transaction conflicts MySQL explicitly allows retrying."""
    capabilities = self._capabilities
    if (
        capabilities is None
        or capabilities.name != "mysql"
        or error.orig is None
        or not error.orig.args
    ):
        return False
    code = error.orig.args[0]
    return isinstance(code, int) and code in _MYSQL_RETRYABLE_CONFLICT_CODES


def _is_retryable_sqlite_lock(
    self: SQLAlchemyOpenSandboxState, error: DBAPIError
) -> bool:
    """Recognize SQLite lock result codes without matching driver text."""

    if self._dialect != "sqlite" or not isinstance(error.orig, sqlite3.Error):
        return False
    code = getattr(error.orig, "sqlite_errorcode", None)
    if not isinstance(code, int):
        return False
    return (code & 0xFF) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}


async def _begin_write_transaction(
    self: SQLAlchemyOpenSandboxState, connection: AsyncConnection
) -> None:
    """Open one dialect-specific write transaction without retrying it."""

    capabilities = self._require_capabilities()
    if capabilities.name == "sqlite":
        busy_wait_seconds = min(
            self._poll_interval,
            self._sqlite_retry_timeout,
            _SQLITE_BUSY_WAIT_MAX_SECONDS,
        )
        busy_timeout_ms = (
            0 if busy_wait_seconds <= 0 else max(1, math.ceil(busy_wait_seconds * 1000))
        )
        await connection.exec_driver_sql(f"PRAGMA busy_timeout = {busy_timeout_ms}")
        await connection.exec_driver_sql("BEGIN IMMEDIATE")
        return
    await connection.begin()
    if not capabilities.supports_skip_locked:
        await connection.exec_driver_sql(
            "SET SESSION innodb_lock_wait_timeout = "
            f"{_MYSQL_57_LOCK_WAIT_TIMEOUT_SECONDS}"
        )


async def _capture_write_connection_setting(
    self: SQLAlchemyOpenSandboxState,
    connection: AsyncConnection,
) -> _WriteConnectionSetting | None:
    """Read the host connection setting that this write attempt must restore."""

    capabilities = self._require_capabilities()
    if capabilities.name == "sqlite":
        value = (await connection.exec_driver_sql("PRAGMA busy_timeout")).scalar_one()
        if not isinstance(value, int):
            raise OpenSandboxStateError("SQLite returned an invalid busy_timeout")
        # Reading a PRAGMA activates SQLAlchemy's autobegin wrapper even though SQLite
        # has not opened the write transaction yet.
        await connection.rollback()
        return _WriteConnectionSetting(name="sqlite_busy_timeout", value=value)
    if capabilities.supports_skip_locked:
        return None
    value = (
        await connection.exec_driver_sql("SELECT @@SESSION.innodb_lock_wait_timeout")
    ).scalar_one()
    if not isinstance(value, int):
        raise OpenSandboxStateError(
            "MySQL returned an invalid innodb_lock_wait_timeout"
        )
    await connection.rollback()
    return _WriteConnectionSetting(name="mysql_lock_wait_timeout", value=value)


async def _restore_write_connection_setting(
    connection: AsyncConnection,
    setting: _WriteConnectionSetting | None,
) -> None:
    """Restore one borrowed pool connection before it becomes reusable."""

    if setting is None:
        return
    if setting.name == "sqlite_busy_timeout":
        await connection.exec_driver_sql(f"PRAGMA busy_timeout = {setting.value}")
    else:
        await connection.exec_driver_sql(
            f"SET SESSION innodb_lock_wait_timeout = {setting.value}"
        )
    # SET SESSION and PRAGMA activate SQLAlchemy's local transaction marker. Rolling
    # it back does not undo the connection-level value; it only returns a clean wrapper.
    await connection.rollback()


async def _commit_write_transaction(
    self: SQLAlchemyOpenSandboxState,
    connection: AsyncConnection,
    disposition: _WriteConnectionDisposition,
) -> None:
    """Settle COMMIT and retry only SQLite's known uncommitted BUSY result."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + self._sqlite_retry_timeout
    delay = min(self._poll_interval, _SQLITE_RETRY_MAX_DELAY_SECONDS)
    while True:
        if disposition.cancellation is not None:
            await connection.rollback()
            raise disposition.cancellation

        async def commit_once() -> None:
            if self._dialect == "sqlite":
                await connection.exec_driver_sql("COMMIT")
                # Raw BEGIN/COMMIT keeps SQLAlchemy's autobegin marker active.
                # A no-op DBAPI rollback resets only that local wrapper state.
                await connection.rollback()
                return
            await connection.commit()

        commit_task = asyncio.create_task(
            commit_once(),
            name="tinkerfin-opensandbox-state-commit",
        )
        cancellation: asyncio.CancelledError | None = None
        while not commit_task.done():
            try:
                await asyncio.wait((commit_task,))
            except asyncio.CancelledError as error:
                current = asyncio.current_task()
                if current is None or current.cancelling() == 0:
                    break
                if cancellation is None:
                    cancellation = error
                continue

        commit_error: BaseException | None = None
        if commit_task.cancelled():
            commit_error = asyncio.CancelledError(
                "OpenSandbox database COMMIT task was cancelled"
            )
        else:
            try:
                commit_task.result()
            except BaseException as error:  # noqa: BLE001 - preserve DB outcome
                commit_error = error

        if cancellation is None:
            cancellation = disposition.cancellation
        if cancellation is not None:
            if commit_error is None:
                cancellation.add_note(
                    "OpenSandbox write COMMIT completed after caller cancellation"
                )
            else:
                cancellation.add_note(
                    "OpenSandbox write COMMIT also failed after caller "
                    f"cancellation: {type(commit_error).__name__}: {commit_error}"
                )
                if isinstance(commit_error, DBAPIError) and (
                    self._is_retryable_sqlite_lock(commit_error)
                ):
                    try:
                        await connection.rollback()
                    except BaseException as rollback_error:  # noqa: BLE001
                        disposition.discard_connection = True
                        cancellation.add_note(
                            "OpenSandbox cancelled COMMIT rollback also failed: "
                            f"{type(rollback_error).__name__}: {rollback_error}"
                        )
                else:
                    disposition.discard_connection = True
            raise cancellation
        if commit_error is None:
            return
        if isinstance(commit_error, asyncio.CancelledError):
            disposition.discard_connection = True
            raise commit_error
        if isinstance(commit_error, DBAPIError) and (
            self._is_retryable_sqlite_lock(commit_error)
        ):
            remaining = deadline - loop.time()
            if remaining <= 0:
                await connection.rollback()
                raise OpenSandboxStateError(
                    "SQLite COMMIT lock retry budget exhausted after "
                    f"{self._sqlite_retry_timeout:g} seconds"
                ) from commit_error
            sleep_for = min(delay, remaining)
            try:
                await asyncio.sleep(sleep_for)
            except asyncio.CancelledError:
                await connection.rollback()
                raise
            if sleep_for >= remaining:
                await connection.rollback()
                raise OpenSandboxStateError(
                    "SQLite COMMIT lock retry budget exhausted after "
                    f"{self._sqlite_retry_timeout:g} seconds"
                ) from commit_error
            delay = min(delay * 2, _SQLITE_RETRY_MAX_DELAY_SECONDS)
            continue
        disposition.discard_connection = True
        raise OpenSandboxStateCommitUncertainError(
            "OpenSandbox write COMMIT outcome is uncertain; "
            "the transaction was not retried"
        ) from commit_error


async def _write_transaction_once(
    self: SQLAlchemyOpenSandboxState,
    operation: Callable[[AsyncConnection], Awaitable[_ResultT]],
) -> _ResultT:
    """Finish database I/O before applying cancellation at the commit boundary.

    SQLAlchemy's aiosqlite cursor execution can retain an unconsumed cursor when
    cancelled between execute and fetchall. Even closing its connection can then
    retain SQLite locks. Keep the complete attempt owned, record caller intent,
    and roll back before COMMIT when cancellation has been requested. An already
    issued COMMIT retains its confirmed or uncertain outcome handling.
    """
    disposition = _WriteConnectionDisposition()
    task = asyncio.create_task(
        _execute_write_transaction(self, operation, disposition),
        name="tinkerfin-sandbox-state-write",
    )
    return await _await_database_task(task, disposition)


async def _read_rows(
    self: SQLAlchemyOpenSandboxState, statement: Select[_SelectRowT]
) -> Sequence[RowMapping]:
    """Consume a read and return its connection before propagating cancellation."""

    async def read() -> Sequence[RowMapping]:
        async with self._engine.connect() as connection:
            return (await connection.execute(statement)).mappings().all()

    task = asyncio.create_task(read(), name="tinkerfin-sandbox-state-read")
    return await _await_database_task(task)


async def _await_database_task(
    task: asyncio.Task[_ResultT],
    disposition: _WriteConnectionDisposition | None = None,
) -> _ResultT:
    """Keep driver results and connection return owned through repeated cancellation."""
    try:
        await asyncio.wait((task,))
        return task.result()
    except asyncio.CancelledError as cancellation:
        current = asyncio.current_task()
        if (
            current is not None
            and current.cancelling()
            and disposition is not None
            and disposition.cancellation is None
        ):
            disposition.cancellation = cancellation
        while not task.done():
            try:
                await asyncio.wait((task,))
            except asyncio.CancelledError:
                continue
            except BaseException:  # noqa: BLE001 - retrieve the owned outcome below
                break
        try:
            task.result()
        except BaseException as error:  # noqa: BLE001 - caller cancellation remains primary
            if not isinstance(error, asyncio.CancelledError):
                cancellation.add_note(
                    f"Sandbox State transaction settlement also failed: {type(error).__name__}"
                )
        raise cancellation


async def _execute_write_transaction(
    self: SQLAlchemyOpenSandboxState,
    operation: Callable[[AsyncConnection], Awaitable[_ResultT]],
    connection_disposition: _WriteConnectionDisposition,
) -> _ResultT:
    """Run one write attempt and expose only safely retryable SQLite locks."""

    connection = await self._engine.connect()
    retryable_error: DBAPIError | None = None
    connection_setting: _WriteConnectionSetting | None = None
    primary_error: BaseException | None = None
    try:
        if connection_disposition.cancellation is not None:
            raise connection_disposition.cancellation
        connection_setting = await _capture_write_connection_setting(self, connection)
        if connection_disposition.cancellation is not None:
            raise connection_disposition.cancellation
        try:
            await self._begin_write_transaction(connection)
        except DBAPIError as error:
            try:
                await connection.rollback()
            except BaseException as rollback_error:  # noqa: BLE001
                error.add_note(
                    "OpenSandbox SQLite BEGIN rollback also failed: "
                    f"{type(rollback_error).__name__}: {rollback_error}"
                )
                raise error.with_traceback(error.__traceback__)
            if self._is_retryable_sqlite_lock(error):
                retryable_error = error
            else:
                raise

        if retryable_error is None:
            try:
                if connection_disposition.cancellation is not None:
                    raise connection_disposition.cancellation
                result = await operation(connection)
                if connection_disposition.cancellation is not None:
                    raise connection_disposition.cancellation
            except BaseException as operation_error:
                try:
                    await connection.rollback()
                except BaseException as rollback_error:  # noqa: BLE001
                    operation_error.add_note(
                        "OpenSandbox write rollback also failed: "
                        f"{type(rollback_error).__name__}: {rollback_error}"
                    )
                    raise operation_error.with_traceback(operation_error.__traceback__)
                if isinstance(operation_error, DBAPIError) and (
                    self._is_retryable_sqlite_lock(operation_error)
                ):
                    retryable_error = operation_error
                else:
                    raise
            else:
                await self._commit_write_transaction(
                    connection,
                    connection_disposition,
                )
                return result
    except BaseException as error:
        primary_error = error
        raise
    finally:
        # Connection return has the same owner as statement consumption. Caller
        # cancellation must not interrupt pool reset or lose a borrowed slot.
        cleanup_error: BaseException | None = None
        discard_connection = (
            connection_disposition.discard_connection
            or isinstance(
                primary_error,
                OpenSandboxStateCommitUncertainError,
            )
            or (
                isinstance(primary_error, DBAPIError)
                and primary_error.connection_invalidated
            )
        )
        if discard_connection:
            try:
                await connection.invalidate()
            except BaseException as error:  # noqa: BLE001 - preserve primary outcome
                cleanup_error = error
        else:
            try:
                await _restore_write_connection_setting(connection, connection_setting)
            except BaseException as error:  # noqa: BLE001 - invalidate before reuse
                cleanup_error = error
                try:
                    await connection.invalidate()
                except BaseException as invalidate_error:  # noqa: BLE001
                    error.add_note(
                        "OpenSandbox connection invalidation also failed: "
                        f"{type(invalidate_error).__name__}: {invalidate_error}"
                    )
        try:
            await connection.close()
        except BaseException as error:  # noqa: BLE001 - preserve the primary failure
            if cleanup_error is None:
                cleanup_error = error
            else:
                cleanup_error.add_note(
                    "OpenSandbox connection close also failed: "
                    f"{type(error).__name__}: {error}"
                )
        if cleanup_error is not None:
            if primary_error is None:
                raise cleanup_error
            primary_error.add_note(
                "OpenSandbox connection setting cleanup also failed: "
                f"{type(cleanup_error).__name__}: {cleanup_error}"
            )

    assert retryable_error is not None
    raise _RetryableSQLiteWriteError(retryable_error)


async def _run_write_transaction(
    self: SQLAlchemyOpenSandboxState,
    operation: Callable[[AsyncConnection], Awaitable[_ResultT]],
) -> _ResultT:
    """Retry only rolled-back SQLite lock conflicts within one deadline."""

    if self._dialect != "sqlite":
        return await self._write_transaction_once(operation)

    loop = asyncio.get_running_loop()
    deadline = loop.time() + self._sqlite_retry_timeout
    delay = min(self._poll_interval, _SQLITE_RETRY_MAX_DELAY_SECONDS)
    while True:
        try:
            return await self._write_transaction_once(operation)
        except _RetryableSQLiteWriteError as retry:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise OpenSandboxStateError(
                    "SQLite write lock retry budget exhausted after "
                    f"{self._sqlite_retry_timeout:g} seconds"
                ) from retry.error
            sleep_for = min(delay, remaining)
            await asyncio.sleep(sleep_for)
            if sleep_for >= remaining:
                raise OpenSandboxStateError(
                    "SQLite write lock retry budget exhausted after "
                    f"{self._sqlite_retry_timeout:g} seconds"
                ) from retry.error
            delay = min(delay * 2, _SQLITE_RETRY_MAX_DELAY_SECONDS)


async def _run_claim_transaction(
    self: SQLAlchemyOpenSandboxState,
    operation: Callable[[AsyncConnection], Awaitable[_ResultT]],
) -> _ResultT | None:
    """Run one claim operation and preserve MySQL 5.7 timeout semantics."""

    try:
        return await self._run_write_transaction(operation)
    except DBAPIError as exc:
        capabilities = self._require_capabilities()
        code = exc.orig.args[0] if exc.orig is not None and exc.orig.args else None
        if (
            capabilities.name == "mysql"
            and not capabilities.supports_skip_locked
            and code == 1205
        ):
            return None
        raise


async def start(self: SQLAlchemyOpenSandboxState, *, warm_pool_size: int) -> None:
    """Create the schema through one instance-owned startup attempt.

    Concurrent callers using the same capacity share the shielded attempt. Caller
    cancellation does not cancel database work that may already own a worker row.
    A capacity conflict rolls back before worker registration and can be retried
    after the conflicting workers close. Failures with uncertain commit state
    remain attached to this instance so a retry cannot duplicate ownership.

    Args:
        warm_pool_size: Database-global warm-slot capacity for this namespace.

    Raises:
        OpenSandboxStateConfigurationError: This instance or another active worker
            uses a different capacity.
        OpenSandboxStateError: The State is closed or cannot initialize safely.
        ValueError: The capacity is negative.
    """
    if isinstance(warm_pool_size, bool) or not isinstance(warm_pool_size, int):
        raise TypeError("warm_pool_size must be an integer")
    if warm_pool_size < 0:
        raise ValueError("warm_pool_size must not be negative")
    async with self._start_lock:
        if self._closed:
            raise OpenSandboxStateError("OpenSandbox state is closed")
        start_task = self._start_task
        if (
            start_task is not None
            and self._is_retryable_start_failure(start_task)
            and self._close_task is None
        ):
            self._start_task = None
            self._warm_pool_size = None
            start_task = None
        if start_task is not None:
            if self._warm_pool_size != warm_pool_size:
                raise OpenSandboxStateConfigurationError(
                    "OpenSandbox State is already started with a different "
                    "warm_pool_size"
                )
        else:
            self._warm_pool_size = warm_pool_size
            start_task = asyncio.create_task(
                self._start_once(warm_pool_size=warm_pool_size),
                name=f"tinkerfin-opensandbox-start:{self._worker_id}",
            )
            self._start_task = start_task
    try:
        await asyncio.shield(start_task)
    except OpenSandboxStateConfigurationError:
        async with self._start_lock:
            if self._start_task is start_task and self._close_task is None:
                self._start_task = None
                self._warm_pool_size = None
        raise


def _is_retryable_start_failure(start_task: asyncio.Task[None]) -> bool:
    """Return whether a settled startup failure is known to precede registration."""
    if not start_task.done() or start_task.cancelled():
        return False
    return isinstance(
        start_task.exception(),
        OpenSandboxStateConfigurationError,
    )


async def _start_once(self: SQLAlchemyOpenSandboxState, *, warm_pool_size: int) -> None:
    """Initialize the database and worker renewal task under the startup lock."""
    async with self._engine.connect() as connection:
        self._capabilities = await connection.run_sync(_capabilities_from_connection)

    async def initialize(connection: AsyncConnection) -> None:
        now = self._now()
        await connection.run_sync(_initialize_schema)

        await connection.execute(
            delete(_workers).where(
                _workers.c.namespace == self._namespace,
                _workers.c.lease_expires_at <= now,
            )
        )
        active_capacities = set(
            (
                await connection.execute(
                    select(_workers.c.warm_pool_size).where(
                        _workers.c.namespace == self._namespace,
                        _workers.c.lease_expires_at > now,
                    )
                )
            ).scalars()
        )
        if active_capacities and active_capacities != {warm_pool_size}:
            raise OpenSandboxStateConfigurationError(
                "Active OpenSandbox workers use a different warm_pool_size"
            )

        existing_slots = {
            int(row.slot): row.sandbox_id
            for row in (
                await connection.execute(
                    select(
                        _warm_slots.c.slot,
                        _warm_slots.c.sandbox_id,
                    ).where(_warm_slots.c.namespace == self._namespace)
                )
            )
        }
        if not active_capacities:
            # No live worker can still own a warm transition. Releasing every stale
            # claim lets the next manager validate published IDs or refill empty slots
            # before it reports readiness.
            await connection.execute(
                update(_warm_slots)
                .where(
                    _warm_slots.c.namespace == self._namespace,
                    _warm_slots.c.slot < warm_pool_size,
                )
                .values(
                    claim_token=None,
                    lease_expires_at=None,
                    updated_at=now,
                )
            )
            for slot, sandbox_id in existing_slots.items():
                if slot < warm_pool_size:
                    continue
                if sandbox_id is not None:
                    await self._enqueue_cleanup_in_transaction(
                        connection,
                        str(sandbox_id),
                        now=now,
                    )
            await connection.execute(
                delete(_warm_slots).where(
                    _warm_slots.c.namespace == self._namespace,
                    _warm_slots.c.slot >= warm_pool_size,
                )
            )
        for slot in range(warm_pool_size):
            if slot in existing_slots:
                continue
            await connection.execute(
                insert(_warm_slots).values(
                    namespace=self._namespace,
                    slot=slot,
                    sandbox_id=None,
                    generation=0,
                    claim_token=None,
                    lease_expires_at=None,
                    updated_at=now,
                )
            )
        await connection.execute(
            insert(_workers).values(
                namespace=self._namespace,
                worker_id=self._worker_id,
                warm_pool_size=warm_pool_size,
                lease_expires_at=now + timedelta(seconds=self._worker_lease_ttl),
                updated_at=now,
            )
        )

    async with self._startup_lock():
        await self._run_write_transaction(initialize)
    self._started = True
    self._worker_renew_task = asyncio.create_task(
        self._renew_worker_loop(),
        name=f"tinkerfin-opensandbox-worker:{self._worker_id}",
    )


async def _renew_worker_loop(self: SQLAlchemyOpenSandboxState) -> None:
    """Keep this worker active without exposing another coordinator."""
    try:
        while True:
            await asyncio.sleep(self._worker_lease_ttl / 3)

            async def renew(connection: AsyncConnection) -> None:
                now = self._now()
                result = await connection.execute(
                    update(_workers)
                    .where(
                        _workers.c.namespace == self._namespace,
                        _workers.c.worker_id == self._worker_id,
                    )
                    .values(
                        lease_expires_at=now
                        + timedelta(seconds=self._worker_lease_ttl),
                        updated_at=now,
                    )
                )
                if result.rowcount != 1:
                    raise OpenSandboxStateError(
                        "OpenSandbox worker registration was lost"
                    )

            await self._run_write_transaction(renew)
    except asyncio.CancelledError:
        raise
    # Renewal crosses engine and driver boundaries, so any ordinary failure makes
    # this worker fail closed.
    except Exception as exc:  # noqa: BLE001
        self._worker_failure = exc
