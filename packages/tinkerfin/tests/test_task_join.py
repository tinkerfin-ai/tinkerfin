"""Public cancellation-safe joins for host-owned runtime tasks."""

from __future__ import annotations

import asyncio

import pytest

from tinkerfin import join_task


async def test_join_task_returns_the_owned_task_result() -> None:
    """Hosts receive the result after the owned task settles normally."""

    async def produce() -> str:
        await asyncio.sleep(0)
        return "settled"

    assert await join_task(asyncio.create_task(produce())) == "settled"


async def test_join_task_preserves_repeated_caller_cancellation() -> None:
    """Repeated caller cancellation cannot interrupt owned task settlement."""

    entered = asyncio.Event()
    release = asyncio.Event()

    async def settle() -> None:
        entered.set()
        await release.wait()

    owned = asyncio.create_task(settle())
    waiter = asyncio.create_task(join_task(owned))
    await asyncio.wait_for(entered.wait(), timeout=2)

    waiter.cancel("first cancellation")
    await asyncio.sleep(0)
    waiter.cancel("second cancellation")
    await asyncio.sleep(0)
    assert not waiter.done()
    assert not owned.cancelled()

    release.set()
    with pytest.raises(asyncio.CancelledError, match="first cancellation"):
        await waiter
    assert owned.done()
    assert owned.exception() is None


async def test_join_task_distinguishes_owned_task_cancellation() -> None:
    """Owned task cancellation propagates without marking the joining caller."""

    async def cancelled() -> None:
        raise asyncio.CancelledError("owned cancellation")

    current = asyncio.current_task()
    assert current is not None
    before = current.cancelling()
    with pytest.raises(asyncio.CancelledError, match="owned cancellation"):
        await join_task(asyncio.create_task(cancelled()))
    assert current.cancelling() == before
