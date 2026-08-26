"""Native object-stream coordination, observation, cleanup, and SSE mapping."""

from __future__ import annotations

import asyncio
import inspect
import math
from collections.abc import AsyncGenerator, AsyncIterator
from typing import TYPE_CHECKING, TypeVar, cast

from ._tasks import join_task
from .errors import (
    RunCoordinationError,
    RunCoordinationOwnershipLostError,
    RunCoordinationTimeoutError,
    RunCoordinationUnavailableError,
    TinkerFinError,
    TinkerFinErrorCode,
    TinkerFinLifecycleError,
)
from .native import NativeStreamPart, normalize_native_stream_part
from .sse import (
    SseBody,
    SseEventIdResolver,
    SseMapper,
    SsePayload,
    encode_sse_payload,
)

if TYPE_CHECKING:
    from .runtime import _GraphRunStream

PartT = TypeVar("PartT")

__all__ = ["_finish", "_finish_once", "_observe", "_start"]


def _coordination_error(operation: str, error: Exception) -> RunCoordinationError:
    """Translate only failures owned by a replaceable coordinator boundary."""

    if isinstance(error, RunCoordinationError):
        return error
    diagnostic_context = (
        dict(error.diagnostic_context) if isinstance(error, TinkerFinError) else {}
    )
    diagnostic_context["operation"] = operation
    if isinstance(error, TinkerFinError):
        if error.code is TinkerFinErrorCode.REDIS_LEASE_LOST:
            return RunCoordinationOwnershipLostError(
                "Run coordination ownership was lost",
                diagnostic_context=diagnostic_context,
                cause=error,
            )
        if error.code is TinkerFinErrorCode.REDIS_LEASE_TIMEOUT:
            return RunCoordinationTimeoutError(
                "Run coordination timed out",
                diagnostic_context=diagnostic_context,
                cause=error,
            )
        if error.code is TinkerFinErrorCode.REDIS_LEASE_UNAVAILABLE:
            return RunCoordinationUnavailableError(
                "Run coordination is unavailable",
                diagnostic_context=diagnostic_context,
                cause=error,
            )
    if isinstance(error, TimeoutError):
        return RunCoordinationTimeoutError(
            "Run coordination timed out",
            diagnostic_context=diagnostic_context,
            cause=error,
        )
    return RunCoordinationError(
        "Run coordination failed",
        diagnostic_context=diagnostic_context,
        cause=error,
    )


def _validate_timeout(
    timeout: float | None,
    *,
    name: str = "timeout",
) -> float | None:
    if timeout is None:
        return None
    if isinstance(timeout, bool) or not isinstance(timeout, int | float):
        raise TypeError(f"{name} must be a number or None")
    value = float(timeout)
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    return value


async def _map_sse_item(
    item: PartT,
    *,
    mapper: SseMapper[PartT] | None,
    default: SsePayload,
) -> SsePayload | None:
    if mapper is None:
        return default
    mapped = mapper(item)
    if not inspect.isawaitable(mapped):
        raise TypeError("SSE mapper must return an awaitable")
    payload = await mapped
    if payload is not None and not isinstance(payload, SsePayload):
        raise TypeError("SSE mapper must resolve to SsePayload or None")
    return payload


async def _resolve_sse_event_id(
    item: PartT,
    resolver: SseEventIdResolver[PartT] | None,
) -> str | int | None:
    if resolver is None:
        return None
    resolved = resolver(item)
    if not inspect.isawaitable(resolved):
        raise TypeError("event_id_resolver must return an awaitable")
    return await resolved


async def __anext__(self: _GraphRunStream[PartT]) -> PartT:
    if self._closed:
        raise StopAsyncIteration
    current = cast(asyncio.Task[object] | None, asyncio.current_task())
    if current is None:  # pragma: no cover - async methods run in a Task
        raise TinkerFinLifecycleError("a Graph run stream requires an asyncio task")
    active = self._active_task
    if active is not None and not active.done():
        raise TinkerFinLifecycleError("a Graph run stream operation is already active")
    self._active_task = current
    try:
        if not self._started:
            await self._start()
        source = self._source
        assert source is not None
        try:
            part = await anext(source)
        except StopAsyncIteration:
            await self._finish(None)
            raise
        except BaseException as error:
            await self._finish(error)
            raise
        try:
            await self._observe(part)
        except BaseException as error:
            await self._finish(error)
            raise
        return part
    finally:
        if self._active_task is current:
            self._active_task = None


async def aclose(self: _GraphRunStream[PartT]) -> None:
    """Close the native source and release coordination idempotently.

    Closure requested from a part-observer call chain preserves delivery of the
    part currently being observed. An external closer cancels an active pull.
    """

    current = asyncio.current_task()
    active = self._active_task
    observer_lineage_active = (
        bool(self._active_observers) and self._observer_lineage.get()
    )
    if (
        active is not None
        and active is not current
        and not active.done()
        and not observer_lineage_active
    ):
        active.cancel()
        await asyncio.gather(active, return_exceptions=True)
    await self._finish(None)


