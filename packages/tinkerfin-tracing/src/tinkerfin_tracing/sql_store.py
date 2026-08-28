"""Borrowed-AsyncEngine SQLite and MySQL implementation of the Trace Store contract."""

from __future__ import annotations

import asyncio
import hashlib
import math
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TypeVar, cast
from uuid import uuid4

from sqlalchemy import and_, delete, desc, func, insert, inspect, select, text, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tinkerfin_contracts import RunIdentity

from .codec import CanonicalTracePayloadCodec
from .errors import (
    TraceProjectionCheckpointConflict,
    TraceQuotaExceeded,
    TraceRunConflict,
    TraceStoreError,
    TraceStoreProtocolError,
    TraceStoreTimeout,
    TraceThreadNotFound,
)
from .facts import RunFact, TraceEvent, TraceSemanticFact
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
    StoreThreadSnapshot,
    StoreWriterSnapshot,
    TraceProjectionCheckpoint,
    TraceThreadKey,
    TraceWriter,
    _checkpoint_lookup,
    _event,
)

_ResultT = TypeVar("_ResultT")


@dataclass(frozen=True, slots=True)
class SqlTraceStoreOptions:
    """Configure database-clock writer leases, polling, and bounded retries.

    Attributes:
        writer_lease_seconds: Database-clock ownership duration after each successful
            open, heartbeat, or append.
        heartbeat_interval_seconds: Delay between writer lease renewals. It must be
            shorter than the lease.
        follow_poll_seconds: Delay between empty cross-instance follow polls.
        retry_attempts: Total attempts for a retryable transaction, including the first.
        retry_delay_seconds: Base delay for linear retry backoff.
    """

    writer_lease_seconds: float = 30.0
    heartbeat_interval_seconds: float = 10.0
    follow_poll_seconds: float = 0.05
    retry_attempts: int = 5
    retry_delay_seconds: float = 0.02

    def __post_init__(self) -> None:
        """Normalize timing values and enforce lease/retry ordering constraints."""

        for name in (
            "writer_lease_seconds",
            "heartbeat_interval_seconds",
            "follow_poll_seconds",
            "retry_delay_seconds",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise TypeError(f"{name} must be numeric")
            if not math.isfinite(float(value)) or float(value) <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if self.heartbeat_interval_seconds >= self.writer_lease_seconds:
            raise ValueError("heartbeat interval must be shorter than writer lease")
        if isinstance(self.retry_attempts, bool) or not isinstance(
            self.retry_attempts, int
        ):
            raise TypeError("retry_attempts must be an integer")
        if self.retry_attempts < 1:
            raise ValueError("retry_attempts must be a positive integer")


class _SqlTraceWriter:
    """Own one leased SQL writer token and its heartbeat task."""

    def __init__(
        self,
        store: SqlAlchemyTraceStore,
        *,
        key: TraceThreadKey,
        run_id: str,
        owner_token: str,
        fence: int,
    ) -> None:
        self._store = store
        self._key = key
        self._run_id = run_id
        self._owner_token = owner_token
        self._fence = fence
        self._closed = False
        self._failure: BaseException | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._heartbeat = asyncio.create_task(
            self._heartbeat_forever(),
            name=f"tinkerfin-trace-sql-heartbeat:{run_id}",
        )
        self._heartbeat.add_done_callback(self._heartbeat_finished)

    @property
    def key(self) -> TraceThreadKey:
        """Return the exact generation protected by this lease."""

        return self._key

    @property
    def run_id(self) -> str:
        """Return the semantic Run protected by this lease."""

        return self._run_id

    async def append(
        self,
        facts: tuple[TraceSemanticFact, ...],
        *,
        mandatory: bool = False,
    ) -> tuple[TraceEvent, ...]:
        """Append under the current token/fence after checking heartbeat health."""

        if self._closed:
            raise TraceStoreProtocolError("Trace writer is closed")
        if self._failure is not None:
            raise TraceStoreProtocolError(
                "Trace writer heartbeat failed",
                cause=self._failure,
            )
        return await self._store._append_sql(
            self._key,
            run_id=self._run_id,
            owner_token=self._owner_token,
            fence=self._fence,
            facts=facts,
            mandatory=mandatory,
        )

    async def aclose(self) -> None:
        """Retain one close task and never dispose the borrowed Store Engine."""

        task = self._close_task
        if task is None:
            task = asyncio.create_task(
                self._close_once(),
                name=f"tinkerfin-trace-sql-writer-close:{self._run_id}",
            )
            task.add_done_callback(self._close_finished)
            self._close_task = task
        await asyncio.shield(task)

    async def _heartbeat_forever(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._store.options.heartbeat_interval_seconds)
                await self._store._renew_writer(
                    self._key,
                    run_id=self._run_id,
                    owner_token=self._owner_token,
                    fence=self._fence,
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - capture heartbeat health for append
            self._failure = error

    async def _close_once(self) -> None:
        self._closed = True
        self._heartbeat.cancel()
        await asyncio.gather(self._heartbeat, return_exceptions=True)
        await self._store._close_sql_writer(
            self._key,
            run_id=self._run_id,
            owner_token=self._owner_token,
            fence=self._fence,
        )

    @staticmethod
    def _heartbeat_finished(task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()

    @staticmethod
    def _close_finished(task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()


class SqlAlchemyTraceStore:
    """Persist Trace Ledger generations through one borrowed asynchronous Engine.

    The Store supports SQLite and MySQL only. It owns schema initialization and each
    returned writer owns its heartbeat task. The Store never changes pool sizing or
    disposes the host Engine. Contract coverage lives in
    ``tests/test_store_implementations.py``, ``tests/test_sqlite_store.py``, and
    ``tests/test_mysql_store.py``.

    Args:
        engine: Borrowed asynchronous Engine using ``sqlite+aiosqlite`` or
            ``mysql+asyncmy``.
        namespace: Stable logical isolation key shared by cooperating Store instances.
        limits: Ledger, event, thread, reserve, and follow capacity limits.
        options: Lease, polling, and retry settings.
        codec: Canonical fact and Projection-state codec. The supplied concrete codec
            remains borrowed and is never closed.

    Raises:
        TypeError: An argument has the wrong public type.
        ValueError: Namespace or Engine dialect is unsupported.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        namespace: str = "default",
        limits: TraceLimits | None = None,
        options: SqlTraceStoreOptions | None = None,
        codec: CanonicalTracePayloadCodec | None = None,
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
        if limits is not None and not isinstance(limits, TraceLimits):
            raise TypeError("limits must be a TraceLimits or None")
        if options is not None and not isinstance(options, SqlTraceStoreOptions):
            raise TypeError("options must be SqlTraceStoreOptions or None")
        if codec is not None and not isinstance(codec, CanonicalTracePayloadCodec):
            raise TypeError("codec must be CanonicalTracePayloadCodec or None")
        dialect = engine.url.get_backend_name()
        if dialect not in {"sqlite", "mysql"}:
            raise ValueError("SqlAlchemyTraceStore supports only SQLite and MySQL")
        self._engine = engine
        self._namespace = namespace
        self._namespace_hash = _digest(namespace)
        self._dialect = dialect
        self._limits = limits or TraceLimits()
        self._options = options or SqlTraceStoreOptions()
        self._codec = codec or CanonicalTracePayloadCodec()
        self._setup_lock = asyncio.Lock()
        self._setup_task: asyncio.Task[None] | None = None

    @property
    def namespace(self) -> str:
        """Return the immutable logical namespace."""

        return self._namespace

    @property
    def limits(self) -> TraceLimits:
        """Return limits enforced inside SQL transactions."""

        return self._limits

    @property
    def options(self) -> SqlTraceStoreOptions:
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
        for attempt in range(self._options.retry_attempts):
            try:
                if self._dialect == "mysql":
                    await self._setup_mysql()
                else:
                    async with self._raw_write_connection(
                        sqlite_fail_fast=False
                    ) as connection:
                        await connection.run_sync(metadata.create_all)
                        await connection.run_sync(_validate_reflected_schema)
                        await self._ensure_namespace(connection)
                return
            except DBAPIError as error:
                if not _retryable(error, dialect=self._dialect):
                    raise TraceStoreError(
                        "Trace Store schema setup failed",
                        cause=error,
                    ) from error
                if attempt + 1 >= self._options.retry_attempts:
                    raise TraceStoreTimeout(
                        "Trace Store schema setup retry budget was exhausted",
                        cause=error,
                    ) from error
                await asyncio.sleep(self._options.retry_delay_seconds * (attempt + 1))
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
                await connection.run_sync(metadata.create_all)
                await connection.run_sync(_validate_reflected_schema)
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

    async def open_writer(self, identity: RunIdentity) -> TraceWriter:
        """Create or take over one expired incomplete writer under fencing.

        Args:
            identity: Canonical Thread and Run identity for this writer.

        Returns:
            A writer bound to the current non-reusable generation and one fence token.

        Raises:
            TypeError: ``identity`` is not a RunIdentity.
            TraceRunConflict: The Run is live, complete, or already committed.
            TraceQuotaExceeded: Thread, event reserve, byte, or Tracer capacity cannot
                admit this Run.
            TraceStoreError: Database persistence is unavailable.
            TraceStoreProtocolError: Stored identity or namespace evidence conflicts.
        """

        if not isinstance(identity, RunIdentity):
            raise TypeError("identity must be a RunIdentity")
        await self.setup()
        thread_hash = _digest(identity.thread_id)
        run_hash = _digest(identity.run_id)
        owner_token = uuid4().hex

        async def operation(connection: AsyncConnection) -> tuple[TraceThreadKey, int]:
            now = await _database_now(connection)
            # Namespace-first locking makes thread creation and global quota admission
            # atomic across distinct thread rows. Every SQL write path that needs both
            # locks uses this order to avoid a cross-thread deadlock cycle.
            await self._locked_namespace(connection)
            row = (
                (
                    await connection.execute(
                        select(threads)
                        .where(
                            threads.c.namespace_hash == self._namespace_hash,
                            threads.c.thread_hash == thread_hash,
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                generation = uuid4().hex
                await connection.execute(
                    insert(threads).values(
                        namespace_hash=self._namespace_hash,
                        thread_hash=thread_hash,
                        namespace=self._namespace,
                        thread_id=identity.thread_id,
                        generation=generation,
                        next_seq=1,
                        persisted_bytes=0,
                        created_at=now,
                        updated_at=now,
                    )
                )
            else:
                _verify_thread_row(row, self._namespace, identity.thread_id)
                generation = cast(str, row["generation"])
            existing = (
                (
                    await connection.execute(
                        select(writers)
                        .where(
                            writers.c.namespace_hash == self._namespace_hash,
                            writers.c.thread_hash == thread_hash,
                            writers.c.generation == generation,
                            writers.c.run_hash == run_hash,
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            expires = now + timedelta(seconds=self._options.writer_lease_seconds)
            if existing is None:
                reserves = (
                    await connection.execute(
                        select(
                            func.coalesce(
                                func.sum(writers.c.remaining_event_reserve), 0
                            ),
                            func.coalesce(
                                func.sum(writers.c.remaining_byte_reserve), 0
                            ),
                        ).where(
                            writers.c.namespace_hash == self._namespace_hash,
                            writers.c.thread_hash == thread_hash,
                            writers.c.generation == generation,
                            writers.c.active.is_(True),
                        )
                    )
                ).one()
                if (
                    cast(int, row["next_seq"] if row is not None else 1)
                    - 1
                    + cast(int, reserves[0])
                    + self._limits.terminal_reserve_events_per_run
                    > self._limits.max_thread_events
                ):
                    raise TraceQuotaExceeded("Thread cannot reserve terminal events")
                if (
                    cast(int, row["persisted_bytes"] if row is not None else 0)
                    + cast(int, reserves[1])
                    + self._limits.terminal_reserve_bytes_per_run
                    > self._limits.max_thread_bytes
                ):
                    raise TraceQuotaExceeded("Thread cannot reserve terminal bytes")
                namespace_totals = (
                    await connection.execute(
                        select(
                            func.count(),
                            func.coalesce(func.sum(threads.c.persisted_bytes), 0),
                        ).where(threads.c.namespace_hash == self._namespace_hash)
                    )
                ).one()
                namespace_reserve = await connection.scalar(
                    select(
                        func.coalesce(func.sum(writers.c.remaining_byte_reserve), 0)
                    ).where(
                        writers.c.namespace_hash == self._namespace_hash,
                        writers.c.active.is_(True),
                    )
                )
                if (
                    row is None
                    and cast(int, namespace_totals[0]) > self._limits.max_tracer_threads
                ):
                    raise TraceQuotaExceeded("Tracer thread quota is exhausted")
                if (
                    cast(int, namespace_totals[1])
                    + cast(int, namespace_reserve)
                    + self._limits.terminal_reserve_bytes_per_run
                    > self._limits.max_tracer_bytes
                ):
                    raise TraceQuotaExceeded("Tracer cannot reserve terminal bytes")
                fence = 1
                await connection.execute(
                    insert(writers).values(
                        namespace_hash=self._namespace_hash,
                        thread_hash=thread_hash,
                        generation=generation,
                        run_hash=run_hash,
                        run_id=identity.run_id,
                        owner_token=owner_token,
                        fence=fence,
                        lease_expires_at=expires,
                        active=True,
                        terminal_committed=False,
                        closed_committed=False,
                        committed_events=0,
                        remaining_event_reserve=self._limits.terminal_reserve_events_per_run,
                        remaining_byte_reserve=self._limits.terminal_reserve_bytes_per_run,
                        created_at=now,
                        updated_at=now,
                    )
                )
            else:
                if existing["run_id"] != identity.run_id:
                    raise TraceStoreProtocolError("Trace Run digest collision")
                if (
                    existing["owner_token"] == owner_token
                    and bool(existing["active"])
                    and not bool(existing["closed_committed"])
                ):
                    # A connection can fail after COMMIT but before its response. The
                    # retry retains this attempt's random token, so equality proves the
                    # exact initial open or expired-writer takeover already committed.
                    fence = cast(int, existing["fence"])
                    return (
                        TraceThreadKey(
                            namespace=self._namespace,
                            thread_id=identity.thread_id,
                            generation=generation,
                        ),
                        fence,
                    )
                if not bool(existing["active"]) or bool(existing["closed_committed"]):
                    raise TraceRunConflict("Trace Run identity already exists")
                if cast(datetime, existing["lease_expires_at"]) > now:
                    raise TraceRunConflict("Trace Run writer is still active")
                fence = cast(int, existing["fence"]) + 1
                await connection.execute(
                    update(writers)
                    .where(
                        writers.c.namespace_hash == self._namespace_hash,
                        writers.c.thread_hash == thread_hash,
                        writers.c.generation == generation,
                        writers.c.run_hash == run_hash,
                    )
                    .values(
                        owner_token=owner_token,
                        fence=fence,
                        lease_expires_at=expires,
                        active=True,
                        updated_at=now,
                    )
                )
            return (
                TraceThreadKey(
                    namespace=self._namespace,
                    thread_id=identity.thread_id,
                    generation=generation,
                ),
                fence,
            )

        key, fence = await self._write(operation)
        return _SqlTraceWriter(
            self,
            key=key,
            run_id=identity.run_id,
            owner_token=owner_token,
            fence=fence,
        )

    async def snapshot(self, thread_id: str) -> StoreThreadSnapshot:
        """Return current generation metadata using the database clock for leases.

        Args:
            thread_id: Canonical Thread identity in this Store namespace.

        Returns:
            Fixed prefix, persisted-byte, generation, and live-writer metadata from one
            repeatable-read snapshot.

        Raises:
            ValueError: ``thread_id`` is not canonical bounded text.
            TraceThreadNotFound: The Thread does not exist.
            TraceStoreError: The database read fails.
            TraceStoreProtocolError: Stored identity evidence conflicts.
        """

        _validate_thread_id(thread_id)
        await self.setup()
        async with self._read_connection() as connection:
            row = await self._thread_row(connection, thread_id=thread_id)
            key = TraceThreadKey(
                namespace=self._namespace,
                thread_id=thread_id,
                generation=cast(str, row["generation"]),
            )
            return await self._snapshot_row(connection, key, row)

    async def snapshot_key(self, key: TraceThreadKey) -> StoreThreadSnapshot:
        """Return metadata for one exact generation or reject a stale handle.

        Args:
            key: Exact namespace, Thread, and generation handle.

        Returns:
            Metadata for the key's fixed current observation point.

        Raises:
            TypeError: ``key`` is not a TraceThreadKey.
            TraceThreadNotFound: Namespace, Thread, or generation is not current.
            TraceStoreError: The database read fails.
            TraceStoreProtocolError: Stored identity evidence conflicts.
        """

        self._validate_key(key)
        await self.setup()
        async with self._read_connection() as connection:
            row = await self._thread_row(connection, thread_id=key.thread_id)
            if row["generation"] != key.generation:
                raise TraceThreadNotFound("Trace generation does not exist")
            return await self._snapshot_row(connection, key, row)

    async def _snapshot_row(
        self,
        connection: AsyncConnection,
        key: TraceThreadKey,
        row: object,
    ) -> StoreThreadSnapshot:
        """Build one lease-aware metadata view inside the caller's read snapshot."""

        mapping = cast(dict[str, object], row)
        now = await _database_now(connection)
        active_rows = (
            await connection.execute(
                select(writers.c.run_id, writers.c.committed_events)
                .where(
                    writers.c.namespace_hash == self._namespace_hash,
                    writers.c.thread_hash == _digest(key.thread_id),
                    writers.c.generation == key.generation,
                    writers.c.active.is_(True),
                    writers.c.lease_expires_at > now,
                )
                .order_by(writers.c.run_id)
            )
        ).all()
        return StoreThreadSnapshot(
            key=key,
            as_of_seq=cast(int, mapping["next_seq"]) - 1,
            persisted_bytes=cast(int, mapping["persisted_bytes"]),
            active_writers=tuple(
                StoreWriterSnapshot(
                    run_id=cast(str, item.run_id),
                    committed_events=cast(int, item.committed_events),
                )
                for item in active_rows
            ),
        )

    async def read_events(
        self,
        key: TraceThreadKey,
        *,
        after_seq: int,
        as_of_seq: int,
        limit: int,
    ) -> tuple[TraceEvent, ...]:
        """Read one ascending bounded page from an exact generation.

        Args:
            key: Exact generation handle.
            after_seq: Exclusive non-negative lower sequence.
            as_of_seq: Inclusive fixed-prefix upper sequence.
            limit: Positive maximum number of events returned.

        Returns:
            Defensive immutable events ordered by increasing global sequence.

        Raises:
            TypeError: ``key`` has the wrong type.
            ValueError: A cursor or limit is outside the bounded contract.
            TraceThreadNotFound: The exact generation is unavailable.
            TraceStoreError: The database read fails.
            TraceStoreProtocolError: Payload digest, canonical data, or searchable
                metadata is corrupt.
        """

        self._validate_key(key)
        if after_seq < 0 or as_of_seq < 0 or limit < 1:
            raise ValueError("event cursor values must be non-negative and bounded")
        await self.setup()
        async with self._read_connection() as connection:
            await self._require_key(connection, key)
            rows = (
                (
                    await connection.execute(
                        select(events)
                        .where(
                            events.c.namespace_hash == self._namespace_hash,
                            events.c.thread_hash == _digest(key.thread_id),
                            events.c.generation == key.generation,
                            events.c.trace_seq > after_seq,
                            events.c.trace_seq <= as_of_seq,
                        )
                        .order_by(events.c.trace_seq)
                        .limit(limit)
                    )
                )
                .mappings()
                .all()
            )
            return tuple(self._decode_event(key, row) for row in rows)

    async def read_events_reverse(
        self,
        key: TraceThreadKey,
        *,
        before_seq: int,
        limit: int,
    ) -> tuple[TraceEvent, ...]:
        """Read one newest-first bounded page below an exclusive sequence.

        Args:
            key: Exact generation handle.
            before_seq: Positive exclusive upper sequence at or below tail plus one.
            limit: Positive maximum number of events returned.

        Returns:
            Defensive immutable events ordered by decreasing global sequence.

        Raises:
            TypeError: ``key`` has the wrong type.
            ValueError: Cursor or limit is outside the current generation.
            TraceThreadNotFound: The exact generation is unavailable.
            TraceStoreError: The database read fails.
            TraceStoreProtocolError: Stored payload or metadata is corrupt.
        """

        self._validate_key(key)
        if before_seq < 1 or limit < 1:
            raise ValueError("reverse event cursor values must be positive")
        await self.setup()
        async with self._read_connection() as connection:
            snapshot = await self._thread_row(connection, thread_id=key.thread_id)
            if snapshot["generation"] != key.generation:
                raise TraceThreadNotFound("Trace generation does not exist")
            if before_seq > cast(int, snapshot["next_seq"]):
                raise ValueError("before_seq exceeds the current Trace tail")
            rows = (
                (
                    await connection.execute(
                        select(events)
                        .where(
                            events.c.namespace_hash == self._namespace_hash,
                            events.c.thread_hash == _digest(key.thread_id),
                            events.c.generation == key.generation,
                            events.c.trace_seq < before_seq,
                        )
                        .order_by(desc(events.c.trace_seq))
                        .limit(limit)
                    )
                )
                .mappings()
                .all()
            )
            return tuple(self._decode_event(key, row) for row in rows)

    async def _append_sql(
        self,
        key: TraceThreadKey,
        *,
        run_id: str,
        owner_token: str,
        fence: int,
        facts: tuple[TraceSemanticFact, ...],
        mandatory: bool,
    ) -> tuple[TraceEvent, ...]:
        """Commit canonical facts under namespace, thread, and writer fencing.

        Event IDs and encoded payloads are allocated before entering ``_write`` and are
        retained across every retry. The operation can therefore distinguish a lost
        commit response from a new append. Namespace-first locking protects global
        quota accounting; the thread row allocates a continuous sequence; the writer
        row enforces lease, fence, terminal reserve, and exactly-once transition
        ownership.

        Args:
            key: Exact thread generation owned by the writer.
            run_id: Run whose writer row must remain active and fenced.
            owner_token: Private lease token returned when the writer opened.
            fence: Monotonic ownership generation paired with the token.
            facts: Ordered semantic facts accepted as one atomic transaction.
            mandatory: Whether terminal reserve, rather than ordinary capacity, applies.

        Returns:
            Immutable committed events in input fact order.

        Raises:
            TraceStoreProtocolError: Ownership, capacity, terminal, or idempotency
                invariants fail.
            TraceStoreError: The bounded database operation cannot be completed or
                proven committed.
        """

        if not facts:
            return ()
        source_payloads = tuple(
            (uuid4().hex, fact, self._codec.encode_fact(fact)) for fact in facts
        )

        async def operation(connection: AsyncConnection) -> tuple[TraceEvent, ...]:
            now = await _database_now(connection)
            await self._locked_namespace(connection)
            thread = await self._locked_thread(connection, key)
            writer = await self._locked_writer(connection, key, run_id=run_id)
            existing_rows = (
                (
                    await connection.execute(
                        select(events).where(
                            events.c.namespace_hash == self._namespace_hash,
                            events.c.event_id.in_(
                                tuple(item[0] for item in source_payloads)
                            ),
                        )
                    )
                )
                .mappings()
                .all()
            )
            if existing_rows:
                by_id = {cast(str, row["event_id"]): row for row in existing_rows}
                if len(by_id) != len(source_payloads) or any(
                    event_id not in by_id
                    or by_id[event_id]["payload_digest"] != encoded.digest
                    for event_id, _fact_value, encoded in source_payloads
                ):
                    raise TraceStoreProtocolError(
                        "Trace event idempotency evidence conflicts"
                    )
                return tuple(
                    self._decode_event(key, by_id[event_id])
                    for event_id, _fact_value, _encoded in source_payloads
                )
            _require_writer_owner(writer, owner_token=owner_token, fence=fence, now=now)
            next_seq = cast(int, thread["next_seq"])
            prepared: list[tuple[TraceEvent, bytes, str]] = []
            for event_id, fact, encoded in source_payloads:
                if (
                    fact.identity.thread_id != key.thread_id
                    or fact.identity.run_id != run_id
                ):
                    raise TraceStoreProtocolError(
                        "Trace writer fact identity does not match"
                    )
                event = _event(
                    event_id=event_id,
                    trace_seq=next_seq,
                    generation=key.generation,
                    fact=fact,
                )
                if event.persisted_bytes > self._limits.max_event_bytes:
                    raise TraceQuotaExceeded("Trace event exceeds max_event_bytes")
                prepared.append((event, encoded.data, encoded.digest))
                next_seq += 1
            added_bytes = sum(item[0].persisted_bytes for item in prepared)
            terminal_batch = all(
                isinstance(item.fact, RunFact)
                and item.fact.phase in {"terminal", "closed"}
                for item, _payload, _digest_value in prepared
            )
            if mandatory != terminal_batch:
                raise TraceStoreProtocolError(
                    "mandatory append is reserved for terminal facts"
                )
            terminal_committed = bool(writer["terminal_committed"])
            closed_committed = bool(writer["closed_committed"])
            if mandatory:
                phases = tuple(
                    cast(RunFact, item.fact).phase
                    for item, _payload, _digest_value in prepared
                )
                if phases.count("terminal") > 1 or phases.count("closed") > 1:
                    raise TraceStoreProtocolError(
                        "Run terminal and closed facts must be exactly-once"
                    )
                if terminal_committed and "terminal" in phases:
                    raise TraceStoreProtocolError(
                        "Run terminal fact is already committed"
                    )
                if closed_committed:
                    raise TraceStoreProtocolError(
                        "Run closed fact is already committed"
                    )
                if "closed" in phases and not (
                    terminal_committed
                    or (
                        "terminal" in phases
                        and phases.index("terminal") < phases.index("closed")
                    )
                ):
                    raise TraceStoreProtocolError(
                        "Run closed fact requires a committed terminal fact"
                    )
                terminal_committed = terminal_committed or "terminal" in phases
                closed_committed = "closed" in phases
            elif terminal_committed or closed_committed:
                raise TraceStoreProtocolError(
                    "Ordinary Trace facts cannot follow the Run terminal"
                )
            remaining_events = cast(int, writer["remaining_event_reserve"])
            remaining_bytes = cast(int, writer["remaining_byte_reserve"])
            if mandatory and (
                len(prepared) > remaining_events or added_bytes > remaining_bytes
            ):
                raise TraceQuotaExceeded("Run terminal reserve is exhausted")
            event_limit = self._limits.max_thread_events
            byte_limit = self._limits.max_thread_bytes
            if not mandatory:
                reserves = (
                    await connection.execute(
                        select(
                            func.coalesce(
                                func.sum(writers.c.remaining_event_reserve), 0
                            ),
                            func.coalesce(
                                func.sum(writers.c.remaining_byte_reserve), 0
                            ),
                        ).where(
                            writers.c.namespace_hash == self._namespace_hash,
                            writers.c.thread_hash == _digest(key.thread_id),
                            writers.c.generation == key.generation,
                            writers.c.active.is_(True),
                        )
                    )
                ).one()
                event_limit -= cast(int, reserves[0])
                byte_limit -= cast(int, reserves[1])
            if next_seq - 1 > event_limit:
                raise TraceQuotaExceeded("Thread event quota is exhausted")
            if cast(int, thread["persisted_bytes"]) + added_bytes > byte_limit:
                raise TraceQuotaExceeded("Thread byte quota is exhausted")
            total_bytes = await connection.scalar(
                select(func.coalesce(func.sum(threads.c.persisted_bytes), 0)).where(
                    threads.c.namespace_hash == self._namespace_hash
                )
            )
            total_reserve = await connection.scalar(
                select(
                    func.coalesce(func.sum(writers.c.remaining_byte_reserve), 0)
                ).where(
                    writers.c.namespace_hash == self._namespace_hash,
                    writers.c.active.is_(True),
                )
            )
            tracer_limit = self._limits.max_tracer_bytes
            if not mandatory:
                tracer_limit -= cast(int, total_reserve)
            if cast(int, total_bytes) + added_bytes > tracer_limit:
                raise TraceQuotaExceeded("Tracer byte quota is exhausted")
            for event, payload, digest_value in prepared:
                await connection.execute(
                    insert(events).values(
                        namespace_hash=self._namespace_hash,
                        thread_hash=_digest(key.thread_id),
                        generation=key.generation,
                        trace_seq=event.trace_seq,
                        event_id=event.event_id,
                        run_hash=_digest(run_id),
                        run_id=run_id,
                        fact_kind=event.fact.kind,
                        occurred_at=event.fact.occurred_at.astimezone(UTC).replace(
                            tzinfo=None
                        ),
                        payload=payload,
                        payload_digest=digest_value,
                        persisted_bytes=event.persisted_bytes,
                        created_at=now,
                    )
                )
            await connection.execute(
                update(threads)
                .where(
                    threads.c.namespace_hash == self._namespace_hash,
                    threads.c.thread_hash == _digest(key.thread_id),
                    threads.c.generation == key.generation,
                )
                .values(
                    next_seq=next_seq,
                    persisted_bytes=cast(int, thread["persisted_bytes"]) + added_bytes,
                    updated_at=now,
                )
            )
            writer_values: dict[str, object] = {
                "committed_events": cast(int, writer["committed_events"])
                + len(prepared),
                "lease_expires_at": now
                + timedelta(seconds=self._options.writer_lease_seconds),
                "terminal_committed": terminal_committed,
                "closed_committed": closed_committed,
                "updated_at": now,
            }
            if mandatory:
                if closed_committed:
                    writer_values.update(
                        remaining_event_reserve=0,
                        remaining_byte_reserve=0,
                    )
                else:
                    writer_values.update(
                        remaining_event_reserve=remaining_events - len(prepared),
                        remaining_byte_reserve=remaining_bytes - added_bytes,
                    )
            await connection.execute(
                update(writers)
                .where(
                    _writer_where(self, key, run_id),
                    writers.c.owner_token == owner_token,
                    writers.c.fence == fence,
                )
                .values(**writer_values)
            )
            return tuple(
                event.model_copy(deep=True)
                for event, _payload, _digest_value in prepared
            )

        return await self._write(operation)

    def follow(
        self, key: TraceThreadKey, *, after_seq: int
    ) -> AsyncGenerator[tuple[TraceEvent, ...], None]:
        """Poll bounded cross-instance pages until cancellation or deletion.

        Args:
            key: Exact generation handle.
            after_seq: Last sequence already observed by the caller.

        Returns:
            Cancellation-responsive async iterator of non-empty ordered batches.

        Raises:
            TypeError: ``key`` has the wrong type.
            ValueError: ``after_seq`` is negative.
            TraceThreadNotFound: Iteration observes generation deletion.
            TraceStoreError: A polling read fails.
            TraceStoreProtocolError: Stored event evidence is corrupt.
        """

        self._validate_key(key)
        if after_seq < 0:
            raise ValueError("after_seq must be non-negative")

        async def iterate() -> AsyncGenerator[tuple[TraceEvent, ...], None]:
            cursor = after_seq
            while True:
                snapshot = await self.snapshot_key(key)
                batch = await self.read_events(
                    key,
                    after_seq=cursor,
                    as_of_seq=snapshot.as_of_seq,
                    limit=self._limits.follow_batch_size,
                )
                if batch:
                    cursor = batch[-1].trace_seq
                    yield batch
                    continue
                await asyncio.sleep(self._options.follow_poll_seconds)

        return iterate()

    async def delete(self, key: TraceThreadKey) -> None:
        """Delete one inactive exact generation without touching another namespace.

        Args:
            key: Exact generation selected for deletion.

        Raises:
            TypeError: ``key`` has the wrong type.
            TraceThreadNotFound: The exact generation is unavailable.
            TraceRunConflict: A writer still holds an unexpired lease.
            TraceStoreError: The delete transaction fails.
            TraceStoreProtocolError: Stored identity evidence conflicts.
        """

        self._validate_key(key)

        async def operation(connection: AsyncConnection) -> None:
            now = await _database_now(connection)
            await self._locked_namespace(connection)
            await self._locked_thread(connection, key)
            active = await connection.scalar(
                select(func.count())
                .select_from(writers)
                .where(
                    writers.c.namespace_hash == self._namespace_hash,
                    writers.c.thread_hash == _digest(key.thread_id),
                    writers.c.generation == key.generation,
                    writers.c.active.is_(True),
                    writers.c.lease_expires_at > now,
                )
            )
            if cast(int, active) > 0:
                raise TraceRunConflict("Active Trace writers prevent deletion")
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

        await self._write(operation)

    async def _renew_writer(
        self, key: TraceThreadKey, *, run_id: str, owner_token: str, fence: int
    ) -> None:
        """Renew only a still-unexpired token/fence using the database clock."""

        async def operation(connection: AsyncConnection) -> None:
            now = await _database_now(connection)
            result = await connection.execute(
                update(writers)
                .where(
                    _writer_where(self, key, run_id),
                    writers.c.owner_token == owner_token,
                    writers.c.fence == fence,
                    writers.c.active.is_(True),
                    writers.c.lease_expires_at > now,
                )
                .values(
                    lease_expires_at=now
                    + timedelta(seconds=self._options.writer_lease_seconds),
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                raise TraceStoreProtocolError("Trace writer lease ownership was lost")

        await self._write(operation)

    async def _close_sql_writer(
        self, key: TraceThreadKey, *, run_id: str, owner_token: str, fence: int
    ) -> None:
        """Release matching database ownership without disturbing a newer lease.

        Closing is local-resource cleanup as well as a best-effort database release.
        A replacement may already own the row after this writer's lease expired, so a
        token/fence mismatch is the idempotent success case for this method. Append and
        heartbeat paths still fail closed on that mismatch; close alone must never
        deactivate or delete the replacement's row.
        """

        async def operation(connection: AsyncConnection) -> None:
            await self._locked_namespace(connection)
            thread = (
                (
                    await connection.execute(
                        select(threads)
                        .where(
                            threads.c.namespace_hash == self._namespace_hash,
                            threads.c.thread_hash == _digest(key.thread_id),
                            threads.c.generation == key.generation,
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if thread is None:
                return
            _verify_thread_row(thread, self._namespace, key.thread_id)
            row = (
                (
                    await connection.execute(
                        select(writers)
                        .where(_writer_where(self, key, run_id))
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return
            if row["run_id"] != run_id:
                raise TraceStoreProtocolError("Trace writer Run digest collision")
            if (
                row["owner_token"] != owner_token
                or row["fence"] != fence
                or not bool(row["active"])
            ):
                return
            where = and_(
                _writer_where(self, key, run_id),
                writers.c.owner_token == owner_token,
                writers.c.fence == fence,
                writers.c.active.is_(True),
            )
            if cast(int, row["committed_events"]) == 0:
                await connection.execute(delete(writers).where(where))
                remaining_writers = await connection.scalar(
                    select(func.count())
                    .select_from(writers)
                    .where(_writer_scope(self, key))
                )
                if (
                    cast(int, thread["next_seq"]) == 1
                    and cast(int, remaining_writers) == 0
                ):
                    await connection.execute(
                        delete(threads).where(
                            threads.c.namespace_hash == self._namespace_hash,
                            threads.c.thread_hash == _digest(key.thread_id),
                            threads.c.generation == key.generation,
                        )
                    )
            else:
                await connection.execute(
                    update(writers)
                    .where(where)
                    .values(
                        active=False,
                        remaining_event_reserve=0,
                        remaining_byte_reserve=0,
                    )
                )

        await self._write(operation)

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

    async def _locked_thread(self, connection: AsyncConnection, key: TraceThreadKey):
        row = (
            (
                await connection.execute(
                    select(threads)
                    .where(
                        threads.c.namespace_hash == self._namespace_hash,
                        threads.c.thread_hash == _digest(key.thread_id),
                        threads.c.generation == key.generation,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise TraceThreadNotFound("Trace generation does not exist")
        _verify_thread_row(row, self._namespace, key.thread_id)
        return row

    async def _locked_writer(
        self, connection: AsyncConnection, key: TraceThreadKey, *, run_id: str
    ):
        row = (
            (
                await connection.execute(
                    select(writers)
                    .where(_writer_where(self, key, run_id))
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None or row["run_id"] != run_id:
            raise TraceStoreProtocolError("Trace writer ownership does not exist")
        return row

    def _decode_event(self, key: TraceThreadKey, row: object) -> TraceEvent:
        mapping = cast(dict[str, object], row)
        payload = bytes(cast(bytes, mapping["payload"]))
        if self._codec.digest(payload) != mapping["payload_digest"]:
            raise TraceStoreProtocolError("Trace event payload digest mismatch")
        fact = self._codec.decode_fact(payload)
        if (
            fact.identity.thread_id != key.thread_id
            or fact.identity.run_id != mapping["run_id"]
            or mapping["run_hash"] != _digest(fact.identity.run_id)
            or mapping["fact_kind"] != fact.kind
            or mapping["occurred_at"]
            != fact.occurred_at.astimezone(UTC).replace(tzinfo=None)
        ):
            raise TraceStoreProtocolError(
                "Trace event searchable metadata conflicts with payload"
            )
        event = _event(
            event_id=cast(str, mapping["event_id"]),
            trace_seq=cast(int, mapping["trace_seq"]),
            generation=key.generation,
            fact=fact,
        )
        if event.persisted_bytes != mapping["persisted_bytes"]:
            raise TraceStoreProtocolError("Trace event size metadata conflicts")
        return event

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
        for attempt in range(self._options.retry_attempts):
            try:
                async with self._raw_write_connection() as connection:
                    return await operation(connection)
            except DBAPIError as error:
                if not _retryable(error, dialect=self._dialect):
                    raise TraceStoreError(
                        "Trace Store database operation failed",
                        cause=error,
                    ) from error
                if attempt + 1 >= self._options.retry_attempts:
                    raise TraceStoreTimeout(
                        "Trace Store retry budget was exhausted",
                        cause=error,
                    ) from error
                await asyncio.sleep(self._options.retry_delay_seconds * (attempt + 1))
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
        except SQLAlchemyError as error:
            raise TraceStoreError(
                "Trace Store database read failed",
                cause=error,
            ) from error

    async def load_projection_checkpoint(
        self,
        key: TraceThreadKey,
        *,
        projection_name: str,
        run_id: str | None,
        as_of_seq: int,
    ) -> TraceProjectionCheckpoint | None:
        """Load the newest disposable cache state inside one fixed prefix.

        Args:
            key: Exact generation handle.
            projection_name: Canonical Projection registration name.
            run_id: Optional Run-scoped cache key.
            as_of_seq: Inclusive fixed-prefix upper bound.

        Returns:
            A defensive checkpoint copy, or ``None`` when no state is in range.

        Raises:
            TypeError: An argument has the wrong public type.
            ValueError: An identifier or sequence is invalid.
            TraceThreadNotFound: The exact generation is unavailable.
            TraceStoreError: The database read fails.
            TraceStoreProtocolError: Identity, digest, canonical state, or stored
                searchable metadata conflicts.
        """

        self._validate_key(key)
        _checkpoint_lookup(
            key,
            projection_name=projection_name,
            run_id=run_id,
            as_of_seq=as_of_seq,
        )
        await self.setup()
        scope = run_id or ""
        async with self._read_connection() as connection:
            await self._require_key(connection, key)
            row = (
                (
                    await connection.execute(
                        select(projection_checkpoints)
                        .where(
                            projection_checkpoints.c.namespace_hash
                            == self._namespace_hash,
                            projection_checkpoints.c.thread_hash
                            == _digest(key.thread_id),
                            projection_checkpoints.c.generation == key.generation,
                            projection_checkpoints.c.projection_hash
                            == _digest(projection_name),
                            projection_checkpoints.c.run_scope_hash == _digest(scope),
                            projection_checkpoints.c.as_of_seq <= as_of_seq,
                        )
                        .order_by(desc(projection_checkpoints.c.as_of_seq))
                        .limit(1)
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            if row["projection_name"] != projection_name or row["run_scope"] != scope:
                raise TraceStoreProtocolError("Projection checkpoint digest collision")
            payload = bytes(cast(bytes, row["state_payload"]))
            if self._codec.digest(payload) != row["state_digest"]:
                raise TraceStoreProtocolError("Projection checkpoint digest mismatch")
            return TraceProjectionCheckpoint(
                key=key,
                projection_name=projection_name,
                run_id=run_id,
                as_of_seq=cast(int, row["as_of_seq"]),
                state=self._codec.decode_json(payload),
            )

    async def save_projection_checkpoint(
        self, checkpoint: TraceProjectionCheckpoint, *, expected_as_of_seq: int | None
    ) -> TraceProjectionCheckpoint:
        """Compare-and-swap a disposable checkpoint with unknown-commit proof.

        Repeating the exact current identity, prefix, and canonical state succeeds even
        when ``expected_as_of_seq`` names its predecessor. This is the only idempotent
        exception to strict forward advancement and lets a caller verify a commit whose
        response was lost.

        Args:
            checkpoint: Exact generation, Projection scope, prefix, and finite state.
            expected_as_of_seq: Current predecessor prefix, or ``None`` for first state.

        Returns:
            A defensive copy of the committed or already-proven checkpoint.

        Raises:
            TypeError: ``checkpoint`` has the wrong type.
            ValueError: ``expected_as_of_seq`` is negative.
            TraceThreadNotFound: The exact generation is unavailable.
            TraceProjectionCheckpointConflict: CAS predecessor or forward advancement
                does not match.
            TraceStoreError: The database transaction fails.
            TraceStoreProtocolError: State exceeds the Ledger prefix or identity,
                digest, canonical data, or idempotency evidence conflicts.
        """

        if not isinstance(checkpoint, TraceProjectionCheckpoint):
            raise TypeError("checkpoint must be a TraceProjectionCheckpoint")
        self._validate_key(checkpoint.key)
        if expected_as_of_seq is not None and expected_as_of_seq < 0:
            raise ValueError("expected_as_of_seq must be non-negative or None")
        encoded = self._codec.encode_json(checkpoint.state)

        async def operation(connection: AsyncConnection) -> TraceProjectionCheckpoint:
            thread = await self._locked_thread(connection, checkpoint.key)
            if checkpoint.as_of_seq > cast(int, thread["next_seq"]) - 1:
                raise TraceStoreProtocolError(
                    "Projection checkpoint exceeds the committed Trace prefix"
                )
            scope = checkpoint.run_id or ""
            latest = await connection.scalar(
                select(func.max(projection_checkpoints.c.as_of_seq)).where(
                    projection_checkpoints.c.namespace_hash == self._namespace_hash,
                    projection_checkpoints.c.thread_hash
                    == _digest(checkpoint.key.thread_id),
                    projection_checkpoints.c.generation == checkpoint.key.generation,
                    projection_checkpoints.c.projection_hash
                    == _digest(checkpoint.projection_name),
                    projection_checkpoints.c.run_scope_hash == _digest(scope),
                )
            )
            if latest is not None:
                identity_row = (
                    await connection.execute(
                        select(
                            projection_checkpoints.c.projection_name,
                            projection_checkpoints.c.run_scope,
                        )
                        .where(
                            projection_checkpoints.c.namespace_hash
                            == self._namespace_hash,
                            projection_checkpoints.c.thread_hash
                            == _digest(checkpoint.key.thread_id),
                            projection_checkpoints.c.generation
                            == checkpoint.key.generation,
                            projection_checkpoints.c.projection_hash
                            == _digest(checkpoint.projection_name),
                            projection_checkpoints.c.run_scope_hash == _digest(scope),
                        )
                        .limit(1)
                    )
                ).one()
                if (
                    identity_row.projection_name != checkpoint.projection_name
                    or identity_row.run_scope != scope
                ):
                    raise TraceStoreProtocolError(
                        "Projection checkpoint digest collision"
                    )
            if latest == checkpoint.as_of_seq:
                stored_state = (
                    await connection.execute(
                        select(
                            projection_checkpoints.c.state_payload,
                            projection_checkpoints.c.state_digest,
                        ).where(
                            projection_checkpoints.c.namespace_hash
                            == self._namespace_hash,
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
                ).one()
                stored_payload = bytes(cast(bytes, stored_state.state_payload))
                if self._codec.digest(stored_payload) != stored_state.state_digest:
                    raise TraceStoreProtocolError(
                        "Projection checkpoint payload digest mismatch"
                    )
                if stored_state.state_digest != encoded.digest:
                    raise TraceStoreProtocolError(
                        "Projection checkpoint idempotency evidence conflicts"
                    )
                return checkpoint.model_copy(deep=True)
            if latest != expected_as_of_seq:
                raise TraceProjectionCheckpointConflict(
                    "Projection checkpoint compare-and-swap failed",
                    context={
                        "projection": checkpoint.projection_name,
                        "current_as_of_seq": cast(int | None, latest),
                    },
                )
            if latest is not None and checkpoint.as_of_seq <= cast(int, latest):
                raise TraceProjectionCheckpointConflict(
                    "Projection checkpoint must advance its fixed prefix",
                    context={
                        "projection": checkpoint.projection_name,
                        "current_as_of_seq": cast(int, latest),
                    },
                )
            now = await _database_now(connection)
            await connection.execute(
                insert(projection_checkpoints).values(
                    namespace_hash=self._namespace_hash,
                    thread_hash=_digest(checkpoint.key.thread_id),
                    generation=checkpoint.key.generation,
                    projection_hash=_digest(checkpoint.projection_name),
                    run_scope_hash=_digest(scope),
                    projection_name=checkpoint.projection_name,
                    run_scope=scope,
                    as_of_seq=checkpoint.as_of_seq,
                    state_payload=encoded.data,
                    state_digest=encoded.digest,
                    created_at=now,
                )
            )
            return checkpoint.model_copy(deep=True)

        return await self._write(operation)


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


def _validate_reflected_schema(connection: Connection) -> None:
    """Reject missing, stale, or foreign-key-coupled Trace-owned SQL objects.

    ``create_all`` intentionally does not migrate an existing table. This reflection
    gate therefore enforces the repository's single-current-Schema rule and directs a
    deployment with stale Trace tables to the controlled rebuild path instead of
    failing later in an Agent transaction.
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


def _writer_scope(store: SqlAlchemyTraceStore, key: TraceThreadKey):
    return and_(
        writers.c.namespace_hash == store._namespace_hash,
        writers.c.thread_hash == _digest(key.thread_id),
        writers.c.generation == key.generation,
    )


def _writer_where(store: SqlAlchemyTraceStore, key: TraceThreadKey, run_id: str):
    return and_(_writer_scope(store, key), writers.c.run_hash == _digest(run_id))


def _require_writer_owner(
    row: object,
    *,
    owner_token: str,
    fence: int,
    now: datetime | None,
) -> None:
    mapping = cast(dict[str, object], row)
    if (
        mapping["owner_token"] != owner_token
        or mapping["fence"] != fence
        or not bool(mapping["active"])
        or (now is not None and cast(datetime, mapping["lease_expires_at"]) <= now)
    ):
        raise TraceStoreProtocolError("Trace writer lease ownership was lost")


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
        value = await connection.scalar(select(func.current_timestamp()))
    if not isinstance(value, datetime):
        raise TraceStoreProtocolError("database did not return a timestamp")
    return value.replace(tzinfo=None)


def _consume_task_exception(task: asyncio.Task[object]) -> None:
    """Retrieve retained task failures when every current waiter was cancelled."""

    if not task.cancelled():
        task.exception()


__all__ = ["SqlAlchemyTraceStore", "SqlTraceStoreOptions"]
