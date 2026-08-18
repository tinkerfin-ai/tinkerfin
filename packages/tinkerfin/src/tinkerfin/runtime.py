"""Stateless Graph binding and native asynchronous streaming."""

from __future__ import annotations

import asyncio
import inspect
import math
from collections.abc import (
    AsyncGenerator,
    AsyncIterable,
    AsyncIterator,
    Awaitable,
    Callable,
    Mapping,
)
from contextlib import AbstractAsyncContextManager
from contextvars import ContextVar
from typing import Generic, TypeAlias, TypeVar, cast, overload

from ag_ui.core import BaseEvent

from tinkerfin_agui_adapter import (
    AgUiLifecycleEventFactory,
    DeepAgentAgUiAdapter,
    micro_batch,
)

from ._tasks import join_task
from .agui_native import (
    AgUiNativeStreamConfig,
    AgUiNativeStreamConfigurationError,
    AgUiNativeStreamInvocation,
)
from .coordination import RunCoordinator
from .native import NativeStreamPart, normalize_native_stream_part
from .sse import (
    SseBody,
    SseEventIdResolver,
    SseMapper,
    SsePayload,
    SsePreflight,
    encode_sse_payload,
)

PrincipalT = TypeVar("PrincipalT")
PartT = TypeVar("PartT")

PartObserver: TypeAlias = Callable[[PartT], Awaitable[None]]
EventObserver = Callable[[BaseEvent], Awaitable[None]]


class _AgUiStreamDeadlineExceeded(TimeoutError):
    """Identify only the total pull deadline owned by `AgUiEventStream`."""


class AgUiSettlementTimeoutError(TimeoutError):
    """The caller stopped waiting while AG-UI close settlement remains owned."""

    def __init__(self, *, timeout: float) -> None:
        self.timeout = timeout
        super().__init__(f"AG-UI settlement timed out after {timeout:g} seconds")


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


def _validate_run_identifier(name: str, value: object) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value or value != value.strip():
        raise ValueError(f"{name} must be non-blank without surrounding whitespace")


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


class GraphRunStream(Generic[PartT]):
    """Single-use native stream with deterministic upstream cleanup."""

    def __init__(
        self,
        *,
        source_factory: Callable[[], AsyncIterator[PartT]],
        coordination_factory: (Callable[[], AbstractAsyncContextManager[None]] | None),
        on_part: PartObserver[PartT] | None,
    ) -> None:
        self._source_factory = source_factory
        self._coordination_factory = coordination_factory
        self._on_part = on_part
        self._source: AsyncIterator[PartT] | None = None
        self._coordination: AbstractAsyncContextManager[None] | None = None
        self._started = False
        self._closed = False
        self._active_task: asyncio.Task[object] | None = None
        self._observer_lineage = ContextVar(
            f"tinkerfin_graph_part_observer_lineage_{id(self)}",
            default=False,
        )
        self._active_observers = 0
        self._finish_task: asyncio.Task[None] | None = None

    def __aiter__(self) -> GraphRunStream[PartT]:
        return self

    async def __anext__(self) -> PartT:
        if self._closed:
            raise StopAsyncIteration
        current = cast(asyncio.Task[object] | None, asyncio.current_task())
        if current is None:  # pragma: no cover - async methods run in a Task
            raise RuntimeError("a Graph run stream requires an asyncio task")
        active = self._active_task
        if active is not None and not active.done():
            raise RuntimeError("a Graph run stream operation is already active")
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

    async def aclose(self) -> None:
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
        self,
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

    async def _observe(self, part: PartT) -> None:
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

    async def _start(self) -> None:
        coordination_factory = self._coordination_factory
        if coordination_factory is not None:
            coordination = coordination_factory()
            await coordination.__aenter__()
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

    async def _finish(self, error: BaseException | None) -> None:
        task = self._finish_task
        if task is None:
            self._closed = True
            task = asyncio.create_task(
                self._finish_once(error),
                name="tinkerfin-graph-run-stream-close",
            )
            self._finish_task = task
        await join_task(task)

    async def _finish_once(self, error: BaseException | None) -> None:
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


class NativeGraphRunStream(GraphRunStream[Mapping[str, object]]):
    """Canonical LangGraph v2 stream with a complete durable codec profile."""

    @property
    def messaging_codec_profile(self) -> str:
        """Return the canonical native persistence profile."""

        return "langgraph.stream-part.v2.v1"

    @property
    def messaging_source_type(self) -> type[Mapping[str, object]]:
        """Return the declared live LangGraph envelope class."""

        return Mapping

    @property
    def messaging_replay_type(self) -> type[NativeStreamPart]:
        """Return the decoded durable replay class."""

        return NativeStreamPart


