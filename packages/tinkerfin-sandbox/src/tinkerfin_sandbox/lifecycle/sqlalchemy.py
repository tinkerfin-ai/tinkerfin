"""Optional SQLAlchemy Core implementation of OpenSandbox allocation state."""

from __future__ import annotations

import asyncio
import hashlib
import math
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, TypeVar
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    delete,
    insert,
    select,
    text,
    update,
)
from sqlalchemy import (
    inspect as sa_inspect,
)
from sqlalchemy.dialects import mysql as mysql_dialect
from sqlalchemy.dialects import sqlite as sqlite_dialect
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    create_async_engine,
)
from sqlalchemy.schema import CreateIndex, CreateTable, DefaultClause
from sqlalchemy.sql import Select

from ..errors import (
    OpenSandboxStateConfigurationError,
    OpenSandboxStateError,
    OpenSandboxStateOwnershipError,
)
from .state import (
    OpenSandboxBinding,
    OpenSandboxCleanupClaim,
    OpenSandboxOwnerClaim,
    OpenSandboxState,
    OpenSandboxWarmClaim,
    _owner_digest,
)

_SCHEMA_VERSION = 2
_MYSQL_STARTUP_LOCK_TIMEOUT_SECONDS = 30
_MYSQL_57_LOCK_WAIT_TIMEOUT_SECONDS = 1
_MYSQL_RETRYABLE_CONFLICT_CODES = frozenset({1062, 1205, 1213})
_SQLITE_RETRY_MAX_DELAY_SECONDS = 0.5
_SQLITE_BUSY_WAIT_MAX_SECONDS = 0.01

_ResultT = TypeVar("_ResultT")


class _RetryableSQLiteWriteError(Exception):
    """Carry a rolled-back and closed SQLite lock failure to the retry loop."""

    def __init__(self, error: DBAPIError) -> None:
        super().__init__(str(error))
        self.error = error


class _CommitOutcomeUncertainError(OpenSandboxStateError):
    """Prevent callers from retrying a write whose COMMIT result is unknown."""


@dataclass(frozen=True, slots=True)
class SQLAlchemyOpenSandboxStateSchema:
    """Describe one complete deployable OpenSandbox State database schema.

    Attributes:
        component: Persistent component identified by the schema version row.
        version: Exact schema version represented by the DDL.
        dialect: SQL dialect accepted by the deployment script.
        table_names: Complete table set in deterministic creation order.
        ddl: Full empty-database DDL including indexes and version initialization.
    """

    component: Literal["opensandbox-state"]
    version: int
    dialect: Literal["mysql", "sqlite"]
    table_names: tuple[str, ...]
    ddl: str


@dataclass(frozen=True, slots=True)
class _SQLDialectCapabilities:
    """Hold the verified transaction features of one live SQL server."""

    name: Literal["mysql", "sqlite"]
    server_version: tuple[int, ...]
    supports_skip_locked: bool


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


_metadata = MetaData()

_schema_versions = Table(
    "tinkerfin_opensandbox_schema_versions",
    _metadata,
    Column(
        "component",
        String(64),
        primary_key=True,
        comment="Persistent component whose schema version is recorded",
    ),
    Column(
        "version",
        Integer,
        nullable=False,
        comment="Latest fully committed forward migration",
    ),
    comment="Internal OpenSandbox State schema version",
)

_owners = Table(
    "tinkerfin_opensandbox_owners",
    _metadata,
    Column(
        "namespace",
        String(64),
        nullable=False,
        comment="Logical OpenSandbox State deployment namespace",
    ),
    Column(
        "owner_digest",
        String(43),
        nullable=False,
        comment="URL-safe SHA-256 digest of the namespace and owner key",
    ),
    Column(
        "sandbox_id",
        String(255),
        nullable=True,
        comment="Currently committed remote OpenSandbox identifier",
    ),
    Column(
        "binding_generation",
        BigInteger,
        nullable=False,
        server_default=text("0"),
        comment="Fencing generation that committed the current Sandbox binding",
    ),
    Column(
        "generation",
        BigInteger,
        nullable=False,
        server_default=text("0"),
        comment="Monotonic fencing generation for owner transitions",
    ),
    Column(
        "claim_token",
        String(32),
        nullable=True,
        comment="Opaque token of the worker currently changing this owner",
    ),
    Column(
        "lease_expires_at",
        DateTime(timezone=False),
        nullable=True,
        comment="UTC expiry of the current owner transition lease",
    ),
    Column(
        "updated_at",
        DateTime(timezone=False),
        nullable=False,
        comment="UTC time of the latest owner state mutation",
    ),
    PrimaryKeyConstraint("namespace", "owner_digest"),
    comment="Authoritative owner binding and transition fencing state",
)
Index(
    "ix_tinkerfin_opensandbox_owners_lease",
    _owners.c.namespace,
    _owners.c.lease_expires_at,
)


def _type_signature(column_type: object) -> tuple[str, int | None]:
    """Normalize reflected SQLite/MySQL types into the schema contract."""
    if isinstance(column_type, String):
        return ("string", column_type.length)
    if isinstance(column_type, BigInteger):
        return ("bigint", None)
    if isinstance(column_type, Integer):
        return ("integer", None)
    if isinstance(column_type, DateTime):
        return ("datetime", None)
    return (type(column_type).__name__.lower(), None)


