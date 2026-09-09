"""Shared observable Trace Store contract for in-memory and SQLite implementations."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest
from sqlalchemy import event
from sqlalchemy.engine import ExceptionContext
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from tinkerfin_contracts import RunIdentity
from tinkerfin_tracing import (
    InMemoryTraceStore,
    RunFact,
    SqlAlchemyTraceStore,
    TraceProjectionCheckpoint,
    TraceStore,
    TraceStoreOptions,
    TraceThreadKey,
    verify_trace_ledger_backend,
)
from tinkerfin_tracing.backend import TraceLedgerChange
from tinkerfin_tracing.errors import (
    TraceProjectionCheckpointConflict,
    TraceRunConflict,
    TraceStoreProtocolError,
    TraceThreadNotFound,
)
from tinkerfin_tracing.sql_store import _SqlAlchemyTraceLedgerBackend


def _identity(run_id: str = "run-contract") -> RunIdentity:
    return RunIdentity(threadId="thread-contract", runId=run_id)


def _fact(
    run_id: str,
    phase: Literal["started", "terminal", "closed"],
) -> RunFact:
    return RunFact(
        source_observation_id=f"contract-{run_id}-{phase}",
        identity=_identity(run_id),
        occurred_at=datetime.now(UTC),
        monotonic_ns=1,
        phase=phase,
        input_kind="ordinary" if phase == "started" else None,
        outcome="succeeded" if phase in {"terminal", "closed"} else None,
    )


async def _exercise_store_contract(store: TraceStore) -> None:
    assert isinstance(store, TraceStore)

    empty = await store.open_writer(_identity())
    empty_generation = empty.key.generation
    await asyncio.gather(empty.aclose(), empty.aclose())
    with pytest.raises(TraceThreadNotFound):
        await store.snapshot(_identity().thread_id)

    first = await store.open_writer(_identity())
    second = await store.open_writer(_identity("run-second"))
    assert first.key.generation != empty_generation
    assert first.key == second.key
    with pytest.raises(TraceRunConflict):
        await store.open_writer(_identity())

    first_events, second_events = await asyncio.gather(
        first.append((_fact("run-contract", "started"),)),
        second.append((_fact("run-second", "started"),)),
    )
    assert {first_events[0].trace_seq, second_events[0].trace_seq} == {1, 2}
    snapshot = await store.snapshot(_identity().thread_id)
    assert snapshot.active_run_ids == ("run-contract", "run-second")

    await first.append((_fact("run-contract", "terminal"),), mandatory=True)
    with pytest.raises(TraceStoreProtocolError, match="cannot follow"):
        await first.append((_fact("run-contract", "started"),))
    await first.append((_fact("run-contract", "closed"),), mandatory=True)
    with pytest.raises(TraceStoreProtocolError, match="already committed"):
        await first.append((_fact("run-contract", "closed"),), mandatory=True)
    await first.aclose()
    await second.append(
        (
            _fact("run-second", "terminal"),
            _fact("run-second", "closed"),
        ),
        mandatory=True,
    )
    await second.aclose()

    snapshot = await store.snapshot(_identity().thread_id)
    assert snapshot.as_of_seq == 6
    assert snapshot.active_writers == ()
    assert [
        event.trace_seq
        for event in await store.read_events(
            snapshot.key,
            after_seq=0,
            as_of_seq=snapshot.as_of_seq,
            limit=10,
        )
    ] == [1, 2, 3, 4, 5, 6]
    assert [
        event.trace_seq
        for event in await store.read_events_reverse(
            snapshot.key,
            before_seq=7,
            limit=2,
        )
    ] == [6, 5]

    with pytest.raises(ValueError):
        await store.read_events(snapshot.key, after_seq=-1, as_of_seq=6, limit=1)
    with pytest.raises(ValueError):
        await store.read_events_reverse(snapshot.key, before_seq=8, limit=1)
    wrong_namespace = snapshot.key.model_copy(update={"namespace": "another"})
    with pytest.raises(TraceThreadNotFound):
        await store.snapshot_key(wrong_namespace)

    checkpoint = TraceProjectionCheckpoint(
        key=snapshot.key,
        projection_name="contract.projection",
        run_id="run-contract",
        as_of_seq=4,
        state={"count": 4},
    )
    assert (
        await store.save_projection_checkpoint(
            checkpoint,
            expected_as_of_seq=None,
        )
        == checkpoint
    )
    # An exact retry proves an unknown commit instead of creating another row.
    assert (
        await store.save_projection_checkpoint(
            checkpoint.model_copy(deep=True),
            expected_as_of_seq=None,
        )
        == checkpoint
    )
    with pytest.raises(TraceStoreProtocolError, match="idempotency"):
        await store.save_projection_checkpoint(
            checkpoint.model_copy(update={"state": {"count": 5}}),
            expected_as_of_seq=None,
        )
    with pytest.raises(TraceProjectionCheckpointConflict):
        await store.save_projection_checkpoint(
            checkpoint.model_copy(update={"as_of_seq": 5}),
            expected_as_of_seq=None,
        )
    with pytest.raises(TraceStoreProtocolError, match="committed Trace prefix"):
        await store.save_projection_checkpoint(
            checkpoint.model_copy(update={"as_of_seq": 7}),
            expected_as_of_seq=4,
        )

    await store.delete(snapshot.key)
    with pytest.raises(TraceThreadNotFound):
        await store.snapshot_key(snapshot.key)
    replacement = await store.open_writer(_identity())
    assert replacement.key.generation != snapshot.key.generation
    await replacement.aclose()


async def test_in_memory_store_satisfies_shared_contract() -> None:
    await _exercise_store_contract(InMemoryTraceStore())


async def test_sqlite_store_satisfies_shared_contract(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'contract.db'}")
    try:
        await _exercise_store_contract(
            SqlAlchemyTraceStore(engine, namespace="contract")
        )
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    "held_peer_close", (False, True), ids=("ordinary", "held-close")
)
async def test_sqlite_backend_satisfies_public_cross_instance_verifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    held_peer_close: bool,
) -> None:
    database = tmp_path / "backend-contract.db"
    first_engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    second_engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    # Conformance checks shared data, not transaction latency. Allow a bounded
    # 5.25 seconds of retry backoff; exhaustion is tested separately.
    options = TraceStoreOptions(
        commit_retry_attempts=15, commit_retry_delay_seconds=0.05
    )
    primary = _SqlAlchemyTraceLedgerBackend(
        first_engine, namespace="sqlite-backend-contract", options=options
    )
    peer = _SqlAlchemyTraceLedgerBackend(
        second_engine, namespace="sqlite-backend-contract", options=options
    )
    peer_locked = asyncio.Event()
    close_contended = asyncio.Event()
    if held_peer_close:
        original_primary_commit = primary.commit_ledger_change
        original_peer_commit = peer.commit_ledger_change
        original_peer_transaction = peer._raw_write_connection

        def observe_contention(context: ExceptionContext) -> None:
            error = context.original_exception
            if (
                peer_locked.is_set()
                and context.statement == "BEGIN IMMEDIATE"
                and isinstance(error, sqlite3.Error)
                and getattr(error, "sqlite_errorcode", 0) & 0xFF == sqlite3.SQLITE_BUSY
            ):
                close_contended.set()

        event.listen(first_engine.sync_engine, "handle_error", observe_contention)

        @asynccontextmanager
        async def held_transaction() -> AsyncIterator[AsyncConnection]:
            async with original_peer_transaction() as connection:
                peer_locked.set()
                # Release only after the competing client observes a real busy
                # error, without imposing a transaction-speed assumption.
                async with asyncio.timeout(10):
                    await close_contended.wait()
                yield connection

        async def primary_commit(change: TraceLedgerChange):
            if change.kind == "close_writer" and change.run_id == "contract-first":
                async with asyncio.timeout(10):
                    await peer_locked.wait()
            return await original_primary_commit(change)

        async def peer_commit(change: TraceLedgerChange):
            if change.kind == "close_writer" and change.run_id == "contract-second":
                with monkeypatch.context() as patch:
                    patch.setattr(peer, "_raw_write_connection", held_transaction)
                    return await original_peer_commit(change)
            return await original_peer_commit(change)

        monkeypatch.setattr(primary, "commit_ledger_change", primary_commit)
        monkeypatch.setattr(peer, "commit_ledger_change", peer_commit)
    try:
        await verify_trace_ledger_backend(
            primary,
            peer,
            namespace="sqlite-backend-contract",
            options=options,
        )
        assert peer_locked.is_set() is held_peer_close
        assert close_contended.is_set() is held_peer_close
    finally:
        try:
            await first_engine.dispose()
        finally:
            await second_engine.dispose()


async def test_sqlite_concurrent_schema_first_start_uses_one_current_shape(
    tmp_path: Path,
) -> None:
    database = tmp_path / "concurrent-setup.db"
    first_engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    second_engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    first = SqlAlchemyTraceStore(first_engine, namespace="setup")
    second = SqlAlchemyTraceStore(second_engine, namespace="setup")
    try:
        await asyncio.gather(first.setup(), second.setup())
        writers = await asyncio.gather(
            first.open_writer(_identity("first")),
            second.open_writer(_identity("second")),
        )
        assert writers[0].key == writers[1].key
        await asyncio.gather(*(writer.aclose() for writer in writers))
    finally:
        await first_engine.dispose()
        await second_engine.dispose()


def test_trace_store_key_is_the_only_generation_handle() -> None:
    """Keep the shared contract free of SQL connection or third-party models."""

    assert set(TraceThreadKey.model_fields) == {"namespace", "thread_id", "generation"}
