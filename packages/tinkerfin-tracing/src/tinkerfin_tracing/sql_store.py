"""Borrowed-AsyncEngine SQLite and MySQL implementation of the Trace Store contract."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from functools import wraps
from types import CoroutineType
from typing import Any, Concatenate, Literal, ParamSpec, TypeVar, cast

from sqlalchemy import (
    and_,
    delete,
    desc,
    func,
    insert,
    inspect,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.pool import PoolProxiedConnection
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.schema import Column

from .backend import (
    StoredTraceCheckpoint,
    StoredTraceEntry,
    StoredTraceEntryPage,
    StoredTraceEvent,
    StoredTraceEventPage,
    TraceCheckpointRequest,
    TraceEntryMutation,
    TraceEntryQueryRequest,
    TraceEntryRebuildRequest,
    TraceEventPageRequest,
    TraceLedgerChange,
    TraceLedgerCommitResult,
    TraceLedgerState,
    TraceLedgerStateRequest,
    TraceLedgerStorageEffect,
    TraceLedgerThreadState,
    TraceLedgerWriterState,
    TraceStoreOptions,
    resolve_ledger_change,
)
from .codec import CanonicalTracePayloadCodec
from .durable_store import DurableTraceStore
from .entries import TraceEntryKind, TraceEntryStatus, TraceFacets, TraceFilter
from .errors import (
    TraceStoreError,
    TraceStoreProtocolError,
    TraceStoreTimeout,
    TraceThreadNotFound,
)
from .facts import TraceEvent
from .limits import TraceLimits
from .sql_schema import (
    TRACE_TABLE_NAMES,
    entries,
    events,
    metadata,
    namespaces,
    projection_checkpoints,
    threads,
    writers,
)
from .store import (
    StoreWriterSnapshot,
    TraceThreadKey,
    _checkpoint_lookup,
    _event,
)

_ResultT = TypeVar("_ResultT")
_BackendT = TypeVar("_BackendT")
_OperationP = ParamSpec("_OperationP")


async def _join_owned_task(
    task: asyncio.Future[_ResultT],
    *,
    primary_error: BaseException | None,
    failure_label: str,
    suppress_task_cancellation: bool = False,
) -> _ResultT | None:
    """Settle one owned task across repeated cancellation of its caller."""

    current = asyncio.current_task()
    cancel_count = current.cancelling() if current is not None else 0
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            next_count = current.cancelling() if current is not None else 0
            if next_count > cancel_count or (cancellation is None and next_count > 0):
                cancellation = cancellation or error
                cancel_count = next_count
                continue
            if task.done():
                break
            raise
        except BaseException:  # noqa: BLE001 - inspect the retained outcome below
            break

    task_error: BaseException | None = None
    result: _ResultT | None = None
    try:
        result = task.result()
    except asyncio.CancelledError as error:
        if not suppress_task_cancellation:
            task_error = error
    except BaseException as error:  # noqa: BLE001 - preserve exact owned outcome
        task_error = error

    if primary_error is not None:
        if task_error is not None:
            primary_error.add_note(
                f"{failure_label} also failed: "
                f"{type(task_error).__module__}.{type(task_error).__qualname__}"
            )
        return result
    if cancellation is not None:
        if task_error is not None:
            cancellation.add_note(
                f"{failure_label} also failed: "
                f"{type(task_error).__module__}.{type(task_error).__qualname__}"
            )
        raise cancellation.with_traceback(cancellation.__traceback__)
    if task_error is not None:
        raise task_error.with_traceback(task_error.__traceback__)
    return result


async def _run_database_operation(
    operation: Awaitable[_ResultT],
    *,
    task_name: str,
) -> _ResultT:
    """Run database I/O in one task that receives at most one cancellation request."""

    task = asyncio.ensure_future(operation)
    if isinstance(task, asyncio.Task):
        task.set_name(task_name)
    current = asyncio.current_task()
    cancel_count = current.cancelling() if current is not None else 0
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as error:
        next_count = current.cancelling() if current is not None else 0
        if next_count <= cancel_count:
            return task.result()
        if not task.done():
            task.cancel()
        await _join_owned_task(
            task,
            primary_error=error,
            failure_label="Database operation",
            suppress_task_cancellation=True,
        )
        raise


def _owned_database_operation(
    operation: Callable[
        Concatenate[_BackendT, _OperationP],
        CoroutineType[Any, Any, _ResultT],
    ],
) -> Callable[
    Concatenate[_BackendT, _OperationP],
    CoroutineType[Any, Any, _ResultT],
]:
    """Give one public backend read a cancellation-isolated task owner."""

    @wraps(operation)
    async def wrapped(
        owner: _BackendT,
        /,
        *args: _OperationP.args,
        **kwargs: _OperationP.kwargs,
    ) -> _ResultT:
        return await _run_database_operation(
            operation(owner, *args, **kwargs),
            task_name=f"tinkerfin-trace-sql-{operation.__name__.replace('_', '-')}",
        )

    return wrapped


async def _complete_connection_cleanup(
    operation: Awaitable[object],
    *,
    task_name: str,
    primary_error: BaseException | None = None,
) -> None:
    """Finish one borrowed-connection cleanup before propagating cancellation.

    SQLAlchemy 2.0.52 shields ``AsyncConnection.close()`` with a child task, but the
    cancelled caller stops awaiting that child immediately. Retaining and awaiting the
    task here prevents a pool return from outliving the Store operation that owns it.
    Repeated caller cancellation is recorded while the owned cleanup remains joined.

    Args:
        operation: Borrowed-connection cleanup that must settle before returning.
        task_name: Diagnostic name for the retained cleanup task.
        primary_error: Earlier failure that must keep precedence over cleanup failure.
    """

    task = asyncio.ensure_future(operation)
    if isinstance(task, asyncio.Task):
        task.set_name(task_name)
    await _join_owned_task(
        task,
        primary_error=primary_error,
        failure_label="Borrowed-connection cleanup",
    )


async def _discard_cancelled_connection(
    connection: AsyncConnection,
    pooled: PoolProxiedConnection | None,
    error: asyncio.CancelledError,
) -> None:
    """Discard a driver connection that may no longer match the server protocol.

    asyncmy marks an interrupted command as ``Cancelled during execution``. Returning
    that socket through the normal rollback-on-return path produces a reset failure and
    may leave the pool checkout owned until garbage collection. The public pool proxy
    can synchronously invalidate an async driver outside SQLAlchemy's cancelled
    greenlet; a following rollback only clears SQLAlchemy's local transaction state.
    """

    if pooled is None:
        await _complete_connection_cleanup(
            connection.invalidate(error),
            task_name="tinkerfin-trace-connection-invalidate",
            primary_error=error,
        )
    elif pooled.is_valid:

        async def invalidate_pooled_connection() -> None:
            pooled.invalidate(error)

        await _complete_connection_cleanup(
            invalidate_pooled_connection(),
            task_name="tinkerfin-trace-pool-connection-invalidate",
            primary_error=error,
        )
    if connection.in_transaction():
        await _complete_connection_cleanup(
            connection.rollback(),
            task_name="tinkerfin-trace-transaction-cancel",
            primary_error=error,
        )


class _SqlAlchemyTraceLedgerBackend:
    """Persist Trace Ledger generations through one borrowed asynchronous Engine.

    The Backend supports SQLite and MySQL only, owns schema preparation, and applies
    framework-resolved effects under database locks. It never changes pool sizing or
    disposes the host Engine. Contract coverage lives in
    ``tests/test_store_implementations.py``, ``tests/test_sqlite_store.py``, and
    ``tests/test_mysql_store.py``.

    Args:
        engine: Borrowed asynchronous Engine using ``sqlite+aiosqlite`` or
            ``mysql+asyncmy``.
        namespace: Stable logical isolation key shared by cooperating Store instances.
        options: Bounded SQL preparation and commit retry settings.

    Raises:
        TypeError: An argument has the wrong public type.
        ValueError: Namespace or Engine dialect is unsupported.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        namespace: str = "default",
        options: TraceStoreOptions | None = None,
    ) -> None:
        """Bind validated policy objects without performing database I/O.

        See the class docstring for arguments, ownership, and validation failures.
        """

        if not isinstance(engine, AsyncEngine):
            raise TypeError("engine must be an AsyncEngine")
        if not isinstance(namespace, str):
            raise TypeError("namespace must be a string")
        if not namespace or namespace != namespace.strip() or len(namespace) > 2048:
            raise ValueError("namespace must be canonical text of at most 2048 chars")
        if options is not None and not isinstance(options, TraceStoreOptions):
            raise TypeError("options must be TraceStoreOptions or None")
        dialect = engine.url.get_backend_name()
        if dialect not in {"sqlite", "mysql"}:
            raise ValueError("SqlAlchemyTraceStore supports only SQLite and MySQL")
        self._engine = engine
        self._namespace = namespace
        self._namespace_hash = _digest(namespace)
        self._dialect = dialect
        self._options = options or TraceStoreOptions()
        self._setup_lock = asyncio.Lock()
        self._setup_task: asyncio.Task[None] | None = None

    @property
    def namespace(self) -> str:
        """Return the immutable logical namespace."""

        return self._namespace

    @property
    def options(self) -> TraceStoreOptions:
        """Return immutable lease, retry, and follow settings."""

        return self._options

    async def setup(self) -> None:
        """Create every owned table once without disposing the borrowed Engine.

        Concurrent Store instances coordinate through the database itself. MySQL uses
        a connection-scoped advisory lock because ``MetaData.create_all(checkfirst)``
        alone has a check/create race; SQLite serializes the same sequence with
        ``BEGIN IMMEDIATE``. Caller cancellation does not cancel the retained setup
        task. Cancellation waits for that retained task to settle before propagating,
        so a host cannot dispose the borrowed Engine while setup still uses it; another
        caller can then observe its exact success or failure.
        A later call replaces a completed failed or self-cancelled setup task, allowing
        transient database failures to recover without reconstructing the Store.

        Raises:
            TraceStoreTimeout: Schema exclusion or retry budget is exhausted.
            TraceStoreError: SQLAlchemy or database setup fails.
            TraceStoreProtocolError: Existing Trace-owned objects do not match the one
                current Schema, or a digest collision is detected.
        """

        async with self._setup_lock:
            task = self._setup_task
            if (
                task is not None
                and task.done()
                and (task.cancelled() or task.exception() is not None)
            ):
                # Only the next explicit setup call retries. Callers that were already
                # waiting on the retained task still observe the same original failure.
                self._setup_task = None
                task = None
            if task is None:
                task = asyncio.create_task(
                    self._setup_once(),
                    name="tinkerfin-trace-sql-setup",
                )
                task.add_done_callback(_consume_task_exception)
                self._setup_task = task
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            await _join_owned_task(
                task,
                primary_error=error,
                failure_label="Trace Store setup",
            )
            raise

    async def _setup_once(self) -> None:
        for attempt in range(self._options.commit_retry_attempts):
            try:
                if self._dialect == "mysql":
                    await self._setup_mysql()
                else:
                    async with self._raw_write_connection(
                        sqlite_fail_fast=False
                    ) as connection:
                        await connection.run_sync(_prepare_trace_schema)
                        await self._ensure_namespace(connection)
                return
            except DBAPIError as error:
                if not _retryable(error, dialect=self._dialect):
                    raise TraceStoreError(
                        "Trace Store schema setup failed",
                        cause=error,
                    ) from error
                if attempt + 1 >= self._options.commit_retry_attempts:
                    raise TraceStoreTimeout(
                        "Trace Store schema setup retry budget was exhausted",
                        cause=error,
                    ) from error
                await asyncio.sleep(
                    self._options.commit_retry_delay_seconds * (attempt + 1)
                )
            except SQLAlchemyError as error:
                raise TraceStoreError(
                    "Trace Store schema setup failed",
                    cause=error,
                ) from error

    async def _setup_mysql(self) -> None:
        """Serialize MySQL check/create and namespace registration on one connection."""

        database = self._engine.url.database or ""
        lock_name = f"tinkerfin-trace:{_digest(database)[:40]}:schema"
        async with self._engine.connect() as connection:
            acquired = await connection.scalar(
                text("SELECT GET_LOCK(:lock_name, 30)"),
                {"lock_name": lock_name},
            )
            if acquired != 1:
                raise TraceStoreTimeout("Trace Store schema lock could not be acquired")
            try:
                await connection.run_sync(_prepare_trace_schema)
                await self._ensure_namespace(connection)
                await connection.commit()
            finally:
                try:
                    await connection.scalar(
                        text("SELECT RELEASE_LOCK(:lock_name)"),
                        {"lock_name": lock_name},
                    )
                    await connection.commit()
                except SQLAlchemyError:
                    # MySQL also releases advisory locks when this connection closes.
                    # Preserve the primary setup result rather than replacing it with
                    # a release failure from an already broken connection.
                    pass

    async def _ensure_namespace(self, connection: AsyncConnection) -> None:
        """Register and collision-check the logical namespace under setup exclusion."""

        now = await _database_now(connection)
        existing = await connection.scalar(
            select(namespaces.c.namespace).where(
                namespaces.c.namespace_hash == self._namespace_hash
            )
        )
        if existing is None:
            await connection.execute(
                insert(namespaces).values(
                    namespace_hash=self._namespace_hash,
                    namespace=self._namespace,
                    created_at=now,
                )
            )
        elif existing != self._namespace:
            raise TraceStoreProtocolError("Trace namespace digest collision")

    async def prepare_storage(self) -> None:
        """Idempotently prepare and validate the current SQL Trace structures."""

        await self.setup()

    async def commit_ledger_change(
        self,
        change: TraceLedgerChange,
    ) -> TraceLedgerCommitResult:
        """Resolve and atomically persist one framework-owned Ledger change."""

        if not isinstance(change, TraceLedgerChange):
            raise TypeError("change must be a TraceLedgerChange")
        if change.namespace != self._namespace:
            raise TraceThreadNotFound("Trace namespace does not exist")
        await self.setup()
        delete_generation_was_observed = False
        expected_renewal_expiry: datetime | None = None
        checkpoint_commit_was_attempted = False

        async def operation(connection: AsyncConnection) -> TraceLedgerCommitResult:
            nonlocal delete_generation_was_observed
            nonlocal expected_renewal_expiry
            nonlocal checkpoint_commit_was_attempted
            await self._locked_namespace(connection)
            # Every Trace writer locks namespace before thread or writer rows. Sampling
            # after that potentially long wait gives successful ownership changes their
            # complete configured lease instead of a deadline already spent in the queue.
            now = await _database_now(connection)
            existing = None
            if change.kind == "append_events":
                existing = await self._existing_backend_append(connection, change)
            if (
                change.kind == "save_projection_checkpoint"
                and checkpoint_commit_was_attempted
            ):
                checkpoint_result = await self._existing_backend_checkpoint(
                    connection,
                    change,
                )
                if checkpoint_result is not None:
                    return checkpoint_result
            state = await self._load_backend_state(
                connection,
                thread_id=_change_thread_id(change),
                run_id=_change_run_id(change),
                include_active_writers=change.kind == "delete_generation",
                checkpoint_request=_change_checkpoint_request(change),
                now=now,
                lock=True,
            )
            proven_renewal = False
            if change.kind == "renew_writer" and expected_renewal_expiry is not None:
                writer = state.target_writer
                proven_renewal = bool(
                    writer is not None
                    and writer.owner_token == change.owner_token
                    and writer.fence == change.fence
                    and writer.lease_expires_at >= expected_renewal_expiry
                )
            if change.kind == "delete_generation":
                if state.thread is None or state.thread.key != change.key:
                    if delete_generation_was_observed:
                        return TraceLedgerCommitResult(
                            kind="delete_generation",
                            key=change.key,
                        )
                else:
                    delete_generation_was_observed = True
            resolved_change = (
                change
                if existing is None
                else replace(
                    change,
                    facts=(),
                    proven_events=existing.events,
                )
            )
            if proven_renewal:
                resolved_change = replace(
                    resolved_change,
                    proven_renewal=True,
                )
            effect = resolve_ledger_change(resolved_change, state)
            if change.kind == "renew_writer" and effect.writer is not None:
                expected_renewal_expiry = effect.writer.lease_expires_at
            await self._apply_backend_effect(connection, change, effect, now=now)
            if change.kind == "save_projection_checkpoint":
                checkpoint_commit_was_attempted = True
            return effect.result

        return await self._write(operation)

    @_owned_database_operation
    async def load_ledger_state(
        self,
        request: TraceLedgerStateRequest,
    ) -> TraceLedgerState:
        """Load one consistent namespace, thread, and optional writer state."""

        if not isinstance(request, TraceLedgerStateRequest):
            raise TypeError("request must be a TraceLedgerStateRequest")
        if request.namespace != self._namespace:
            raise TraceThreadNotFound("Trace namespace does not exist")
        _validate_thread_id(request.thread_id)
        await self.setup()
        async with self._read_connection() as connection:
            now = await _database_now(connection)
            state = await self._load_backend_state(
                connection,
                thread_id=request.thread_id,
                run_id=request.run_id,
                include_active_writers=request.include_active_writers,
                checkpoint_request=None,
                now=now,
                lock=False,
            )
            if (
                request.generation is not None
                and state.thread is not None
                and state.thread.key.generation != request.generation
            ):
                raise TraceThreadNotFound("Trace generation does not exist")
            return state

    @_owned_database_operation
    async def read_event_page(
        self,
        request: TraceEventPageRequest,
    ) -> StoredTraceEventPage:
        """Read one raw exact-generation event page and its observed tail."""

        if not isinstance(request, TraceEventPageRequest):
            raise TypeError("request must be a TraceEventPageRequest")
        self._validate_key(request.key)
        if request.limit < 1:
            raise ValueError("event page limit must be positive")
        await self.setup()
        async with self._read_connection() as connection:
            thread = await self._thread_row(
                connection,
                thread_id=request.key.thread_id,
            )
            if thread["generation"] != request.key.generation:
                raise TraceThreadNotFound("Trace generation does not exist")
            tail = cast(int, thread["next_seq"]) - 1
            criteria = [
                events.c.namespace_hash == self._namespace_hash,
                events.c.thread_hash == _digest(request.key.thread_id),
                events.c.generation == request.key.generation,
            ]
            if request.direction == "forward":
                after = 0 if request.after_seq is None else request.after_seq
                as_of = tail if request.as_of_seq is None else request.as_of_seq
                criteria.extend(
                    (events.c.trace_seq > after, events.c.trace_seq <= as_of)
                )
                statement = (
                    select(events)
                    .where(*criteria)
                    .order_by(events.c.trace_seq)
                    .limit(request.limit)
                )
            else:
                before = tail + 1 if request.before_seq is None else request.before_seq
                criteria.append(events.c.trace_seq < before)
                statement = (
                    select(events)
                    .where(*criteria)
                    .order_by(desc(events.c.trace_seq))
                    .limit(request.limit)
                )
            rows = (await connection.execute(statement)).mappings().all()
            return StoredTraceEventPage(
                key=request.key,
                tail_seq=tail,
                events=tuple(_stored_event_from_row(row) for row in rows),
            )

    @_owned_database_operation
    async def query_trace_entries(
        self,
        request: TraceEntryQueryRequest,
    ) -> StoredTraceEntryPage:
        """Filter indexed metadata and join only referenced Ledger payloads."""

        if not isinstance(request, TraceEntryQueryRequest):
            raise TypeError("request must be a TraceEntryQueryRequest")
        self._validate_key(request.key)
        if not isinstance(request.where, TraceFilter):
            raise TypeError("request.where must be a TraceFilter")
        if request.limit < 1:
            raise ValueError("entry page limit must be positive")
        if (request.before_started_at is None) != (request.before_entry_id is None):
            raise ValueError("entry cursor time and ID must be supplied together")
        await self.setup()
        async with self._read_connection() as connection:
            thread = await self._thread_row(
                connection,
                thread_id=request.key.thread_id,
            )
            if thread["generation"] != request.key.generation:
                raise TraceThreadNotFound("Trace generation does not exist")
            tail = cast(int, thread["next_seq"]) - 1
            base_criteria = _trace_entry_criteria(self, request)
            facets = await _trace_entry_facets(connection, self, request)
            page_criteria = list(base_criteria)
            if request.before_started_at is not None:
                assert request.before_entry_id is not None
                cursor_time = _database_naive(request.before_started_at)
                cursor_hash = _digest(request.before_entry_id)
                page_criteria.append(
                    or_(
                        entries.c.started_at < cursor_time,
                        and_(
                            entries.c.started_at == cursor_time,
                            entries.c.entry_hash < cursor_hash,
                        ),
                    )
                )
            rows: Sequence[RowMapping] = (
                (
                    await connection.execute(
                        select(entries)
                        .where(*page_criteria)
                        .order_by(
                            desc(entries.c.started_at), desc(entries.c.entry_hash)
                        )
                        .limit(request.limit + 1)
                    )
                )
                .mappings()
                .all()
            )
            for row in rows:
                _validate_entry_row(row, where=request.where)
            has_more = len(rows) > request.limit
            selected: list[RowMapping] = list(rows[: request.limit])
            cursor_row = selected[-1] if has_more and selected else None
            result_rows: dict[str, RowMapping] = {
                cast(str, row["entry_id"]): row for row in selected
            }
            if request.where.include_ancestors:
                pending: set[str] = {
                    cast(str, row["parent_id"])
                    for row in selected
                    if row["parent_id"] is not None
                }
                run_ids = frozenset(request.run_ids)
                while pending:
                    unresolved = pending - result_rows.keys()
                    if not unresolved:
                        break
                    parent_rows: Sequence[RowMapping] = (
                        (
                            await connection.execute(
                                select(entries).where(
                                    entries.c.namespace_hash == self._namespace_hash,
                                    entries.c.thread_hash
                                    == _digest(request.key.thread_id),
                                    entries.c.generation == request.key.generation,
                                    entries.c.entry_hash.in_(
                                        tuple(_digest(value) for value in unresolved)
                                    ),
                                )
                            )
                        )
                        .mappings()
                        .all()
                    )
                    pending = set[str]()
                    for row in parent_rows:
                        _validate_entry_row(row, where=None)
                        entry_id = cast(str, row["entry_id"])
                        if entry_id not in unresolved:
                            raise TraceStoreProtocolError(
                                "Trace entry parent digest collision"
                            )
                        if cast(str, row["run_id"]) not in run_ids:
                            continue
                        result_rows[entry_id] = row
                        if row["parent_id"] is not None:
                            pending.add(cast(str, row["parent_id"]))
            sequences = {
                cast(int, row[column])
                for row in result_rows.values()
                for column in ("started_seq", "updated_seq")
            }
            event_rows: Sequence[RowMapping] = (
                (
                    await connection.execute(
                        select(events).where(
                            events.c.namespace_hash == self._namespace_hash,
                            events.c.thread_hash == _digest(request.key.thread_id),
                            events.c.generation == request.key.generation,
                            events.c.trace_seq.in_(tuple(sequences)),
                        )
                    )
                )
                .mappings()
                .all()
                if sequences
                else ()
            )
            event_records = {
                cast(int, row["trace_seq"]): _stored_event_from_row(row)
                for row in event_rows
            }
            tracked_rows = (
                await connection.execute(
                    select(events.c.run_id)
                    .where(
                        events.c.namespace_hash == self._namespace_hash,
                        events.c.thread_hash == _digest(request.key.thread_id),
                        events.c.generation == request.key.generation,
                        events.c.fact_kind == "call.tracking",
                        events.c.run_hash.in_(
                            tuple(_digest(value) for value in request.run_ids)
                        ),
                    )
                    .distinct()
                )
            ).scalars()
            tracked_run_ids = frozenset(cast(str, value) for value in tracked_rows)
            requested_run_ids = frozenset(request.run_ids)
            return StoredTraceEntryPage(
                key=request.key,
                as_of_seq=tail,
                entries=tuple(
                    _stored_entry_from_row(row, event_records)
                    for row in sorted(
                        result_rows.values(),
                        key=lambda item: (
                            cast(datetime, item["started_at"]),
                            cast(str, item["entry_hash"]),
                        ),
                        reverse=True,
                    )
                ),
                facets=facets,
                has_more=has_more,
                next_started_at=(
                    None
                    if cursor_row is None
                    else _as_utc(cast(datetime, cursor_row["started_at"]))
                ),
                next_entry_id=(
                    None if cursor_row is None else cast(str, cursor_row["entry_id"])
                ),
                call_tracking_present=(
                    bool(requested_run_ids) and requested_run_ids <= tracked_run_ids
                ),
            )

    async def rebuild_trace_entries(
        self,
        request: TraceEntryRebuildRequest,
    ) -> int:
        """Atomically replace derived entries when the Ledger tail is unchanged."""

        if not isinstance(request, TraceEntryRebuildRequest):
            raise TypeError("request must be a TraceEntryRebuildRequest")
        self._validate_key(request.key)
        if request.as_of_seq < 0:
            raise ValueError("entry rebuild tail must be non-negative")
        if any(
            mutation.updated_seq > request.as_of_seq
            or (
                mutation.started_seq is not None
                and mutation.started_seq > request.as_of_seq
            )
            for mutation in request.mutations
        ):
            raise ValueError("entry rebuild mutations exceed the requested tail")

        async def operation(connection: AsyncConnection) -> int:
            await self._locked_namespace(connection)
            row = (
                (
                    await connection.execute(
                        select(threads)
                        .where(
                            threads.c.namespace_hash == self._namespace_hash,
                            threads.c.thread_hash == _digest(request.key.thread_id),
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None or row["generation"] != request.key.generation:
                raise TraceThreadNotFound("Trace generation does not exist")
            _verify_thread_row(row, self._namespace, request.key.thread_id)
            if cast(int, row["next_seq"]) - 1 != request.as_of_seq:
                raise TraceStoreProtocolError(
                    "Trace Ledger changed while rebuilding entries"
                )
            scope = (
                entries.c.namespace_hash == self._namespace_hash,
                entries.c.thread_hash == _digest(request.key.thread_id),
                entries.c.generation == request.key.generation,
            )
            await connection.execute(delete(entries).where(*scope))
            for mutation in request.mutations:
                await self._apply_entry_mutation(connection, request.key, mutation)
            return len(
                {
                    mutation.entry_id
                    for mutation in request.mutations
                    if mutation.started_seq is not None
                }
            )

        return await self._write(operation)

    @_owned_database_operation
    async def load_projection_checkpoint(
        self,
        request: TraceCheckpointRequest,
    ) -> StoredTraceCheckpoint | None:
        """Load the newest raw checkpoint inside one exact fixed prefix."""

        if not isinstance(request, TraceCheckpointRequest):
            raise TypeError("request must be a TraceCheckpointRequest")
        self._validate_key(request.key)
        _checkpoint_lookup(
            request.key,
            projection_name=request.projection_name,
            run_id=request.run_id,
            as_of_seq=request.as_of_seq,
        )
        await self.setup()
        async with self._read_connection() as connection:
            await self._require_key(connection, request.key)
            row = await self._checkpoint_row(
                connection,
                request,
                at_or_below=True,
            )
            return (
                None if row is None else _stored_checkpoint_from_row(request.key, row)
            )

    async def _load_backend_state(
        self,
        connection: AsyncConnection,
        *,
        thread_id: str,
        run_id: str | None,
        include_active_writers: bool,
        checkpoint_request: TraceCheckpointRequest | None,
        now: datetime,
        lock: bool,
    ) -> TraceLedgerState:
        thread_hash = _digest(thread_id)
        thread_statement = select(threads).where(
            threads.c.namespace_hash == self._namespace_hash,
            threads.c.thread_hash == thread_hash,
        )
        if lock:
            thread_statement = thread_statement.with_for_update()
        thread_row = (
            (await connection.execute(thread_statement)).mappings().one_or_none()
        )
        if thread_row is not None:
            _verify_thread_row(thread_row, self._namespace, thread_id)

        namespace_totals = (
            await connection.execute(
                select(
                    func.count(),
                    func.coalesce(func.sum(threads.c.persisted_bytes), 0),
                ).where(threads.c.namespace_hash == self._namespace_hash)
            )
        ).one()
        namespace_reserve = await connection.scalar(
            select(func.coalesce(func.sum(writers.c.remaining_byte_reserve), 0)).where(
                writers.c.namespace_hash == self._namespace_hash,
                writers.c.active.is_(True),
            )
        )

        target_writer = None
        writer_count = 0
        thread_reserved_events = 0
        thread_reserved_bytes = 0
        active_writers: tuple[StoreWriterSnapshot, ...] = ()
        if thread_row is not None:
            generation = cast(str, thread_row["generation"])
            writer_scope = (
                writers.c.namespace_hash == self._namespace_hash,
                writers.c.thread_hash == thread_hash,
                writers.c.generation == generation,
            )
            aggregates = (
                await connection.execute(
                    select(
                        func.count(),
                        func.coalesce(func.sum(writers.c.remaining_event_reserve), 0),
                        func.coalesce(func.sum(writers.c.remaining_byte_reserve), 0),
                    ).where(*writer_scope)
                )
            ).one()
            writer_count = cast(int, aggregates[0])
            active_reserves = (
                await connection.execute(
                    select(
                        func.coalesce(func.sum(writers.c.remaining_event_reserve), 0),
                        func.coalesce(func.sum(writers.c.remaining_byte_reserve), 0),
                    ).where(*writer_scope, writers.c.active.is_(True))
                )
            ).one()
            thread_reserved_events = cast(int, active_reserves[0])
            thread_reserved_bytes = cast(int, active_reserves[1])
            if run_id is not None:
                target_statement = select(writers).where(
                    *writer_scope,
                    writers.c.run_hash == _digest(run_id),
                )
                if lock:
                    target_statement = target_statement.with_for_update()
                target_row = (
                    (await connection.execute(target_statement))
                    .mappings()
                    .one_or_none()
                )
                if target_row is not None:
                    if target_row["run_id"] != run_id:
                        raise TraceStoreProtocolError("Trace Run digest collision")
                    target_writer = _ledger_writer_from_row(target_row)
            if include_active_writers:
                rows = (
                    await connection.execute(
                        select(writers.c.run_id, writers.c.committed_events)
                        .where(
                            *writer_scope,
                            writers.c.active.is_(True),
                            writers.c.lease_expires_at > now,
                        )
                        .order_by(writers.c.run_id)
                    )
                ).all()
                active_writers = tuple(
                    StoreWriterSnapshot(
                        run_id=cast(str, row.run_id),
                        committed_events=cast(int, row.committed_events),
                    )
                    for row in rows
                )

        current_checkpoint = None
        if checkpoint_request is not None and thread_row is not None:
            row = await self._checkpoint_row(
                connection,
                checkpoint_request,
                at_or_below=False,
                lock=lock,
            )
            if row is not None:
                current_checkpoint = _stored_checkpoint_from_row(
                    checkpoint_request.key,
                    row,
                )
        return TraceLedgerState(
            observed_at=_as_utc(now),
            namespace_thread_count=cast(int, namespace_totals[0]),
            namespace_persisted_bytes=cast(int, namespace_totals[1]),
            namespace_reserved_bytes=cast(int, namespace_reserve),
            thread=(
                None
                if thread_row is None
                else TraceLedgerThreadState(
                    key=TraceThreadKey(
                        namespace=self._namespace,
                        thread_id=thread_id,
                        generation=cast(str, thread_row["generation"]),
                    ),
                    next_seq=cast(int, thread_row["next_seq"]),
                    persisted_bytes=cast(int, thread_row["persisted_bytes"]),
                )
            ),
            target_writer=target_writer,
            writer_count=writer_count,
            thread_reserved_events=thread_reserved_events,
            thread_reserved_bytes=thread_reserved_bytes,
            active_writers=active_writers,
            current_checkpoint=current_checkpoint,
        )

    async def _checkpoint_row(
        self,
        connection: AsyncConnection,
        request: TraceCheckpointRequest,
        *,
        at_or_below: bool,
        lock: bool = False,
    ):
        scope = request.run_id or ""
        criteria = [
            projection_checkpoints.c.namespace_hash == self._namespace_hash,
            projection_checkpoints.c.thread_hash == _digest(request.key.thread_id),
            projection_checkpoints.c.generation == request.key.generation,
            projection_checkpoints.c.projection_hash
            == _digest(request.projection_name),
            projection_checkpoints.c.run_scope_hash == _digest(scope),
        ]
        if at_or_below:
            criteria.append(projection_checkpoints.c.as_of_seq <= request.as_of_seq)
        statement = (
            select(projection_checkpoints)
            .where(*criteria)
            .order_by(desc(projection_checkpoints.c.as_of_seq))
            .limit(1)
        )
        if lock:
            statement = statement.with_for_update()
        row = (await connection.execute(statement)).mappings().one_or_none()
        if row is not None and (
            row["projection_name"] != request.projection_name
            or row["run_scope"] != scope
        ):
            raise TraceStoreProtocolError("Projection checkpoint digest collision")
        return row

    async def _existing_backend_append(
        self,
        connection: AsyncConnection,
        change: TraceLedgerChange,
    ) -> TraceLedgerCommitResult | None:
        if not change.facts:
            return None
        rows = (
            (
                await connection.execute(
                    select(events).where(
                        events.c.namespace_hash == self._namespace_hash,
                        events.c.event_id.in_(
                            tuple(item.event_id for item in change.facts)
                        ),
                    )
                )
            )
            .mappings()
            .all()
        )
        if not rows:
            return None
        by_id = {cast(str, row["event_id"]): row for row in rows}
        if len(by_id) != len(change.facts):
            raise TraceStoreProtocolError("Trace event idempotency evidence conflicts")
        public_events: list[TraceEvent] = []
        for draft in change.facts:
            row = by_id.get(draft.event_id)
            if (
                row is None
                or row["payload_digest"] != draft.payload_digest
                or bytes(cast(bytes, row["payload"])) != draft.canonical_payload
                or row["generation"]
                != (None if change.key is None else change.key.generation)
                or row["run_id"] != change.run_id
                or row["fact_kind"] != draft.fact.kind
                or _as_utc(cast(datetime, row["occurred_at"])) != draft.fact.occurred_at
            ):
                raise TraceStoreProtocolError(
                    "Trace event idempotency evidence conflicts"
                )
            event = _event(
                event_id=draft.event_id,
                trace_seq=cast(int, row["trace_seq"]),
                generation=cast(str, row["generation"]),
                fact=draft.fact,
                copy_fact=False,
            )
            if event.persisted_bytes != row["persisted_bytes"]:
                raise TraceStoreProtocolError(
                    "Trace event idempotency size evidence conflicts"
                )
            public_events.append(event)
        return TraceLedgerCommitResult(
            kind="append_events",
            key=change.key,
            run_id=change.run_id,
            events=tuple(public_events),
        )

    async def _existing_backend_checkpoint(
        self,
        connection: AsyncConnection,
        change: TraceLedgerChange,
    ) -> TraceLedgerCommitResult | None:
        checkpoint = change.checkpoint
        payload = change.canonical_checkpoint_state
        digest = change.checkpoint_state_digest
        if checkpoint is None or payload is None or digest is None:
            return None
        scope = checkpoint.run_id or ""
        row = (
            (
                await connection.execute(
                    select(projection_checkpoints).where(
                        projection_checkpoints.c.namespace_hash == self._namespace_hash,
                        projection_checkpoints.c.thread_hash
                        == _digest(checkpoint.key.thread_id),
                        projection_checkpoints.c.generation
                        == checkpoint.key.generation,
                        projection_checkpoints.c.projection_hash
                        == _digest(checkpoint.projection_name),
                        projection_checkpoints.c.run_scope_hash == _digest(scope),
                        projection_checkpoints.c.as_of_seq == checkpoint.as_of_seq,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        if (
            row["projection_name"] != checkpoint.projection_name
            or row["run_scope"] != scope
            or row["state_digest"] != digest
            or bytes(cast(bytes, row["state_payload"])) != payload
        ):
            raise TraceStoreProtocolError(
                "Projection checkpoint idempotency evidence conflicts"
            )
        return TraceLedgerCommitResult(
            kind="save_projection_checkpoint",
            key=checkpoint.key,
            checkpoint=checkpoint.model_copy(deep=True),
        )

    async def _apply_backend_effect(
        self,
        connection: AsyncConnection,
        change: TraceLedgerChange,
        effect: TraceLedgerStorageEffect,
        *,
        now: datetime,
    ) -> None:
        key = effect.result.key or change.key
        if effect.delete_generation:
            if key is None:
                raise TraceStoreProtocolError("Delete effect has no generation")
            criteria = and_(
                events.c.namespace_hash == self._namespace_hash,
                events.c.thread_hash == _digest(key.thread_id),
                events.c.generation == key.generation,
            )
            await connection.execute(delete(events).where(criteria))
            await connection.execute(
                delete(entries).where(
                    entries.c.namespace_hash == self._namespace_hash,
                    entries.c.thread_hash == _digest(key.thread_id),
                    entries.c.generation == key.generation,
                )
            )
            await connection.execute(
                delete(projection_checkpoints).where(
                    projection_checkpoints.c.namespace_hash == self._namespace_hash,
                    projection_checkpoints.c.thread_hash == _digest(key.thread_id),
                    projection_checkpoints.c.generation == key.generation,
                )
            )
            await connection.execute(delete(writers).where(_writer_scope(self, key)))
            await connection.execute(
                delete(threads).where(
                    threads.c.namespace_hash == self._namespace_hash,
                    threads.c.thread_hash == _digest(key.thread_id),
                    threads.c.generation == key.generation,
                )
            )
            return
        if effect.remove_thread:
            if key is None:
                raise TraceStoreProtocolError("Remove-thread effect has no generation")
            if effect.remove_writer_run_id is not None:
                await connection.execute(
                    delete(writers).where(
                        _writer_where(self, key, effect.remove_writer_run_id)
                    )
                )
            await connection.execute(
                delete(projection_checkpoints).where(
                    projection_checkpoints.c.namespace_hash == self._namespace_hash,
                    projection_checkpoints.c.thread_hash == _digest(key.thread_id),
                    projection_checkpoints.c.generation == key.generation,
                )
            )
            await connection.execute(
                delete(entries).where(
                    entries.c.namespace_hash == self._namespace_hash,
                    entries.c.thread_hash == _digest(key.thread_id),
                    entries.c.generation == key.generation,
                )
            )
            await connection.execute(
                delete(threads).where(
                    threads.c.namespace_hash == self._namespace_hash,
                    threads.c.thread_hash == _digest(key.thread_id),
                    threads.c.generation == key.generation,
                )
            )
            return
        if effect.thread is not None:
            thread_hash = _digest(effect.thread.key.thread_id)
            existing = await connection.scalar(
                select(func.count())
                .select_from(threads)
                .where(
                    threads.c.namespace_hash == self._namespace_hash,
                    threads.c.thread_hash == thread_hash,
                )
            )
            if cast(int, existing) == 0:
                await connection.execute(
                    insert(threads).values(
                        namespace_hash=self._namespace_hash,
                        thread_hash=thread_hash,
                        namespace=self._namespace,
                        thread_id=effect.thread.key.thread_id,
                        generation=effect.thread.key.generation,
                        next_seq=effect.thread.next_seq,
                        persisted_bytes=effect.thread.persisted_bytes,
                        created_at=now,
                        updated_at=now,
                    )
                )
            else:
                await connection.execute(
                    update(threads)
                    .where(
                        threads.c.namespace_hash == self._namespace_hash,
                        threads.c.thread_hash == thread_hash,
                        threads.c.generation == effect.thread.key.generation,
                    )
                    .values(
                        next_seq=effect.thread.next_seq,
                        persisted_bytes=effect.thread.persisted_bytes,
                        updated_at=now,
                    )
                )
        if key is None:
            key = None if effect.thread is None else effect.thread.key
        if key is None:
            return
        if effect.remove_writer_run_id is not None:
            await connection.execute(
                delete(writers).where(
                    _writer_where(self, key, effect.remove_writer_run_id)
                )
            )
        if effect.writer is not None:
            where = _writer_where(self, key, effect.writer.run_id)
            exists = await connection.scalar(
                select(func.count()).select_from(writers).where(where)
            )
            values = dict(
                owner_token=effect.writer.owner_token,
                fence=effect.writer.fence,
                lease_expires_at=_database_naive(effect.writer.lease_expires_at),
                active=effect.writer.active,
                terminal_committed=effect.writer.terminal_committed,
                closed_committed=effect.writer.closed_committed,
                committed_events=effect.writer.committed_events,
                remaining_event_reserve=effect.writer.remaining_event_reserve,
                remaining_byte_reserve=effect.writer.remaining_byte_reserve,
                updated_at=now,
            )
            if cast(int, exists) == 0:
                await connection.execute(
                    insert(writers).values(
                        namespace_hash=self._namespace_hash,
                        thread_hash=_digest(key.thread_id),
                        generation=key.generation,
                        run_hash=_digest(effect.writer.run_id),
                        run_id=effect.writer.run_id,
                        created_at=now,
                        **values,
                    )
                )
            else:
                await connection.execute(update(writers).where(where).values(**values))
        for record in effect.events:
            await connection.execute(
                insert(events).values(
                    namespace_hash=self._namespace_hash,
                    thread_hash=_digest(key.thread_id),
                    generation=key.generation,
                    trace_seq=record.trace_seq,
                    event_id=record.event_id,
                    run_hash=_digest(record.run_id),
                    run_id=record.run_id,
                    fact_kind=record.fact_kind,
                    occurred_at=_database_naive(record.occurred_at),
                    payload=record.canonical_payload,
                    payload_digest=record.payload_digest,
                    persisted_bytes=record.persisted_bytes,
                    created_at=now,
                )
            )
        for mutation in effect.entry_mutations:
            await self._apply_entry_mutation(connection, key, mutation)
        if effect.checkpoint is not None:
            scope = effect.checkpoint.run_id or ""
            await connection.execute(
                insert(projection_checkpoints).values(
                    namespace_hash=self._namespace_hash,
                    thread_hash=_digest(key.thread_id),
                    generation=key.generation,
                    projection_hash=_digest(effect.checkpoint.projection_name),
                    run_scope_hash=_digest(scope),
                    projection_name=effect.checkpoint.projection_name,
                    run_scope=scope,
                    as_of_seq=effect.checkpoint.as_of_seq,
                    state_payload=effect.checkpoint.canonical_state,
                    state_digest=effect.checkpoint.state_digest,
                    created_at=now,
                )
            )

    async def _apply_entry_mutation(
        self,
        connection: AsyncConnection,
        key: TraceThreadKey,
        mutation: TraceEntryMutation,
    ) -> None:
        """Apply one payload-free index mutation in the Ledger transaction."""

        entry_hash = _digest(mutation.entry_id)
        criteria = and_(
            entries.c.namespace_hash == self._namespace_hash,
            entries.c.thread_hash == _digest(key.thread_id),
            entries.c.generation == key.generation,
            entries.c.entry_hash == entry_hash,
        )
        row = (
            (
                await connection.execute(
                    select(entries).where(criteria).with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            if (
                mutation.kind is None
                or mutation.status is None
                or mutation.name is None
                or mutation.run_id is None
                or mutation.namespace is None
                or mutation.started_at is None
                or mutation.started_seq is None
            ):
                return
            namespace = _encode_namespace(mutation.namespace)
            await connection.execute(
                insert(entries).values(
                    namespace_hash=self._namespace_hash,
                    thread_hash=_digest(key.thread_id),
                    generation=key.generation,
                    entry_hash=entry_hash,
                    entry_id=mutation.entry_id,
                    parent_hash=(
                        None
                        if mutation.parent_id is None
                        else _digest(mutation.parent_id)
                    ),
                    parent_id=mutation.parent_id,
                    kind=mutation.kind.value,
                    status=mutation.status.value,
                    name_hash=_digest(mutation.name),
                    name=mutation.name,
                    run_hash=_digest(mutation.run_id),
                    run_id=mutation.run_id,
                    graph_namespace_hash=_digest(namespace),
                    graph_namespace=namespace,
                    agent_hash=(
                        None
                        if mutation.agent_name is None
                        else _digest(mutation.agent_name)
                    ),
                    agent_name=mutation.agent_name,
                    provider_hash=(
                        None
                        if mutation.provider is None
                        else _digest(mutation.provider)
                    ),
                    provider=mutation.provider,
                    model_hash=(
                        None if mutation.model is None else _digest(mutation.model)
                    ),
                    model=mutation.model,
                    started_at=_database_naive(mutation.started_at),
                    first_output_at=(
                        None
                        if mutation.first_output_at is None
                        else _database_naive(mutation.first_output_at)
                    ),
                    completed_at=(
                        None
                        if mutation.completed_at is None
                        else _database_naive(mutation.completed_at)
                    ),
                    started_seq=mutation.started_seq,
                    updated_seq=mutation.updated_seq,
                )
            )
            return
        if row["entry_id"] != mutation.entry_id:
            raise TraceStoreProtocolError("Trace entry digest collision")
        if mutation.updated_seq < cast(int, row["updated_seq"]):
            raise TraceStoreProtocolError("Trace entry sequence moved backwards")
        immutable = (
            ("kind", None if mutation.kind is None else mutation.kind.value),
            ("name", mutation.name),
            ("run_id", mutation.run_id),
            (
                "graph_namespace",
                None
                if mutation.namespace is None
                else _encode_namespace(mutation.namespace),
            ),
        )
        if any(
            value is not None and row[column] != value for column, value in immutable
        ):
            raise TraceStoreProtocolError("Trace entry immutable metadata changed")
        if mutation.started_seq is not None:
            if mutation.started_at is None:
                await connection.execute(
                    update(entries)
                    .where(criteria)
                    .values(
                        started_seq=mutation.started_seq,
                        updated_seq=mutation.updated_seq,
                    )
                )
            return
        values: dict[str, object] = {"updated_seq": mutation.updated_seq}
        if mutation.status is not None:
            values["status"] = mutation.status.value
        if mutation.parent_id is not None:
            if row["parent_id"] is not None and row["parent_id"] != mutation.parent_id:
                raise TraceStoreProtocolError("Trace entry parent changed")
            values["parent_id"] = mutation.parent_id
            values["parent_hash"] = _digest(mutation.parent_id)
        for column, value in (
            ("agent_name", mutation.agent_name),
            ("provider", mutation.provider),
            ("model", mutation.model),
        ):
            if value is None:
                continue
            existing = row[column]
            if existing is not None and existing != value:
                raise TraceStoreProtocolError(f"Trace entry {column} changed")
            values[column] = value
            values[f"{column.removesuffix('_name')}_hash"] = _digest(value)
        if (
            mutation.started_at is not None
            and _as_utc(cast(datetime, row["started_at"])) != mutation.started_at
        ):
            raise TraceStoreProtocolError("Trace entry start time changed")
        if mutation.first_output_at is not None:
            values["first_output_at"] = _database_naive(mutation.first_output_at)
        if mutation.completed_at is not None:
            values["completed_at"] = _database_naive(mutation.completed_at)
        await connection.execute(update(entries).where(criteria).values(**values))

    async def _thread_row(self, connection: AsyncConnection, *, thread_id: str):
        row = (
            (
                await connection.execute(
                    select(threads).where(
                        threads.c.namespace_hash == self._namespace_hash,
                        threads.c.thread_hash == _digest(thread_id),
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise TraceThreadNotFound("Trace thread does not exist")
        _verify_thread_row(row, self._namespace, thread_id)
        return row

    async def _locked_namespace(self, connection: AsyncConnection) -> None:
        """Lock and collision-check the namespace row before global accounting."""

        existing = await connection.scalar(
            select(namespaces.c.namespace)
            .where(namespaces.c.namespace_hash == self._namespace_hash)
            .with_for_update()
        )
        if existing is None:
            raise TraceStoreProtocolError("Trace namespace registration is missing")
        if existing != self._namespace:
            raise TraceStoreProtocolError("Trace namespace digest collision")

    def _validate_key(self, key: TraceThreadKey) -> None:
        """Reject a key from another Store before issuing a database query."""

        if not isinstance(key, TraceThreadKey):
            raise TypeError("key must be a TraceThreadKey")
        if key.namespace != self._namespace:
            raise TraceThreadNotFound("Trace namespace does not exist")

    async def _require_key(
        self, connection: AsyncConnection, key: TraceThreadKey
    ) -> None:
        row = await self._thread_row(connection, thread_id=key.thread_id)
        if row["generation"] != key.generation:
            raise TraceThreadNotFound("Trace generation does not exist")

    async def _write(
        self, operation: Callable[[AsyncConnection], Awaitable[_ResultT]]
    ) -> _ResultT:
        """Keep repeated caller cancellation outside the active SQL transaction."""

        return await _run_database_operation(
            self._execute_write(operation),
            task_name="tinkerfin-trace-sql-write",
        )

    async def _execute_write(
        self, operation: Callable[[AsyncConnection], Awaitable[_ResultT]]
    ) -> _ResultT:
        """Run one retry-safe transaction and expose only stable Store failures.

        The same ``operation`` closure is reused after retryable lock/busy failures and
        after a commit whose outcome is unknown to the client. Append and checkpoint
        closures therefore retain their event IDs or canonical state digest and verify
        committed evidence before attempting another insert.
        """

        await self.setup()
        for attempt in range(self._options.commit_retry_attempts):
            try:
                async with self._raw_write_connection() as connection:
                    return await operation(connection)
            except DBAPIError as error:
                if not _retryable(error, dialect=self._dialect):
                    raise TraceStoreError(
                        "Trace Store database operation failed",
                        cause=error,
                    ) from error
                if attempt + 1 >= self._options.commit_retry_attempts:
                    raise TraceStoreTimeout(
                        "Trace Store retry budget was exhausted",
                        cause=error,
                    ) from error
                await asyncio.sleep(
                    self._options.commit_retry_delay_seconds * (attempt + 1)
                )
            except SQLAlchemyError as error:
                raise TraceStoreError(
                    "Trace Store database operation failed",
                    cause=error,
                ) from error
        raise TraceStoreTimeout("Trace Store retry budget was exhausted")

    @asynccontextmanager
    async def _raw_write_connection(
        self,
        *,
        sqlite_fail_fast: bool = True,
    ) -> AsyncGenerator[AsyncConnection, None]:
        """Own one transaction while leaving Engine and pool ownership with the host.

        Ordinary SQLite writes temporarily disable the driver's blocking busy wait so
        lock conflicts enter the Store's bounded, cancellation-responsive retry loop.
        The prior per-connection PRAGMA is restored before the borrowed connection
        returns to the host pool. Schema setup opts out because its retained task must
        serialize concurrent first starts rather than consume the short write budget.
        """

        connection = await self._engine.connect()
        pooled: PoolProxiedConnection | None = None
        sqlite_busy_timeout: int | None = None
        primary_error: BaseException | None = None
        try:
            pooled = await connection.get_raw_connection()
            if self._dialect == "sqlite":
                if sqlite_fail_fast:
                    value = await connection.scalar(text("PRAGMA busy_timeout"))
                    if not isinstance(value, int) or value < 0:
                        raise TraceStoreProtocolError(
                            "SQLite did not return a valid busy timeout"
                        )
                    sqlite_busy_timeout = value
                    await connection.exec_driver_sql("PRAGMA busy_timeout = 0")
                await connection.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                connection = await connection.execution_options(
                    isolation_level="READ COMMITTED"
                )
                await connection.begin()
            yield connection
            await connection.commit()
        except asyncio.CancelledError as error:
            primary_error = error
            await _discard_cancelled_connection(connection, pooled, error)
            raise
        except BaseException as error:
            primary_error = error
            try:
                await connection.rollback()
            except Exception as rollback_error:  # noqa: BLE001 - preserve primary failure
                error.add_note(
                    "Trace Store transaction rollback also failed: "
                    f"{type(rollback_error).__module__}."
                    f"{type(rollback_error).__qualname__}"
                )
            raise
        finally:

            async def close_connection() -> None:
                if sqlite_busy_timeout is not None:
                    try:
                        await connection.exec_driver_sql(
                            f"PRAGMA busy_timeout = {sqlite_busy_timeout}"
                        )
                    except SQLAlchemyError:
                        # An invalidated connection cannot return to the pool;
                        # SQLAlchemy discards it, so no changed PRAGMA can leak to
                        # another borrower.
                        pass
                await connection.close()

            await _complete_connection_cleanup(
                close_connection(),
                task_name="tinkerfin-trace-write-connection-close",
                primary_error=primary_error,
            )

    @asynccontextmanager
    async def _read_connection(self) -> AsyncGenerator[AsyncConnection, None]:
        """Read one fixed database snapshot and translate infrastructure failure.

        A host may configure MySQL for READ COMMITTED, which would let the metadata and
        active-writer statements observe different commits. The temporary connection
        option gives each Store read a REPEATABLE READ transaction; SQLAlchemy restores
        the borrowed pool connection's original isolation level on return. Cancellation
        invalidates only the current borrowed connection because asyncmy cannot safely
        roll back a command interrupted in flight. Contract coverage lives in
        ``tests/test_mysql_store.py::test_mysql_cancelled_reads_return_pool_capacity``.
        """

        try:
            connection = await self._engine.connect()
            pooled: PoolProxiedConnection | None = None
            primary_error: BaseException | None = None
            try:
                pooled = await connection.get_raw_connection()
                if self._dialect == "mysql":
                    connection = await connection.execution_options(
                        isolation_level="REPEATABLE READ"
                    )
                    transaction = await connection.begin()
                    yield connection
                    await transaction.commit()
                else:
                    # Python's SQLite driver legacy mode does not BEGIN for SELECT.
                    # An explicit transaction fixes generation metadata and event rows
                    # to the same snapshot instead of allowing a concurrent append
                    # between statements.
                    await connection.exec_driver_sql("BEGIN")
                    yield connection
                    if connection.in_transaction():
                        await connection.rollback()
            except asyncio.CancelledError as error:
                primary_error = error
                await _discard_cancelled_connection(connection, pooled, error)
                raise
            except BaseException as error:
                primary_error = error
                if connection.in_transaction():
                    try:
                        await connection.rollback()
                    except Exception as rollback_error:  # noqa: BLE001
                        error.add_note(
                            "Trace Store read rollback also failed: "
                            f"{type(rollback_error).__module__}."
                            f"{type(rollback_error).__qualname__}"
                        )
                raise
            finally:
                if not connection.closed:
                    await _complete_connection_cleanup(
                        connection.close(),
                        task_name="tinkerfin-trace-read-connection-close",
                        primary_error=primary_error,
                    )
        except SQLAlchemyError as error:
            raise TraceStoreError(
                "Trace Store database read failed",
                cause=error,
            ) from error


class SqlAlchemyTraceStore(DurableTraceStore):
    """Persist the framework-owned Trace Ledger through a borrowed SQLAlchemy Engine.

    SQLite and MySQL use the same public five-operation backend boundary and Ledger
    reducer as every other durable Store. The convenience Store owns no Engine resource
    and preserves the current six-table SQL shape.

    Args:
        engine: Borrowed asynchronous SQLite or MySQL Engine.
        namespace: Stable logical isolation key.
        limits: Ledger and terminal-reserve capacity limits.
        options: Writer lease, follow, and commit retry settings.
        codec: Canonical payload codec borrowed by the Store.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        namespace: str = "default",
        limits: TraceLimits | None = None,
        options: TraceStoreOptions | None = None,
        codec: CanonicalTracePayloadCodec | None = None,
    ) -> None:
        """Initialize a Store over a borrowed asynchronous SQLAlchemy Engine.

        Args:
            engine: Borrowed SQLite or MySQL Engine.
            namespace: Stable logical isolation key.
            limits: Optional capacity limits.
            options: Optional writer and retry settings.
            codec: Optional canonical payload codec.
        """

        resolved_limits = limits or TraceLimits()
        resolved_options = options or TraceStoreOptions()
        resolved_codec = codec or CanonicalTracePayloadCodec()
        backend = _SqlAlchemyTraceLedgerBackend(
            engine,
            namespace=namespace,
            options=resolved_options,
        )
        super().__init__(
            backend,
            namespace=namespace,
            limits=resolved_limits,
            options=resolved_options,
            codec=resolved_codec,
        )


def _change_thread_id(change: TraceLedgerChange) -> str:
    if change.identity is not None:
        return change.identity.thread_id
    if change.key is not None:
        return change.key.thread_id
    if change.checkpoint is not None:
        return change.checkpoint.key.thread_id
    raise TraceStoreProtocolError("Trace Ledger change has no thread identity")


def _change_run_id(change: TraceLedgerChange) -> str | None:
    if change.identity is not None:
        return change.identity.run_id
    return change.run_id


def _change_checkpoint_request(
    change: TraceLedgerChange,
) -> TraceCheckpointRequest | None:
    checkpoint = change.checkpoint
    if checkpoint is None:
        return None
    return TraceCheckpointRequest(
        key=checkpoint.key,
        projection_name=checkpoint.projection_name,
        run_id=checkpoint.run_id,
        as_of_seq=checkpoint.as_of_seq,
    )


def _ledger_writer_from_row(row: object) -> TraceLedgerWriterState:
    mapping = cast(dict[str, object], row)
    return TraceLedgerWriterState(
        run_id=cast(str, mapping["run_id"]),
        owner_token=cast(str, mapping["owner_token"]),
        fence=cast(int, mapping["fence"]),
        lease_expires_at=_as_utc(cast(datetime, mapping["lease_expires_at"])),
        active=bool(mapping["active"]),
        terminal_committed=bool(mapping["terminal_committed"]),
        closed_committed=bool(mapping["closed_committed"]),
        committed_events=cast(int, mapping["committed_events"]),
        remaining_event_reserve=cast(int, mapping["remaining_event_reserve"]),
        remaining_byte_reserve=cast(int, mapping["remaining_byte_reserve"]),
    )


def _stored_event_from_row(row: object) -> StoredTraceEvent:
    mapping = cast(dict[str, object], row)
    return StoredTraceEvent(
        event_id=cast(str, mapping["event_id"]),
        trace_seq=cast(int, mapping["trace_seq"]),
        run_id=cast(str, mapping["run_id"]),
        fact_kind=cast(str, mapping["fact_kind"]),
        occurred_at=_as_utc(cast(datetime, mapping["occurred_at"])),
        canonical_payload=bytes(cast(bytes, mapping["payload"])),
        payload_digest=cast(str, mapping["payload_digest"]),
        persisted_bytes=cast(int, mapping["persisted_bytes"]),
    )


def _encode_namespace(value: tuple[str, ...]) -> str:
    return json.dumps(
        list(value),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _decode_namespace(value: object) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise TraceStoreProtocolError("Trace entry namespace is not text")
    try:
        parsed = json.loads(value)
    except ValueError as error:
        raise TraceStoreProtocolError(
            "Trace entry namespace is invalid JSON",
            cause=error,
        ) from error
    if not isinstance(parsed, list):
        raise TraceStoreProtocolError("Trace entry namespace is not a string array")
    items = cast(list[object], parsed)
    if any(not isinstance(item, str) for item in items):
        raise TraceStoreProtocolError("Trace entry namespace is not a string array")
    return tuple(cast(str, item) for item in items)


def _trace_entry_criteria(
    backend: _SqlAlchemyTraceLedgerBackend,
    request: TraceEntryQueryRequest,
    *,
    exclude: Literal[
        "kinds",
        "statuses",
        "agent_names",
        "middleware_names",
        "skill_names",
        "providers",
        "models",
    ]
    | None = None,
) -> tuple[ColumnElement[bool], ...]:
    where = request.where
    criteria: list[ColumnElement[bool]] = [
        entries.c.namespace_hash == backend._namespace_hash,
        entries.c.thread_hash == _digest(request.key.thread_id),
        entries.c.generation == request.key.generation,
        entries.c.run_hash.in_(tuple(_digest(value) for value in request.run_ids)),
    ]
    if where.kinds and exclude != "kinds":
        criteria.append(entries.c.kind.in_(tuple(value.value for value in where.kinds)))
    if where.statuses and exclude != "statuses":
        criteria.append(
            entries.c.status.in_(tuple(value.value for value in where.statuses))
        )
    if where.parent_id is not None:
        criteria.append(entries.c.parent_hash == _digest(where.parent_id))
    if where.agent_names and exclude != "agent_names":
        criteria.append(
            entries.c.agent_hash.in_(
                tuple(_digest(value) for value in where.agent_names)
            )
        )
    if where.middleware_names and exclude != "middleware_names":
        criteria.extend(
            (
                entries.c.kind == TraceEntryKind.MIDDLEWARE.value,
                entries.c.name_hash.in_(
                    tuple(_digest(value) for value in where.middleware_names)
                ),
            )
        )
    if where.skill_names and exclude != "skill_names":
        criteria.extend(
            (
                entries.c.kind == TraceEntryKind.SKILL.value,
                entries.c.name_hash.in_(
                    tuple(_digest(value) for value in where.skill_names)
                ),
            )
        )
    if where.providers and exclude != "providers":
        criteria.append(
            entries.c.provider_hash.in_(
                tuple(_digest(value) for value in where.providers)
            )
        )
    if where.models and exclude != "models":
        criteria.append(
            entries.c.model_hash.in_(tuple(_digest(value) for value in where.models))
        )
    if where.namespaces:
        criteria.append(
            entries.c.graph_namespace_hash.in_(
                tuple(_digest(_encode_namespace(value)) for value in where.namespaces)
            )
        )
    if where.search is not None:
        escaped = (
            where.search.lower()
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        search = f"%{escaped}%"
        criteria.append(
            or_(
                func.lower(entries.c.name).like(search, escape="\\"),
                func.lower(entries.c.agent_name).like(search, escape="\\"),
                func.lower(entries.c.provider).like(search, escape="\\"),
                func.lower(entries.c.model).like(search, escape="\\"),
            )
        )
    if where.started_after is not None:
        criteria.append(entries.c.started_at > _database_naive(where.started_after))
    if where.started_before is not None:
        criteria.append(entries.c.started_at < _database_naive(where.started_before))
    return tuple(criteria)


async def _trace_entry_facets(
    connection: AsyncConnection,
    backend: _SqlAlchemyTraceLedgerBackend,
    request: TraceEntryQueryRequest,
) -> TraceFacets:
    async def grouped(
        column: Column[Any],
        *extra: ColumnElement[bool],
        exclude: Literal[
            "kinds",
            "statuses",
            "agent_names",
            "middleware_names",
            "skill_names",
            "providers",
            "models",
        ],
    ) -> dict[str, int]:
        criteria = _trace_entry_criteria(backend, request, exclude=exclude)
        rows = (
            await connection.execute(
                select(column, func.count()).where(*criteria, *extra).group_by(column)
            )
        ).all()
        return {
            cast(str, value): cast(int, count)
            for value, count in rows
            if value is not None
        }

    raw_kinds = await grouped(entries.c.kind, exclude="kinds")
    raw_statuses = await grouped(entries.c.status, exclude="statuses")
    return TraceFacets(
        kinds={TraceEntryKind(value): count for value, count in raw_kinds.items()},
        statuses={
            TraceEntryStatus(value): count for value, count in raw_statuses.items()
        },
        agents=await grouped(entries.c.agent_name, exclude="agent_names"),
        middleware=await grouped(
            entries.c.name,
            entries.c.kind == TraceEntryKind.MIDDLEWARE.value,
            exclude="middleware_names",
        ),
        skills=await grouped(
            entries.c.name,
            entries.c.kind == TraceEntryKind.SKILL.value,
            exclude="skill_names",
        ),
        providers=await grouped(entries.c.provider, exclude="providers"),
        models=await grouped(entries.c.model, exclude="models"),
    )


def _validate_entry_row(row: object, *, where: TraceFilter | None) -> None:
    mapping = cast(dict[str, object], row)
    required_pairs = (
        ("entry_id", "entry_hash"),
        ("name", "name_hash"),
        ("run_id", "run_hash"),
        ("graph_namespace", "graph_namespace_hash"),
    )
    optional_pairs = (
        ("parent_id", "parent_hash"),
        ("agent_name", "agent_hash"),
        ("provider", "provider_hash"),
        ("model", "model_hash"),
    )
    for value_column, hash_column in required_pairs:
        value = mapping[value_column]
        if not isinstance(value, str) or mapping[hash_column] != _digest(value):
            raise TraceStoreProtocolError("Trace entry searchable metadata conflicts")
    for value_column, hash_column in optional_pairs:
        value = mapping[value_column]
        digest = mapping[hash_column]
        if (value is None) != (digest is None) or (
            isinstance(value, str) and digest != _digest(value)
        ):
            raise TraceStoreProtocolError("Trace entry optional metadata conflicts")
    namespace = _decode_namespace(mapping["graph_namespace"])
    if where is None:
        return
    if where.parent_id is not None and mapping["parent_id"] != where.parent_id:
        raise TraceStoreProtocolError("Trace entry parent filter digest collision")
    if where.agent_names and mapping["agent_name"] not in where.agent_names:
        raise TraceStoreProtocolError("Trace entry Agent filter digest collision")
    if where.middleware_names and mapping["name"] not in where.middleware_names:
        raise TraceStoreProtocolError("Trace entry middleware filter digest collision")
    if where.skill_names and mapping["name"] not in where.skill_names:
        raise TraceStoreProtocolError("Trace entry Skill filter digest collision")
    if where.providers and mapping["provider"] not in where.providers:
        raise TraceStoreProtocolError("Trace entry provider filter digest collision")
    if where.models and mapping["model"] not in where.models:
        raise TraceStoreProtocolError("Trace entry model filter digest collision")
    if where.namespaces and namespace not in where.namespaces:
        raise TraceStoreProtocolError("Trace entry namespace filter digest collision")


def _stored_entry_from_row(
    row: object,
    event_records: dict[int, StoredTraceEvent],
) -> StoredTraceEntry:
    mapping = cast(dict[str, object], row)
    started_seq = cast(int, mapping["started_seq"])
    updated_seq = cast(int, mapping["updated_seq"])
    try:
        started_event = event_records[started_seq]
        updated_event = event_records[updated_seq]
    except KeyError as error:
        raise TraceStoreProtocolError(
            "Trace entry references an unavailable Ledger event",
            cause=error,
        ) from error
    try:
        kind = TraceEntryKind(cast(str, mapping["kind"]))
        status = TraceEntryStatus(cast(str, mapping["status"]))
    except ValueError as error:
        raise TraceStoreProtocolError(
            "Trace entry kind or status is invalid",
            cause=error,
        ) from error
    return StoredTraceEntry(
        entry_id=cast(str, mapping["entry_id"]),
        parent_id=cast(str | None, mapping["parent_id"]),
        kind=kind,
        status=status,
        name=cast(str, mapping["name"]),
        run_id=cast(str, mapping["run_id"]),
        namespace=_decode_namespace(mapping["graph_namespace"]),
        agent_name=cast(str | None, mapping["agent_name"]),
        provider=cast(str | None, mapping["provider"]),
        model=cast(str | None, mapping["model"]),
        started_at=_as_utc(cast(datetime, mapping["started_at"])),
        first_output_at=(
            None
            if mapping["first_output_at"] is None
            else _as_utc(cast(datetime, mapping["first_output_at"]))
        ),
        completed_at=(
            None
            if mapping["completed_at"] is None
            else _as_utc(cast(datetime, mapping["completed_at"]))
        ),
        started_seq=started_seq,
        updated_seq=updated_seq,
        started_event=started_event,
        updated_event=updated_event,
    )


def _stored_checkpoint_from_row(
    key: TraceThreadKey,
    row: object,
) -> StoredTraceCheckpoint:
    mapping = cast(dict[str, object], row)
    run_scope = cast(str, mapping["run_scope"])
    return StoredTraceCheckpoint(
        key=key,
        projection_name=cast(str, mapping["projection_name"]),
        run_id=run_scope or None,
        as_of_seq=cast(int, mapping["as_of_seq"]),
        canonical_state=bytes(cast(bytes, mapping["state_payload"])),
        state_digest=cast(str, mapping["state_digest"]),
    )


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _database_naive(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validate_thread_id(thread_id: str) -> None:
    if (
        not isinstance(thread_id, str)
        or not thread_id
        or thread_id != thread_id.strip()
        or len(thread_id) > 2048
    ):
        raise ValueError("thread_id must be canonical text of at most 2048 chars")


def _prepare_trace_schema(connection: Connection) -> None:
    """Create only a wholly new Trace Schema and reject partial existing storage."""

    table_names = set(inspect(connection).get_table_names())
    trace_owned = {name for name in table_names if name.startswith("tinkerfin_trace_")}
    if not trace_owned:
        metadata.create_all(connection)
    _validate_reflected_schema(connection)


def _validate_reflected_schema(connection: Connection) -> None:
    """Reject missing, stale, or foreign-key-coupled Trace-owned SQL objects.

    This gate runs before any create operation when Trace-owned tables already exist.
    It enforces the single-current-Schema rule and directs stale or partial storage to
    the controlled rebuild path instead of silently replacing lost evidence.
    """

    inspector = inspect(connection)
    table_names = set(inspector.get_table_names())
    owned_names = set(TRACE_TABLE_NAMES)
    unknown_owned = {
        name for name in table_names if name.startswith("tinkerfin_trace_")
    } - owned_names
    if unknown_owned:
        raise TraceStoreProtocolError("Unknown Trace-owned SQL tables exist")
    if not owned_names <= table_names:
        raise TraceStoreProtocolError("Trace Store SQL tables are incomplete")
    for table in metadata.sorted_tables:
        reflected_columns = {
            item["name"]: item for item in inspector.get_columns(table.name)
        }
        expected_names = tuple(column.name for column in table.columns)
        if set(reflected_columns) != set(expected_names):
            raise TraceStoreProtocolError(
                f"Trace Store table {table.name!r} has stale columns"
            )
        for column in table.columns:
            reflected = reflected_columns[column.name]
            if bool(reflected["nullable"]) != bool(column.nullable):
                raise TraceStoreProtocolError(
                    f"Trace Store column {table.name}.{column.name} has stale nullability"
                )
            expected_type = str(column.type.compile(dialect=connection.dialect)).upper()
            reflected_type = str(
                reflected["type"].compile(dialect=connection.dialect)
            ).upper()
            boolean_equivalent = expected_type in {"BOOL", "BOOLEAN"} and (
                reflected_type in {"BOOL", "BOOLEAN", "TINYINT(1)"}
            )
            if reflected_type != expected_type and not boolean_equivalent:
                raise TraceStoreProtocolError(
                    f"Trace Store column {table.name}.{column.name} has a stale type"
                )
            if (
                connection.dialect.name == "mysql"
                and reflected.get("comment") != column.comment
            ):
                raise TraceStoreProtocolError(
                    f"Trace Store column {table.name}.{column.name} has stale comment"
                )
        primary_key = inspector.get_pk_constraint(table.name)
        if tuple(primary_key.get("constrained_columns") or ()) != tuple(
            column.name for column in table.primary_key.columns
        ):
            raise TraceStoreProtocolError(
                f"Trace Store table {table.name!r} has a stale primary key"
            )
        if inspector.get_foreign_keys(table.name):
            raise TraceStoreProtocolError(
                f"Trace Store table {table.name!r} must not contain foreign keys"
            )
        if inspector.get_check_constraints(table.name):
            raise TraceStoreProtocolError(
                f"Trace Store table {table.name!r} must not contain check constraints"
            )
        reflected_indexes = {
            cast(str, item["name"]): (
                tuple(cast(list[str], item["column_names"])),
                bool(item.get("unique", False)),
            )
            for item in inspector.get_indexes(table.name)
        }
        expected_indexes = {
            cast(str, index.name): (
                tuple(column.name for column in index.columns),
                bool(index.unique),
            )
            for index in table.indexes
        }
        if reflected_indexes != expected_indexes:
            raise TraceStoreProtocolError(
                f"Trace Store table {table.name!r} has stale indexes"
            )
        if connection.dialect.name == "mysql":
            table_comment = inspector.get_table_comment(table.name).get("text")
            if table_comment != table.comment:
                raise TraceStoreProtocolError(
                    f"Trace Store table {table.name!r} has a stale comment"
                )


def _verify_thread_row(row: object, namespace: str, thread_id: str) -> None:
    mapping = cast(dict[str, object], row)
    if mapping["namespace"] != namespace or mapping["thread_id"] != thread_id:
        raise TraceStoreProtocolError("Trace thread digest collision")


def _writer_scope(store: _SqlAlchemyTraceLedgerBackend, key: TraceThreadKey):
    return and_(
        writers.c.namespace_hash == store._namespace_hash,
        writers.c.thread_hash == _digest(key.thread_id),
        writers.c.generation == key.generation,
    )


def _writer_where(
    store: _SqlAlchemyTraceLedgerBackend,
    key: TraceThreadKey,
    run_id: str,
):
    return and_(_writer_scope(store, key), writers.c.run_hash == _digest(run_id))


def _retryable(error: DBAPIError, *, dialect: str) -> bool:
    original = error.orig
    if dialect == "mysql":
        args = getattr(original, "args", ())
        # 1205/1213 are safe transaction rollbacks. Connection-loss codes can be
        # reported while COMMIT is in flight, so append/checkpoint closures must query
        # their retained id/digest evidence before inserting on the replacement link.
        return bool(args) and args[0] in {1205, 1213, 2006, 2013, 2055}
    code = getattr(original, "sqlite_errorcode", None)
    return code in {5, 6, 261, 262, 517, 773}


async def _database_now(connection: AsyncConnection) -> datetime:
    # MySQL CURRENT_TIMESTAMP follows the session time zone of the borrowed Engine.
    # UTC_TIMESTAMP is independent of host session configuration; SQLite's documented
    # CURRENT_TIMESTAMP is already UTC. Both remain database clocks for lease fencing.
    if connection.dialect.name == "mysql":
        value = await connection.scalar(text("SELECT UTC_TIMESTAMP(6)"))
    else:
        value = await connection.scalar(
            text("SELECT strftime('%Y-%m-%d %H:%M:%f', 'now')")
        )
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
    if not isinstance(value, datetime):
        raise TraceStoreProtocolError("database did not return a timestamp")
    return value.replace(tzinfo=None)


def _consume_task_exception(task: asyncio.Task[object]) -> None:
    """Retrieve retained task failures when every current waiter was cancelled."""

    if not task.cancelled():
        task.exception()


__all__ = ["SqlAlchemyTraceStore"]
