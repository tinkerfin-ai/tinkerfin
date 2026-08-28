"""Disposable MySQL 8.x Trace Store concurrency, follow, lease, and fencing E2E."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

import pytest
from asyncmy.errors import OperationalError
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from tinkerfin_contracts import RunIdentity
from tinkerfin_tracing import (
    CapturedValue,
    MessageFact,
    RunFact,
    TraceLimits,
    TraceProjectionCheckpoint,
    Tracer,
)
from tinkerfin_tracing.errors import (
    TraceQuotaExceeded,
    TraceRunConflict,
    TraceStoreProtocolError,
)
from tinkerfin_tracing.sql_schema import TRACE_TABLE_NAMES
from tinkerfin_tracing.sql_store import SqlAlchemyTraceStore, SqlTraceStoreOptions

pytestmark = pytest.mark.docker_integration


@pytest.fixture(autouse=True)
def _use_disposable_mysql(
    monkeypatch: pytest.MonkeyPatch,
    mysql_admin_url: str,
) -> None:
    """Route every case to the repository-owned disposable MySQL 8.4 service."""

    monkeypatch.setenv("TINKERFIN_TRACE_MYSQL_URL", mysql_admin_url)


def _url() -> str:
    value = os.getenv("TINKERFIN_TRACE_MYSQL_URL")
    if not value:
        pytest.skip("TINKERFIN_TRACE_MYSQL_URL is not configured")
    return value


def _identity(run_id: str) -> RunIdentity:
    return RunIdentity(threadId="mysql-trace-thread", runId=run_id)


def _fact(run_id: str, phase: Literal["started", "terminal", "closed"]) -> RunFact:
    return RunFact(
        source_observation_id=f"mysql-{run_id}-{phase}",
        identity=_identity(run_id),
        occurred_at=datetime.now(UTC),
        monotonic_ns=1,
        phase=phase,
        input_kind="ordinary" if phase == "started" else None,
        outcome="succeeded" if phase != "started" else None,
    )


async def test_mysql_cross_instance_sequence_follow_and_expired_writer_fencing() -> (
    None
):
    first_engine = create_async_engine(_url())
    second_engine = create_async_engine(_url())
    namespace = f"mysql-e2e-{uuid4().hex}"
    options = SqlTraceStoreOptions(
        writer_lease_seconds=2,
        heartbeat_interval_seconds=0.5,
        follow_poll_seconds=0.01,
    )
    first_store = SqlAlchemyTraceStore(
        first_engine,
        namespace=namespace,
        options=options,
    )
    second_store = SqlAlchemyTraceStore(
        second_engine,
        namespace=namespace,
        options=options,
    )
    try:
        first = await first_store.open_writer(_identity("run-a"))
        with pytest.raises(TraceRunConflict):
            await second_store.open_writer(_identity("run-a"))
        second = await second_store.open_writer(_identity("run-b"))
        first_batch, second_batch = await asyncio.gather(
            first.append((_fact("run-a", "started"),)),
            second.append((_fact("run-b", "started"),)),
        )
        assert {first_batch[0].trace_seq, second_batch[0].trace_seq} == {1, 2}

        snapshot = await second_store.snapshot(_identity("run-a").thread_id)
        follower = second_store.follow(snapshot.key, after_seq=snapshot.as_of_seq)
        waiting = asyncio.create_task(anext(follower))
        terminal = await first.append(
            (_fact("run-a", "terminal"), _fact("run-a", "closed")),
            mandatory=True,
        )
        assert await asyncio.wait_for(waiting, timeout=2) == terminal
        await follower.aclose()

        async with second_engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE tinkerfin_trace_writers "
                    "SET lease_expires_at = DATE_SUB(CURRENT_TIMESTAMP(6), INTERVAL 1 SECOND) "
                    "WHERE run_id = 'run-b' AND active = 1"
                )
            )
        replacement = await first_store.open_writer(_identity("run-b"))
        with pytest.raises(TraceStoreProtocolError):
            await second.append((_fact("run-b", "started"),))
        await replacement.append(
            (_fact("run-b", "terminal"), _fact("run-b", "closed")),
            mandatory=True,
        )
        await replacement.aclose()

        completed = await first_store.open_writer(_identity("run-c"))
        await completed.append(
            (_fact("run-c", "terminal"), _fact("run-c", "closed")),
            mandatory=True,
        )
        async with second_engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE tinkerfin_trace_writers "
                    "SET lease_expires_at = DATE_SUB(UTC_TIMESTAMP(6), INTERVAL 1 SECOND) "
                    "WHERE run_id = 'run-c' AND active = 1"
                )
            )
        with pytest.raises(TraceRunConflict):
            await second_store.open_writer(_identity("run-c"))
        await completed.aclose()

        await first.aclose()
        await second.aclose()
        final = await first_store.snapshot(_identity("run-a").thread_id)
        checkpoint = TraceProjectionCheckpoint(
            key=final.key,
            projection_name="mysql.concurrent-projection",
            run_id=None,
            as_of_seq=final.as_of_seq,
            state={"count": final.as_of_seq},
        )
        assert await asyncio.gather(
            first_store.save_projection_checkpoint(
                checkpoint,
                expected_as_of_seq=None,
            ),
            second_store.save_projection_checkpoint(
                checkpoint.model_copy(deep=True),
                expected_as_of_seq=None,
            ),
        ) == [checkpoint, checkpoint]
        with pytest.raises(TraceStoreProtocolError, match="idempotency"):
            await second_store.save_projection_checkpoint(
                checkpoint.model_copy(update={"state": {"count": -1}}),
                expected_as_of_seq=None,
            )
        await first_store.delete(final.key)
    finally:
        await first_engine.dispose()
        await second_engine.dispose()


async def test_mysql_concurrent_first_setup_reflects_the_only_current_schema() -> None:
    database_name = f"tinkerfin_trace_setup_{uuid4().hex}"
    configured_url = make_url(_url())
    admin_engine = create_async_engine(configured_url.set(database="mysql"))
    first_engine = None
    second_engine = None
    try:
        async with admin_engine.begin() as connection:
            await connection.exec_driver_sql(
                f"CREATE DATABASE `{database_name}` CHARACTER SET utf8mb4"
            )
        database_url = configured_url.set(database=database_name)
        first_engine = create_async_engine(database_url)
        second_engine = create_async_engine(database_url)
        first = SqlAlchemyTraceStore(first_engine, namespace="concurrent-setup")
        second = SqlAlchemyTraceStore(second_engine, namespace="concurrent-setup")

        await asyncio.gather(first.setup(), second.setup())

        async with first_engine.connect() as connection:
            reflected = await connection.run_sync(
                lambda sync_connection: {
                    "tables": tuple(inspect(sync_connection).get_table_names()),
                    "events": {
                        item["name"]: item
                        for item in inspect(sync_connection).get_columns(
                            "tinkerfin_trace_events"
                        )
                    },
                    "writers": {
                        item["name"]: item
                        for item in inspect(sync_connection).get_columns(
                            "tinkerfin_trace_writers"
                        )
                    },
                }
            )
        assert set(reflected["tables"]) == set(TRACE_TABLE_NAMES)
        assert type(reflected["events"]["payload"]["type"]).__name__ == "LONGBLOB"
        assert reflected["events"]["payload"]["comment"]
        assert {"terminal_committed", "closed_committed"} <= set(reflected["writers"])
    finally:
        if first_engine is not None:
            await first_engine.dispose()
        if second_engine is not None:
            await second_engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.exec_driver_sql(
                f"DROP DATABASE IF EXISTS `{database_name}`"
            )
        await admin_engine.dispose()


async def test_mysql_process_crash_exposes_missing_tail_then_allows_takeover() -> None:
    namespace = f"mysql-crash-{uuid4().hex}"
    options = SqlTraceStoreOptions(
        writer_lease_seconds=0.6,
        heartbeat_interval_seconds=0.2,
        follow_poll_seconds=0.01,
    )
    engine = create_async_engine(_url())
    store = SqlAlchemyTraceStore(engine, namespace=namespace, options=options)
    await store.setup()
    child_source = """
