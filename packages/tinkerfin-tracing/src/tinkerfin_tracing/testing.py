"""Executable conformance checks for third-party Trace Ledger backends."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from tinkerfin_contracts import RunIdentity

from .backend import TraceLedgerBackend, TraceStoreOptions
from .durable_store import DurableTraceStore
from .errors import TraceThreadNotFound
from .facts import RunFact, TraceEvent
from .store import TraceProjectionCheckpoint, TraceThreadKey, TraceWriter


async def verify_trace_ledger_backend(
    primary_backend: TraceLedgerBackend,
    peer_backend: TraceLedgerBackend,
    *,
    namespace: str | None = None,
    options: TraceStoreOptions | None = None,
) -> None:
    """Verify shared ordering, replay, checkpoints, following, and deletion.

    Both backend values must use independent client instances connected to the same
    physical storage. The verifier creates a random namespace by default, never closes
    either borrowed backend, and deletes the Trace generation it creates. A backend
    still needs provider-specific fault injection for transaction interruption,
    storage-clock failure, and uncertain commit responses.

    Args:
        primary_backend: First backend instance used for writer ownership and reads.
        peer_backend: Independent instance observing and mutating the same storage.
        namespace: Optional empty test namespace controlled by the caller.
        options: Optional short test lease and polling settings.

    Raises:
        TypeError: A backend does not implement the complete public protocol.
        AssertionError: An observable shared Ledger invariant fails.
        TraceStoreError: The backend cannot complete a required operation.
    """

    if not isinstance(primary_backend, TraceLedgerBackend):
        raise TypeError("primary_backend must implement TraceLedgerBackend")
    if not isinstance(peer_backend, TraceLedgerBackend):
        raise TypeError("peer_backend must implement TraceLedgerBackend")
    if primary_backend is peer_backend:
        raise ValueError("backend verifier requires two independent instances")
    resolved_namespace = namespace or f"trace-backend-contract-{uuid4().hex}"
    resolved_options = options or TraceStoreOptions(
        writer_lease_seconds=2.0,
        writer_heartbeat_interval_seconds=0.5,
        follow_poll_seconds=0.01,
        commit_retry_attempts=5,
        commit_retry_delay_seconds=0.001,
    )
    primary = DurableTraceStore(
        primary_backend,
        namespace=resolved_namespace,
        options=resolved_options,
    )
    peer = DurableTraceStore(
        peer_backend,
        namespace=resolved_namespace,
        options=resolved_options,
    )
    await asyncio.gather(primary.setup(), peer.setup())

    thread_id = f"thread-{uuid4().hex}"
    first_identity = RunIdentity(threadId=thread_id, runId="contract-first")
    second_identity = RunIdentity(threadId=thread_id, runId="contract-second")
    writers: list[TraceWriter] = []
    generation_key: TraceThreadKey | None = None
    follower: AsyncGenerator[tuple[TraceEvent, ...], None] | None = None
    waiting: asyncio.Task[tuple[TraceEvent, ...]] | None = None
    primary_error: BaseException | None = None
    cleanup_errors: list[BaseException] = []
    try:
        first = await primary.open_writer(first_identity)
        writers.append(first)
        generation_key = first.key
        second = await peer.open_writer(second_identity)
        writers.append(second)
        first_events, second_events = await asyncio.gather(
            first.append((_run_fact(first_identity, "started"),)),
            second.append((_run_fact(second_identity, "started"),)),
        )
        assert {first_events[0].trace_seq, second_events[0].trace_seq} == {1, 2}
        snapshot = await peer.snapshot(thread_id)
        assert snapshot.as_of_seq == 2
        assert snapshot.active_run_ids == ("contract-first", "contract-second")

        await first.append(
            (
                _run_fact(first_identity, "terminal"),
                _run_fact(first_identity, "closed"),
            ),
            mandatory=True,
        )
        await second.append(
            (
                _run_fact(second_identity, "terminal"),
                _run_fact(second_identity, "closed"),
            ),
            mandatory=True,
        )
        await asyncio.gather(first.aclose(), second.aclose())
        snapshot = await primary.snapshot(thread_id)
        assert snapshot.as_of_seq == 6
        assert snapshot.active_writers == ()
        assert [
            event.trace_seq
            for event in await peer.read_events(
                snapshot.key,
                after_seq=0,
                as_of_seq=6,
                limit=10,
            )
        ] == [1, 2, 3, 4, 5, 6]
        assert [
            event.trace_seq
            for event in await primary.read_events_reverse(
                snapshot.key,
                before_seq=7,
                limit=2,
            )
        ] == [6, 5]

        checkpoint = TraceProjectionCheckpoint(
            key=snapshot.key,
            projection_name="contract.counter",
            run_id="contract-first",
            as_of_seq=4,
            state={"count": 4},
        )
        assert (
            await primary.save_projection_checkpoint(
                checkpoint,
                expected_as_of_seq=None,
            )
            == checkpoint
        )
        assert (
            await peer.load_projection_checkpoint(
                snapshot.key,
                projection_name="contract.counter",
                run_id="contract-first",
                as_of_seq=4,
            )
            == checkpoint
        )

        follower = peer.follow(snapshot.key, after_seq=snapshot.as_of_seq)
        third_identity = RunIdentity(threadId=thread_id, runId="contract-third")
        third = await primary.open_writer(third_identity)
        writers.append(third)
        waiting = asyncio.create_task(anext(follower))
        await third.append((_run_fact(third_identity, "started"),))
        followed = await asyncio.wait_for(waiting, timeout=2)
        waiting = None
        assert [event.trace_seq for event in followed] == [7]
        await third.append(
            (
                _run_fact(third_identity, "terminal"),
                _run_fact(third_identity, "closed"),
            ),
            mandatory=True,
        )
    except BaseException as error:  # noqa: BLE001 - cleanup preserves primary failure
        primary_error = error
    finally:
        if waiting is not None:
            waiting.cancel()
            waiting_result = await asyncio.gather(waiting, return_exceptions=True)
            cleanup_errors.extend(
                item
                for item in waiting_result
                if isinstance(item, BaseException)
                and not isinstance(item, asyncio.CancelledError)
            )
        if follower is not None:
            try:
                await follower.aclose()
            except BaseException as error:  # noqa: BLE001 - retain cleanup evidence
                cleanup_errors.append(error)
        writer_results = await asyncio.gather(
            *(writer.aclose() for writer in reversed(writers)),
            return_exceptions=True,
        )
        cleanup_errors.extend(
            item for item in writer_results if isinstance(item, BaseException)
        )
        if generation_key is not None:
            try:
                await primary.delete(generation_key)
            except TraceThreadNotFound:
                pass
            except BaseException as error:  # noqa: BLE001 - retain cleanup evidence
                cleanup_errors.append(error)

    if primary_error is not None:
        for error in cleanup_errors:
            primary_error.add_note(
                "Trace Backend verifier cleanup also failed: "
                f"{type(error).__name__}: {error}"
            )
        raise primary_error.with_traceback(primary_error.__traceback__)
    if cleanup_errors:
        cleanup_error = cleanup_errors[0]
        for error in cleanup_errors[1:]:
            cleanup_error.add_note(
                "Additional Trace Backend verifier cleanup failure: "
                f"{type(error).__name__}: {error}"
            )
        raise cleanup_error
    if generation_key is None:
        raise AssertionError("Trace Backend verifier did not create a generation")
    try:
        await peer.snapshot_key(generation_key)
    except TraceThreadNotFound:
        pass
    else:
        raise AssertionError("deleted Trace generation remained visible to peer")


def _run_fact(
    identity: RunIdentity,
    phase: Literal["started", "terminal", "closed"],
) -> RunFact:
    terminal = phase in {"terminal", "closed"}
    return RunFact(
        source_observation_id=f"contract-{identity.run_id}-{phase}",
        identity=identity,
        occurred_at=datetime.now(UTC),
        monotonic_ns=1,
        phase=phase,
        input_kind=None if terminal else "ordinary",
        outcome="succeeded" if terminal else None,
    )


__all__ = ["verify_trace_ledger_backend"]
