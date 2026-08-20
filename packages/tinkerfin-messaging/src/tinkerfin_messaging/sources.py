"""Reusable single-use asynchronous message source adapters."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar, cast, overload

from tinkerfin_agui_adapter import Identity

from ._identity import required_identifier, required_identity
from .messaging import (
    CancelCallback,
    CancelContext,
    _ContextCancelCallback,
    _invoke_cancel,
    _normalize_cancel_callback,
)
from .protocols import MessageSource

SourceT = TypeVar("SourceT")
SourceT_co = TypeVar("SourceT_co", covariant=True)
MappedT = TypeVar("MappedT")
ReplayT = TypeVar("ReplayT")


class CancellableMessageSource(
    MessageSource[SourceT_co],
    Protocol[SourceT_co],
):
    """Message source that explicitly publishes its producer cancellation callback."""

    @property
    def messaging_cancel_callback(self) -> CancelCallback[SourceT_co] | None: ...


@dataclass(frozen=True, slots=True)
class MessageSourceBinding(Generic[SourceT]):
    """Pair one opened source with its optional producer cancellation callback."""

    source: MessageSource[SourceT]
    cancel: CancelCallback[SourceT] | None = None


@dataclass(frozen=True, slots=True)
class _OpenedMessageSource(Generic[SourceT]):
    source: MessageSource[SourceT]
    cancel: _ContextCancelCallback[SourceT] | None


class DeferredMessageSource(Generic[SourceT]):
    """Open one source on its first owner pull and never for an unused attachment.

    The wrapper owns the opened binding and keeps it available through Messaging's
    cancellation-versus-settlement decision. Natural exhaustion does not close the
    binding; the producer owner completes that lifecycle through `aclose()`.

    Args:
        opener: Async factory returning the source and its optional cancel callback.
        cancellable: Durable capability declared before backend preparation. The opened
            binding must contain a callback exactly when this value is true.
        cancel_after_first_item: Delay the callback until the first pull has produced
            an item or terminal outcome. Use this when the first item establishes a
            protocol lifecycle that cancellation must not overtake.

    Raises:
        TypeError: The opener or cancellable declaration has an invalid shape.
    """

    def __init__(
        self,
        opener: Callable[[], Awaitable[MessageSourceBinding[SourceT]]],
        *,
        cancellable: bool,
        cancel_after_first_item: bool = False,
    ) -> None:
        if not callable(opener):
            raise TypeError("opener must be an async callable")
        if not isinstance(cancellable, bool):
            raise TypeError("cancellable must be a boolean")
        if not isinstance(cancel_after_first_item, bool):
            raise TypeError("cancel_after_first_item must be a boolean")
        if cancel_after_first_item and not cancellable:
            raise ValueError("cancel_after_first_item requires a cancellable source")
        self._opener = opener
        self._cancellable = cancellable
        self._cancel_after_first_item = cancel_after_first_item
        self._claimed = False
        self._closed = False
        self._exhausted = False
        self._first_pull_settled = asyncio.Event()
        self._iterator: AsyncIterator[SourceT] | None = None
        self._open_task: asyncio.Task[_OpenedMessageSource[SourceT]] | None = None
        self._binding: _OpenedMessageSource[SourceT] | None = None
        self._active_task: asyncio.Task[object] | None = None
        self._close_task: asyncio.Task[None] | None = None

    @property
    def messaging_cancel_callback(self) -> CancelCallback[SourceT] | None:
        """Advertise the source-owned callback before durable preparation."""

        if not self._cancellable:
            return None
        return self.cancel

    def messaging_cancel_callback_matches(self, callback: object) -> bool:
        """Return whether a supplied callback names this source's same owner."""

        return callback == self.cancel

    def __aiter__(self) -> DeferredMessageSource[SourceT]:
        if self._claimed:
            raise RuntimeError("a deferred source can only be consumed once")
        if self._closed:
            raise RuntimeError("a closed deferred source cannot be consumed")
        self._claimed = True
        return self

    async def __anext__(self) -> SourceT:
        if self._closed or self._exhausted:
            raise StopAsyncIteration
        current = cast(asyncio.Task[object] | None, asyncio.current_task())
        if current is None:  # pragma: no cover - async methods run in a Task
            raise RuntimeError("a deferred source requires an asyncio task")
        active = self._active_task
        if active is not None and not active.done():
            raise RuntimeError("a deferred source operation is already active")
        self._active_task = current
        try:
            iterator = self._iterator
            if iterator is None:
                binding = await self._open()
                iterator = aiter(binding.source)
                self._iterator = iterator
            first_pull = not self._first_pull_settled.is_set()
            try:
                item = await anext(iterator)
            except StopAsyncIteration:
                # Messaging owns settlement and closes the source afterwards. Keeping
                # the binding alive here lets an already accepted cancellation invoke
                # its callback without racing natural exhaustion.
                self._exhausted = True
                if first_pull:
                    self._first_pull_settled.set()
                raise
            except asyncio.CancelledError:
                # A source-owned cancel callback can cancel this pull and wait for it.
                # Messaging closes the source after that callback settles; closing here
                # would make the pull and callback wait on each other.
                if first_pull:
                    self._first_pull_settled.set()
                raise
            except BaseException:
                if first_pull:
                    self._first_pull_settled.set()
                raise
            if first_pull:
                self._first_pull_settled.set()
            return item
        finally:
            if self._active_task is current:
                self._active_task = None

    async def cancel(self, context: CancelContext) -> Iterable[SourceT] | None:
        """Wait for the owner source and invoke its normalized cancellation callback."""

        if not self._cancellable:
            raise RuntimeError("this deferred source is not cancellable")
        binding = await self._open()
        if self._cancel_after_first_item:
            await self._first_pull_settled.wait()
        callback = binding.cancel
        if callback is None:  # pragma: no cover - validated while opening
            raise RuntimeError("the opened source has no cancellation callback")
        return await _invoke_cancel(callback, context)

    async def aclose(self) -> None:
        """Close without opening an unused source, or settle an active source once."""

        current = asyncio.current_task()
        active = self._active_task
        if active is not None and active is not current and not active.done():
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)
        await self._finish()

    async def _open(self) -> _OpenedMessageSource[SourceT]:
        if self._closed:
            raise RuntimeError("a closed deferred source cannot be opened")
        task = self._open_task
        if task is None:
            task = asyncio.create_task(
                self._open_once(),
                name="tinkerfin-messaging-deferred-source-open",
            )
            self._open_task = task
        return await asyncio.shield(task)

    async def _open_once(self) -> _OpenedMessageSource[SourceT]:
        opened = self._opener()
        if not inspect.isawaitable(opened):
            raise TypeError("opener must return an awaitable")
        binding = await opened
        if not isinstance(binding, MessageSourceBinding):
            raise TypeError("opener must resolve to MessageSourceBinding")
        if not isinstance(binding.source, MessageSource):
            raise TypeError("opened source must implement MessageSource")
        cancel = binding.cancel
        if cancel is None:
            cancel = getattr(binding.source, "messaging_cancel_callback", None)
        try:
            normalized = None if cancel is None else _normalize_cancel_callback(cancel)
        except Exception as error:
            await self._close_rejected_source(binding.source, error)
            raise
        if self._cancellable != (normalized is not None):
            error = TypeError(
                "opened cancellation callback does not match cancellable declaration"
            )
            await self._close_rejected_source(binding.source, error)
            raise error
        resolved = _OpenedMessageSource(
            source=binding.source,
            cancel=normalized,
        )
        self._binding = resolved
        return resolved

    @staticmethod
    async def _close_rejected_source(
        source: MessageSource[SourceT],
        error: Exception,
    ) -> None:
        try:
            await source.aclose()
        except asyncio.CancelledError as cancellation:
            cancellation.add_note(
                f"deferred source opening also failed: {type(error).__name__}: {error}"
            )
            raise
        except Exception as close_error:  # noqa: BLE001 - host source cleanup
            error.add_note(
                "opened source cleanup also failed: "
                f"{type(close_error).__name__}: {close_error}"
            )

    async def _finish(self) -> None:
        task = self._close_task
        if task is None:
            self._closed = True
            task = asyncio.create_task(
                self._close_once(),
                name="tinkerfin-messaging-deferred-source-close",
            )
            task.add_done_callback(self._close_finished)
            self._close_task = task
        await asyncio.shield(task)

    async def _close_once(self) -> None:
        open_task = self._open_task
        if open_task is not None and not open_task.done():
            open_task.cancel()
            await asyncio.gather(open_task, return_exceptions=True)
        binding = self._binding
        self._binding = None
        self._iterator = None
        if binding is None:
            return
        await binding.source.aclose()

    @staticmethod
    def _close_finished(task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()


class ProfiledDeferredMessageSource(
    DeferredMessageSource[SourceT],
    Generic[SourceT, ReplayT],
):
    """Defer opening while publishing a complete immutable codec profile."""

    def __init__(
        self,
        opener: Callable[[], Awaitable[MessageSourceBinding[SourceT]]],
        *,
        identity: Identity,
        codec_profile: str,
        source_type: type[SourceT],
        replay_type: type[ReplayT],
        cancellable: bool,
        cancel_after_first_item: bool = False,
    ) -> None:
        super().__init__(
            opener,
            cancellable=cancellable,
            cancel_after_first_item=cancel_after_first_item,
        )
        self._messaging_identity = required_identity(identity)
        self._messaging_codec_profile = required_identifier(
            "codec_profile",
            codec_profile,
        )
        if not isinstance(source_type, type):
            raise TypeError("source_type must be a type")
        if not isinstance(replay_type, type):
            raise TypeError("replay_type must be a type")
        self._messaging_source_type = source_type
        self._messaging_replay_type = replay_type

    @property
    def messaging_identity(self) -> Identity:
        """Return the durable run identity without opening the source."""

        return self._messaging_identity

    @property
    def messaging_codec_profile(self) -> str:
        """Return the codec profile without opening the source."""

        return self._messaging_codec_profile

    @property
    def messaging_source_type(self) -> type[SourceT]:
        """Return the live source item type."""

        return self._messaging_source_type

    @property
    def messaging_replay_type(self) -> type[ReplayT]:
        """Return the replay item type."""

        return self._messaging_replay_type


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
        source_cancel = getattr(source, "messaging_cancel_callback", None)
        self._source_cancel = source_cancel
        self._normalized_cancel = (
            None if source_cancel is None else _normalize_cancel_callback(source_cancel)
        )
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

    @property
    def messaging_cancel_callback(self) -> CancelCallback[MappedT] | None:
        """Publish a mapped callback only when the borrowed source owns one."""

        if self._normalized_cancel is None:
            return None
        return self.cancel

    def messaging_cancel_callback_matches(self, callback: object) -> bool:
        """Match this callback or the equivalent owner before mapping."""

        if callback == self.cancel or callback == self._source_cancel:
            return True
        matcher = getattr(self._source, "messaging_cancel_callback_matches", None)
        return bool(callable(matcher) and matcher(callback))

    async def _iterate(self) -> AsyncGenerator[MappedT, None]:
        try:
            async for item in self._source:
                yield await self._apply_transform(item)
        finally:
            self._delivery = None

    async def cancel(self, context: CancelContext) -> tuple[MappedT, ...] | None:
        """Cancel the borrowed source and atomically transform its finite tail."""

        callback = self._normalized_cancel
        if callback is None:
            raise RuntimeError("this mapped source is not cancellable")
        tail = await _invoke_cancel(callback, context)
        if tail is None:
            return None
        transformed: list[MappedT] = []
        for item in tail:
            transformed.append(await self._apply_transform(item))
        return tuple(transformed)

    async def _apply_transform(self, item: SourceT) -> MappedT:
        transformed = self._transform(item)
        if inspect.isawaitable(transformed):
            transformed = await cast(Awaitable[MappedT], transformed)
        return cast(MappedT, transformed)

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
    source: CancellableMessageSource[SourceT],
    transform: Callable[[SourceT], Awaitable[MappedT]],
) -> CancellableMessageSource[MappedT]: ...


@overload
def map_source(
    source: CancellableMessageSource[SourceT],
    transform: Callable[[SourceT], MappedT],
) -> CancellableMessageSource[MappedT]: ...


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
