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

from ag_ui.core import BaseEvent, RunErrorEvent, RunStartedEvent

from tinkerfin_agui_adapter import (
    AgUiLifecycleEventFactory,
    DeepAgentAgUiAdapter,
    Identity,
    micro_batch,
)

from ._tasks import join_task
from .agui_native import (
    AgUiNativeStreamConfig,
    AgUiNativeStreamConfigurationError,
    AgUiNativeStreamInvocation,
)
from .coordination import RunCoordinator
from .deep_agent import CREATE_DEEP_AGENT
from .native import NativeStreamPart, normalize_native_stream_part
from .sse import (
    SseBody,
    SseEventIdResolver,
    SseMapper,
    SsePayload,
    SsePreflight,
    encode_sse_payload,
)

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
        identity: Identity | None = None,
    ) -> None:
        self._source_factory = source_factory
        self._coordination_factory = coordination_factory
        self._on_part = on_part
        self._identity = identity
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
    def messaging_identity(self) -> Identity:
        """Return the immutable durable run identity."""

        identity = self._identity
        if identity is None:
            raise RuntimeError("a native stream requires an Identity")
        return identity

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

    @property
    def messaging_identity(self) -> Identity:
        """Return the immutable durable run identity."""

        return self._identity

    @property
    def messaging_cancel_callback(
        self,
    ) -> Callable[[], Awaitable[list[BaseEvent]]]:
        """Publish the stream-owned idempotent cancellation callback."""

        return self.abort

    def messaging_cancel_callback_matches(self, callback: object) -> bool:
        """Return whether a supplied callback names this stream's same owner."""

        return callback == self.abort

    @classmethod
    def from_initialization_error(
        cls,
        error: Exception,
        *,
        identity: Identity,
    ) -> AgUiEventStream:
        """Create one standard lifecycle for an owner-only Runtime setup failure."""

        if not isinstance(error, Exception):
            raise TypeError("error must be an Exception")

        async def failed_parts() -> AsyncIterator[object]:
            if False:  # pragma: no cover - supplies the async iterator shape
                yield None
            raise error

        stream = cls(
            parts=failed_parts(),
            identity=identity,
            expose_reasoning_events=False,
            expose_subagent_events=True,
            prior_tool_call_ids=frozenset(),
            timeout=None,
            settlement_timeout=None,
            on_event=None,
        )
        stream._runtime_error_code = "runtime_initialization_error"
        stream._initialization_failed = True
        return stream

    def __init__(
        self,
        *,
        parts: AsyncIterable[object],
        identity: Identity,
        expose_reasoning_events: bool,
        expose_subagent_events: bool,
        prior_tool_call_ids: frozenset[str],
        timeout: float | None,
        settlement_timeout: float | None = None,
        on_event: EventObserver | None,
    ) -> None:
        self._lifecycle = AgUiLifecycleEventFactory()
        self._lifecycle.validate_identity(identity)
        self._identity = identity
        self._timeout = _validate_timeout(timeout)
        self._settlement_timeout = _validate_timeout(
            settlement_timeout,
            name="settlement_timeout",
        )
        self._deadline: float | None = None
        self._upstream = aiter(parts)
        self._upstream_closed = False
        self._adapter = DeepAgentAgUiAdapter(
            identity=identity,
            expose_reasoning_events=expose_reasoning_events,
            expose_subagent_events=expose_subagent_events,
            prior_tool_call_ids=prior_tool_call_ids,
        )
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
        self._runtime_error_code = "runtime_error"
        self._initialization_failed = False
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
            yield self._decorate_initialization_event(
                self._lifecycle.started(identity=self._identity)
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
                identity=self._identity,
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
                else self._runtime_error_code
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
                yield self._decorate_initialization_event(
                    self._lifecycle.failed(
                        identity=self._identity,
                        message="Agent run failed",
                        code=error_code,
                    )
                )
        except BaseException as error:
            primary = error
            raise
        finally:
            await self._close_upstream(primary)

    def _decorate_initialization_event(self, event: BaseEvent) -> BaseEvent:
        """Mark only main lifecycle events emitted for initialization failure."""

        if not self._initialization_failed or not isinstance(
            event,
            RunStartedEvent | RunErrorEvent,
        ):
            return event
        raw_event: dict[str, object] = (
            dict(event.raw_event)
            if isinstance(event.raw_event, dict)
            else {
                "threadId": self._identity.thread_id,
                "runId": self._identity.run_id,
            }
        )
        raw_event["initializationFailed"] = True
        return event.model_copy(update={"raw_event": raw_event})

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


class TinkerFinRun(Generic[PartT]):
    """Single-use binding of one native source factory and optional Identity."""

    __slots__ = (
        "_on_part",
        "_identity",
        "_run_coordinator",
        "_source_factory",
        "_stream_claimed",
    )

    def __init__(
        self,
        *,
        source_factory: Callable[[], AsyncIterator[PartT]],
        run_coordinator: RunCoordinator | None,
        identity: Identity | None,
        on_part: PartObserver[PartT] | None,
    ) -> None:
        self._source_factory = source_factory
        self._run_coordinator = run_coordinator
        self._identity = identity
        self._on_part = on_part
        self._stream_claimed = False

    def astream(self) -> GraphRunStream[PartT]:
        """Claim and create the run's one native object stream."""

        source_factory, coordination_factory, on_part = self._claim_stream_inputs()
        return GraphRunStream(
            source_factory=source_factory,
            coordination_factory=coordination_factory,
            on_part=on_part,
            identity=self._identity,
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
        identity = self._identity
        coordination_factory = (
            None
            if coordinator is None
            else lambda: coordinator(cast(Identity, identity))
        )
        return (
            self._source_factory,
            coordination_factory,
            self._on_part,
        )

    def astream_agui(
        self,
        *,
        timeout: float | None = None,
        settlement_timeout: float | None = None,
        expose_reasoning_events: bool = False,
        expose_subagent_events: bool = True,
        prior_tool_call_ids: frozenset[str] = frozenset(),
        on_event: EventObserver | None = None,
    ) -> AgUiEventStream:
        """Claim the native source and convert it to one AG-UI event stream.

        Args:
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

        identity = self._identity
        if identity is None:
            raise ValueError("AG-UI streaming requires an Identity")
        if on_event is not None and not callable(on_event):
            raise TypeError("on_event must be an async callable or None")
        return AgUiEventStream(
            parts=self.astream(),
            identity=identity,
            timeout=timeout,
            settlement_timeout=settlement_timeout,
            expose_reasoning_events=expose_reasoning_events,
            expose_subagent_events=expose_subagent_events,
            prior_tool_call_ids=prior_tool_call_ids,
            on_event=on_event,
        )


class NativeTinkerFinRun(
    TinkerFinRun[Mapping[str, object]],
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
            identity=self._identity,
        )


class TinkerFin:
    """Globally shareable stateless factory for single-use source bindings."""

    __slots__ = ("_run_coordinator",)

    create_deep_agent = CREATE_DEEP_AGENT

    def __init__(
        self,
        *,
        run_coordinator: RunCoordinator | None = None,
    ) -> None:
        if run_coordinator is not None and not callable(run_coordinator):
            raise TypeError("run_coordinator must be callable or None")
        self._run_coordinator = run_coordinator

    @overload
    def run(
        self,
        source_factory: AgUiNativeStreamInvocation,
        *,
        identity: Identity,
        on_part: PartObserver[Mapping[str, object]] | None = None,
    ) -> NativeTinkerFinRun: ...

    @overload
    def run(
        self,
        source_factory: Callable[[], AsyncIterator[PartT]],
        *,
        identity: Identity | None = None,
        on_part: PartObserver[PartT] | None = None,
    ) -> TinkerFinRun[PartT]: ...

    def run(
        self,
        source_factory: (
            AgUiNativeStreamInvocation | Callable[[], AsyncIterator[PartT]]
        ),
        *,
        identity: Identity | None = None,
        on_part: (
            PartObserver[Mapping[str, object]] | PartObserver[PartT] | None
        ) = None,
    ) -> NativeTinkerFinRun | TinkerFinRun[PartT]:
        """Bind one lazy generic source or preflighted AG-UI native invocation.

        Args:
            source_factory: A zero-argument asynchronous source factory, or a native
                invocation created by ``AgUiNativeStreamConfig.bind``.
            identity: Optional thread and run identity. Strict native invocations and
                configured coordinators require it.
            on_part: Optional asynchronous observer awaited before native delivery or
                AG-UI conversion.

        Returns:
            A single-use generic run, or a profiled native run for a strict invocation.

        Raises:
            TypeError: The source or observer is not callable.
            ValueError: ``identity`` is missing when required.
            AgUiNativeStreamConfigurationError: A strict invocation is invalid.
        """

        strict_invocation = isinstance(source_factory, AgUiNativeStreamInvocation)
        if strict_invocation:
            source_factory._validate()
        if not callable(source_factory):
            raise TypeError("source_factory must be callable")
        self._validate_run_binding(identity=identity, on_part=on_part)
        coordinator = self._run_coordinator
        if strict_invocation:
            if identity is None:
                raise ValueError("a strict native invocation requires an Identity")
            invocation = cast(
                AgUiNativeStreamInvocation, source_factory
            )._bind_identity(identity)
            return NativeTinkerFinRun(
                source_factory=invocation,
                run_coordinator=coordinator,
                identity=identity,
                on_part=cast(
                    PartObserver[Mapping[str, object]] | None,
                    on_part,
                ),
            )
        return TinkerFinRun(
            source_factory=source_factory,
            run_coordinator=coordinator,
            identity=identity,
            on_part=cast(PartObserver[PartT] | None, on_part),
        )

    def _validate_run_binding(
        self,
        *,
        identity: Identity | None,
        on_part: object | None,
    ) -> None:
        """校验一次请求绑定，不创建 Graph、source 或协调上下文"""

        coordinator = self._run_coordinator
        if identity is not None and not isinstance(identity, Identity):
            raise TypeError("identity must be an Identity or None")
        if coordinator is not None and identity is None:
            raise ValueError("identity is required when run_coordinator is configured")
        if on_part is not None and not callable(on_part):
            raise TypeError("on_part must be an async callable or None")

    def _run_native(
        self,
        source_factory: Callable[[], AsyncIterator[Mapping[str, object]]],
        *,
        identity: Identity,
        on_part: PartObserver[object] | None,
    ) -> NativeTinkerFinRun:
        """Bind a validated canonical v2 source without changing public low-level API."""

        self._validate_run_binding(identity=identity, on_part=on_part)
        return NativeTinkerFinRun(
            source_factory=source_factory,
            run_coordinator=self._run_coordinator,
            identity=identity,
            on_part=cast(PartObserver[Mapping[str, object]] | None, on_part),
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
