"""AG-UI stream cancellation, upstream cleanup, observation, and SSE mapping."""

from __future__ import annotations

import asyncio
import inspect
import math
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, cast

from ag_ui.core import BaseEvent, RunErrorEvent, RunStartedEvent

from ._runtime_streams import _map_sse_item, _resolve_sse_event_id
from ._tasks import join_task
from .errors import AgUiSettlementTimeoutError, TinkerFinLifecycleError
from .sse import (
    SseBody,
    SseEventIdResolver,
    SseMapper,
    SsePayload,
    encode_sse_payload,
)

if TYPE_CHECKING:
    from .runtime import AgUiEventStream

__all__ = [
    "_close",
    "_close_finished",
    "_close_once",
    "_close_upstream",
    "_decorate_initialization_event",
    "_next_part",
    "_observe",
    "_record_secondary_error_note",
]


class _AgUiStreamDeadlineExceeded(TimeoutError):
    """Identify only the total pull deadline owned by `AgUiEventStream`."""


async def __anext__(self: AgUiEventStream) -> BaseEvent:
    if self._closed:
        raise StopAsyncIteration
    current = cast(asyncio.Task[object] | None, asyncio.current_task())
    if current is None:  # pragma: no cover - async methods run in a Task
        raise TinkerFinLifecycleError("an AG-UI event stream requires an asyncio task")
    active = self._active_task
    if active is not None and not active.done():
        raise TinkerFinLifecycleError(
            "an AG-UI event stream operation is already active"
        )
    self._active_task = current
    try:
        try:
            event = await anext(self._source)
        except StopAsyncIteration:
            self._closed = True
            raise
        except asyncio.CancelledError as cancellation:
            conversion_error = self.error
            if conversion_error is not None:
                cancellation.add_note(
                    "AG-UI conversion also failed: "
                    f"{type(conversion_error).__name__}: {conversion_error}"
                )
            for note in self._secondary_error_notes:
                cancellation.add_note(note)
            raise
        except Exception as error:
            if self.error is None:
                self.error = error
            await self._close(error)
            raise
        observer = self._on_event
        if observer is not None:
            try:
                await self._observe(event)
            except Exception as error:
                if self.error is None:
                    self.error = error
                await self._close(error)
                raise
        return event
    finally:
        if self._active_task is current:
            self._active_task = None


async def abort(self: AgUiEventStream) -> list[BaseEvent]:
    """Cancel the active conversion and return one observed cancelled tail.

    Returns:
        The observed cancellation tail, or an empty list after a terminal state
        or prior abort delivery.

    Raises:
        RuntimeError: Called recursively from this stream's event observer before
            the main run reaches a terminal state.
    """

    if self._completed or self._abort_events_delivered:
        return []
    current = asyncio.current_task()
    if self._active_observers and self._observer_lineage.get():
        raise TinkerFinLifecycleError(
            "AgUiEventStream.abort() cannot be called from its on_event callback"
        )
    self._aborted = True
    active = self._active_task
    active_to_cancel = (
        active
        if active is not None and active is not current and not active.done()
        else None
    )
    await self._close(None, active=active_to_cancel)

    tail = self._adapter.abort(code="cancelled")
    if self._main_started:
        tail.append(
            self._decorate_initialization_event(
                self._lifecycle.failed(
                    identity=self._identity,
                    message="Agent run cancelled",
                    code="cancelled",
                )
            )
        )
    self._abort_events_delivered = True
    for event in tail:
        await self._observe(event)
    return tail


async def aclose(self: AgUiEventStream) -> None:
    """Close this stream and its upstream parts idempotently.

    Closure requested from an event-observer call chain preserves delivery of
    the event currently being observed. An external closer cancels an active
    pull before waiting for the shared cleanup.
    """

    current = asyncio.current_task()
    active = self._active_task
    observer_lineage_active = (
        bool(self._active_observers) and self._observer_lineage.get()
    )
    active_to_cancel = (
        active
        if active is not None
        and active is not current
        and not active.done()
        and not observer_lineage_active
        else None
    )
    await self._close(None, active=active_to_cancel)


def to_sse(
    self: AgUiEventStream,
    *,
    mapper: SseMapper[BaseEvent] | None = None,
    event_id_resolver: SseEventIdResolver[BaseEvent] | None = None,
) -> SseBody[str]:
    """Consume this AG-UI object stream as safely framed SSE text."""

    async def frames() -> AsyncGenerator[str, None]:
        try:
            async for event in self:
                payload = await _map_sse_item(
                    event,
                    mapper=mapper,
                    default=SsePayload(
                        data=event.model_dump_json(
                            by_alias=True,
                            exclude_none=True,
                        )
                    ),
                )
                if payload is None:
                    continue
                event_id = await _resolve_sse_event_id(
                    event,
                    event_id_resolver,
                )
                yield encode_sse_payload(payload, event_id=event_id)
        finally:
            await self.aclose()

    return SseBody(source_factory=frames, close=self.aclose)