def to_sse(
    self: _GraphRunStream[PartT],
    *,
    timeout: float | None = None,
    mapper: SseMapper[NativeStreamPart] | None = None,
    event_id_resolver: SseEventIdResolver[NativeStreamPart] | None = None,
) -> SseBody[str]:
    """Consume this native object stream as safely framed SSE text."""

    total_timeout = _validate_timeout(timeout)

    async def frames() -> AsyncGenerator[str, None]:
        deadline: float | None = None
        if total_timeout is not None:
            loop = asyncio.get_running_loop()
            try:
                deadline = loop.time() + total_timeout
            except OverflowError:
                deadline = math.inf
        try:
            while True:
                try:
                    if deadline is None:
                        raw_part = await anext(self)
                        part = normalize_native_stream_part(raw_part)
                        payload = await _map_sse_item(
                            part,
                            mapper=mapper,
                            default=SsePayload(
                                data=part.model_dump_json(by_alias=True),
                                event="stream-part",
                            ),
                        )
                        if payload is None:
                            continue
                        event_id = await _resolve_sse_event_id(
                            part,
                            event_id_resolver,
                        )
                        frame = encode_sse_payload(payload, event_id=event_id)
                    else:
                        async with asyncio.timeout_at(deadline):
                            raw_part = await anext(self)
                            part = normalize_native_stream_part(raw_part)
                            payload = await _map_sse_item(
                                part,
                                mapper=mapper,
                                default=SsePayload(
                                    data=part.model_dump_json(by_alias=True),
                                    event="stream-part",
                                ),
                            )
                            if payload is None:
                                continue
                            event_id = await _resolve_sse_event_id(
                                part,
                                event_id_resolver,
                            )
                            frame = encode_sse_payload(
                                payload,
                                event_id=event_id,
                            )
                except StopAsyncIteration:
                    return
                except TimeoutError as error:
                    raise TimeoutError("native SSE stream timed out") from error
                yield frame
        finally:
            await self.aclose()

    return SseBody(source_factory=frames, close=self.aclose)


async def _observe(self: _GraphRunStream[PartT], part: PartT) -> None:
    observer = self._on_part
    if observer is None:
        return
    self._active_observers += 1
    token = self._observer_lineage.set(True)
    try:
        observed = observer(part)
        if not inspect.isawaitable(observed):
            raise TypeError("on_part must return an awaitable")
        await observed
    finally:
        self._observer_lineage.reset(token)
        self._active_observers -= 1


async def _start(self: _GraphRunStream[PartT]) -> None:
    coordination_factory = self._coordination_factory
    if coordination_factory is not None:
        try:
            coordination = coordination_factory()
            await coordination.__aenter__()
        except Exception as error:
            translated = _coordination_error("enter", error)
            raise translated from error
        self._coordination = coordination
    try:
        source = self._source_factory()
        if not isinstance(source, AsyncIterator):
            raise TypeError("source_factory must return an async iterator")
        self._source = source
    except BaseException as error:
        await self._finish(error)
        raise
    self._started = True


async def _finish(self: _GraphRunStream[PartT], error: BaseException | None) -> None:
    task = self._finish_task
    if task is None:
        self._closed = True
        task = asyncio.create_task(
            self._finish_once(error),
            name="tinkerfin-graph-run-stream-close",
        )
        self._finish_task = task
    await join_task(task)


async def _finish_once(
    self: _GraphRunStream[PartT], error: BaseException | None
) -> None:
    source = self._source
    self._source = None
    coordination = self._coordination
    self._coordination = None
    cleanup_errors: list[BaseException] = []
    try:
        if source is not None:
            close = getattr(source, "aclose", None)
            if close is not None:
                await close()
    except BaseException as cleanup_error:  # noqa: BLE001 - cleanup outcome
        cleanup_errors.append(cleanup_error)
    if coordination is not None:
        try:
            await coordination.__aexit__(
                None if error is None else type(error),
                error,
                None if error is None else error.__traceback__,
            )
        except BaseException as cleanup_error:  # noqa: BLE001 - cleanup outcome
            if isinstance(cleanup_error, Exception):
                cleanup_errors.append(_coordination_error("exit", cleanup_error))
            else:
                cleanup_errors.append(cleanup_error)

    if error is not None:
        for cleanup_error in cleanup_errors:
            error.add_note(
                "Graph run cleanup also failed: "
                f"{type(cleanup_error).__name__}: {cleanup_error}"
            )
        return
    if len(cleanup_errors) == 1:
        raise cleanup_errors[0]
    if cleanup_errors:
        raise BaseExceptionGroup("Graph run cleanup failed", cleanup_errors)