class AgUiEventStream:
    """Own one observed AG-UI stream and its independently budgeted close task."""

    @property
    def messaging_codec_profile(self) -> str:
        """Return the canonical AG-UI event persistence profile."""

        return "agui.event.v1"

    @property
    def messaging_source_type(self) -> type[BaseEvent]:
        """Return the declared live AG-UI event base class."""

        return BaseEvent

    @property
    def messaging_replay_type(self) -> type[BaseEvent]:
        """Return the decoded durable event base class."""

        return BaseEvent

    def __init__(
        self,
        *,
        parts: AsyncIterable[object],
        thread_id: str,
        run_id: str,
        parent_run_id: str | None,
        expose_reasoning_events: bool,
        expose_subagent_events: bool,
        prior_tool_call_ids: frozenset[str],
        timeout: float | None,
        settlement_timeout: float | None = None,
        on_event: EventObserver | None,
    ) -> None:
        self._thread_id = thread_id
        self._run_id = run_id
        self._parent_run_id = parent_run_id
        self._timeout = _validate_timeout(timeout)
        self._settlement_timeout = _validate_timeout(
            settlement_timeout,
            name="settlement_timeout",
        )
        self._deadline: float | None = None
        self._upstream = aiter(parts)
        self._upstream_closed = False
        self._adapter = DeepAgentAgUiAdapter(
            run_id,
            expose_reasoning_events=expose_reasoning_events,
            expose_subagent_events=expose_subagent_events,
            prior_tool_call_ids=prior_tool_call_ids,
        )
        self._lifecycle = AgUiLifecycleEventFactory()
        self._source = aiter(micro_batch(self._convert()))
        self._on_event = on_event
        self._closed = False
        self._active_task: asyncio.Task[object] | None = None
        self._observer_lineage = ContextVar(
            f"tinkerfin_agui_observer_lineage_{id(self)}",
            default=False,
        )
        self._active_observers = 0
        self._close_task: asyncio.Task[None] | None = None
        self._main_started = False
        self._completed = False
        self._aborted = False
        self._abort_events_delivered = False
        self._secondary_error_notes: list[str] = []
        self.error: Exception | None = None

    def __aiter__(self) -> AgUiEventStream:
        return self

    async def __anext__(self) -> BaseEvent:
        if self._closed:
            raise StopAsyncIteration
        current = cast(asyncio.Task[object] | None, asyncio.current_task())
        if current is None:  # pragma: no cover - async methods run in a Task
            raise RuntimeError("an AG-UI event stream requires an asyncio task")
        active = self._active_task
        if active is not None and not active.done():
            raise RuntimeError("an AG-UI event stream operation is already active")
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

    async def abort(self) -> list[BaseEvent]:
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
            raise RuntimeError(
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
                self._lifecycle.failed(
                    run_id=self._run_id,
                    message="Agent run cancelled",
                    code="cancelled",
                )
            )
        self._abort_events_delivered = True
        for event in tail:
            await self._observe(event)
        return tail

    async def aclose(self) -> None:
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
        self,
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
        self,
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
                        "AG-UI processing also failed: "
                        f"{type(primary).__name__}: {primary}"
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

    @staticmethod
    def _close_finished(task: asyncio.Task[None]) -> None:
        """Consume a retained close failure even when no caller waits again."""

        if not task.cancelled():
            task.exception()

    async def _close_once(self, active: asyncio.Task[object] | None) -> None:
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

    async def _observe(self, event: BaseEvent) -> None:
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

    async def _close_upstream(self, primary: BaseException | None) -> None:
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
                        "AG-UI processing also failed: "
                        f"{type(primary).__name__}: {primary}"
                    )
                for note in getattr(cleanup_error, "__notes__", ()):
                    self._record_secondary_error_note(note)
                raise
            if primary is not None and not isinstance(primary, GeneratorExit):
                note = (
                    f"native parts cleanup also failed: CancelledError: {cleanup_error}"
                )
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

    def _record_secondary_error_note(self, note: str) -> None:
        """Retain cleanup evidence across the micro-batch cancellation boundary."""

        if note not in self._secondary_error_notes:
            self._secondary_error_notes.append(note)

    async def _convert(self) -> AsyncIterator[BaseEvent]:
        primary: BaseException | None = None
        terminal = False
        try:
            self._main_started = True
            yield self._lifecycle.started(
                thread_id=self._thread_id,
                run_id=self._run_id,
                parent_run_id=self._parent_run_id,
            )
            while True:
                try:
                    part = await self._next_part()
                except StopAsyncIteration:
                    break
                if self._aborted:
                    return
                for event in self._adapter.process(part):
                    yield event
            await self._close_upstream(None)
            for event in self._adapter.finish():
                yield event
            outcome = self._adapter.main_outcome()
            self._completed = True
            terminal = True
            yield self._lifecycle.finished(
                thread_id=self._thread_id,
                run_id=self._run_id,
                outcome=outcome,
            )
        except asyncio.CancelledError as error:
            primary = error
            raise
        except GeneratorExit as error:
            primary = error
            raise
        except Exception as error:  # noqa: BLE001 - protocol error terminal
            self.error = error
            primary = error
            error_code = (
                "stream_timeout"
                if isinstance(error, _AgUiStreamDeadlineExceeded)
                else "runtime_error"
            )
            try:
                await self._close_upstream(error)
            except asyncio.CancelledError as cancellation:
                primary = cancellation
                raise
            for event in self._adapter.abort(code=error_code):
                yield event
            if not terminal:
                terminal = True
                self._completed = True
                yield self._lifecycle.failed(
                    run_id=self._run_id,
                    message="Agent run failed",
                    code=error_code,
                )
        except BaseException as error:
            primary = error
            raise
        finally:
            await self._close_upstream(primary)

    async def _next_part(self) -> object:
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


