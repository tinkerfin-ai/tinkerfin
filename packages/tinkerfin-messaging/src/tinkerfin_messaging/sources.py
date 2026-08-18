"""Reusable single-use asynchronous message source adapters."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Iterable
from typing import Generic, TypeVar, cast, overload

from .protocols import MessageSource

SourceT = TypeVar("SourceT")
MappedT = TypeVar("MappedT")


class FiniteMessageSource(Generic[SourceT]):
    """Expose a fixed event snapshot as one asynchronous source."""

    def __init__(self, events: Iterable[SourceT]) -> None:
        self._events = tuple(events)
        self._claimed = False
        self._closed = False
        self._iterator: AsyncGenerator[SourceT, None] | None = None

    @classmethod
    def from_events(
        cls,
        events: Iterable[SourceT],
    ) -> FiniteMessageSource[SourceT]:
        """Snapshot finite events without taking ownership of caller containers.

        Args:
            events: Finite iterable copied immediately in its current order.

        Returns:
            A source that can be consumed exactly once.
        """

        return cls(events)

    def __aiter__(self) -> AsyncIterator[SourceT]:
        if self._claimed:
            raise RuntimeError("a finite source can only be consumed once")
        if self._closed:
            raise RuntimeError("a closed finite source cannot be consumed")
        self._claimed = True
        iterator = self._iterate()
        self._iterator = iterator
        return iterator

    async def _iterate(self) -> AsyncGenerator[SourceT, None]:
        try:
            for event in self._events:
                yield event
        finally:
            self._closed = True
            self._iterator = None

    async def aclose(self) -> None:
        """Close an unclaimed or partially consumed finite source idempotently."""

        if self._closed:
            return
        self._closed = True
        iterator = self._iterator
        self._iterator = None
        if iterator is not None:
            await iterator.aclose()


class _MappedMessageSource(Generic[SourceT, MappedT]):
    """Apply one synchronous or asynchronous transform with source backpressure."""

    def __init__(
        self,
        source: MessageSource[SourceT],
        transform: Callable[[SourceT], MappedT | Awaitable[MappedT]],
    ) -> None:
        self._source = source
        self._transform = transform
        self._claimed = False
        self._closed = False
        self._close_lock = asyncio.Lock()
        self._delivery: AsyncGenerator[MappedT, None] | None = None

    def __aiter__(self) -> AsyncIterator[MappedT]:
        if self._claimed:
            raise RuntimeError("a mapped source can only be consumed once")
        if self._closed:
            raise RuntimeError("a closed mapped source cannot be consumed")
        self._claimed = True
        delivery = self._iterate()
        self._delivery = delivery
        return delivery

    async def _iterate(self) -> AsyncGenerator[MappedT, None]:
        try:
            async for item in self._source:
                transformed = self._transform(item)
                if inspect.isawaitable(transformed):
                    transformed = await cast(Awaitable[MappedT], transformed)
                yield cast(MappedT, transformed)
        finally:
            self._delivery = None

    async def aclose(self) -> None:
        """Close active delivery and the borrowed source exactly once."""

        delivery = self._delivery
        if delivery is not None:
            await delivery.aclose()
            self._delivery = None
        await self._close_source()

    async def _close_source(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            await self._source.aclose()


@overload
def map_source(
    source: MessageSource[SourceT],
    transform: Callable[[SourceT], Awaitable[MappedT]],
) -> MessageSource[MappedT]: ...


@overload
def map_source(
    source: MessageSource[SourceT],
    transform: Callable[[SourceT], MappedT],
) -> MessageSource[MappedT]: ...


def map_source(
    source: MessageSource[SourceT],
    transform: Callable[[SourceT], MappedT | Awaitable[MappedT]],
) -> MessageSource[MappedT]:
    """Map source values lazily while preserving lifecycle and backpressure.

    Any awaitable returned by ``transform`` is awaited before the next source item is
    requested. The returned source is deliberately unprofiled because a transform can
    change the value type and therefore invalidate the original codec profile.

    Args:
        source: Single-use source closed when the returned adapter is closed.
        transform: Synchronous or asynchronous per-item transformation.

    Returns:
        A single-use mapped source.
    """

    return _MappedMessageSource(source, transform)
