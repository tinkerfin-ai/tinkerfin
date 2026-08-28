"""Native object-stream coordination, observation, cleanup, and SSE mapping."""

from __future__ import annotations

import asyncio
import inspect
import math
from collections.abc import AsyncGenerator, AsyncIterator
from typing import TYPE_CHECKING, TypeVar, cast

from tinkerfin_contracts import RunTerminalOutcome
from tinkerfin_native_stream import NativeStreamContractError

from ._tasks import join_task
from .errors import (
    RunCoordinationError,
    RunCoordinationOwnershipLostError,
    RunCoordinationTimeoutError,
    RunCoordinationUnavailableError,
    RunObservationError,
    TinkerFinError,
    TinkerFinErrorCode,
    TinkerFinLifecycleError,
    TinkerFinStreamProtocolError,
)
from .native import NativeStreamPart
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


def _native_contract_error(
    error: NativeStreamContractError,
) -> TinkerFinStreamProtocolError:
    """Translate the shared Native contract failure at the Runtime boundary."""

    return TinkerFinStreamProtocolError(
        "Native stream part violates the current contract",
        context=error.context,
        diagnostic_context={"native_code": error.code.value},
        cause=error,
    )


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
    """Own one pull, publish its canonical frame, and preserve terminal ordering.

    Lazy startup and Observer failure racing happen inside the same active-operation
    slot. Natural exhaustion alone selects success or interrupt; every exception and
    cancellation settles the shared Runtime lifecycle before it propagates.
    """

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
            part = await _next_or_observer_failure(self, source)
        except StopAsyncIteration:
            outcome: RunTerminalOutcome
            if self._observation.context.input_kind == "abandon":
                outcome = "abandoned"
            elif self._last_root_interrupt_ids:
                outcome = "interrupted"
            else:
                outcome = "succeeded"
            await self._finish(None, outcome=outcome)
            raise
        except BaseException as error:
            if isinstance(error, Exception):
                self.error = error
            await self._finish(error, outcome=_error_outcome(error))
            raise
        try:
            await self._observe(part)
        except BaseException as error:
            if isinstance(error, Exception):
                self.error = error
            await self._finish(error, outcome=_error_outcome(error))
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
    await self._finish(
        None,
        outcome="cancelled" if self._started else None,
    )