def _default_signature(value: object | None) -> str | None:
    """Normalize equivalent SQLite/MySQL reflected server defaults."""
    if value is None:
        return None
    normalized = str(value).strip()
    while len(normalized) >= 2 and normalized[0] == "(" and normalized[-1] == ")":
        normalized = normalized[1:-1].strip()
    if (
        len(normalized) >= 2
        and normalized[0] in {"'", '"'}
        and normalized[-1] == normalized[0]
    ):
        normalized = normalized[1:-1]
    return normalized.casefold()


def _validate_schema(sync_connection: Connection) -> None:
    """Reject existing tables that do not match the internal State schema."""
    inspector = sa_inspect(sync_connection)
    issues: list[str] = []
    for table in _metadata.sorted_tables:
        if not inspector.has_table(table.name):
            issues.append(f"{table.name}: missing table")
            continue

        reflected_columns = {
            str(column["name"]): column for column in inspector.get_columns(table.name)
        }
        expected_names = {column.name for column in table.columns}
        actual_names = set(reflected_columns)
        if actual_names != expected_names:
            missing = sorted(expected_names - actual_names)
            unexpected = sorted(actual_names - expected_names)
            issues.append(
                f"{table.name}: columns differ; missing={missing}, "
                f"unexpected={unexpected}"
            )
            continue

        for expected in table.columns:
            actual = reflected_columns[expected.name]
            if _type_signature(actual["type"]) != _type_signature(expected.type):
                issues.append(f"{table.name}.{expected.name}: incompatible type")
            if bool(actual["nullable"]) != bool(expected.nullable):
                issues.append(f"{table.name}.{expected.name}: incompatible nullable")
            server_default = expected.server_default
            expected_default = _default_signature(
                server_default.arg
                if isinstance(server_default, DefaultClause)
                else server_default
            )
            actual_default = _default_signature(actual.get("default"))
            if actual_default != expected_default:
                issues.append(f"{table.name}.{expected.name}: incompatible default")

        reflected_primary_key = tuple(
            inspector.get_pk_constraint(table.name).get("constrained_columns") or ()
        )
        expected_primary_key = tuple(
            column.name for column in table.primary_key.columns
        )
        if reflected_primary_key != expected_primary_key:
            issues.append(f"{table.name}: incompatible primary key")

        reflected_indexes = {
            str(index["name"]): tuple(index.get("column_names") or ())
            for index in inspector.get_indexes(table.name)
            if index.get("name") is not None
        }
        for expected_index in table.indexes:
            expected_name = expected_index.name
            if expected_name is None:
                issues.append(f"{table.name}: expected index has no name")
                continue
            expected_columns = tuple(column.name for column in expected_index.columns)
            if reflected_indexes.get(expected_name) != expected_columns:
                issues.append(
                    f"{table.name}: missing or incompatible index {expected_name}"
                )

    if issues:
        raise OpenSandboxStateError(
            "OpenSandbox State schema is incompatible: " + "; ".join(issues)
        )


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


def _has_schema_version_table(sync_connection: Connection) -> bool:
    """Return whether initialization has created the version table."""
    return sa_inspect(sync_connection).has_table(str(_schema_versions.name))


def _migrate_v1_to_v2(sync_connection: Connection) -> None:
    """Add Worker capacity and lease state introduced by schema version 2."""
    _workers.create(sync_connection, checkfirst=True)
    for index in _workers.indexes:
        index.create(sync_connection, checkfirst=True)


_workers = Table(
    "tinkerfin_opensandbox_workers",
    _metadata,
    Column(
        "namespace",
        String(64),
        nullable=False,
        comment="Logical OpenSandbox State deployment namespace",
    ),
    Column(
        "worker_id",
        String(32),
        nullable=False,
        comment="Opaque identifier of one live State instance",
    ),
    Column(
        "warm_pool_size",
        Integer,
        nullable=False,
        comment="Global ready Sandbox capacity requested by this worker",
    ),
    Column(
        "lease_expires_at",
        DateTime(timezone=False),
        nullable=False,
        comment="UTC expiry used to ignore workers that exited without cleanup",
    ),
    Column(
        "updated_at",
        DateTime(timezone=False),
        nullable=False,
        comment="UTC time of the latest Worker registration mutation",
    ),
    PrimaryKeyConstraint("namespace", "worker_id"),
    comment="Live OpenSandbox State workers and shared warm-pool agreement",
)
Index(
    "ix_tinkerfin_opensandbox_workers_lease",
    _workers.c.namespace,
    _workers.c.lease_expires_at,
)

_warm_slots = Table(
    "tinkerfin_opensandbox_warm_slots",
    _metadata,
    Column(
        "namespace",
        String(64),
        nullable=False,
        comment="Logical OpenSandbox State deployment namespace",
    ),
    Column(
        "slot",
        Integer,
        nullable=False,
        comment="Zero-based global warm-pool slot within the namespace",
    ),
    Column(
        "sandbox_id",
        String(255),
        nullable=True,
        comment="Ready remote Sandbox currently held by this global slot",
    ),
    Column(
        "generation",
        BigInteger,
        nullable=False,
        server_default=text("0"),
        comment="Monotonic fencing generation for this warm slot",
    ),
    Column(
        "claim_token",
        String(32),
        nullable=True,
        comment="Opaque token of the worker currently filling this slot",
    ),
    Column(
        "lease_expires_at",
        DateTime(timezone=False),
        nullable=True,
        comment="UTC expiry of the current warm-slot fill lease",
    ),
    Column(
        "updated_at",
        DateTime(timezone=False),
        nullable=False,
        comment="UTC time of the latest warm-slot mutation",
    ),
    PrimaryKeyConstraint("namespace", "slot"),
    comment="Database-global OpenSandbox warm-pool slots",
)
Index(
    "ix_tinkerfin_opensandbox_warm_slots_available",
    _warm_slots.c.namespace,
    _warm_slots.c.sandbox_id,
    _warm_slots.c.lease_expires_at,
)