class TinkerFinRun(Generic[PartT, PrincipalT]):
    """Single-use binding of one native source factory and optional principal."""

    __slots__ = (
        "_on_part",
        "_principal",
        "_run_coordinator",
        "_source_factory",
        "_stream_claimed",
    )

    def __init__(
        self,
        *,
        source_factory: Callable[[], AsyncIterator[PartT]],
        run_coordinator: RunCoordinator[PrincipalT] | None,
        principal: PrincipalT | None,
        on_part: PartObserver[PartT] | None,
    ) -> None:
        self._source_factory = source_factory
        self._run_coordinator = run_coordinator
        self._principal = principal
        self._on_part = on_part
        self._stream_claimed = False

    def astream(self) -> GraphRunStream[PartT]:
        """Claim and create the run's one native object stream."""

        source_factory, coordination_factory, on_part = self._claim_stream_inputs()
        return GraphRunStream(
            source_factory=source_factory,
            coordination_factory=coordination_factory,
            on_part=on_part,
        )

    def _claim_stream_inputs(
        self,
    ) -> tuple[
        Callable[[], AsyncIterator[PartT]],
        Callable[[], AbstractAsyncContextManager[None]] | None,
        PartObserver[PartT] | None,
    ]:
        """Claim the binding and return the inputs for exactly one stream type."""

        if self._stream_claimed:
            raise RuntimeError("a TinkerFin run can create only one object stream")
        self._stream_claimed = True
        coordinator = self._run_coordinator
        principal = self._principal
        coordination_factory = (
            None
            if coordinator is None
            else lambda: coordinator(cast(PrincipalT, principal))
        )
        return (
            self._source_factory,
            coordination_factory,
            self._on_part,
        )

    def astream_agui(
        self,
        *,
        thread_id: str,
        run_id: str,
        parent_run_id: str | None = None,
        timeout: float | None = None,
        settlement_timeout: float | None = None,
        expose_reasoning_events: bool = False,
        expose_subagent_events: bool = True,
        prior_tool_call_ids: frozenset[str] = frozenset(),
        on_event: EventObserver | None = None,
    ) -> AgUiEventStream:
        """Claim the native source and convert it to one AG-UI event stream.

        Args:
            thread_id: Stable AG-UI thread identity supplied by the caller.
            run_id: Stable identity for this AG-UI request.
            parent_run_id: Optional run-lineage identity; never subgraph provenance.
            timeout: Optional total native-part pull deadline in seconds.
            settlement_timeout: Optional per-caller close-settlement wait in seconds.
                Expiry never cancels the retained close task.
            expose_reasoning_events: Whether verified public reasoning emits events.
            expose_subagent_events: Whether validated non-root events are emitted.
            prior_tool_call_ids: Scoped Tool call IDs already emitted before resume.
            on_event: Optional async observer awaited before each event is delivered.

        Returns:
            A single-use observed AG-UI object stream.

        Raises:
            TypeError: An identifier, callback, timeout, or option has the wrong type.
            ValueError: An identifier or timeout value is invalid.
        """

        _validate_run_identifier("thread_id", thread_id)
        _validate_run_identifier("run_id", run_id)
        if parent_run_id is not None:
            _validate_run_identifier("parent_run_id", parent_run_id)
        if on_event is not None and not callable(on_event):
            raise TypeError("on_event must be an async callable or None")
        return AgUiEventStream(
            parts=self.astream(),
            thread_id=thread_id,
            run_id=run_id,
            parent_run_id=parent_run_id,
            timeout=timeout,
            settlement_timeout=settlement_timeout,
            expose_reasoning_events=expose_reasoning_events,
            expose_subagent_events=expose_subagent_events,
            prior_tool_call_ids=prior_tool_call_ids,
            on_event=on_event,
        )


