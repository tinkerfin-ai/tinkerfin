"""Cancellation-safe ownership for live Trace followers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from types import TracebackType
from typing import Generic, Protocol, TypeVar, cast, runtime_checkable

from .errors import TraceFollowLifecycleError

_ItemT_co = TypeVar("_ItemT_co", covariant=True)
_ResultT = TypeVar("_ResultT")
_END = object()


@runtime_checkable
class TraceFollow(Protocol[_ItemT_co]):
    """A single-use live Trace iterator that owns upstream settlement.

    Use the asynchronous context-manager scope when a loop may stop early. Only one
    pull may be active; external close may cancel that pull and waits for upstream
    settlement before returning.
    """

    def __aiter__(self) -> TraceFollow[_ItemT_co]:
        """Return this live subscription."""

        ...

    async def __anext__(self) -> _ItemT_co:
        """Return the next committed Trace update.

        Raises:
            StopAsyncIteration: The follower has completed or closed.
            TraceFollowLifecycleError: Another pull is already active.
        """

        ...

    async def aclose(self) -> None:
        """Stop following and wait until the upstream iterator is closed.

        Raises:
            TraceFollowLifecycleError: The active pull tries to close itself.
        """

        ...

    async def __aenter__(self) -> TraceFollow[_ItemT_co]:
        """Enter a scope that closes this follower on every exit path.

        Raises:
            TraceFollowLifecycleError: This follower is already closed.
        """

        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close while preserving body failures and process-control priority."""

        ...


async def _close_trace_source(
    source: object,
    *,
    primary_error: BaseException | None,
) -> None:
    """Close one followed source without replacing an earlier operation failure."""

    close = getattr(source, "aclose", None)
    if close is None:
        return
    try:
        await close()
    except BaseException as close_error:
        if primary_error is None:
            raise
        primary_error.add_note(
            "Trace follower close also failed: "
            f"{type(close_error).__name__}: {close_error}"
        )


async def _join_owned_task(
    task: asyncio.Task[_ResultT],
    *,
    primary_error: BaseException | None,
    suppress_task_cancellation: bool = False,
) -> _ResultT | None:
    """Settle one owned task across repeated cancellation of its caller."""

    current = asyncio.current_task()
    cancel_count = current.cancelling() if current is not None else 0
    caller_cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            next_count = current.cancelling() if current is not None else 0
            if next_count > cancel_count:
                caller_cancellation = caller_cancellation or error
                cancel_count = next_count
                continue
            if task.done():
                break
            raise
        except BaseException:  # noqa: BLE001 - inspect the retained task below
            break

    task_error: BaseException | None = None
    result: _ResultT | None = None
    try:
        result = task.result()
    except asyncio.CancelledError as error:
        if not suppress_task_cancellation:
            task_error = error
    except BaseException as error:  # noqa: BLE001 - preserve the exact outcome
        task_error = error

    if primary_error is not None:
        process_control = caller_cancellation
        if (
            process_control is None
            and task_error is not None
            and not isinstance(task_error, Exception)
        ):
            process_control = task_error
            task_error = None
        if isinstance(primary_error, Exception) and process_control is not None:
            if task_error is not None:
                process_control.add_note(
                    "Trace follow settlement also failed: "
                    f"{type(task_error).__name__}: {task_error}"
                )
            process_control.add_note(
                "Trace follow operation also failed: "
                f"{type(primary_error).__name__}: {primary_error}"
            )
            raise process_control.with_traceback(
                process_control.__traceback__
            ) from primary_error
        if caller_cancellation is not None:
            primary_error.add_note(
                "Trace follow settlement also received caller cancellation: "
                f"{caller_cancellation}"
            )
        if task_error is not None:
            primary_error.add_note(
                "Trace follow settlement also failed: "
                f"{type(task_error).__name__}: {task_error}"
            )
        return result
    if caller_cancellation is not None:
        if task_error is not None:
            caller_cancellation.add_note(
                "Trace follow settlement also failed: "
                f"{type(task_error).__name__}: {task_error}"
            )
        raise caller_cancellation.with_traceback(caller_cancellation.__traceback__)
    if task_error is not None:
        raise task_error.with_traceback(task_error.__traceback__)
    return result