_cleanup = Table(
    "tinkerfin_opensandbox_cleanup",
    _metadata,
    Column(
        "namespace",
        String(64),
        nullable=False,
        comment="Logical OpenSandbox State deployment namespace",
    ),
    Column(
        "sandbox_id",
        String(255),
        nullable=False,
        comment="Orphaned remote Sandbox awaiting confirmed destruction",
    ),
    Column(
        "generation",
        BigInteger,
        nullable=False,
        server_default=text("0"),
        comment="Monotonic fencing generation for cleanup attempts",
    ),
    Column(
        "claim_token",
        String(32),
        nullable=True,
        comment="Opaque token of the worker currently destroying this Sandbox",
    ),
    Column(
        "lease_expires_at",
        DateTime(timezone=False),
        nullable=True,
        comment="UTC expiry of the current cleanup lease",
    ),
    Column(
        "attempts",
        Integer,
        nullable=False,
        server_default=text("0"),
        comment="Number of times this cleanup target has been claimed",
    ),
    Column(
        "created_at",
        DateTime(timezone=False),
        nullable=False,
        comment="UTC time when this cleanup target was first enqueued",
    ),
    Column(
        "updated_at",
        DateTime(timezone=False),
        nullable=False,
        comment="UTC time of the latest cleanup mutation",
    ),
    PrimaryKeyConstraint("namespace", "sandbox_id"),
    comment="Durable retry queue for failed remote Sandbox destruction",
)
Index(
    "ix_tinkerfin_opensandbox_cleanup_lease",
    _cleanup.c.namespace,
    _cleanup.c.lease_expires_at,
)


def get_sqlalchemy_opensandbox_state_schema(
    *,
    dialect: Literal["mysql", "sqlite"],
) -> SQLAlchemyOpenSandboxStateSchema:
    """Build the complete OpenSandbox State schema without database I/O.

    Args:
        dialect: Deployment SQL dialect. MySQL output targets the common MySQL
            5.7 and 8.x DDL subset.

    Returns:
        An immutable descriptor containing deterministic full-database DDL.

    Raises:
        ValueError: The requested dialect is not ``mysql`` or ``sqlite``.
    """
    if dialect == "mysql":
        compiler = mysql_dialect.dialect()
        compiler.server_version_info = (5, 7, 0)
    elif dialect == "sqlite":
        compiler = sqlite_dialect.dialect()
    else:
        raise ValueError("dialect must be 'mysql' or 'sqlite'")

    tables = tuple(_metadata.sorted_tables)
    statements = [
        str(CreateTable(table).compile(dialect=compiler)).strip() for table in tables
    ]
    indexes = sorted(
        (index for table in tables for index in table.indexes),
        key=lambda index: str(index.name),
    )
    statements.extend(
        str(CreateIndex(index).compile(dialect=compiler)).strip() for index in indexes
    )
    version_insert = insert(_schema_versions).values(
        component="opensandbox-state",
        version=_SCHEMA_VERSION,
    )
    statements.append(
        str(
            version_insert.compile(
                dialect=compiler,
                compile_kwargs={"literal_binds": True},
            )
        ).strip()
    )
    normalized = tuple(
        "\n".join(line.rstrip() for line in statement.splitlines())
        for statement in statements
    )
    return SQLAlchemyOpenSandboxStateSchema(
        component="opensandbox-state",
        version=_SCHEMA_VERSION,
        dialect=dialect,
        table_names=tuple(str(table.name) for table in tables),
        ddl=";\n\n".join(normalized) + ";\n",
    )


