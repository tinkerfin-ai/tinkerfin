"""Disposable MySQL 8.x Trace Store concurrency, follow, lease, and fencing E2E."""

from __future__ import annotations

import asyncio
import gc
import json
import logging
import os
import sys
import warnings
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Literal, cast
from uuid import uuid4

import pytest
from asyncmy.errors import OperationalError
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, SAWarning
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool

from tinkerfin_contracts import RunIdentity
from tinkerfin_tracing import (
    CapturedValue,
    CapturePolicy,
    MessageFact,
    ModelCallFact,
    RunFact,
    TraceEntryKind,
    TraceFilter,
    TraceLimits,
    TraceProjectionCheckpoint,
    Tracer,
    TraceStoreOptions,
    verify_trace_ledger_backend,
)
from tinkerfin_tracing.errors import (
    TraceQuotaExceeded,
    TraceRunConflict,
    TraceStoreProtocolError,
)
from tinkerfin_tracing.sql_schema import TRACE_TABLE_NAMES
from tinkerfin_tracing.sql_store import (
    SqlAlchemyTraceStore,
    _SqlAlchemyTraceLedgerBackend,
)

pytestmark = pytest.mark.docker_integration


async def test_mysql_cancelled_reads_return_pool_capacity(
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine = create_async_engine(_url(), pool_size=1, max_overflow=0)
    blocker_engine = create_async_engine(_url(), pool_size=1, max_overflow=0)
    store = SqlAlchemyTraceStore(
        engine,
        namespace=f"mysql-cancelled-read-{uuid4().hex}",
    )
    writer = await store.open_writer(_identity("cancelled-read"))
    try:
        await writer.append((_fact("cancelled-read", "started"),))
        snapshot = await store.snapshot(_identity("cancelled-read").thread_id)
        blocker = await blocker_engine.connect()
        try:
            await blocker.exec_driver_sql("LOCK TABLES tinkerfin_trace_entries WRITE")
            caplog.clear()
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                query = asyncio.create_task(
                    store.query_trace_entries(
                        snapshot.key,
                        run_ids=("cancelled-read",),
                        where=TraceFilter(),
                        limit=10,
                    )
                )
                await asyncio.sleep(0.05)
                assert not query.done()
                for attempt in range(8):
                    if query.done():
                        break
                    query.cancel(f"cancelled read {attempt + 1}")
                    await asyncio.sleep(0)
                with pytest.raises(asyncio.CancelledError) as captured:
                    await query
                assert captured.value.args == ("cancelled read 1",)

                pool = cast(AsyncAdaptedQueuePool, engine.sync_engine.pool)
                assert pool.checkedout() == 0
                gc.collect()
                await asyncio.sleep(0)
            assert not [item for item in caught if issubclass(item.category, SAWarning)]
            assert not [
                record
                for record in caplog.records
                if record.name.startswith("sqlalchemy.pool")
                and record.levelno >= logging.ERROR
            ]
        finally:
            await blocker.exec_driver_sql("UNLOCK TABLES")
            await blocker.close()

        assert (await store.snapshot(snapshot.key.thread_id)).key == snapshot.key
    finally:
        await writer.aclose()
        await blocker_engine.dispose()
        await engine.dispose()


async def test_mysql_cancelled_writes_preserve_data_and_return_pool_capacity(
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine = create_async_engine(_url(), pool_size=1, max_overflow=0)
    blocker_engine = create_async_engine(_url(), pool_size=1, max_overflow=0)
    store = SqlAlchemyTraceStore(
        engine,
        namespace=f"mysql-cancelled-write-{uuid4().hex}",
    )
    writer = await store.open_writer(_identity("cancelled-write"))
    try:
        await writer.append((_fact("cancelled-write", "started"),))
        await writer.append(
            (
                _fact("cancelled-write", "terminal"),
                _fact("cancelled-write", "closed"),
            ),
            mandatory=True,
        )
        await writer.aclose()
        snapshot = await store.snapshot(_identity("cancelled-write").thread_id)
        blocker = await blocker_engine.connect()
        try:
            await blocker.exec_driver_sql("LOCK TABLES tinkerfin_trace_threads WRITE")
            caplog.clear()
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                deletion = asyncio.create_task(store.delete(snapshot.key))
                await asyncio.sleep(0.05)
                assert not deletion.done()
                for attempt in range(8):
                    if deletion.done():
                        break
                    deletion.cancel(f"cancelled write {attempt + 1}")
                    await asyncio.sleep(0)
                with pytest.raises(asyncio.CancelledError) as captured:
                    await deletion
                assert captured.value.args == ("cancelled write 1",)

                pool = cast(AsyncAdaptedQueuePool, engine.sync_engine.pool)
                assert pool.checkedout() == 0
                gc.collect()
                await asyncio.sleep(0)
            assert not [item for item in caught if issubclass(item.category, SAWarning)]
            assert not [
                record
                for record in caplog.records
                if record.name.startswith("sqlalchemy.pool")
                and record.levelno >= logging.ERROR
            ]
        finally:
            await blocker.exec_driver_sql("UNLOCK TABLES")
            await blocker.close()

        assert (await store.snapshot(snapshot.key.thread_id)).key == snapshot.key
    finally:
        await writer.aclose()
        await blocker_engine.dispose()
        await engine.dispose()


async def test_mysql_entry_query_filters_and_joins_ledger_details() -> None:
    engine = create_async_engine(_url(), pool_pre_ping=True)
    namespace = f"mysql-entry-query-{uuid4().hex}"
    store = SqlAlchemyTraceStore(engine, namespace=namespace)
    writer = await store.open_writer(_identity("entry-query"))
    marker = "mysql-final-request"
    now = datetime.now(UTC)
    try:
        await writer.append(
            (
                _fact("entry-query", "started"),
                ModelCallFact(
                    source_observation_id="mysql-model-start",
                    identity=_identity("entry-query"),
                    occurred_at=now,
                    monotonic_ns=2,
                    phase="started",
                    call_id="mysql-model-call",
                    provider="deepseek",
                    model="deepseek-chat",
                    request=CapturePolicy.public_history().capture(
                        {"messages": [{"content": marker}]},
                        max_bytes=4096,
                    ),
                ),
                ModelCallFact(
                    source_observation_id="mysql-model-completed",
                    identity=_identity("entry-query"),
                    occurred_at=now,
                    monotonic_ns=3,
                    phase="completed",
                    call_id="mysql-model-call",
                ),
            )
        )
        snapshot = await store.snapshot(_identity("entry-query").thread_id)
        page = await store.query_trace_entries(
            snapshot.key,
            run_ids=("entry-query",),
            where=TraceFilter(
                kinds={TraceEntryKind.PROVIDER},
                providers={"deepseek"},
                search="deepseek-chat",
            ),
            limit=10,
        )

        assert len(page.entries) == 1
        start_fact = page.entries[0].started_event.fact
        assert isinstance(start_fact, ModelCallFact)
        assert start_fact.request is not None
        assert start_fact.request.value == {"messages": [{"content": marker}]}
        literal_wildcard = await store.query_trace_entries(
            snapshot.key,
            run_ids=("entry-query",),
            where=TraceFilter(
                kinds={TraceEntryKind.PROVIDER},
                search="%",
            ),
            limit=10,
        )
        assert literal_wildcard.entries == ()
        async with engine.connect() as connection:
            plan = (
                (
                    await connection.execute(
                        text(
                            "EXPLAIN SELECT entry_id FROM tinkerfin_trace_entries "
                            "WHERE namespace_hash = SHA2(:namespace, 256) "
                            "AND thread_hash = SHA2(:thread_id, 256) "
                            "AND generation = :generation "
                            "AND run_hash = SHA2(:run_id, 256) "
                            "AND kind = 'model'"
                        ),
                        {
                            "namespace": namespace,
                            "thread_id": snapshot.key.thread_id,
                            "generation": snapshot.key.generation,
                            "run_id": "entry-query",
                        },
                    )
                )
                .mappings()
                .one()
            )
        assert plan["key"] in {
            "ix_tinkerfin_trace_entries_run",
            "ix_tinkerfin_trace_entries_kind_status",
        }
    finally:
        await writer.aclose()
        await engine.dispose()


def _backend(store: SqlAlchemyTraceStore) -> _SqlAlchemyTraceLedgerBackend:
    backend = store.backend
    assert isinstance(backend, _SqlAlchemyTraceLedgerBackend)
    return backend


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
    options = TraceStoreOptions(
        writer_lease_seconds=2,
        writer_heartbeat_interval_seconds=0.5,
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
    options = TraceStoreOptions(
        writer_lease_seconds=0.6,
        writer_heartbeat_interval_seconds=0.2,
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
from tinkerfin_tracing import RunFact, TraceStoreOptions
from tinkerfin_tracing.sql_store import SqlAlchemyTraceStore

async def main():
    url, namespace = sys.argv[1:]
    engine = create_async_engine(url)
    store = SqlAlchemyTraceStore(
        engine,
        namespace=namespace,
        options=TraceStoreOptions(
            writer_lease_seconds=0.6,
            writer_heartbeat_interval_seconds=0.2,
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
        options=TraceStoreOptions(
            commit_retry_attempts=2,
            commit_retry_delay_seconds=0.001,
        ),
    )
    identity = _identity(f"retry-{error_code}")
    writer = await store.open_writer(identity)
    original = _backend(store)._raw_write_connection
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
        monkeypatch.setattr(_backend(store), "_raw_write_connection", fail_after_commit)
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
        monkeypatch.setattr(_backend(store), "_raw_write_connection", original)
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
        options=TraceStoreOptions(
            writer_lease_seconds=10,
            writer_heartbeat_interval_seconds=5,
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


async def test_mysql_backend_satisfies_public_cross_instance_verifier() -> None:
    first_engine = create_async_engine(_url(), pool_pre_ping=True)
    second_engine = create_async_engine(_url(), pool_pre_ping=True)
    namespace = f"mysql-backend-contract-{uuid4().hex}"
    options = TraceStoreOptions(
        writer_lease_seconds=3,
        writer_heartbeat_interval_seconds=0.5,
        follow_poll_seconds=0.01,
        commit_retry_attempts=10,
        commit_retry_delay_seconds=0.001,
    )
    try:
        await verify_trace_ledger_backend(
            _SqlAlchemyTraceLedgerBackend(
                first_engine,
                namespace=namespace,
                options=options,
            ),
            _SqlAlchemyTraceLedgerBackend(
                second_engine,
                namespace=namespace,
                options=options,
            ),
            namespace=namespace,
            options=options,
        )
    finally:
        await first_engine.dispose()
        await second_engine.dispose()


async def test_mysql_writer_lease_starts_after_namespace_lock_wait() -> None:
    engine = create_async_engine(_url(), pool_pre_ping=True)
    namespace = f"mysql-lease-clock-{uuid4().hex}"
    identity = _identity("lease-clock")
    store = SqlAlchemyTraceStore(
        engine,
        namespace=namespace,
        options=TraceStoreOptions(
            writer_lease_seconds=1,
            writer_heartbeat_interval_seconds=0.9,
            commit_retry_attempts=5,
            commit_retry_delay_seconds=0.001,
        ),
    )
    await store.setup()
    writer = None
    open_writer = None
    try:
        async with engine.connect() as blocker:
            async with blocker.begin():
                await blocker.execute(
                    text(
                        "SELECT namespace_hash FROM tinkerfin_trace_namespaces "
                        "WHERE namespace = :namespace FOR UPDATE"
                    ),
                    {"namespace": namespace},
                )
                open_writer = asyncio.create_task(store.open_writer(identity))
                await asyncio.sleep(1.2)
                assert not open_writer.done()
        writer = await asyncio.wait_for(open_writer, timeout=5)
        async with engine.connect() as connection:
            remaining_microseconds = await connection.scalar(
                text(
                    "SELECT TIMESTAMPDIFF(MICROSECOND, UTC_TIMESTAMP(6), "
                    "lease_expires_at) FROM tinkerfin_trace_writers "
                    "WHERE namespace_hash = SHA2(:namespace, 256) AND run_id = :run_id"
                ),
                {"namespace": namespace, "run_id": identity.run_id},
            )
        assert isinstance(remaining_microseconds, int)
        assert remaining_microseconds > 500_000
    finally:
        if open_writer is not None and not open_writer.done():
            open_writer.cancel()
            await asyncio.gather(open_writer, return_exceptions=True)
        if writer is not None:
            await writer.aclose()
        await engine.dispose()
