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
from tinkerfin_tracing.capture import CapturedValue
from tinkerfin_tracing.errors import (
    TraceStoreError,
    TraceStoreProtocolError,
    TraceStoreTimeout,
)
from tinkerfin_tracing.facts import RunFact
from tinkerfin_tracing.sql_schema import TRACE_TABLE_NAMES
from tinkerfin_tracing.sql_store import SqlAlchemyTraceStore, SqlTraceStoreOptions
from tinkerfin_tracing.store import TraceProjectionCheckpoint


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


async def test_sqlite_store_auto_setup_round_trip_and_generation_delete(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'trace.db'}")
    borrowed_pool = engine.pool
    store = SqlAlchemyTraceStore(
        engine,
        namespace="tests",
        options=SqlTraceStoreOptions(
            writer_lease_seconds=2,
            heartbeat_interval_seconds=0.5,
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
    original = store._setup_once
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

    monkeypatch.setattr(store, "_setup_once", fail_once)
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


async def test_sql_setup_caller_cancellation_keeps_the_running_shared_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'setup-cancel.db'}")
    store = SqlAlchemyTraceStore(engine)
    original = store._setup_once
    started = asyncio.Event()
    release = asyncio.Event()
    attempts = 0

    async def delayed_setup() -> None:
        nonlocal attempts
        attempts += 1
        started.set()
        await release.wait()
        await original()

    monkeypatch.setattr(store, "_setup_once", delayed_setup)
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
    original = store._setup_once
    attempts = 0

    async def cancel_once() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise asyncio.CancelledError
        await original()

    monkeypatch.setattr(store, "_setup_once", cancel_once)
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
        options=SqlTraceStoreOptions(retry_attempts=2, retry_delay_seconds=0.001),
    )
    writer = await store.open_writer(_identity())
    original = store._raw_write_connection

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

        monkeypatch.setattr(store, "_raw_write_connection", transaction)

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
    finally:
        monkeypatch.setattr(store, "_raw_write_connection", original)
        await writer.aclose()
        await engine.dispose()


async def test_sqlite_unknown_writer_open_commit_reuses_owner_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'unknown-open.db'}")
    store = SqlAlchemyTraceStore(
        engine,
        options=SqlTraceStoreOptions(retry_attempts=2, retry_delay_seconds=0.001),
    )
    await store.setup()
    original = store._raw_write_connection
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
        monkeypatch.setattr(store, "_raw_write_connection", fail_after_commit)
        writer = await store.open_writer(_identity())
        snapshot = await store.snapshot(_identity().thread_id)
        assert snapshot.active_run_ids == (_identity().run_id,)
        assert snapshot.as_of_seq == 0
    finally:
        monkeypatch.setattr(store, "_raw_write_connection", original)
        if writer is not None:
            await writer.aclose()
        await engine.dispose()


async def test_sqlite_retry_exhaustion_uses_stable_store_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'timeout.db'}")
    store = SqlAlchemyTraceStore(
        engine,
        options=SqlTraceStoreOptions(retry_attempts=2, retry_delay_seconds=0.001),
    )
    writer = await store.open_writer(_identity())
    original = store._raw_write_connection
    original_error = sqlite3.OperationalError("database is locked")
    original_error.sqlite_errorcode = sqlite3.SQLITE_BUSY

    @asynccontextmanager
    async def unavailable() -> AsyncIterator[AsyncConnection]:
        raise DBAPIError(None, None, original_error, False)
        yield  # pragma: no cover - required only by the async context manager shape

    try:
        monkeypatch.setattr(store, "_raw_write_connection", unavailable)
        with pytest.raises(TraceStoreTimeout) as captured:
            await writer.append((_fact("started"),))
        assert isinstance(captured.value.cause, DBAPIError)
    finally:
        monkeypatch.setattr(store, "_raw_write_connection", original)
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