async def _close(
    self: AgUiEventStream,
    primary: BaseException | None,
    *,
    active: asyncio.Task[object] | None = None,
) -> None:
    task = self._close_task
    if task is None:
        self._closed = True
        task = asyncio.create_task(
            self._close_once(active),
            name="tinkerfin-agui-event-stream-close",
        )
        self._close_task = task
        task.add_done_callback(self._close_finished)
    try:
        settlement_timeout = self._settlement_timeout
        if settlement_timeout is None:
            await join_task(task)
        elif task.done():
            task.result()
        else:
            settlement_deadline = asyncio.timeout(settlement_timeout)
            try:
                async with settlement_deadline:
                    await asyncio.shield(task)
            except TimeoutError as error:
                if settlement_deadline.expired():
                    raise AgUiSettlementTimeoutError(
                        timeout=settlement_timeout
                    ) from error
                raise
    except BaseException as cleanup_error:
        current = asyncio.current_task()
        if isinstance(cleanup_error, asyncio.CancelledError) and (
            current is not None and current.cancelling()
        ):
            if primary is not None and not isinstance(primary, GeneratorExit):
                cleanup_error.add_note(
                    f"AG-UI processing also failed: {type(primary).__name__}: {primary}"
                )
            for note in getattr(cleanup_error, "__notes__", ()):
                self._record_secondary_error_note(note)
            raise
        if primary is not None and not isinstance(primary, GeneratorExit):
            primary.add_note(
                "AG-UI cleanup also failed: "
                f"{type(cleanup_error).__name__}: {cleanup_error}"
            )
            return
        raise


def _close_finished(task: asyncio.Task[None]) -> None:
    """Consume a retained close failure even when no caller waits again."""

    if not task.cancelled():
        task.exception()


async def _close_once(
    self: AgUiEventStream,
    active: asyncio.Task[object] | None,
) -> None:
    if active is not None and not active.done():
        active.cancel()
        await asyncio.gather(active, return_exceptions=True)
    primary: BaseException | None = None
    close = getattr(self._source, "aclose", None)
    try:
        if close is not None:
            await close()
    except BaseException as error:  # noqa: BLE001 - cleanup owns all outcomes
        primary = error
    await self._close_upstream(primary)
    if primary is not None:
        raise primary.with_traceback(primary.__traceback__)


async def _observe(self: AgUiEventStream, event: BaseEvent) -> None:
    observer = self._on_event
    if observer is None:
        return
    # ContextVar marks callback-derived tasks; the active count keeps delayed
    # descendants valid after their callback returns.
    self._active_observers += 1
    token = self._observer_lineage.set(True)
    try:
        observed = observer(event)
        if not inspect.isawaitable(observed):
            raise TypeError("on_event must return an awaitable")
        await observed
    finally:
        self._observer_lineage.reset(token)
        self._active_observers -= 1


async def _close_upstream(self: AgUiEventStream, primary: BaseException | None) -> None:
    if self._upstream_closed:
        return
    close = getattr(self._upstream, "aclose", None)
    if close is None:
        self._upstream_closed = True
        return
    current = asyncio.current_task()
    cancel_count = current.cancelling() if current is not None else 0
    try:
        await close()
    except asyncio.CancelledError as cleanup_error:
        next_cancel_count = current.cancelling() if current is not None else 0
        caller_cancelled = next_cancel_count > cancel_count
        if caller_cancelled and isinstance(primary, asyncio.CancelledError):
            primary.add_note(
                "native parts cleanup also received caller cancellation: "
                f"{cleanup_error}"
            )
            for note in getattr(cleanup_error, "__notes__", ()):
                primary.add_note(note)
            return
        if caller_cancelled:
            if primary is not None and not isinstance(primary, GeneratorExit):
                cleanup_error.add_note(
                    f"AG-UI processing also failed: {type(primary).__name__}: {primary}"
                )
            for note in getattr(cleanup_error, "__notes__", ()):
                self._record_secondary_error_note(note)
            raise
        if primary is not None and not isinstance(primary, GeneratorExit):
            note = f"native parts cleanup also failed: CancelledError: {cleanup_error}"
            primary.add_note(note)
            self._record_secondary_error_note(note)
            return
        raise
    except BaseException as cleanup_error:
        self._upstream_closed = True
        if primary is not None and not isinstance(primary, GeneratorExit):
            note = (
                "native parts cleanup also failed: "
                f"{type(cleanup_error).__name__}: {cleanup_error}"
            )
            primary.add_note(note)
            self._record_secondary_error_note(note)
            return
        raise
    else:
        self._upstream_closed = True


def _record_secondary_error_note(self: AgUiEventStream, note: str) -> None:
    """Retain cleanup evidence across the micro-batch cancellation boundary."""

    if note not in self._secondary_error_notes:
        self._secondary_error_notes.append(note)


def _decorate_initialization_event(
    self: AgUiEventStream, event: BaseEvent
) -> BaseEvent:
    """Mark only main lifecycle events emitted for initialization failure."""

    if not self._initialization_failed or not isinstance(
        event,
        RunStartedEvent | RunErrorEvent,
    ):
        return event
    raw_candidate = cast(object, event.raw_event)
    raw_event: dict[str, object] = (
        dict(cast(dict[str, object], raw_candidate))
        if isinstance(raw_candidate, dict)
        else {
            "threadId": self._identity.thread_id,
            "runId": self._identity.run_id,
        }
    )
    raw_event["initializationFailed"] = True
    return event.model_copy(update={"raw_event": raw_event})


async def _next_part(self: AgUiEventStream) -> object:
    timeout = self._timeout
    if timeout is None:
        return await anext(self._upstream)
    if timeout == 0:
        raise _AgUiStreamDeadlineExceeded("AG-UI stream timed out")
    loop = asyncio.get_running_loop()
    deadline = self._deadline
    if deadline is None:
        try:
            deadline = loop.time() + timeout
        except OverflowError:
            deadline = math.inf
        self._deadline = deadline
    if loop.time() >= deadline:
        raise _AgUiStreamDeadlineExceeded("AG-UI stream timed out")
    timeout_context = asyncio.timeout_at(deadline)
    try:
        async with timeout_context:
            return await anext(self._upstream)
    except TimeoutError as error:
        if timeout_context.expired():
            raise _AgUiStreamDeadlineExceeded("AG-UI stream timed out") from error
        raise
