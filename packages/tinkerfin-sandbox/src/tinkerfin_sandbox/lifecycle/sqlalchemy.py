"""Optional SQLAlchemy Core implementation of OpenSandbox allocation state."""

from __future__ import annotations

import asyncio
import hashlib
import math
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import wraps
from typing import Literal, TypeVar, cast
from uuid import uuid4

from sqlalchemy import (
    delete,
    text,
)
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    create_async_engine,
)

from ..errors import (
    OpenSandboxStateError,
    OpenSandboxStateTimeoutError,
    OpenSandboxStateUnavailableError,
    UnexpectedOpenSandboxStateError,
)
from . import _sql_availability, _sql_schema, _sql_state_ops, _sql_transactions
from ._sql_schema import _cleanup as _cleanup
from ._sql_schema import _warm_slots as _warm_slots
from ._sql_schema import _workers
from ._sql_transactions import (
    _MYSQL_STARTUP_LOCK_TIMEOUT_SECONDS,
    _SQLDialectCapabilities,
    _WriteConnectionDisposition,
)
from ._sql_transactions import _apply_claim_lock as _apply_claim_lock
from ._sql_transactions import (
    _resolve_dialect_capabilities as _resolve_dialect_capabilities,
)
from .availability import (
    OpenSandboxAvailability,
    OpenSandboxAvailabilityPhase,
    OpenSandboxHolderUpdate,
)
from .state import (
    OpenSandboxBinding,
    OpenSandboxCleanupClaim,
    OpenSandboxOwnerClaim,
    OpenSandboxReadyWarmClaim,
    OpenSandboxState,
    OpenSandboxWarmClaim,
)

_ResultT = TypeVar("_ResultT")
_AsyncFunctionT = TypeVar(
    "_AsyncFunctionT",
    bound=Callable[..., Awaitable[object]],
)


def _state_operation(
    operation: str,
) -> Callable[[_AsyncFunctionT], _AsyncFunctionT]:
    """Translate SQLAlchemy and driver failures at each public State boundary."""

    def decorate(
        function: _AsyncFunctionT,
    ) -> _AsyncFunctionT:
        @wraps(function)
        async def wrapped(*args: object, **kwargs: object) -> object:
            state = cast(SQLAlchemyOpenSandboxState, args[0])
            diagnostic_context = {
                "implementation": "sqlalchemy",
                "dialect": state._dialect,
                "operation": operation,
            }
            try:
                return await function(*args, **kwargs)
            except OpenSandboxStateError as error:
                error._enrich_diagnostic_context(diagnostic_context)
                raise
            except SQLAlchemyTimeoutError as error:
                translated = OpenSandboxStateTimeoutError(
                    f"OpenSandbox State {operation} timed out",
                    diagnostic_context=diagnostic_context,
                    cause=error,
                )
                raise translated from error
            except DBAPIError as error:
                error_type = (
                    OpenSandboxStateUnavailableError
                    if error.connection_invalidated
                    else UnexpectedOpenSandboxStateError
                )
                translated = error_type(
                    f"OpenSandbox State {operation} failed",
                    diagnostic_context=diagnostic_context,
                    cause=error,
                )
                raise translated from error
            except (SQLAlchemyError, OSError) as error:
                translated = OpenSandboxStateUnavailableError(
                    f"OpenSandbox State {operation} is unavailable",
                    diagnostic_context=diagnostic_context,
                    cause=error,
                )
                raise translated from error
            except (TypeError, ValueError):
                raise
            except Exception as error:
                translated = UnexpectedOpenSandboxStateError(
                    f"OpenSandbox State {operation} failed",
                    diagnostic_context=diagnostic_context,
                    cause=error,
                )
                raise translated from error

        return cast(_AsyncFunctionT, wrapped)

    return decorate


@dataclass(frozen=True, slots=True)
class SQLAlchemyOpenSandboxStateSchema:
    """Describe one complete deployable OpenSandbox State database schema.

    Attributes:
        dialect: SQL dialect accepted by the deployment script.
        table_names: Complete table set in deterministic creation order.
        ddl: Full empty-database DDL including indexes.
    """

    dialect: Literal["mysql", "sqlite"]
    table_names: tuple[str, ...]
    ddl: str


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

    return _sql_schema.get_sqlalchemy_opensandbox_state_schema(
        dialect=dialect,
    )


