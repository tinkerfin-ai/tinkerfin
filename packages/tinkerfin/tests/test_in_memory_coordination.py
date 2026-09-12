from __future__ import annotations

import asyncio
from types import TracebackType

import pytest

from tinkerfin import RunIdentity
from tinkerfin.coordination import InMemoryRunCoordinator


def _identity(thread_id: str, *, run_id: str = "run-1") -> RunIdentity:
    return RunIdentity(namespace="test", thread_id=thread_id, run_id=run_id)


@pytest.mark.parametrize("custom_key", [False, True])
async def test_namespaces_isolate_default_and_custom_coordination(
    custom_key: bool,
) -> None:
    coordinator = InMemoryRunCoordinator(
        key_resolver=(lambda _: "same-resource") if custom_key else None
    )
    entered = asyncio.Event()
    attempted = asyncio.Event()
    release = asyncio.Event()

    async def other_namespace() -> None:
        attempted.set()
        async with coordinator(
            RunIdentity(namespace="other", thread_id="same", run_id="run")
        ):
            entered.set()
            await release.wait()

    async with coordinator(
        RunIdentity(namespace="test", thread_id="same", run_id="run")
    ):
        other = asyncio.create_task(other_namespace())
        try:
            await attempted.wait()
            assert entered.is_set()
        finally:
            release.set()
            other.cancel()
            await asyncio.gather(other, return_exceptions=True)


async def test_default_coordination_serializes_different_runs_in_one_thread() -> None:
    coordinator = InMemoryRunCoordinator()
    active = 0
    peak = 0

    async def run(run_id: str) -> None:
        nonlocal active, peak
        async with coordinator(
            RunIdentity(namespace="test", thread_id="same", run_id=run_id)
        ):
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1

    await asyncio.gather(run("one"), run("two"))
    assert peak == 1


class _ControllableLock:
    """Expose one deterministic wait point around an existing asyncio lock."""

    def __init__(self, lock: asyncio.Lock) -> None:
        self._lock = lock
        self.block = False
        self.waiting = asyncio.Event()
        self.release = asyncio.Event()

    async def __aenter__(self) -> None:
        if self.block:
            self.waiting.set()
            await self.release.wait()
        await self._lock.acquire()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        self._lock.release()


async def _wait_for_registered_users(
    coordinator: InMemoryRunCoordinator,
    *,
    key: str,
    users: int,
) -> None:
    for _ in range(100):
        entries = coordinator._entries
        if any(entry.users == users for entry in entries.values()):
            return
        await asyncio.sleep(0)
    raise AssertionError(f"{key!r} did not reach {users} registered users")


@pytest.mark.asyncio
async def test_same_resolved_key_never_enters_two_scopes_concurrently() -> None:
    coordinator = InMemoryRunCoordinator(key_resolver=lambda value: value.thread_id)
    active = 0
    peak = 0

    async def coordinate() -> None:
        nonlocal active, peak
        async with coordinator(_identity("user-1")):
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1

    await asyncio.gather(coordinate(), coordinate())

    assert peak == 1


@pytest.mark.asyncio
async def test_different_resolved_keys_enter_without_waiting_for_each_other() -> None:
    coordinator = InMemoryRunCoordinator(key_resolver=lambda value: value.thread_id)
    release = asyncio.Event()
    attempting = (asyncio.Event(), asyncio.Event())
    entered = (asyncio.Event(), asyncio.Event())

    async def coordinate(index: int, identity: RunIdentity) -> None:
        attempting[index].set()
        async with coordinator(identity):
            entered[index].set()
            await release.wait()

    tasks = (
        asyncio.create_task(coordinate(0, _identity("user-1"))),
        asyncio.create_task(coordinate(1, _identity("user-2"))),
    )
    try:
        await asyncio.gather(*(event.wait() for event in attempting))
        assert all(event.is_set() for event in entered)
    finally:
        release.set()
    await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_repeated_cancellation_cannot_leak_a_waiting_key_entry() -> None:
    coordinator = InMemoryRunCoordinator(key_resolver=lambda value: value.thread_id)
    holder_entered = asyncio.Event()
    release_holder = asyncio.Event()

    async def hold_key() -> None:
        async with coordinator(_identity("same", run_id="holder")):
            holder_entered.set()
            await release_holder.wait()

    async def wait_for_key() -> None:
        async with coordinator(_identity("same", run_id="waiter")):
            raise AssertionError("cancelled waiter entered the protected scope")

    holder = asyncio.create_task(hold_key())
    waiter: asyncio.Task[None] | None = None
    gate: _ControllableLock | None = None
    try:
        await holder_entered.wait()
        waiter = asyncio.create_task(wait_for_key())
        await _wait_for_registered_users(coordinator, key="same", users=2)
        gate = _ControllableLock(coordinator._entries_lock)
        gate.block = True
        setattr(coordinator, "_entries_lock", gate)

        waiter.cancel("first cancellation")
        await gate.waiting.wait()
        waiter.cancel("second cancellation")
        gate.release.set()

        with pytest.raises(asyncio.CancelledError) as raised:
            await waiter
        assert raised.value.args == ("first cancellation",)

        release_holder.set()
        await holder
        assert coordinator._entries == {}

        async with coordinator(_identity("same", run_id="after")):
            pass
        assert coordinator._entries == {}
    finally:
        if gate is not None:
            gate.release.set()
        release_holder.set()
        pending = [task for task in (holder, waiter) if task is not None]
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancellation_during_settlement_preserves_the_scope_failure() -> None:
    coordinator = InMemoryRunCoordinator(key_resolver=lambda value: value.thread_id)
    entered = asyncio.Event()
    fail_scope = asyncio.Event()

    async def fail_after_release() -> None:
        async with coordinator(_identity("same")):
            entered.set()
            await fail_scope.wait()
            raise ValueError("scope failed")

    operation = asyncio.create_task(fail_after_release())
    gate: _ControllableLock | None = None
    try:
        await entered.wait()
        gate = _ControllableLock(coordinator._entries_lock)
        gate.block = True
        setattr(coordinator, "_entries_lock", gate)
        fail_scope.set()
        await gate.waiting.wait()

        operation.cancel("caller stopped")
        gate.release.set()

        with pytest.raises(asyncio.CancelledError) as raised:
            await operation
        assert raised.value.args == ("caller stopped",)
        assert any(
            "ValueError: scope failed" in note for note in raised.value.__notes__
        )
        assert coordinator._entries == {}
    finally:
        if gate is not None:
            gate.release.set()
        fail_scope.set()
        await asyncio.gather(operation, return_exceptions=True)
