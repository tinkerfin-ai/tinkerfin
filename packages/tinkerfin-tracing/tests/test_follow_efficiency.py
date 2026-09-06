"""Idle SQL budgets and lossless, thread-specific follow notifications."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tinkerfin_contracts import RunIdentity
from tinkerfin_tracing import (
    DurableTraceStore,
    InMemoryTraceStore,
    RunFact,
    SqlAlchemyTraceStore,
    TraceEvent,
    Tracer,
    TraceStore,
    TraceStoreOptions,
    TraceStoreUpdate,
    TraceThreadNotFound,
)
from tinkerfin_tracing.backend import StoredTraceEventPage, TraceEventPageRequest


@pytest.fixture(
    params=("sqlite", pytest.param("mysql", marks=pytest.mark.docker_integration))
)
async def trace_engine(
    request: pytest.FixtureRequest, tmp_path: Path
) -> AsyncIterator[AsyncEngine]:
    url = (
        request.getfixturevalue("mysql_admin_url")
        if request.param == "mysql"
        else f"sqlite+aiosqlite:///{tmp_path / 'follow.db'}"
    )
    assert isinstance(url, str)
    engine = create_async_engine(url)
    try:
        yield engine
    finally:
        await engine.dispose()


def _fact(identity: RunIdentity, phase: Literal["started", "terminal"]) -> RunFact:
    return RunFact(
        identity=identity,
        source_observation_id=f"{identity.run_id}-{phase}",
        occurred_at=datetime.now(UTC),
        monotonic_ns=1,
        phase=phase,
        input_kind="ordinary" if phase == "started" else None,
        outcome="succeeded" if phase == "terminal" else None,
    )


async def _write_run(store: TraceStore, *, thread: str, run: str) -> None:
    identity = RunIdentity(threadId=thread, runId=run)
    writer = await store.open_writer(identity)
    try:
        await writer.append((_fact(identity, "started"),))
        await writer.append((_fact(identity, "terminal"),), mandatory=True)
    finally:
        await writer.aclose()


async def _next_events(
    follower: AsyncIterator[TraceStoreUpdate],
) -> tuple[TraceEvent, ...]:
    async for update in follower:
        if update.events:
            return update.events
    raise AssertionError("follower ended before event delivery")


@pytest.mark.parametrize("graph_follow", (False, True))
async def test_idle_follow_uses_one_tail_read_and_stops_on_cancel(
    trace_engine: AsyncEngine,
    graph_follow: bool,
) -> None:
    store = SqlAlchemyTraceStore(trace_engine, namespace=f"idle-{uuid4().hex}")
    await _write_run(store, thread="idle", run="first")
    snapshot = await store.snapshot("idle")
    graph = await Tracer(store=store).query("idle")
    statements: list[str] = []
    first_select = asyncio.Event()

    def count_select(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _many: bool,
    ) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)
            first_select.set()

    event.listen(trace_engine.sync_engine, "before_cursor_execute", count_select)
    follower = (
        graph.follow()
        if graph_follow
        else store.follow(snapshot.key, after_seq=snapshot.as_of_seq)
    )

    async def next_update() -> None:
        async for update in follower:
            if not isinstance(update, TraceStoreUpdate) or update.events:
                return

    pending = asyncio.create_task(next_update())
    try:
        await asyncio.wait_for(first_select.wait(), timeout=2)
        await asyncio.sleep(0.15)
        assert not pending.done()
        assert len(statements) == 1
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await follower.aclose()
        count_after_close = len(statements)
        await asyncio.sleep(0.05)
        assert len(statements) == count_after_close
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await follower.aclose()
        event.remove(trace_engine.sync_engine, "before_cursor_execute", count_select)
        await store.delete(snapshot.key)


async def test_commit_between_empty_read_and_wait_is_not_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = DurableTraceStore(
        InMemoryTraceStore().backend, options=TraceStoreOptions(follow_poll_seconds=10)
    )
    await _write_run(store, thread="followed", run="first")
    snapshot = await store.snapshot("followed")
    empty_read = asyncio.Event()
    release_read = asyncio.Event()
    original_read = store.backend.read_event_page
    reads = 0

    async def gate_empty_page(request: TraceEventPageRequest) -> StoredTraceEventPage:
        nonlocal reads
        page = await original_read(request)
        reads += 1
        if reads == 1:
            assert not page.events
            empty_read.set()
            await release_read.wait()
        return page

    monkeypatch.setattr(store.backend, "read_event_page", gate_empty_page)
    follower = store.follow(snapshot.key, after_seq=snapshot.as_of_seq)
    pending = asyncio.create_task(_next_events(follower))
    try:
        await asyncio.wait_for(empty_read.wait(), timeout=1)
        await _write_run(store, thread="followed", run="second")
        release_read.set()
        batch = await asyncio.wait_for(pending, timeout=0.5)
        assert [item.trace_seq for item in batch] == [3, 4]
        assert {item.fact.identity.run_id for item in batch} == {"second"}
    finally:
        release_read.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await follower.aclose()


async def test_other_threads_do_not_wake_an_idle_follower(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = DurableTraceStore(
        InMemoryTraceStore().backend, options=TraceStoreOptions(follow_poll_seconds=10)
    )
    await _write_run(store, thread="followed", run="first")
    snapshot = await store.snapshot("followed")
    first_read = asyncio.Event()
    original_read = store.backend.read_event_page
    reads = 0

    async def count_read(request: TraceEventPageRequest) -> StoredTraceEventPage:
        nonlocal reads
        page = await original_read(request)
        reads += 1
        first_read.set()
        return page

    monkeypatch.setattr(store.backend, "read_event_page", count_read)
    follower = store.follow(snapshot.key, after_seq=snapshot.as_of_seq)
    pending = asyncio.create_task(_next_events(follower))
    try:
        await asyncio.wait_for(first_read.wait(), timeout=1)
        await _write_run(store, thread="unrelated", run="other")
        await asyncio.sleep(0.02)
        assert reads == 1
        await store.delete(snapshot.key)
        with pytest.raises(TraceThreadNotFound):
            await asyncio.wait_for(pending, timeout=0.5)
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await follower.aclose()


async def test_closing_one_follower_preserves_the_other_local_notification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = DurableTraceStore(
        InMemoryTraceStore().backend, options=TraceStoreOptions(follow_poll_seconds=10)
    )
    await _write_run(store, thread="shared", run="first")
    snapshot = await store.snapshot("shared")
    ready = asyncio.Event()
    original_read = store.backend.read_event_page
    reads = 0

    async def count_read(request: TraceEventPageRequest) -> StoredTraceEventPage:
        nonlocal reads
        page = await original_read(request)
        reads += 1
        if reads == 2:
            ready.set()
        return page

    monkeypatch.setattr(store.backend, "read_event_page", count_read)
    first = store.follow(snapshot.key, after_seq=snapshot.as_of_seq)
    second = store.follow(snapshot.key, after_seq=snapshot.as_of_seq)
    first_pull = asyncio.create_task(_next_events(first))
    second_pull = asyncio.create_task(_next_events(second))
    try:
        await asyncio.wait_for(ready.wait(), timeout=1)
        first_pull.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_pull
        await first.aclose()
        await _write_run(store, thread="shared", run="second")
        batch = await asyncio.wait_for(second_pull, timeout=0.5)
        sequences = [item.trace_seq for item in batch]
        if sequences == [3]:
            sequences.extend(
                item.trace_seq
                for item in await asyncio.wait_for(_next_events(second), 0.5)
            )
        assert sequences == [3, 4]
    finally:
        for pending in (first_pull, second_pull):
            if not pending.done():
                pending.cancel()
        await asyncio.gather(first_pull, second_pull, return_exceptions=True)
        await first.aclose()
        await second.aclose()


@pytest.mark.parametrize("close_before_follow", (False, True))
async def test_follow_reports_a_writer_close_without_inventing_terminal_facts(
    close_before_follow: bool,
) -> None:
    store = InMemoryTraceStore()
    tracer = Tracer(store=store)
    identity = RunIdentity(threadId="unsettled", runId="run")
    writer = await store.open_writer(identity)
    await writer.append((_fact(identity, "started"),))
    trace = await tracer.get(identity.thread_id)
    assert trace.summary.status.execution == "running"
    follower = trace.follow()
    if close_before_follow:
        await writer.aclose()
    pending = asyncio.create_task(anext(follower))
    try:
        if not close_before_follow:
            await asyncio.sleep(0.02)
            await writer.aclose()
        current = await tracer.get(identity.thread_id)
        assert current.summary.status.execution == "unknown"
        update = await asyncio.wait_for(pending, timeout=0.5)
        assert update.as_of_seq == trace.as_of_seq
        assert update.summary == current.summary
        assert update.events == update.facts == ()
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await follower.aclose()
        await writer.aclose()


async def test_sql_follow_observes_expired_remote_writer_without_new_events(
    trace_engine: AsyncEngine,
) -> None:
    namespace = f"expired-{uuid4().hex}"
    options = TraceStoreOptions(follow_poll_seconds=0.02)
    writer_store = SqlAlchemyTraceStore(
        trace_engine, namespace=namespace, options=options
    )
    reader_store = SqlAlchemyTraceStore(
        trace_engine, namespace=namespace, options=options
    )
    tracer = Tracer(store=reader_store)
    identity = RunIdentity(threadId="expired", runId="run")
    writer = await writer_store.open_writer(identity)
    await writer.append((_fact(identity, "started"),))
    trace = await tracer.get(identity.thread_id)
    graph = await tracer.query(identity.thread_id)
    follower = trace.follow()
    pending = asyncio.create_task(anext(follower))
    try:
        await asyncio.sleep(0.02)
        async with trace_engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE tinkerfin_trace_writers "
                    "SET lease_expires_at = :expired "
                    "WHERE generation = :generation AND run_id = :run_id"
                ),
                {
                    "expired": datetime(2000, 1, 1),
                    "generation": trace.key.generation,
                    "run_id": identity.run_id,
                },
            )
        update = await asyncio.wait_for(pending, timeout=1)
        assert update.summary.status.execution == "unknown"
        assert update.summary.completeness.missing_tail
        assert update.as_of_seq == trace.as_of_seq
        assert update.events == update.facts == ()
        assert (await tracer.query(identity.thread_id)).snapshot == graph.snapshot
        current = await tracer.get(identity.thread_id)
        assert update.summary == current.summary
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await follower.aclose()
        await writer.aclose()
        await writer_store.delete(trace.key)


async def test_follow_observes_same_sequence_writer_recovery_and_one_terminal(
    trace_engine: AsyncEngine,
) -> None:
    store = SqlAlchemyTraceStore(
        trace_engine,
        namespace=f"recovered-{uuid4().hex}",
        options=TraceStoreOptions(follow_poll_seconds=0.02),
    )
    tracer = Tracer(store=store)
    identity = RunIdentity(threadId="resumed-owner", runId="run")
    writer = await store.open_writer(identity)
    await writer.append((_fact(identity, "started"),))
    trace = await tracer.get(identity.thread_id)
    follower = trace.follow()
    try:
        async with trace_engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE tinkerfin_trace_writers "
                    "SET lease_expires_at = :expired "
                    "WHERE generation = :generation AND run_id = :run_id"
                ),
                {
                    "expired": datetime(2000, 1, 1),
                    "generation": trace.key.generation,
                    "run_id": identity.run_id,
                },
            )
        lost = await asyncio.wait_for(anext(follower), 1)
        assert lost.status.execution == "unknown"
        assert lost.as_of_seq == trace.as_of_seq
        replacement = await store.open_writer(identity)
        try:
            recovered = await asyncio.wait_for(anext(follower), 1)
            assert recovered.status.execution == "running"
            assert recovered.observed_at >= lost.observed_at
            assert recovered.as_of_seq == lost.as_of_seq
            assert recovered.events == ()
            await replacement.append((_fact(identity, "terminal"),), mandatory=True)
            terminal = await asyncio.wait_for(anext(follower), 1)
            assert terminal.status.execution == "succeeded"
            assert [event.fact for event in terminal.events] == list(terminal.facts)
            assert len(terminal.events) == 1
            await replacement.aclose()
            pending = asyncio.create_task(anext(follower))
            await asyncio.sleep(0.02)
            assert not pending.done()
            await follower.aclose()
            with pytest.raises(asyncio.CancelledError):
                await pending
        finally:
            await replacement.aclose()
    finally:
        await follower.aclose()
        await writer.aclose()
        await store.delete(trace.key)