class NativeTinkerFinRun(
    TinkerFinRun[Mapping[str, object], PrincipalT],
    Generic[PrincipalT],
):
    """Single-use binding whose object stream is canonical LangGraph v2 data."""

    __slots__ = ()

    def astream(self) -> NativeGraphRunStream:
        """Claim and create one profiled canonical native stream."""

        source_factory, coordination_factory, on_part = self._claim_stream_inputs()
        return NativeGraphRunStream(
            source_factory=source_factory,
            coordination_factory=coordination_factory,
            on_part=on_part,
        )


class TinkerFin(Generic[PrincipalT]):
    """Globally shareable stateless factory for single-use source bindings."""

    __slots__ = ("_run_coordinator",)

    def __init__(
        self,
        *,
        run_coordinator: RunCoordinator[PrincipalT] | None = None,
    ) -> None:
        if run_coordinator is not None and not callable(run_coordinator):
            raise TypeError("run_coordinator must be callable or None")
        self._run_coordinator = run_coordinator

    @overload
    def run(
        self,
        source_factory: AgUiNativeStreamInvocation,
        *,
        principal: PrincipalT | None = None,
        on_part: PartObserver[Mapping[str, object]] | None = None,
    ) -> NativeTinkerFinRun[PrincipalT]: ...

    @overload
    def run(
        self,
        source_factory: Callable[[], AsyncIterator[PartT]],
        *,
        principal: PrincipalT | None = None,
        on_part: PartObserver[PartT] | None = None,
    ) -> TinkerFinRun[PartT, PrincipalT]: ...

    def run(
        self,
        source_factory: (
            AgUiNativeStreamInvocation | Callable[[], AsyncIterator[PartT]]
        ),
        *,
        principal: PrincipalT | None = None,
        on_part: (
            PartObserver[Mapping[str, object]] | PartObserver[PartT] | None
        ) = None,
    ) -> NativeTinkerFinRun[PrincipalT] | TinkerFinRun[PartT, PrincipalT]:
        """Bind one lazy generic source or preflighted AG-UI native invocation.

        Args:
            source_factory: A zero-argument asynchronous source factory, or a native
                invocation created by ``AgUiNativeStreamConfig.bind``.
            principal: Optional application principal required by a configured run
                coordinator and forbidden without one.
            on_part: Optional asynchronous observer awaited before native delivery or
                AG-UI conversion.

        Returns:
            A single-use generic run, or a profiled native run for a strict invocation.

        Raises:
            TypeError: The source or observer is not callable.
            ValueError: ``principal`` does not match coordinator configuration.
            AgUiNativeStreamConfigurationError: A strict invocation is invalid.
        """

        strict_invocation = isinstance(source_factory, AgUiNativeStreamInvocation)
        if strict_invocation:
            source_factory._validate()
        coordinator = self._run_coordinator
        if coordinator is None and principal is not None:
            raise ValueError("principal requires a run_coordinator")
        if coordinator is not None and principal is None:
            raise ValueError("principal is required when run_coordinator is configured")
        if not callable(source_factory):
            raise TypeError("source_factory must be callable")
        if on_part is not None and not callable(on_part):
            raise TypeError("on_part must be an async callable or None")
        if strict_invocation:
            return NativeTinkerFinRun(
                source_factory=cast(AgUiNativeStreamInvocation, source_factory),
                run_coordinator=coordinator,
                principal=principal,
                on_part=cast(
                    PartObserver[Mapping[str, object]] | None,
                    on_part,
                ),
            )
        return TinkerFinRun(
            source_factory=source_factory,
            run_coordinator=coordinator,
            principal=principal,
            on_part=cast(PartObserver[PartT] | None, on_part),
        )


__all__ = [
    "AgUiEventStream",
    "AgUiNativeStreamConfig",
    "AgUiNativeStreamConfigurationError",
    "AgUiNativeStreamInvocation",
    "AgUiSettlementTimeoutError",
    "EventObserver",
    "GraphRunStream",
    "NativeGraphRunStream",
    "NativeStreamPart",
    "NativeTinkerFinRun",
    "PartObserver",
    "SseBody",
    "SseEventIdResolver",
    "SseMapper",
    "SsePayload",
    "SsePreflight",
    "TinkerFin",
    "TinkerFinRun",
]