class SQLAlchemyOpenSandboxState(OpenSandboxState):
    """Persist OpenSandbox allocation state through SQLAlchemy Core.

    The State accepts either an owned URL or a borrowed ``AsyncEngine``. Borrowed mode
    never creates, reconfigures, or disposes the host Engine; both modes still own
    worker registration and background renewal tasks.

    Args:
        url: Owned SQLite ``sqlite+aiosqlite`` or MySQL ``mysql+asyncmy`` URL.
        engine: Borrowed asynchronous SQLite or MySQL Engine.
        namespace: Logical deployment namespace stored with every state row.
        lease_ttl: Seconds before an abandoned State claim or Worker can be fenced out.
        poll_interval: Seconds between attempts while another worker owns a claim.
        sqlite_retry_timeout: Maximum seconds spent retrying rolled-back SQLite write
            lock conflicts. The first attempt is always made.

    Raises:
        TypeError: Engine or timing values have the wrong public type.
        ValueError: Engine ownership selection, namespace, timing, or dialect is invalid.
    """

    def __init__(
        self,
        *,
        url: str | None = None,
        engine: AsyncEngine | None = None,
        namespace: str = "",
        lease_ttl: float = 15.0,
        poll_interval: float = 0.05,
        sqlite_retry_timeout: float = 5.0,
    ) -> None:
        """Initialize owned or borrowed SQLAlchemy persistence without database I/O.

        Exactly one of ``url`` and ``engine`` is required. Timing and ownership values
        are validated before any connection, task, or schema mutation occurs.

        Args:
            url: URL used to create an Engine owned by this State.
            engine: Existing asynchronous Engine borrowed from the host.
            namespace: Logical deployment namespace stored with State records.
            lease_ttl: Claim and Worker lease duration in seconds.
            poll_interval: Delay between contested claim attempts in seconds.
            sqlite_retry_timeout: Bounded SQLite lock retry duration in seconds.

        Raises:
            TypeError: Engine or timing values have the wrong public type.
            ValueError: Ownership selection, namespace, timing, or dialect is invalid.
        """

        if (url is None) == (engine is None):
            raise ValueError("exactly one of url or engine must be provided")
        if engine is not None and not isinstance(engine, AsyncEngine):
            raise TypeError("engine must be an AsyncEngine or None")

        if len(namespace) > 64:
            raise ValueError("namespace must contain at most 64 characters")
        if isinstance(lease_ttl, bool) or not isinstance(lease_ttl, int | float):
            raise TypeError("lease_ttl must be a number")
        if isinstance(poll_interval, bool) or not isinstance(
            poll_interval, int | float
        ):
            raise TypeError("poll_interval must be a number")
        resolved_lease_ttl = float(lease_ttl)
        resolved_poll_interval = float(poll_interval)
        if not math.isfinite(resolved_lease_ttl) or resolved_lease_ttl <= 0:
            raise ValueError("lease_ttl must be a finite positive number")
        if not math.isfinite(resolved_poll_interval) or resolved_poll_interval <= 0:
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
        self._lease_ttl = resolved_lease_ttl
        self._poll_interval = resolved_poll_interval
        self._sqlite_retry_timeout = resolved_sqlite_retry_timeout
        self._worker_id = uuid4().hex
        self._worker_lease_ttl = resolved_lease_ttl
        self._worker_renew_task: asyncio.Task[None] | None = None
        self._worker_failure: Exception | None = None
        self._owns_engine = url is not None
        self._engine = (
            create_async_engine(url) if url is not None else cast(AsyncEngine, engine)
        )
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

        return _sql_transactions._is_retryable_mysql_conflict(
            self,
            error,
        )

    def _is_retryable_sqlite_lock(self, error: DBAPIError) -> bool:
        """Recognize SQLite lock result codes without matching driver text."""

        return _sql_transactions._is_retryable_sqlite_lock(
            self,
            error,
        )

    async def _begin_write_transaction(self, connection: AsyncConnection) -> None:
        """Open one dialect-specific write transaction without retrying it."""

        return await _sql_transactions._begin_write_transaction(
            self,
            connection,
        )

    async def _commit_write_transaction(
        self,
        connection: AsyncConnection,
        disposition: _WriteConnectionDisposition,
    ) -> None:
        """Settle COMMIT and retry only SQLite's known uncommitted BUSY result."""

        return await _sql_transactions._commit_write_transaction(
            self,
            connection,
            disposition,
        )

    async def _write_transaction_once(
        self,
        operation: Callable[[AsyncConnection], Awaitable[_ResultT]],
    ) -> _ResultT:
        """Run one write attempt and expose only safely retryable SQLite locks."""

        return await _sql_transactions._write_transaction_once(
            self,
            operation,
        )

    async def _run_write_transaction(
        self,
        operation: Callable[[AsyncConnection], Awaitable[_ResultT]],
    ) -> _ResultT:
        """Retry only rolled-back SQLite lock conflicts within one deadline."""

        return await _sql_transactions._run_write_transaction(
            self,
            operation,
        )

    async def _run_claim_transaction(
        self,
        operation: Callable[[AsyncConnection], Awaitable[_ResultT]],
    ) -> _ResultT | None:
        """Run one claim operation and preserve MySQL 5.7 timeout semantics."""

        return await _sql_transactions._run_claim_transaction(
            self,
            operation,
        )

    @asynccontextmanager
    async def _startup_lock(self) -> AsyncGenerator[None]:
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

    @_state_operation("start")
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

        return await _sql_transactions.start(
            self,
            warm_pool_size=warm_pool_size,
        )

    @staticmethod
    def _is_retryable_start_failure(start_task: asyncio.Task[None]) -> bool:
        """Return whether a settled startup failure is known to precede registration."""

        return _sql_transactions._is_retryable_start_failure(
            start_task,
        )

    async def _start_once(self, *, warm_pool_size: int) -> None:
        """Initialize the database and worker renewal task under the startup lock."""

        return await _sql_transactions._start_once(
            self,
            warm_pool_size=warm_pool_size,
        )

    async def _renew_worker_loop(self) -> None:
        """Keep this worker active without exposing another coordinator."""

        return await _sql_transactions._renew_worker_loop(
            self,
        )

    @_state_operation("acquire_owner")
    async def acquire_owner(self, owner_key: str) -> OpenSandboxOwnerClaim:
        """Wait until this worker atomically owns the next owner generation."""

        return await _sql_state_ops.acquire_owner(
            self,
            owner_key,
        )

    @_state_operation("bind_owner")
    async def bind_owner(
        self,
        claim: OpenSandboxOwnerClaim,
        sandbox_id: str,
    ) -> OpenSandboxBinding:
        """Commit a binding with claim token, generation, and lease fencing."""

        return await _sql_state_ops.bind_owner(
            self,
            claim,
            sandbox_id,
        )

    @_state_operation("renew_owner")
    async def renew_owner(self, claim: OpenSandboxOwnerClaim) -> bool:
        """Extend a current, unexpired owner lease."""

        return await _sql_state_ops.renew_owner(
            self,
            claim,
        )

    @_state_operation("unbind_owner")
    async def unbind_owner(self, claim: OpenSandboxOwnerClaim) -> None:
        """Remove a binding only while the owner claim remains current."""

        return await _sql_state_ops.unbind_owner(
            self,
            claim,
        )

    @_state_operation("read_binding")
    async def read_binding(self, owner_key: str) -> OpenSandboxBinding | None:
        """Read the latest committed owner binding without claiming it."""

        return await _sql_state_ops.read_binding(
            self,
            owner_key,
        )

    @_state_operation("release_owner")
    async def release_owner(self, claim: OpenSandboxOwnerClaim) -> None:
        """Release only the exact owner claim supplied by the caller."""

        return await _sql_state_ops.release_owner(
            self,
            claim,
        )

    @_state_operation("register_holder")
    async def register_holder(
        self, claim: OpenSandboxOwnerClaim, holder_id: str
    ) -> OpenSandboxAvailability:
        """Register before publishing a handle under the current running intent."""
        return await _sql_availability.register_holder(self, claim, holder_id)

    @_state_operation("read_availability")
    async def read_availability(self, owner_key: str) -> OpenSandboxAvailability | None:
        """Read current intent without waiting for an active owner claim."""
        return await _sql_availability.read_availability(self, owner_key)

    @_state_operation("get_holder_updates")
    async def get_holder_updates(
        self, holder_id: str
    ) -> tuple[OpenSandboxHolderUpdate, ...]:
        """Read all registrations and availability intents for one manager."""
        return await _sql_availability.get_holder_updates(self, holder_id)

    @_state_operation("change_availability")
    async def change_availability(
        self,
        claim: OpenSandboxOwnerClaim,
        expected: OpenSandboxAvailability,
        *,
        phase: OpenSandboxAvailabilityPhase,
        refresh_connection: bool = False,
    ) -> OpenSandboxAvailability:
        """Advance the exact expected intent under the current owner fence."""
        return await _sql_availability.change_availability(
            self, claim, expected, phase=phase, refresh_connection=refresh_connection
        )

    @_state_operation("acknowledge_idle")
    async def acknowledge_idle(
        self, holder_id: str, availability: OpenSandboxAvailability
    ) -> bool:
        """Acknowledge a current drain after admission closes and operations settle."""
        return await _sql_availability.acknowledge_idle(self, holder_id, availability)

    @_state_operation("holders_are_idle")
    async def holders_are_idle(
        self, claim: OpenSandboxOwnerClaim, availability: OpenSandboxAvailability
    ) -> bool:
        """Require explicit current-drain acknowledgements from every holder."""
        return await _sql_availability.holders_are_idle(self, claim, availability)

    @_state_operation("unregister_holder")
    async def unregister_holder(
        self, holder_id: str, availability: OpenSandboxAvailability
    ) -> None:
        """Release an exact binding registration after proving local idle."""
        return await _sql_availability.unregister_holder(self, holder_id, availability)

    @_state_operation("claim_warm_slot")
    async def claim_warm_slot(self) -> OpenSandboxWarmClaim | None:
        """Claim one empty or abandoned global warm-pool slot."""

        return await _sql_state_ops.claim_warm_slot(
            self,
        )

    @_state_operation("claim_ready_warm_slot")
    async def claim_ready_warm_slot(
        self,
        *,
        exclude_slots: Sequence[int],
    ) -> OpenSandboxReadyWarmClaim | None:
        """Fence one published slot for remote health and expiry reconciliation."""

        return await _sql_state_ops.claim_ready_warm_slot(
            self,
            exclude_slots=exclude_slots,
        )

    @_state_operation("discard_ready_warm_slot")
    async def discard_ready_warm_slot(
        self,
        claim: OpenSandboxReadyWarmClaim,
    ) -> None:
        """Clear one unusable published slot and retain durable cleanup."""

        await _sql_state_ops.discard_ready_warm_slot(self, claim)

    @_state_operation("warm_pool_ready")
    async def warm_pool_ready(self) -> bool:
        """Return whether every configured slot retains a published Sandbox."""

        return await _sql_state_ops.warm_pool_ready(self)

    @_state_operation("publish_warm")
    async def publish_warm(
        self,
        claim: OpenSandboxWarmClaim,
        sandbox_id: str,
    ) -> None:
        """Publish a remote Sandbox only through the current warm claim."""

        return await _sql_state_ops.publish_warm(
            self,
            claim,
            sandbox_id,
        )

    @_state_operation("renew_warm")
    async def renew_warm(self, claim: OpenSandboxWarmClaim) -> bool:
        """Extend a current, unexpired warm-slot lease."""

        return await _sql_state_ops.renew_warm(
            self,
            claim,
        )

    @_state_operation("release_warm")
    async def release_warm(self, claim: OpenSandboxWarmClaim) -> None:
        """Release only the exact uncommitted warm claim."""

        return await _sql_state_ops.release_warm(
            self,
            claim,
        )

    @_state_operation("consume_warm")
    async def consume_warm(
        self,
        claim: OpenSandboxOwnerClaim,
    ) -> OpenSandboxBinding | None:
        """Atomically consume and authoritatively bind one ready global slot."""

        return await _sql_state_ops.consume_warm(
            self,
            claim,
        )

    async def _enqueue_cleanup_in_transaction(
        self,
        connection: AsyncConnection,
        sandbox_id: str,
        *,
        now: datetime,
    ) -> None:
        return await _sql_state_ops._enqueue_cleanup_in_transaction(
            self,
            connection,
            sandbox_id,
            now=now,
        )

    @_state_operation("enqueue_cleanup")
    async def enqueue_cleanup(self, sandbox_id: str) -> None:
        """Persist an idempotent remote-destruction retry target."""

        return await _sql_state_ops.enqueue_cleanup(
            self,
            sandbox_id,
        )

    @_state_operation("claim_cleanup")
    async def claim_cleanup(self) -> OpenSandboxCleanupClaim | None:
        """Claim one pending or abandoned cleanup target."""

        return await _sql_state_ops.claim_cleanup(
            self,
        )

    @_state_operation("renew_cleanup")
    async def renew_cleanup(self, claim: OpenSandboxCleanupClaim) -> bool:
        """Extend a current, unexpired cleanup lease."""

        return await _sql_state_ops.renew_cleanup(
            self,
            claim,
        )

    @_state_operation("complete_cleanup")
    async def complete_cleanup(self, claim: OpenSandboxCleanupClaim) -> None:
        """Delete a cleanup row only after its claimant confirms destruction."""

        return await _sql_state_ops.complete_cleanup(
            self,
            claim,
        )

    @_state_operation("release_cleanup")
    async def release_cleanup(self, claim: OpenSandboxCleanupClaim) -> None:
        """Release one failed cleanup claim for a future retry."""

        return await _sql_state_ops.release_cleanup(
            self,
            claim,
        )

    @_state_operation("shutdown_sandbox_ids")
    async def shutdown_sandbox_ids(self) -> tuple[str, ...]:
        """Keep durable bindings, warm slots, and cleanup work on shutdown."""

        return await _sql_state_ops.shutdown_sandbox_ids(
            self,
        )

    @_state_operation("close")
    async def aclose(self) -> None:
        """Settle startup and release State-owned resources idempotently.

        All callers await one shielded close task. Closing waits any owned startup
        attempt, unregisters the worker when its transaction may have committed, and
        then disposes only an Engine created from ``url``. A borrowed Engine remains
        pooled and usable by its host after State closure.
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
        """Settle startup, unregister the worker, and dispose only an owned engine."""
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
        if self._owns_engine:
            await self._engine.dispose()