class _OwnedTraceFollow(Generic[_ItemT_co]):
    def __init__(
        self,
        source_factory: Callable[[], AsyncIterator[_ItemT_co]],
    ) -> None:
        self._source_factory = source_factory
        self._source: AsyncIterator[_ItemT_co] | None = None
        self._active_task: asyncio.Task[object] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._closed = False

    def __aiter__(self) -> _OwnedTraceFollow[_ItemT_co]:
        return self

    async def __aenter__(self) -> _OwnedTraceFollow[_ItemT_co]:
        if self._closed:
            raise TraceFollowLifecycleError("a closed Trace follower cannot be entered")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, traceback
        try:
            await self.aclose()
        except BaseException as close_error:
            if exc_value is None:
                raise
            if isinstance(exc_value, Exception) and not isinstance(
                close_error, Exception
            ):
                close_error.add_note(
                    "Trace follow scope body also failed: "
                    f"{type(exc_value).__name__}: {exc_value}"
                )
                raise close_error.with_traceback(
                    close_error.__traceback__
                ) from exc_value
            exc_value.add_note(
                "Trace follow scope close also failed: "
                f"{type(close_error).__name__}: {close_error}"
            )

    async def __anext__(self) -> _ItemT_co:
        if self._closed:
            raise StopAsyncIteration
        current = cast(asyncio.Task[object] | None, asyncio.current_task())
        if current is None:  # pragma: no cover - async methods run in a Task
            raise TraceFollowLifecycleError("Trace following requires an asyncio task")
        active = self._active_task
        if active is not None and not active.done():
            raise TraceFollowLifecycleError(
                "a Trace follow operation is already active"
            )
        self._active_task = current
        try:
            source = self._source
            if source is None:
                source = self._source_factory()
                self._source = source

            async def pull_next() -> object:
                try:
                    return await anext(source)
                except StopAsyncIteration:
                    return _END

            pull = asyncio.create_task(
                pull_next(),
                name="tinkerfin-trace-follow-pull",
            )
            try:
                result = await asyncio.shield(pull)
            except asyncio.CancelledError as error:
                if not pull.done():
                    pull.cancel()
                await _join_owned_task(
                    pull,
                    primary_error=error,
                    suppress_task_cancellation=True,
                )
                await self._finish(error)
                raise
            except BaseException as error:
                await self._finish(error)
                raise
            if result is _END:
                await self._finish(None)
                raise StopAsyncIteration
            return cast(_ItemT_co, result)
        finally:
            if self._active_task is current:
                self._active_task = None

    async def aclose(self) -> None:
        current = asyncio.current_task()
        active = self._active_task
        if active is current and active is not None:
            raise TraceFollowLifecycleError(
                "a Trace follower cannot close its active pull"
            )
        if active is not None and not active.done():
            active.cancel()
            await _join_owned_task(
                active,
                primary_error=None,
                suppress_task_cancellation=True,
            )
        await self._finish(None)

    async def _finish(self, primary_error: BaseException | None) -> None:
        task = self._close_task
        if task is None:
            self._closed = True
            task = asyncio.create_task(
                self._close_once(),
                name="tinkerfin-trace-follow-close",
            )
            self._close_task = task
        await _join_owned_task(task, primary_error=primary_error)

    async def _close_once(self) -> None:
        source = self._source
        self._source = None
        if source is None:
            return
        await _close_trace_source(source, primary_error=None)


def create_trace_follow(
    source_factory: Callable[[], AsyncIterator[_ItemT_co]],
) -> TraceFollow[_ItemT_co]:
    return _OwnedTraceFollow(source_factory)


__all__ = ["TraceFollow"]