import asyncio
import os
import sys
from datetime import UTC, datetime
from sqlalchemy.ext.asyncio import create_async_engine
from tinkerfin_contracts import RunIdentity
from tinkerfin_tracing import RunFact
from tinkerfin_tracing.sql_store import SqlAlchemyTraceStore, SqlTraceStoreOptions

async def main():
    url, namespace = sys.argv[1:]
    engine = create_async_engine(url)
    store = SqlAlchemyTraceStore(
        engine,
        namespace=namespace,
        options=SqlTraceStoreOptions(
            writer_lease_seconds=0.6,
            heartbeat_interval_seconds=0.2,
            follow_poll_seconds=0.01,
        ),
    )
    identity = RunIdentity(threadId="mysql-crash-thread", runId="mysql-crash-run")
    writer = await store.open_writer(identity)
    await writer.append((RunFact(
        source_observation_id="mysql-crash-started",
        identity=identity,
        occurred_at=datetime.now(UTC),
        monotonic_ns=1,
        phase="started",
        input_kind="ordinary",
    ),))
    os._exit(0)

asyncio.run(main())
"""
    try:
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            child_source,
            _url(),
            namespace,
        )
        assert await child.wait() == 0
        await asyncio.sleep(0.8)

        incomplete = await Tracer(store=store).get("mysql-crash-thread")
        assert incomplete.status.execution == "unknown"
        assert incomplete.completeness.missing_tail is True

        replacement = await store.open_writer(
            RunIdentity(threadId="mysql-crash-thread", runId="mysql-crash-run")
        )
        replacement_identity = RunIdentity(
            threadId="mysql-crash-thread",
            runId="mysql-crash-run",
        )
        await replacement.append(
            (
                RunFact(
                    source_observation_id="mysql-crash-terminal",
                    identity=replacement_identity,
                    occurred_at=datetime.now(UTC),
                    monotonic_ns=2,
                    phase="terminal",
                    outcome="succeeded",
                ),
                RunFact(
                    source_observation_id="mysql-crash-closed",
                    identity=replacement_identity,
                    occurred_at=datetime.now(UTC),
                    monotonic_ns=3,
                    phase="closed",
                    outcome="succeeded",
                ),
            ),
            mandatory=True,
        )
        await replacement.aclose()

        complete = await Tracer(store=store).get("mysql-crash-thread")
        assert complete.status.execution == "succeeded"
        assert complete.completeness.missing_tail is False
        await store.delete(complete.key)
    finally:
        await engine.dispose()


@pytest.mark.parametrize("error_code", [1205, 1213, 2013])
async def test_mysql_retryable_or_unknown_commit_reuses_event_ids(
    error_code: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(_url())
    store = SqlAlchemyTraceStore(
        engine,
        namespace=f"mysql-retry-{error_code}-{uuid4().hex}",
        options=SqlTraceStoreOptions(retry_attempts=2, retry_delay_seconds=0.001),
    )
    identity = _identity(f"retry-{error_code}")
    writer = await store.open_writer(identity)
    original = store._raw_write_connection
    remaining = 1

    @asynccontextmanager
    async def fail_after_commit() -> AsyncIterator[AsyncConnection]:
        nonlocal remaining
        async with original() as connection:
            yield connection
        if remaining:
            remaining -= 1
            raise DBAPIError(
                None,
                None,
                OperationalError(error_code, "injected transaction outcome"),
                error_code == 2013,
            )

    try:
        monkeypatch.setattr(store, "_raw_write_connection", fail_after_commit)
        committed = await writer.append((_fact(f"retry-{error_code}", "started"),))
        snapshot = await store.snapshot(identity.thread_id)
        stored = await store.read_events(
            snapshot.key,
            after_seq=0,
            as_of_seq=snapshot.as_of_seq,
            limit=10,
        )
        assert snapshot.as_of_seq == 1
        assert tuple(event.event_id for event in stored) == (committed[0].event_id,)
    finally:
        monkeypatch.setattr(store, "_raw_write_connection", original)
        await writer.aclose()
        snapshot = await store.snapshot(identity.thread_id)
        await store.delete(snapshot.key)
        await engine.dispose()


async def test_mysql_longblob_round_trips_event_above_generic_blob_limit() -> None:
    engine = create_async_engine(_url())
    store = SqlAlchemyTraceStore(
        engine,
        namespace=f"mysql-longblob-{uuid4().hex}",
    )
    identity = _identity("longblob")
    writer = await store.open_writer(identity)
    try:
        content = "x" * (128 * 1024)
        fact = MessageFact(
            source_observation_id="mysql-longblob-message",
            identity=identity,
            occurred_at=datetime.now(UTC),
            monotonic_ns=1,
            phase="reconciled",
            message_id="message:mysql-longblob",
            source_message_id="mysql-longblob",
            role="assistant",
            content=CapturedValue(
                disposition="inline",
                safe_size_bytes=len(
                    json.dumps(
                        content,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode()
                ),
                value=content,
            ),
        )
        committed = await writer.append((fact,))
        assert committed[0].persisted_bytes > 65_535
        await writer.append(
            (_fact("longblob", "terminal"), _fact("longblob", "closed")),
            mandatory=True,
        )
        await writer.aclose()

        snapshot = await store.snapshot(identity.thread_id)
        stored = await store.read_events(
            snapshot.key,
            after_seq=0,
            as_of_seq=snapshot.as_of_seq,
            limit=10,
        )
        assert stored[0].fact == fact
        await store.delete(snapshot.key)
    finally:
        await writer.aclose()
        await engine.dispose()


async def test_mysql_expired_incomplete_writer_keeps_terminal_reserve() -> None:
    engine = create_async_engine(_url())
    store = SqlAlchemyTraceStore(
        engine,
        namespace=f"mysql-reserve-{uuid4().hex}",
        limits=TraceLimits(
            max_event_bytes=1024,
            max_thread_events=16,
            max_thread_bytes=64 * 1024,
            max_tracer_threads=1,
            max_tracer_bytes=64 * 1024,
            terminal_reserve_events_per_run=2,
            terminal_reserve_bytes_per_run=2048,
        ),
        options=SqlTraceStoreOptions(
            writer_lease_seconds=10,
            heartbeat_interval_seconds=5,
        ),
    )
    first = await store.open_writer(_identity("reserve-a"))
    second = await store.open_writer(_identity("reserve-b"))
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE tinkerfin_trace_writers "
                    "SET lease_expires_at = DATE_SUB(UTC_TIMESTAMP(6), INTERVAL 1 SECOND) "
                    "WHERE run_id = 'reserve-a' AND active = 1"
                )
            )
        await second.append(
            tuple(_fact("reserve-b", "started") for _index in range(12))
        )
        with pytest.raises(TraceQuotaExceeded, match="event quota"):
            await second.append((_fact("reserve-b", "started"),))

        replacement = await store.open_writer(_identity("reserve-a"))
        with pytest.raises(TraceStoreProtocolError):
            await first.append((_fact("reserve-a", "started"),))
        await replacement.append(
            (_fact("reserve-a", "terminal"), _fact("reserve-a", "closed")),
            mandatory=True,
        )
        await second.append(
            (_fact("reserve-b", "terminal"), _fact("reserve-b", "closed")),
            mandatory=True,
        )
        await replacement.aclose()
        await second.aclose()
        await first.aclose()
        snapshot = await store.snapshot(_identity("reserve-a").thread_id)
        assert snapshot.as_of_seq == 16
        await store.delete(snapshot.key)
    finally:
        await first.aclose()
        await second.aclose()
        await engine.dispose()
