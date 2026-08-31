"""Borrowed-AsyncEngine SQLite and MySQL implementation of the Trace Store contract."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import TypeVar, cast

from sqlalchemy import and_, delete, desc, func, insert, inspect, select, text, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from .backend import (
    StoredTraceCheckpoint,
    StoredTraceEvent,
    StoredTraceEventPage,
    TraceCheckpointRequest,
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
        task, so another caller can observe its exact success or failure.
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
        await asyncio.shield(task)

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
        sqlite_busy_timeout: int | None = None
        try:
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
        except BaseException as error:
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
            if sqlite_busy_timeout is not None:
                try:
                    await connection.exec_driver_sql(
                        f"PRAGMA busy_timeout = {sqlite_busy_timeout}"
                    )
                except SQLAlchemyError:
                    # An invalidated connection cannot return to the pool; SQLAlchemy
                    # discards it, so no changed PRAGMA can leak to another borrower.
                    pass
            await connection.close()

    @asynccontextmanager
    async def _read_connection(self) -> AsyncGenerator[AsyncConnection, None]:
        """Read one fixed database snapshot and translate infrastructure failure.

        A host may configure MySQL for READ COMMITTED, which would let the metadata and
        active-writer statements observe different commits. The temporary connection
        option gives each Store read a REPEATABLE READ transaction; SQLAlchemy restores
        the borrowed pool connection's original isolation level on return.
        """

        try:
            async with self._engine.connect() as connection:
                if self._dialect == "mysql":
                    connection = await connection.execution_options(
                        isolation_level="REPEATABLE READ"
                    )
                    async with connection.begin():
                        yield connection
                else:
                    # Python's SQLite driver legacy mode does not BEGIN for SELECT.
                    # An explicit transaction fixes generation metadata and event rows
                    # to the same snapshot instead of allowing a concurrent append
                    # between statements.
                    await connection.exec_driver_sql("BEGIN")
                    try:
                        yield connection
                    finally:
                        if connection.in_transaction():
                            await connection.rollback()
        except SQLAlchemyError as error:
            raise TraceStoreError(
                "Trace Store database read failed",
                cause=error,
            ) from error


class SqlAlchemyTraceStore(DurableTraceStore):
    """Persist the framework-owned Trace Ledger through a borrowed SQLAlchemy Engine.

    SQLite and MySQL use the same public five-operation backend boundary and Ledger
    reducer as every other durable Store. The convenience Store owns no Engine resource
    and preserves the current five-table SQL shape.

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
