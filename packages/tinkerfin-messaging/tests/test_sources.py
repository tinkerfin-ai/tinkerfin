"""Reusable asynchronous source lifecycle and transformation contracts."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from typing import assert_type

import pytest

from tinkerfin_messaging.protocols import MessageSource
from tinkerfin_messaging.sources import FiniteMessageSource, map_source


class _TrackedSource:
    def __init__(self, *items: int) -> None:
        self._items = items
        self.pulled: list[int] = []
        self.close_calls = 0
        self._iterator: AsyncGenerator[int, None] | None = None

    def __aiter__(self) -> AsyncIterator[int]:
        async def iterate() -> AsyncGenerator[int, None]:
            for item in self._items:
                self.pulled.append(item)
                yield item

        self._iterator = iterate()
        return self._iterator

    async def aclose(self) -> None:
        self.close_calls += 1
        iterator = self._iterator
        if iterator is not None:
            await iterator.aclose()


async def test_finite_source_snapshots_order_and_rejects_second_consumption() -> None:
    """Mutating caller input or iterating twice must not alter one finite source."""

    events = [1, 2]
    source = FiniteMessageSource.from_events(events)
    events.append(3)

    assert [item async for item in source] == [1, 2]
    with pytest.raises(RuntimeError, match="only be consumed once"):
        aiter(source)
    await source.aclose()
    await source.aclose()


async def test_finite_source_rejects_consumption_after_close() -> None:
    """A source closed before its claim must never emit its retained events."""

    source = FiniteMessageSource.from_events((1, 2))

    await source.aclose()

    with pytest.raises(RuntimeError, match="closed"):
        aiter(source)


async def test_map_source_supports_sync_and_async_transforms() -> None:
    """Both transform forms must preserve order and close their borrowed source."""

    sync_source = _TrackedSource(1, 2)
    async_source = _TrackedSource(3, 4)

    async def asynchronously(item: int) -> str:
        await asyncio.sleep(0)
        return f"async:{item}"

    sync_mapped = map_source(sync_source, lambda item: f"sync:{item}")
    async_mapped = map_source(async_source, asynchronously)
    assert_type(sync_mapped, MessageSource[str])
    assert_type(async_mapped, MessageSource[str])

    assert [item async for item in sync_mapped] == ["sync:1", "sync:2"]
    assert [item async for item in async_mapped] == ["async:3", "async:4"]
    assert sync_source.close_calls == 0
    assert async_source.close_calls == 0
    await sync_mapped.aclose()
    await async_mapped.aclose()
    assert sync_source.close_calls == 1
    assert async_source.close_calls == 1


async def test_map_source_waits_for_transform_before_pulling_next_item() -> None:
    """An asynchronous transform must apply backpressure to the upstream source."""

    source = _TrackedSource(1, 2)
    transform_started = asyncio.Event()
    transform_release = asyncio.Event()

    async def transform(item: int) -> int:
        transform_started.set()
        await transform_release.wait()
        return item * 10

    mapped = map_source(source, transform)
    iterator = aiter(mapped)
    first = asyncio.ensure_future(anext(iterator))
    await asyncio.wait_for(transform_started.wait(), timeout=1)

    assert source.pulled == [1]
    assert not first.done()

    transform_release.set()
    assert await asyncio.wait_for(first, timeout=1) == 10
    assert await anext(iterator) == 20
    with pytest.raises(StopAsyncIteration):
        await anext(iterator)
    assert source.close_calls == 0
    await mapped.aclose()
    assert source.close_calls == 1


async def test_map_source_closes_upstream_after_transform_failure() -> None:
    """A transform failure must propagate and release the upstream source once."""

    source = _TrackedSource(1)
    expected = ValueError("cannot transform")

    def fail(_item: int) -> int:
        raise expected

    mapped = map_source(source, fail)

    with pytest.raises(ValueError) as captured:
        await anext(aiter(mapped))
    assert captured.value is expected
    assert source.close_calls == 0
    await mapped.aclose()
    assert source.close_calls == 1
    await mapped.aclose()
    assert source.close_calls == 1


async def test_map_source_cancellation_closes_upstream() -> None:
    """Cancelling an in-flight transform must not leave the upstream source open."""

    source = _TrackedSource(1)
    transform_started = asyncio.Event()

    async def block(_item: int) -> int:
        transform_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    mapped = map_source(source, block)
    consuming = asyncio.ensure_future(anext(aiter(mapped)))
    await asyncio.wait_for(transform_started.wait(), timeout=1)
    consuming.cancel()

    with pytest.raises(asyncio.CancelledError):
        await consuming
    assert source.close_calls == 0
    await mapped.aclose()
    assert source.close_calls == 1