async def _next_or_observer_failure(
    self: _GraphRunStream[PartT],
    source: AsyncIterator[PartT],
) -> PartT:
    """Race one upstream pull with the managed Observer failure signal."""

    if not self._observation.enabled:
        return await anext(source)

    async def pull_next() -> PartT:
        return await anext(source)

    pull = asyncio.create_task(pull_next(), name="tinkerfin-graph-run-pull")
    failure = asyncio.create_task(
        self._observation.wait_failure(),
        name="tinkerfin-observer-failure-wait",
    )
    try:
        done, _pending = await asyncio.wait(
            (pull, failure),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if failure in done:
            if not pull.done():
                pull.cancel()
            await asyncio.gather(pull, return_exceptions=True)
            await failure
            raise AssertionError("Observer failure waiter returned without failing")
        return await pull
    except BaseException:
        if not pull.done():
            pull.cancel()
        raise
    finally:
        if not failure.done():
            failure.cancel()
        await asyncio.gather(failure, return_exceptions=True)
        if not pull.done():
            pull.cancel()
        await asyncio.gather(pull, return_exceptions=True)


def _error_outcome(error: BaseException) -> RunTerminalOutcome:
    if isinstance(error, (asyncio.CancelledError, GeneratorExit)):
        return "cancelled"
    return "failed"


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
                        part = self._take_frame(raw_part).replay
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
                            part = self._take_frame(raw_part).replay
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
    """Normalize once, retain the frame sidecar, then notify public consumers.

    The order is Driver validation, Runtime Observation, and finally ``on_part``.
    Keeping the sidecar before callbacks makes the exact same canonical frame available
    to one downstream Adapter, SSE, or Messaging consumer without parsing the raw part.
    """

    try:
        frame = self._stream_driver.normalize(
            part,
            context=self._observation.context,
        )
    except NativeStreamContractError as error:
        translated = _native_contract_error(error)
        raise translated from error
    # The sidecar is the only downstream normalization authority for this raw part.
    # AG-UI, native SSE, and Messaging must consume it rather than parse v2 again.
    self._native_frame = (part, frame)
    self._last_root_interrupt_ids = frame.root_interrupt_ids
    if self._observation.enabled:
        for observation in frame.observations:
            await self._observation.observe(observation)
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
    self._started = True
    try:
        await self._observation.start()
        source = self._source_factory()
        if not isinstance(source, AsyncIterator):
            raise TypeError("source_factory must return an async iterator")
        self._source = source
    except BaseException as error:
        if isinstance(error, Exception):
            self.error = error
        await self._finish(error, outcome=_error_outcome(error))
        raise


async def _finish(
    self: _GraphRunStream[PartT],
    error: BaseException | None,
    *,
    outcome: RunTerminalOutcome | None = None,
) -> None:
    task = self._finish_task
    if task is None:
        self._closed = True
        task = asyncio.create_task(
            self._finish_once(error, outcome),
            name="tinkerfin-graph-run-stream-close",
        )
        self._finish_task = task
    await join_task(task)


async def _finish_once(
    self: _GraphRunStream[PartT],
    error: BaseException | None,
    outcome: RunTerminalOutcome | None,
) -> None:
    """Settle upstream ownership and publish one final Observation lifecycle.

    Upstream and coordinator resources close before terminal Observation. Cleanup can
    downgrade an otherwise successful outcome to failure, while an already selected
    execution error remains primary and receives every cleanup failure as a note.
    Observer terminal/close failures are retained without abandoning other sessions.
    """

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

    # A request-specific settlement hook participates in the same retained close task.
    # It runs after upstream ownership is released and at most once, including when a
    # stream is closed before its first pull.
    on_settle = self._on_settle
    self._on_settle = None
    if on_settle is not None:
        try:
            await on_settle()
        except BaseException as cleanup_error:  # noqa: BLE001 - cleanup outcome
            cleanup_errors.append(cleanup_error)

    effective_outcome = outcome
    if self._started and effective_outcome is None:
        effective_outcome = "failed" if error is not None else "cancelled"
    if (
        cleanup_errors
        and effective_outcome in {"succeeded", "interrupted"}
        and error is None
    ):
        effective_outcome = "failed"

    observation_errors: list[BaseException] = []
    if self._started and effective_outcome is not None:
        terminal_error = error or (cleanup_errors[0] if cleanup_errors else None)
        code = _terminal_code(effective_outcome, terminal_error)
        try:
            await self._observation.terminal(
                effective_outcome,
                code=code,
                error=terminal_error,
                interrupt_ids=self._last_root_interrupt_ids,
            )
        except BaseException as observation_error:  # noqa: BLE001 - cleanup continues
            observation_errors.append(observation_error)
        try:
            await self._observation.close()
        except BaseException as observation_error:  # noqa: BLE001 - report below
            observation_errors.append(observation_error)

    all_secondary = [*cleanup_errors, *observation_errors]
    if error is not None:
        for secondary in all_secondary:
            error.add_note(
                "Graph run settlement also failed: "
                f"{type(secondary).__name__}: {secondary}"
            )
        return
    if len(all_secondary) == 1:
        raise all_secondary[0]
    if all_secondary:
        raise BaseExceptionGroup("Graph run settlement failed", all_secondary)


def _terminal_code(
    outcome: RunTerminalOutcome,
    error: BaseException | None,
) -> str | None:
    if outcome == "failed":
        return (
            "observer_failed"
            if isinstance(error, RunObservationError)
            else "runtime_error"
        )
    if outcome == "cancelled":
        return "cancelled"
    if outcome == "abandoned":
        return "abandoned"
    return None
