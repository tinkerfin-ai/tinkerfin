"""SQLite durable Trace Store sequencing, replay, checkpoints, and borrowed Engine."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from tinkerfin_contracts import RunIdentity
from tinkerfin_tracing.backend import TraceLedgerStateRequest, TraceStoreOptions
from tinkerfin_tracing.capture import CapturedValue
from tinkerfin_tracing.codec import CanonicalTracePayloadCodec, EncodedTracePayload
from tinkerfin_tracing.errors import (
    TraceProjectionCheckpointConflict,
    TraceStoreError,
    TraceStoreProtocolError,
    TraceStoreTimeout,
    TraceThreadNotFound,
)
from tinkerfin_tracing.facts import RunFact, TraceEvent, TraceSemanticFact
from tinkerfin_tracing.sql_schema import TRACE_TABLE_NAMES
from tinkerfin_tracing.sql_store import (
    SqlAlchemyTraceStore,
    _SqlAlchemyTraceLedgerBackend,
)
from tinkerfin_tracing.store import TraceProjectionCheckpoint
from tinkerfin_tracing.writing import TraceBatchWriter, TraceWritePolicy


def _identity(run_id: str = "run-sql") -> RunIdentity:
    return RunIdentity(threadId="thread-sql", runId=run_id)


def _fact(
    phase: Literal["started", "input", "terminal", "closed"],
    *,
    run_id: str = "run-sql",
) -> RunFact:
    captured = (
        CapturedValue(disposition="inline", safe_size_bytes=2, value={})
        if phase == "input"
        else None
    )
    return RunFact(
        source_observation_id=f"observation-{run_id}-{phase}",
        identity=_identity(run_id),
        occurred_at=datetime.now(UTC),
        monotonic_ns=1,
        phase=phase,
        input_kind="ordinary" if phase in {"started", "input"} else None,
        input=captured,
        config=captured,
        outcome="succeeded" if phase in {"terminal", "closed"} else None,
    )


async def _ignore_committed(_events: tuple[TraceEvent, ...]) -> None:
    return None


def _backend(store: SqlAlchemyTraceStore) -> _SqlAlchemyTraceLedgerBackend:
    backend = store.backend
    assert isinstance(backend, _SqlAlchemyTraceLedgerBackend)
    return backend


async def test_sql_batch_admission_reuses_one_canonical_encoding_per_fact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = CanonicalTracePayloadCodec.encode_fact
    encoded_observations: list[str] = []

    def count_encoding(
        codec: CanonicalTracePayloadCodec,
        fact: TraceSemanticFact,
    ) -> EncodedTracePayload:
        encoded_observations.append(fact.source_observation_id)
        return original(codec, fact)

    monkeypatch.setattr(CanonicalTracePayloadCodec, "encode_fact", count_encoding)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'encoding.db'}")
    store = SqlAlchemyTraceStore(engine, namespace="encoding-test")
    try:
        writer = TraceBatchWriter(
            await store.open_writer(_identity()),
            policy=TraceWritePolicy(max_batch_delay_seconds=0),
            on_committed=_ignore_committed,
        )
        await writer.submit((_fact("started"), _fact("input")), mandatory=False)
        await writer.force()
        await writer.aclose()
    finally:
        await engine.dispose()

    assert encoded_observations == [
        "observation-run-sql-started",
        "observation-run-sql-input",
    ]


async def test_sqlite_store_auto_setup_round_trip_and_generation_delete(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'trace.db'}")
    borrowed_pool = engine.pool
    store = SqlAlchemyTraceStore(
        engine,
        namespace="tests",
        options=TraceStoreOptions(
            writer_lease_seconds=2,
            writer_heartbeat_interval_seconds=0.5,
            follow_poll_seconds=0.01,
        ),
    )
    try:
        writer = await store.open_writer(_identity())
        ordinary = await writer.append((_fact("started"), _fact("input")))
        terminal = await writer.append(
            (_fact("terminal"), _fact("closed")),
            mandatory=True,
        )
        await writer.aclose()

        snapshot = await store.snapshot(_identity().thread_id)
        assert snapshot.as_of_seq == 4
        assert snapshot.active_writers == ()
        assert [event.trace_seq for event in ordinary + terminal] == [1, 2, 3, 4]
        assert (
            await store.read_events(
                snapshot.key,
                after_seq=0,
                as_of_seq=4,
                limit=10,
            )
            == ordinary + terminal
        )
        assert [
            event.trace_seq
            for event in await store.read_events_reverse(
                snapshot.key,
                before_seq=5,
                limit=2,
            )
        ] == [4, 3]

        checkpoint = TraceProjectionCheckpoint(
            key=snapshot.key,
            projection_name="tests.projection",
            run_id="run-sql",
            as_of_seq=4,
            state={"count": 4},
        )
        await store.save_projection_checkpoint(checkpoint, expected_as_of_seq=None)
        assert (
            await store.load_projection_checkpoint(
                snapshot.key,
                projection_name="tests.projection",
                run_id="run-sql",
                as_of_seq=4,
            )
            == checkpoint
        )
        await store.delete(snapshot.key)
        replacement = await store.open_writer(_identity())
        assert replacement.key.generation != snapshot.key.generation
        await replacement.aclose()
        assert engine.pool is borrowed_pool

        async with engine.connect() as connection:
            table_names = await connection.run_sync(
                lambda sync_connection: tuple(
                    inspect(sync_connection).get_table_names()
                )
            )
        assert set(TRACE_TABLE_NAMES) <= set(table_names)
    finally:
        await engine.dispose()


async def test_sql_setup_retries_after_one_retained_task_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'setup-retry.db'}")
    store = SqlAlchemyTraceStore(engine)
    original = _backend(store)._setup_once
    first_attempt_started = asyncio.Event()
    release_first_attempt = asyncio.Event()
    attempts = 0

    async def fail_once() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            first_attempt_started.set()
            await release_first_attempt.wait()
            raise TraceStoreError("transient setup failure")
        await original()

    monkeypatch.setattr(_backend(store), "_setup_once", fail_once)
    try:
        first_waiter = asyncio.create_task(store.setup())
        await first_attempt_started.wait()
        second_waiter = asyncio.create_task(store.setup())
        await asyncio.sleep(0)
        release_first_attempt.set()
        first, second = await asyncio.gather(
            first_waiter,
            second_waiter,
            return_exceptions=True,
        )
        assert isinstance(first, TraceStoreError)
        assert isinstance(second, TraceStoreError)
        assert attempts == 1

        await store.setup()
        assert attempts == 2
        await store.setup()
        assert attempts == 2
    finally:
        release_first_attempt.set()
        await engine.dispose()


async def test_sql_setup_rejects_partial_existing_trace_schema(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'partial.db'}")
    store = SqlAlchemyTraceStore(engine)
    writer = await store.open_writer(_identity())
    await writer.append((_fact("started"),))
    await writer.aclose()
    async with engine.begin() as connection:
        await connection.exec_driver_sql("DROP TABLE tinkerfin_trace_events")

    replacement = SqlAlchemyTraceStore(engine)
    try:
        with pytest.raises(TraceStoreProtocolError, match="incomplete"):
            await replacement.setup()
        async with engine.connect() as connection:
            table_names = await connection.run_sync(
                lambda sync_connection: set(inspect(sync_connection).get_table_names())
            )
        assert "tinkerfin_trace_events" not in table_names
    finally:
        await engine.dispose()


async def test_sql_setup_rejects_foreign_check_constraint(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'check.db'}")
    await SqlAlchemyTraceStore(engine).setup()
    async with engine.begin() as connection:
        ddl = await connection.scalar(
            text(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'table' AND name = 'tinkerfin_trace_threads'"
            )
        )
        assert isinstance(ddl, str)
        await connection.exec_driver_sql("DROP TABLE tinkerfin_trace_threads")
        await connection.exec_driver_sql(
            ddl.rstrip()[:-1] + ", CHECK (persisted_bytes = 0))"
        )

    try:
        with pytest.raises(TraceStoreProtocolError, match="check constraints"):
            await SqlAlchemyTraceStore(engine).setup()
    finally:
        await engine.dispose()


async def test_sql_setup_caller_cancellation_keeps_the_running_shared_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'setup-cancel.db'}")
    store = SqlAlchemyTraceStore(engine)
    original = _backend(store)._setup_once
    started = asyncio.Event()
    release = asyncio.Event()
    attempts = 0

    async def delayed_setup() -> None:
        nonlocal attempts
        attempts += 1
        started.set()
        await release.wait()
        await original()

    monkeypatch.setattr(_backend(store), "_setup_once", delayed_setup)
    caller = asyncio.create_task(store.setup())
    try:
        await started.wait()
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller

        release.set()
        await store.setup()
        assert attempts == 1
        await store.setup()
        assert attempts == 1
    finally:
        release.set()
        await engine.dispose()


async def test_sql_setup_retries_after_the_retained_task_cancels_itself(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'setup-self-cancel.db'}"
    )
    store = SqlAlchemyTraceStore(engine)
    original = _backend(store)._setup_once
    attempts = 0

    async def cancel_once() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise asyncio.CancelledError
        await original()

    monkeypatch.setattr(_backend(store), "_setup_once", cancel_once)
    try:
        with pytest.raises(asyncio.CancelledError):
            await store.setup()
        await store.setup()
        assert attempts == 2
    finally:
        await engine.dispose()


async def test_sqlite_unknown_commit_reuses_event_and_checkpoint_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'unknown.db'}")
    store = SqlAlchemyTraceStore(
        engine,
        namespace="unknown-commit",
        options=TraceStoreOptions(
            commit_retry_attempts=2,
            commit_retry_delay_seconds=0.001,
        ),
    )
    writer = await store.open_writer(_identity())
    original = _backend(store)._raw_write_connection

    def busy_error() -> DBAPIError:
        original_error = sqlite3.OperationalError("database is locked")
        original_error.sqlite_errorcode = sqlite3.SQLITE_BUSY
        return DBAPIError(None, None, original_error, False)

    def fail_once_after_commit() -> None:
        remaining = 1

        @asynccontextmanager
        async def transaction() -> AsyncIterator[AsyncConnection]:
            nonlocal remaining
            async with original() as connection:
                yield connection
            if remaining:
                remaining -= 1
                raise busy_error()

        monkeypatch.setattr(_backend(store), "_raw_write_connection", transaction)

    try:
        # The injected DBAPI failure happens after the real transaction committed.
        # Public append must prove the first commit by its retained event IDs.
        fail_once_after_commit()
        committed = await writer.append((_fact("started"),))
        snapshot = await store.snapshot(_identity().thread_id)
        assert snapshot.as_of_seq == 1
        assert [
            event.event_id
            for event in await store.read_events(
                snapshot.key,
                after_seq=0,
                as_of_seq=1,
                limit=10,
            )
        ] == [committed[0].event_id]

        fail_once_after_commit()
        checkpoint = TraceProjectionCheckpoint(
            key=snapshot.key,
            projection_name="unknown.projection",
            run_id="run-sql",
            as_of_seq=1,
            state={"count": 1},
        )
        assert (
            await store.save_projection_checkpoint(
                checkpoint,
                expected_as_of_seq=None,
            )
            == checkpoint
        )
        assert (
            await store.load_projection_checkpoint(
                snapshot.key,
                projection_name="unknown.projection",
                run_id="run-sql",
                as_of_seq=1,
            )
            == checkpoint
        )
        await writer.append(
            (_fact("terminal"), _fact("closed")),
            mandatory=True,
        )
        await writer.aclose()

        fail_once_after_commit()
        await store.delete(snapshot.key)
        with pytest.raises(TraceThreadNotFound):
            await store.snapshot_key(snapshot.key)
    finally:
        monkeypatch.setattr(_backend(store), "_raw_write_connection", original)
        await writer.aclose()
        await engine.dispose()


async def test_sqlite_unknown_writer_open_commit_reuses_owner_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'unknown-open.db'}")
    store = SqlAlchemyTraceStore(
        engine,
        options=TraceStoreOptions(
            writer_lease_seconds=0.2,
            writer_heartbeat_interval_seconds=0.1,
            commit_retry_attempts=2,
            commit_retry_delay_seconds=0.3,
        ),
    )
    await store.setup()
    original = _backend(store)._raw_write_connection
    remaining = 1

    @asynccontextmanager
    async def fail_after_commit() -> AsyncIterator[AsyncConnection]:
        nonlocal remaining
        async with original() as connection:
            yield connection
        if remaining:
            remaining -= 1
            original_error = sqlite3.OperationalError("database is locked")
            original_error.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise DBAPIError(None, None, original_error, False)

    writer = None
    try:
        monkeypatch.setattr(_backend(store), "_raw_write_connection", fail_after_commit)
        writer = await store.open_writer(_identity())
        committed = await writer.append((_fact("started"),))
        snapshot = await store.snapshot(_identity().thread_id)
        assert snapshot.active_run_ids == (_identity().run_id,)
        assert snapshot.as_of_seq == 1
        assert committed[0].trace_seq == 1
    finally:
        monkeypatch.setattr(_backend(store), "_raw_write_connection", original)
        if writer is not None:
            await writer.aclose()
        await engine.dispose()


async def test_sqlite_unknown_append_commit_renews_short_writer_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'append-lease.db'}")
    store = SqlAlchemyTraceStore(
        engine,
        options=TraceStoreOptions(
            writer_lease_seconds=0.2,
            writer_heartbeat_interval_seconds=0.1,
            commit_retry_attempts=2,
            commit_retry_delay_seconds=0.3,
        ),
    )
    writer = await store.open_writer(_identity())
    backend = _backend(store)
    original = backend._raw_write_connection
    remaining = 1

    @asynccontextmanager
    async def fail_after_commit() -> AsyncIterator[AsyncConnection]:
        nonlocal remaining
        async with original() as connection:
            yield connection
        if remaining:
            remaining -= 1
            original_error = sqlite3.OperationalError("database is locked")
            original_error.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise DBAPIError(None, None, original_error, False)

    try:
        monkeypatch.setattr(backend, "_raw_write_connection", fail_after_commit)
        first = await writer.append((_fact("started"),))
        second = await writer.append((_fact("input"),))

        assert [first[0].trace_seq, second[0].trace_seq] == [1, 2]
    finally:
        monkeypatch.setattr(backend, "_raw_write_connection", original)
        await writer.aclose()
        await engine.dispose()


async def test_sqlite_proven_append_survives_peer_takeover_during_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "append-takeover.db"
    first_engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    peer_engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    namespace = "append-takeover"
    options = TraceStoreOptions(
        writer_lease_seconds=0.15,
        writer_heartbeat_interval_seconds=0.14,
        commit_retry_attempts=2,
        commit_retry_delay_seconds=2,
    )
    first_store = SqlAlchemyTraceStore(
        first_engine,
        namespace=namespace,
        options=options,
    )
    peer_store = SqlAlchemyTraceStore(
        peer_engine,
        namespace=namespace,
        options=options,
    )
    identity = _identity()
    writer = await first_store.open_writer(identity)
    backend = _backend(first_store)
    peer_backend = _backend(peer_store)
    original = backend._raw_write_connection
    first_commit_finished = asyncio.Event()
    remaining = 1

    @asynccontextmanager
    async def fail_after_commit() -> AsyncIterator[AsyncConnection]:
        nonlocal remaining
        async with original() as connection:
            yield connection
        if remaining:
            remaining -= 1
            first_commit_finished.set()
            original_error = sqlite3.OperationalError("database is locked")
            original_error.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise DBAPIError(None, None, original_error, False)

    monkeypatch.setattr(backend, "_raw_write_connection", fail_after_commit)
    append = asyncio.create_task(writer.append((_fact("started"),)))
    replacement = None
    try:
        await first_commit_finished.wait()
        await asyncio.sleep(1.2)
        replacement = await peer_store.open_writer(identity)
        committed = await append
        state = await peer_backend.load_ledger_state(
            TraceLedgerStateRequest(
                namespace=namespace,
                thread_id=identity.thread_id,
                generation=replacement.key.generation,
                run_id=identity.run_id,
            )
        )
        stored = await peer_store.read_events(
            replacement.key,
            after_seq=0,
            as_of_seq=1,
            limit=10,
        )

        assert [event.event_id for event in committed] == [stored[0].event_id]
        assert state.target_writer is not None
        assert state.target_writer.fence == 2
    finally:
        monkeypatch.setattr(backend, "_raw_write_connection", original)
        if not append.done():
            append.cancel()
            await asyncio.gather(append, return_exceptions=True)
        await writer.aclose()
        if replacement is not None:
            await replacement.aclose()
        await first_engine.dispose()
        await peer_engine.dispose()


async def test_sqlite_retry_exhaustion_uses_stable_store_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'timeout.db'}")
    store = SqlAlchemyTraceStore(
        engine,
        options=TraceStoreOptions(
            commit_retry_attempts=2,
            commit_retry_delay_seconds=0.001,
        ),
    )
    writer = await store.open_writer(_identity())
    original = _backend(store)._raw_write_connection
    original_error = sqlite3.OperationalError("database is locked")
    original_error.sqlite_errorcode = sqlite3.SQLITE_BUSY

    @asynccontextmanager
    async def unavailable() -> AsyncIterator[AsyncConnection]:
        raise DBAPIError(None, None, original_error, False)
        yield  # pragma: no cover - required only by the async context manager shape

    try:
        monkeypatch.setattr(_backend(store), "_raw_write_connection", unavailable)
        with pytest.raises(TraceStoreTimeout) as captured:
            await writer.append((_fact("started"),))
        assert isinstance(captured.value.cause, DBAPIError)
    finally:
        monkeypatch.setattr(_backend(store), "_raw_write_connection", original)
        await writer.aclose()
        await engine.dispose()


async def test_sqlite_store_rejects_corrupt_opaque_payload(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'corrupt.db'}")
    store = SqlAlchemyTraceStore(engine)
    writer = await store.open_writer(_identity())
    try:
        committed = await writer.append((_fact("started"),))
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE tinkerfin_trace_events SET payload = :payload "
                    "WHERE event_id = :event_id"
                ),
                {"payload": b"{}", "event_id": committed[0].event_id},
            )
        snapshot = await store.snapshot(_identity().thread_id)
        with pytest.raises(TraceStoreProtocolError, match="digest mismatch"):
            await store.read_events(
                snapshot.key,
                after_seq=0,
                as_of_seq=1,
                limit=1,
            )
    finally:
        await writer.aclose()
        await engine.dispose()


async def test_sqlite_fixed_event_read_does_not_cross_concurrent_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fixed-read.db'}")
    store = SqlAlchemyTraceStore(
        engine,
        options=TraceStoreOptions(
            commit_retry_attempts=10,
            commit_retry_delay_seconds=0.01,
        ),
    )
    writer = await store.open_writer(_identity())
    await writer.append((_fact("started"),))
    snapshot = await store.snapshot(_identity().thread_id)
    backend = _backend(store)
    original = backend._thread_row
    head_observed = asyncio.Event()
    continue_read = asyncio.Event()

    async def pause_after_head(connection: AsyncConnection, *, thread_id: str):
        row = await original(connection, thread_id=thread_id)
        head_observed.set()
        await continue_read.wait()
        return row

    monkeypatch.setattr(backend, "_thread_row", pause_after_head)
    read = asyncio.create_task(
        store.read_events(
            snapshot.key,
            after_seq=0,
            as_of_seq=2,
            limit=10,
        )
    )
    append = None
    try:
        await head_observed.wait()
        append = asyncio.create_task(writer.append((_fact("input"),)))
        await asyncio.sleep(0.05)
        continue_read.set()
        page = await read
        await append

        assert [event.trace_seq for event in page] == [1]
    finally:
        continue_read.set()
        if not read.done():
            read.cancel()
            await asyncio.gather(read, return_exceptions=True)
        if append is not None and not append.done():
            append.cancel()
            await asyncio.gather(append, return_exceptions=True)
        monkeypatch.setattr(backend, "_thread_row", original)
        await writer.aclose()
        await engine.dispose()


async def test_sqlite_cancelled_lock_wait_releases_connection_for_retry(
    tmp_path: Path,
) -> None:
    database = tmp_path / "cancel.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database}",
        connect_args={"timeout": 30},
    )
    blocker_engine = create_async_engine(
        f"sqlite+aiosqlite:///{database}",
        connect_args={"timeout": 30},
    )
    store = SqlAlchemyTraceStore(engine)
    writer = await store.open_writer(_identity())
    blocker = await blocker_engine.connect()
    try:
        await blocker.exec_driver_sql("BEGIN IMMEDIATE")
        waiting = asyncio.create_task(writer.append((_fact("started"),)))
        await asyncio.sleep(0.05)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        await blocker.rollback()

        committed = await writer.append((_fact("started"),))
        assert committed[0].trace_seq == 1
        async with engine.connect() as borrowed_again:
            assert await borrowed_again.scalar(text("PRAGMA busy_timeout")) == 30_000
    finally:
        await blocker.close()
        await writer.aclose()
        await engine.dispose()
        await blocker_engine.dispose()


async def test_sqlite_subsecond_heartbeat_keeps_writer_lease_current(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'clock.db'}")
    store = SqlAlchemyTraceStore(
        engine,
        options=TraceStoreOptions(
            writer_lease_seconds=0.4,
            writer_heartbeat_interval_seconds=0.1,
        ),
    )
    writer = await store.open_writer(_identity())
    try:
        await asyncio.sleep(1.2)
        committed = await writer.append((_fact("started"),))
        assert committed[0].trace_seq == 1
    finally:
        await writer.aclose()
        await engine.dispose()


async def test_sqlite_unknown_heartbeat_commit_renews_expired_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'renew.db'}")
    store = SqlAlchemyTraceStore(
        engine,
        options=TraceStoreOptions(
            writer_lease_seconds=0.2,
            writer_heartbeat_interval_seconds=0.1,
            commit_retry_attempts=2,
            commit_retry_delay_seconds=0.3,
        ),
    )
    writer = await store.open_writer(_identity())
    backend = _backend(store)
    original = backend._raw_write_connection
    injected = asyncio.Event()
    remaining = 1

    @asynccontextmanager
    async def fail_after_commit() -> AsyncIterator[AsyncConnection]:
        nonlocal remaining
        async with original() as connection:
            yield connection
        if remaining:
            remaining -= 1
            injected.set()
            original_error = sqlite3.OperationalError("database is locked")
            original_error.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise DBAPIError(None, None, original_error, False)

    monkeypatch.setattr(backend, "_raw_write_connection", fail_after_commit)
    try:
        await asyncio.wait_for(injected.wait(), timeout=2)
        await asyncio.sleep(0.35)
        committed = await writer.append((_fact("started"),))
        assert committed[0].trace_seq == 1
    finally:
        monkeypatch.setattr(backend, "_raw_write_connection", original)
        await writer.aclose()
        await engine.dispose()


async def test_sqlite_zero_event_close_removes_checkpoint_rows(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'orphan.db'}")
    store = SqlAlchemyTraceStore(engine)
    writer = await store.open_writer(_identity())
    await store.save_projection_checkpoint(
        TraceProjectionCheckpoint(
            key=writer.key,
            projection_name="zero.checkpoint",
            run_id=None,
            as_of_seq=0,
            state={"count": 0},
        ),
        expected_as_of_seq=None,
    )
    try:
        await writer.aclose()
        async with engine.connect() as connection:
            checkpoint_count = await connection.scalar(
                text("SELECT COUNT(*) FROM tinkerfin_trace_projection_checkpoints")
            )
        assert checkpoint_count == 0
    finally:
        await writer.aclose()
        await engine.dispose()


async def test_sqlite_unknown_checkpoint_survives_peer_advancement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "checkpoint-advance.db"
    first_engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    peer_engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    options = TraceStoreOptions(
        commit_retry_attempts=2,
        commit_retry_delay_seconds=0.5,
    )
    first = SqlAlchemyTraceStore(
        first_engine,
        namespace="checkpoint-advance",
        options=options,
    )
    peer = SqlAlchemyTraceStore(
        peer_engine,
        namespace="checkpoint-advance",
        options=options,
    )
    writer = await first.open_writer(_identity())
    await writer.append((_fact("started"), _fact("input")))
    backend = _backend(first)
    original = backend._raw_write_connection
    first_commit_finished = asyncio.Event()
    remaining = 1

    @asynccontextmanager
    async def fail_after_commit() -> AsyncIterator[AsyncConnection]:
        nonlocal remaining
        async with original() as connection:
            yield connection
        if remaining:
            remaining -= 1
            first_commit_finished.set()
            original_error = sqlite3.OperationalError("database is locked")
            original_error.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise DBAPIError(None, None, original_error, False)

    first_checkpoint = TraceProjectionCheckpoint(
        key=writer.key,
        projection_name="advancing.checkpoint",
        run_id=None,
        as_of_seq=1,
        state={"count": 1},
    )
    second_checkpoint = first_checkpoint.model_copy(
        update={"as_of_seq": 2, "state": {"count": 2}}
    )
    monkeypatch.setattr(backend, "_raw_write_connection", fail_after_commit)
    saving = asyncio.create_task(
        first.save_projection_checkpoint(
            first_checkpoint,
            expected_as_of_seq=None,
        )
    )
    try:
        await first_commit_finished.wait()
        assert (
            await peer.save_projection_checkpoint(
                second_checkpoint,
                expected_as_of_seq=1,
            )
            == second_checkpoint
        )
        assert await saving == first_checkpoint
    finally:
        monkeypatch.setattr(backend, "_raw_write_connection", original)
        if not saving.done():
            saving.cancel()
            await asyncio.gather(saving, return_exceptions=True)
        await writer.aclose()
        await first_engine.dispose()
        await peer_engine.dispose()


async def test_sqlite_rejects_historical_checkpoint_as_new_cas_retry(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'historical.db'}")
    store = SqlAlchemyTraceStore(engine)
    writer = await store.open_writer(_identity())
    await writer.append((_fact("started"), _fact("input")))
    first = TraceProjectionCheckpoint(
        key=writer.key,
        projection_name="historical.checkpoint",
        run_id=None,
        as_of_seq=1,
        state={"count": 1},
    )
    second = first.model_copy(update={"as_of_seq": 2, "state": {"count": 2}})
    try:
        await store.save_projection_checkpoint(first, expected_as_of_seq=None)
        await store.save_projection_checkpoint(second, expected_as_of_seq=1)
        with pytest.raises(TraceProjectionCheckpointConflict):
            await store.save_projection_checkpoint(first, expected_as_of_seq=None)
    finally:
        await writer.aclose()
        await engine.dispose()