class SQLAlchemyOpenSandboxState(OpenSandboxState):
    """Persist OpenSandbox allocation state through SQLAlchemy Core.

    The State creates and owns its asynchronous engine. Callers provide only an
    async SQLAlchemy URL and must close the State, normally by transferring its
    lifecycle to ``OpenSandboxManager``.

    Args:
        url: SQLite ``sqlite+aiosqlite`` or MySQL ``mysql+asyncmy`` URL.
        namespace: Logical deployment namespace stored with every state row.
        lease_ttl: Seconds before an abandoned State claim or Worker can be fenced out.
        poll_interval: Seconds between attempts while another worker owns a claim.
        sqlite_retry_timeout: Maximum seconds spent retrying rolled-back SQLite write
            lock conflicts. The first attempt is always made.

    Raises:
        ValueError: A namespace or timing option is invalid.
    """

    def __init__(
        self,
        *,
        url: str,
        namespace: str = "",
        lease_ttl: float = 15.0,
        poll_interval: float = 0.05,
        sqlite_retry_timeout: float = 5.0,
    ) -> None:
        if len(namespace) > 64:
            raise ValueError("namespace must contain at most 64 characters")
        if not math.isfinite(lease_ttl) or lease_ttl <= 0:
            raise ValueError("lease_ttl must be a finite positive number")
        if not math.isfinite(poll_interval) or poll_interval <= 0:
            raise ValueError("poll_interval must be a finite positive number")
        if isinstance(sqlite_retry_timeout, bool) or not isinstance(
            sqlite_retry_timeout, int | float
        ):
            raise TypeError("sqlite_retry_timeout must be a number")
        resolved_sqlite_retry_timeout = float(sqlite_retry_timeout)
        if (
            not math.isfinite(resolved_sqlite_retry_timeout)
            or resolved_sqlite_retry_timeout < 0
        ):
            raise ValueError("sqlite_retry_timeout must be finite and non-negative")
        self._namespace = namespace
        self._lease_ttl = lease_ttl
        self._poll_interval = poll_interval
        self._sqlite_retry_timeout = resolved_sqlite_retry_timeout
        self._worker_id = uuid4().hex
        self._worker_lease_ttl = lease_ttl
        self._worker_renew_task: asyncio.Task[None] | None = None
        self._worker_failure: Exception | None = None
        self._engine: AsyncEngine = create_async_engine(url)
        self._dialect = self._engine.url.get_backend_name()
        if self._dialect not in {"sqlite", "mysql"}:
            raise ValueError(
                "SQLAlchemyOpenSandboxState supports only SQLite and MySQL"
            )
        self._capabilities: _SQLDialectCapabilities | None = None
        self._start_lock = asyncio.Lock()
        self._start_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._started = False
        self._warm_pool_size: int | None = None
        self._closed = False

    @property
    def persistent(self) -> bool:
        """SQL bindings and shared resources survive manager shutdown."""
        return True

    @property
    def lease_renew_interval(self) -> float:
        """Renew active claims three times within each lease period."""
        return self._lease_ttl / 3

    def _ensure_open(self) -> None:
        if not self._started:
            raise OpenSandboxStateError("OpenSandbox state has not been started")
        if self._closed:
            raise OpenSandboxStateError("OpenSandbox state is closed")
        if self._worker_failure is not None:
            raise OpenSandboxStateError(
                "OpenSandbox worker registration is no longer valid"
            ) from self._worker_failure

    def _require_capabilities(self) -> _SQLDialectCapabilities:
        """Return capabilities established before schema initialization."""
        capabilities = self._capabilities
        if capabilities is None:
            raise OpenSandboxStateError(
                "OpenSandbox database capabilities have not been initialized"
            )
        return capabilities

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC).replace(tzinfo=None)

    def _is_retryable_mysql_conflict(self, error: DBAPIError) -> bool:
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

    def _is_retryable_sqlite_lock(self, error: DBAPIError) -> bool:
        """Recognize SQLite lock result codes without matching driver text."""

        if self._dialect != "sqlite" or not isinstance(error.orig, sqlite3.Error):
            return False
        code = getattr(error.orig, "sqlite_errorcode", None)
        if not isinstance(code, int):
            return False
        return (code & 0xFF) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}

    async def _begin_write_transaction(self, connection: AsyncConnection) -> None:
        """Open one dialect-specific write transaction without retrying it."""

        capabilities = self._require_capabilities()
        if capabilities.name == "sqlite":
            busy_wait_seconds = min(
                self._poll_interval,
                self._sqlite_retry_timeout,
                _SQLITE_BUSY_WAIT_MAX_SECONDS,
            )
            busy_timeout_ms = (
                0
                if busy_wait_seconds <= 0
                else max(1, math.ceil(busy_wait_seconds * 1000))
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

    async def _commit_write_transaction(self, connection: AsyncConnection) -> None:
        """Settle COMMIT and retry only SQLite's known uncommitted BUSY result."""

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._sqlite_retry_timeout
        delay = min(self._poll_interval, _SQLITE_RETRY_MAX_DELAY_SECONDS)
        while True:

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
                    await asyncio.shield(commit_task)
                except asyncio.CancelledError as error:
                    current = asyncio.current_task()
                    if current is None or current.cancelling() == 0:
                        break
                    if cancellation is None:
                        cancellation = error
                    continue
                except Exception:  # noqa: BLE001 - inspect settled task below
                    break

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
                        await connection.rollback()
                raise cancellation
            if commit_error is None:
                return
            if isinstance(commit_error, asyncio.CancelledError):
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
            raise _CommitOutcomeUncertainError(
                "OpenSandbox write COMMIT outcome is uncertain; "
                "the transaction was not retried"
            ) from commit_error

    async def _write_transaction_once(
        self,
        operation: Callable[[AsyncConnection], Awaitable[_ResultT]],
    ) -> _ResultT:
        """Run one write attempt and expose only safely retryable SQLite locks."""

        connection = await self._engine.connect()
        retryable_error: DBAPIError | None = None
        try:
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
                    result = await operation(connection)
                except BaseException as operation_error:
                    try:
                        await connection.rollback()
                    except BaseException as rollback_error:  # noqa: BLE001
                        operation_error.add_note(
                            "OpenSandbox write rollback also failed: "
                            f"{type(rollback_error).__name__}: {rollback_error}"
                        )
                        raise operation_error.with_traceback(
                            operation_error.__traceback__
                        )
                    if isinstance(operation_error, DBAPIError) and (
                        self._is_retryable_sqlite_lock(operation_error)
                    ):
                        retryable_error = operation_error
                    else:
                        raise
                else:
                    await self._commit_write_transaction(connection)
                    return result
        finally:
            await connection.close()

        assert retryable_error is not None
        raise _RetryableSQLiteWriteError(retryable_error)

    async def _run_write_transaction(
        self,
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
        self,
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

    @asynccontextmanager
    async def _startup_lock(self) -> AsyncIterator[None]:
        """Serialize MySQL DDL and initial State rows inside one database."""
        if self._require_capabilities().name != "mysql":
            yield
            return

        database = self._engine.url.database or ""
        database_digest = hashlib.sha256(database.encode()).hexdigest()[:32]
        lock_name = f"tinkerfin:opensandbox:{database_digest}"
        connection = await self._engine.connect()
        acquired = False
        try:
            result = await connection.scalar(
                text("SELECT GET_LOCK(:lock_name, :timeout_seconds)"),
                {
                    "lock_name": lock_name,
                    "timeout_seconds": _MYSQL_STARTUP_LOCK_TIMEOUT_SECONDS,
                },
            )
            if result != 1:
                raise OpenSandboxStateError(
                    "Timed out acquiring the OpenSandbox State startup lock"
                )
            acquired = True
            yield
        finally:
            try:
                if acquired:
                    released = await connection.scalar(
                        text("SELECT RELEASE_LOCK(:lock_name)"),
                        {"lock_name": lock_name},
                    )
                    if released != 1:
                        await connection.invalidate()
            except BaseException:
                await connection.invalidate()
                raise
            finally:
                await connection.close()

    async def start(self, *, warm_pool_size: int) -> None:
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

    @staticmethod
    def _is_retryable_start_failure(start_task: asyncio.Task[None]) -> bool:
        """Return whether a settled startup failure is known to precede registration."""
        if not start_task.done() or start_task.cancelled():
            return False
        return isinstance(
            start_task.exception(),
            OpenSandboxStateConfigurationError,
        )

    async def _start_once(self, *, warm_pool_size: int) -> None:
        """Initialize the database and worker renewal task under the startup lock."""
        async with self._engine.connect() as connection:
            self._capabilities = await connection.run_sync(
                _capabilities_from_connection
            )

        async def initialize(connection: AsyncConnection) -> None:
            now = self._now()
            has_version_table = await connection.run_sync(_has_schema_version_table)
            current = (
                await connection.scalar(
                    select(_schema_versions.c.version).where(
                        _schema_versions.c.component == "opensandbox-state"
                    )
                )
                if has_version_table
                else None
            )
            if current is None:
                await connection.run_sync(_metadata.create_all)
            elif current > _SCHEMA_VERSION:
                raise OpenSandboxStateError(
                    "OpenSandbox State schema is newer than this package"
                )
            elif current == 1:
                await connection.run_sync(_migrate_v1_to_v2)
            elif current < 1:
                raise OpenSandboxStateError("Unsupported OpenSandbox State schema")

            await connection.run_sync(_validate_schema)
            if current is None:
                await connection.execute(
                    insert(_schema_versions).values(
                        component="opensandbox-state",
                        version=_SCHEMA_VERSION,
                    )
                )
            elif current < _SCHEMA_VERSION:
                await connection.execute(
                    update(_schema_versions)
                    .where(_schema_versions.c.component == "opensandbox-state")
                    .values(version=_SCHEMA_VERSION)
                )

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

    async def _renew_worker_loop(self) -> None:
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

    async def acquire_owner(self, owner_key: str) -> OpenSandboxOwnerClaim:
        """Wait until this worker atomically owns the next owner generation."""
        self._ensure_open()
        digest = _owner_digest(self._namespace, owner_key)
        while True:
            token = uuid4().hex

            async def acquire(
                connection: AsyncConnection,
            ) -> OpenSandboxOwnerClaim | None:
                now = self._now()
                expires_at = now + timedelta(seconds=self._lease_ttl)
                statement = select(_owners).where(
                    _owners.c.namespace == self._namespace,
                    _owners.c.owner_digest == digest,
                )
                if self._require_capabilities().name == "mysql":
                    statement = statement.with_for_update()
                row = (await connection.execute(statement)).mappings().one_or_none()
                if row is None:
                    await connection.execute(
                        insert(_owners).values(
                            namespace=self._namespace,
                            owner_digest=digest,
                            sandbox_id=None,
                            binding_generation=0,
                            generation=1,
                            claim_token=token,
                            lease_expires_at=expires_at,
                            updated_at=now,
                        )
                    )
                    return OpenSandboxOwnerClaim(
                        owner_key=owner_key,
                        owner_digest=digest,
                        token=token,
                        generation=1,
                        binding=None,
                    )
                lease_expires_at = row["lease_expires_at"]
                if row["claim_token"] is not None and not (
                    lease_expires_at is not None and lease_expires_at <= now
                ):
                    return None
                generation = int(row["generation"]) + 1
                result = await connection.execute(
                    update(_owners)
                    .where(
                        _owners.c.namespace == self._namespace,
                        _owners.c.owner_digest == digest,
                        _owners.c.generation == row["generation"],
                    )
                    .values(
                        generation=generation,
                        claim_token=token,
                        lease_expires_at=expires_at,
                        updated_at=now,
                    )
                )
                if result.rowcount != 1:
                    return None
                sandbox_id = row["sandbox_id"]
                binding = (
                    OpenSandboxBinding(
                        sandbox_id=str(sandbox_id),
                        generation=int(row["binding_generation"]),
                    )
                    if sandbox_id is not None
                    else None
                )
                return OpenSandboxOwnerClaim(
                    owner_key=owner_key,
                    owner_digest=digest,
                    token=token,
                    generation=generation,
                    binding=binding,
                )

            try:
                claim = await self._run_write_transaction(acquire)
            except DBAPIError as exc:
                if not self._is_retryable_mysql_conflict(exc):
                    raise
            else:
                if claim is not None:
                    return claim
            await asyncio.sleep(self._poll_interval)

    async def bind_owner(
        self,
        claim: OpenSandboxOwnerClaim,
        sandbox_id: str,
    ) -> OpenSandboxBinding:
        """Commit a binding with claim token, generation, and lease fencing."""
        self._ensure_open()

        async def bind(connection: AsyncConnection) -> None:
            now = self._now()
            result = await connection.execute(
                update(_owners)
                .where(
                    _owners.c.namespace == self._namespace,
                    _owners.c.owner_digest == claim.owner_digest,
                    _owners.c.claim_token == claim.token,
                    _owners.c.generation == claim.generation,
                    _owners.c.lease_expires_at > now,
                )
                .values(
                    sandbox_id=sandbox_id,
                    binding_generation=claim.generation,
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                raise OpenSandboxStateOwnershipError(
                    f"Owner claim for {claim.owner_key!r} is no longer current"
                )

        await self._run_write_transaction(bind)
        return OpenSandboxBinding(
            sandbox_id=sandbox_id,
            generation=claim.generation,
        )

    async def renew_owner(self, claim: OpenSandboxOwnerClaim) -> bool:
        """Extend a current, unexpired owner lease."""
        self._ensure_open()

        async def renew(connection: AsyncConnection) -> bool:
            now = self._now()
            result = await connection.execute(
                update(_owners)
                .where(
                    _owners.c.namespace == self._namespace,
                    _owners.c.owner_digest == claim.owner_digest,
                    _owners.c.claim_token == claim.token,
                    _owners.c.generation == claim.generation,
                    _owners.c.lease_expires_at > now,
                )
                .values(
                    lease_expires_at=now + timedelta(seconds=self._lease_ttl),
                    updated_at=now,
                )
            )
            return result.rowcount == 1

        return await self._run_write_transaction(renew)

    async def unbind_owner(self, claim: OpenSandboxOwnerClaim) -> None:
        """Remove a binding only while the owner claim remains current."""
        self._ensure_open()

        async def unbind(connection: AsyncConnection) -> None:
            now = self._now()
            result = await connection.execute(
                update(_owners)
                .where(
                    _owners.c.namespace == self._namespace,
                    _owners.c.owner_digest == claim.owner_digest,
                    _owners.c.claim_token == claim.token,
                    _owners.c.generation == claim.generation,
                    _owners.c.lease_expires_at > now,
                )
                .values(
                    sandbox_id=None,
                    binding_generation=claim.generation,
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                raise OpenSandboxStateOwnershipError(
                    f"Owner claim for {claim.owner_key!r} is no longer current"
                )

        await self._run_write_transaction(unbind)

    async def read_binding(self, owner_key: str) -> OpenSandboxBinding | None:
        """Read the latest committed owner binding without claiming it."""
        self._ensure_open()
        digest = _owner_digest(self._namespace, owner_key)
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    select(
                        _owners.c.sandbox_id,
                        _owners.c.binding_generation,
                    ).where(
                        _owners.c.namespace == self._namespace,
                        _owners.c.owner_digest == digest,
                    )
                )
            ).one_or_none()
        if row is None or row.sandbox_id is None:
            return None
        return OpenSandboxBinding(
            sandbox_id=str(row.sandbox_id),
            generation=int(row.binding_generation),
        )

    async def release_owner(self, claim: OpenSandboxOwnerClaim) -> None:
        """Release only the exact owner claim supplied by the caller."""
        self._ensure_open()

        async def release(connection: AsyncConnection) -> None:
            await connection.execute(
                update(_owners)
                .where(
                    _owners.c.namespace == self._namespace,
                    _owners.c.owner_digest == claim.owner_digest,
                    _owners.c.claim_token == claim.token,
                    _owners.c.generation == claim.generation,
                )
                .values(
                    claim_token=None,
                    lease_expires_at=None,
                    updated_at=self._now(),
                )
            )

        await self._run_write_transaction(release)

    async def claim_warm_slot(self) -> OpenSandboxWarmClaim | None:
        """Claim one empty or abandoned global warm-pool slot."""
        self._ensure_open()

        async def claim(connection: AsyncConnection) -> OpenSandboxWarmClaim | None:
            now = self._now()
            statement = (
                select(_warm_slots)
                .where(
                    _warm_slots.c.namespace == self._namespace,
                    _warm_slots.c.sandbox_id.is_(None),
                    (_warm_slots.c.claim_token.is_(None))
                    | (_warm_slots.c.lease_expires_at <= now),
                )
                .order_by(_warm_slots.c.slot)
                .limit(1)
            )
            capabilities = self._require_capabilities()
            statement = _apply_claim_lock(
                statement,
                capabilities=capabilities,
            )
            row = (await connection.execute(statement)).mappings().one_or_none()
            if row is None:
                return None
            token = uuid4().hex
            generation = int(row["generation"]) + 1
            result = await connection.execute(
                update(_warm_slots)
                .where(
                    _warm_slots.c.namespace == self._namespace,
                    _warm_slots.c.slot == row["slot"],
                    _warm_slots.c.generation == row["generation"],
                )
                .values(
                    generation=generation,
                    claim_token=token,
                    lease_expires_at=now + timedelta(seconds=self._lease_ttl),
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                return None
            return OpenSandboxWarmClaim(
                slot=int(row["slot"]),
                token=token,
                generation=generation,
            )

        return await self._run_claim_transaction(claim)

    async def publish_warm(
        self,
        claim: OpenSandboxWarmClaim,
        sandbox_id: str,
    ) -> None:
        """Publish a remote Sandbox only through the current warm claim."""
        self._ensure_open()

        async def publish(connection: AsyncConnection) -> None:
            now = self._now()
            result = await connection.execute(
                update(_warm_slots)
                .where(
                    _warm_slots.c.namespace == self._namespace,
                    _warm_slots.c.slot == claim.slot,
                    _warm_slots.c.claim_token == claim.token,
                    _warm_slots.c.generation == claim.generation,
                    _warm_slots.c.lease_expires_at > now,
                )
                .values(
                    sandbox_id=sandbox_id,
                    claim_token=None,
                    lease_expires_at=None,
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                raise OpenSandboxStateOwnershipError(
                    f"Warm slot {claim.slot} is no longer current"
                )

        await self._run_write_transaction(publish)

    async def renew_warm(self, claim: OpenSandboxWarmClaim) -> bool:
        """Extend a current, unexpired warm-slot lease."""
        self._ensure_open()

        async def renew(connection: AsyncConnection) -> bool:
            now = self._now()
            result = await connection.execute(
                update(_warm_slots)
                .where(
                    _warm_slots.c.namespace == self._namespace,
                    _warm_slots.c.slot == claim.slot,
                    _warm_slots.c.claim_token == claim.token,
                    _warm_slots.c.generation == claim.generation,
                    _warm_slots.c.lease_expires_at > now,
                )
                .values(
                    lease_expires_at=now + timedelta(seconds=self._lease_ttl),
                    updated_at=now,
                )
            )
            return result.rowcount == 1

        return await self._run_write_transaction(renew)

    async def release_warm(self, claim: OpenSandboxWarmClaim) -> None:
        """Release only the exact uncommitted warm claim."""
        self._ensure_open()

        async def release(connection: AsyncConnection) -> None:
            await connection.execute(
                update(_warm_slots)
                .where(
                    _warm_slots.c.namespace == self._namespace,
                    _warm_slots.c.slot == claim.slot,
                    _warm_slots.c.claim_token == claim.token,
                    _warm_slots.c.generation == claim.generation,
                )
                .values(
                    claim_token=None,
                    lease_expires_at=None,
                    updated_at=self._now(),
                )
            )

        await self._run_write_transaction(release)

    async def consume_warm(
        self,
        claim: OpenSandboxOwnerClaim,
    ) -> OpenSandboxBinding | None:
        """Atomically consume and authoritatively bind one ready global slot."""
        self._ensure_open()

        async def consume(
            connection: AsyncConnection,
        ) -> OpenSandboxBinding | None:
            now = self._now()
            owner_statement = select(_owners.c.owner_digest).where(
                _owners.c.namespace == self._namespace,
                _owners.c.owner_digest == claim.owner_digest,
                _owners.c.claim_token == claim.token,
                _owners.c.generation == claim.generation,
                _owners.c.lease_expires_at > now,
            )
            capabilities = self._require_capabilities()
            if capabilities.name == "mysql":
                owner_statement = owner_statement.with_for_update()
            if (await connection.execute(owner_statement)).one_or_none() is None:
                raise OpenSandboxStateOwnershipError(
                    f"Owner claim for {claim.owner_key!r} is no longer current"
                )
            slot_statement = (
                select(_warm_slots.c.slot, _warm_slots.c.sandbox_id)
                .where(
                    _warm_slots.c.namespace == self._namespace,
                    _warm_slots.c.sandbox_id.is_not(None),
                    _warm_slots.c.claim_token.is_(None),
                )
                .order_by(_warm_slots.c.slot)
                .limit(1)
            )
            slot_statement = _apply_claim_lock(
                slot_statement,
                capabilities=capabilities,
            )
            slot = (await connection.execute(slot_statement)).one_or_none()
            if slot is None:
                return None
            sandbox_id = str(slot.sandbox_id)
            result = await connection.execute(
                update(_warm_slots)
                .where(
                    _warm_slots.c.namespace == self._namespace,
                    _warm_slots.c.slot == slot.slot,
                    _warm_slots.c.sandbox_id == sandbox_id,
                )
                .values(sandbox_id=None, updated_at=now)
            )
            if result.rowcount != 1:
                raise OpenSandboxStateOwnershipError(
                    f"Warm slot {slot.slot} is no longer available"
                )
            result = await connection.execute(
                update(_owners)
                .where(
                    _owners.c.namespace == self._namespace,
                    _owners.c.owner_digest == claim.owner_digest,
                    _owners.c.claim_token == claim.token,
                    _owners.c.generation == claim.generation,
                    _owners.c.lease_expires_at > now,
                )
                .values(
                    sandbox_id=sandbox_id,
                    binding_generation=claim.generation,
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                raise OpenSandboxStateOwnershipError(
                    f"Owner claim for {claim.owner_key!r} is no longer current"
                )
            return OpenSandboxBinding(
                sandbox_id=sandbox_id,
                generation=claim.generation,
            )

        return await self._run_claim_transaction(consume)

    async def _enqueue_cleanup_in_transaction(
        self,
        connection: AsyncConnection,
        sandbox_id: str,
        *,
        now: datetime,
    ) -> None:
        existing = await connection.scalar(
            select(_cleanup.c.sandbox_id).where(
                _cleanup.c.namespace == self._namespace,
                _cleanup.c.sandbox_id == sandbox_id,
            )
        )
        if existing is not None:
            return
        await connection.execute(
            insert(_cleanup).values(
                namespace=self._namespace,
                sandbox_id=sandbox_id,
                generation=0,
                claim_token=None,
                lease_expires_at=None,
                attempts=0,
                created_at=now,
                updated_at=now,
            )
        )

    async def enqueue_cleanup(self, sandbox_id: str) -> None:
        """Persist an idempotent remote-destruction retry target."""
        self._ensure_open()

        async def enqueue(connection: AsyncConnection) -> None:
            await self._enqueue_cleanup_in_transaction(
                connection,
                sandbox_id,
                now=self._now(),
            )

        while True:
            try:
                await self._run_write_transaction(enqueue)
                return
            except DBAPIError as exc:
                if not self._is_retryable_mysql_conflict(exc):
                    raise
                await asyncio.sleep(self._poll_interval)

    async def claim_cleanup(self) -> OpenSandboxCleanupClaim | None:
        """Claim one pending or abandoned cleanup target."""
        self._ensure_open()

        async def claim(
            connection: AsyncConnection,
        ) -> OpenSandboxCleanupClaim | None:
            now = self._now()
            statement = (
                select(_cleanup)
                .where(
                    _cleanup.c.namespace == self._namespace,
                    (_cleanup.c.claim_token.is_(None))
                    | (_cleanup.c.lease_expires_at <= now),
                )
                .order_by(_cleanup.c.created_at, _cleanup.c.sandbox_id)
                .limit(1)
            )
            capabilities = self._require_capabilities()
            statement = _apply_claim_lock(
                statement,
                capabilities=capabilities,
            )
            row = (await connection.execute(statement)).mappings().one_or_none()
            if row is None:
                return None
            generation = int(row["generation"]) + 1
            token = uuid4().hex
            result = await connection.execute(
                update(_cleanup)
                .where(
                    _cleanup.c.namespace == self._namespace,
                    _cleanup.c.sandbox_id == row["sandbox_id"],
                    _cleanup.c.generation == row["generation"],
                )
                .values(
                    generation=generation,
                    claim_token=token,
                    lease_expires_at=now + timedelta(seconds=self._lease_ttl),
                    attempts=int(row["attempts"]) + 1,
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                return None
            return OpenSandboxCleanupClaim(
                sandbox_id=str(row["sandbox_id"]),
                token=token,
                generation=generation,
            )

        return await self._run_claim_transaction(claim)

    async def renew_cleanup(self, claim: OpenSandboxCleanupClaim) -> bool:
        """Extend a current, unexpired cleanup lease."""
        self._ensure_open()

        async def renew(connection: AsyncConnection) -> bool:
            now = self._now()
            result = await connection.execute(
                update(_cleanup)
                .where(
                    _cleanup.c.namespace == self._namespace,
                    _cleanup.c.sandbox_id == claim.sandbox_id,
                    _cleanup.c.claim_token == claim.token,
                    _cleanup.c.generation == claim.generation,
                    _cleanup.c.lease_expires_at > now,
                )
                .values(
                    lease_expires_at=now + timedelta(seconds=self._lease_ttl),
                    updated_at=now,
                )
            )
            return result.rowcount == 1

        return await self._run_write_transaction(renew)

    async def complete_cleanup(self, claim: OpenSandboxCleanupClaim) -> None:
        """Delete a cleanup row only after its claimant confirms destruction."""
        self._ensure_open()

        async def complete(connection: AsyncConnection) -> None:
            result = await connection.execute(
                delete(_cleanup).where(
                    _cleanup.c.namespace == self._namespace,
                    _cleanup.c.sandbox_id == claim.sandbox_id,
                    _cleanup.c.claim_token == claim.token,
                    _cleanup.c.generation == claim.generation,
                )
            )
            if result.rowcount != 1:
                raise OpenSandboxStateOwnershipError(
                    f"Cleanup claim for {claim.sandbox_id!r} is no longer current"
                )

        await self._run_write_transaction(complete)

    async def release_cleanup(self, claim: OpenSandboxCleanupClaim) -> None:
        """Release one failed cleanup claim for a future retry."""
        self._ensure_open()

        async def release(connection: AsyncConnection) -> None:
            await connection.execute(
                update(_cleanup)
                .where(
                    _cleanup.c.namespace == self._namespace,
                    _cleanup.c.sandbox_id == claim.sandbox_id,
                    _cleanup.c.claim_token == claim.token,
                    _cleanup.c.generation == claim.generation,
                )
                .values(
                    claim_token=None,
                    lease_expires_at=None,
                    updated_at=self._now(),
                )
            )

        await self._run_write_transaction(release)

    async def shutdown_sandbox_ids(self) -> tuple[str, ...]:
        """Keep durable bindings, warm slots, and cleanup work on shutdown."""
        self._ensure_open()
        return ()

    async def aclose(self) -> None:
        """Settle startup and close owned database resources idempotently.

        All callers await one shielded close task. Closing waits any owned startup
        attempt, unregisters the worker when its transaction may have committed, and
        then disposes the asynchronous engine.
        """
        async with self._start_lock:
            close_task = self._close_task
            if close_task is None:
                self._closed = True
                close_task = asyncio.create_task(
                    self._aclose_once(),
                    name=f"tinkerfin-opensandbox-close:{self._worker_id}",
                )
                self._close_task = close_task
        await asyncio.shield(close_task)

    async def _aclose_once(self) -> None:
        """Settle startup, unregister the worker, and dispose the engine."""
        start_task = self._start_task
        if start_task is not None:
            await asyncio.gather(start_task, return_exceptions=True)
        renew_task = self._worker_renew_task
        if renew_task is not None:
            renew_task.cancel()
            await asyncio.gather(renew_task, return_exceptions=True)
            self._worker_renew_task = None
        if start_task is not None:

            async def unregister(connection: AsyncConnection) -> None:
                await connection.execute(
                    delete(_workers).where(
                        _workers.c.namespace == self._namespace,
                        _workers.c.worker_id == self._worker_id,
                    )
                )

            try:
                await self._run_write_transaction(unregister)
            except Exception:  # noqa: BLE001
                # Lease expiry removes a crashed or unreachable worker registration
                pass
        await self._engine.dispose()
