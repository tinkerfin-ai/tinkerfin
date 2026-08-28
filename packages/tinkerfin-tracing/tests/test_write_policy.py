"""Bounded Trace batching, backpressure, cancellation, and terminal settlement."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Literal

import pytest

from tinkerfin_contracts import RunIdentity
from tinkerfin_tracing.facts import RunFact, TraceEvent, TraceSemanticFact
from tinkerfin_tracing.store import InMemoryTraceStore, TraceThreadKey, TraceWriter
from tinkerfin_tracing.writing import TraceBatchWriter, TraceWritePolicy


def _identity() -> RunIdentity:
    return RunIdentity(threadId="thread-write", runId="run-write")


def _fact(index: int) -> RunFact:
    return RunFact(
        source_observation_id=f"observation-{index}",
        identity=_identity(),
        occurred_at=datetime.now(UTC),
        monotonic_ns=index,
        phase="started",
        input_kind="ordinary",
    )


def _terminal(phase: Literal["terminal", "closed"]) -> RunFact:
    return RunFact(
        source_observation_id=f"observation-{phase}",
        identity=_identity(),
        occurred_at=datetime.now(UTC),
        monotonic_ns=100,
        phase=phase,
        outcome="succeeded",
    )


class _RecordingWriter:
    def __init__(self, writer: TraceWriter) -> None:
        self._writer = writer
        self.calls: list[tuple[int, bool]] = []

    @property
    def key(self) -> TraceThreadKey:
        return self._writer.key

    @property
    def run_id(self) -> str:
        return self._writer.run_id

    async def append(
        self,
        facts: tuple[TraceSemanticFact, ...],
        *,
        mandatory: bool = False,
    ) -> tuple[TraceEvent, ...]:
        self.calls.append((len(facts), mandatory))
        return await self._writer.append(facts, mandatory=mandatory)

    async def aclose(self) -> None:
        await self._writer.aclose()


class _GateWriter(_RecordingWriter):
    def __init__(self, writer: TraceWriter) -> None:
        super().__init__(writer)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def append(
        self,
        facts: tuple[TraceSemanticFact, ...],
        *,
        mandatory: bool = False,
    ) -> tuple[TraceEvent, ...]:
        self.started.set()
        await self.release.wait()
        return await super().append(facts, mandatory=mandatory)


async def _ignore_committed(_events: tuple[TraceEvent, ...]) -> None:
    return None


async def test_policy_batches_ordinary_facts_and_isolates_mandatory_terminal() -> None:
    store = InMemoryTraceStore()
    recording = _RecordingWriter(await store.open_writer(_identity()))
    writer = TraceBatchWriter(
        recording,
        policy=TraceWritePolicy(max_batch_delay_seconds=0.05),
        on_committed=_ignore_committed,
    )

    await writer.submit((_fact(1),), mandatory=False)
    await writer.submit((_fact(2),), mandatory=False)
    await writer.force()
    await writer.submit(
        (_terminal("terminal"), _terminal("closed")),
        mandatory=True,
    )
    await writer.aclose()

    assert recording.calls == [(2, False), (2, True)]


async def test_pending_budget_backpressures_and_cancelled_waiter_is_not_enqueued() -> (
    None
):
    store = InMemoryTraceStore()
    gate = _GateWriter(await store.open_writer(_identity()))
    writer = TraceBatchWriter(
        gate,
        policy=TraceWritePolicy(
            max_batch_events=1,
            max_pending_events=2,
            max_batch_delay_seconds=0,
        ),
        on_committed=_ignore_committed,
    )

    await writer.submit((_fact(1),), mandatory=False)
    await gate.started.wait()
    await writer.submit((_fact(2),), mandatory=False)
    blocked = asyncio.create_task(writer.submit((_fact(3),), mandatory=False))
    await asyncio.sleep(0)
    assert not blocked.done()

    blocked.cancel()
    with pytest.raises(asyncio.CancelledError):
        await blocked
    gate.release.set()
    await writer.force()
    snapshot = await store.snapshot(_identity().thread_id)
    assert snapshot.as_of_seq == 2
    await writer.aclose()


async def test_cancelled_force_does_not_cancel_owned_commit_or_leave_tasks() -> None:
    store = InMemoryTraceStore()
    gate = _GateWriter(await store.open_writer(_identity()))
    writer = TraceBatchWriter(
        gate,
        policy=TraceWritePolicy(max_batch_events=1, max_batch_delay_seconds=0),
        on_committed=_ignore_committed,
    )
    await writer.submit((_fact(1),), mandatory=False)
    await gate.started.wait()

    forcing = asyncio.create_task(writer.force())
    await asyncio.sleep(0)
    forcing.cancel("caller stopped waiting")
    with pytest.raises(asyncio.CancelledError, match="caller stopped waiting"):
        await forcing

    gate.release.set()
    await writer.force()
    await writer.aclose()
    snapshot = await store.snapshot(_identity().thread_id)
    assert snapshot.as_of_seq == 1
    await asyncio.sleep(0)
    live_names = {
        task.get_name()
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task() and not task.done()
    }
    assert not {
        name for name in live_names if name.startswith("tinkerfin-trace-writer")
    }
