"""Stateless Graph binding and native asynchronous streaming."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import (
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
from deepagents.graph import DeepAgentState
from langchain_core.language_models import BaseChatModel

from tinkerfin_agui_adapter import (
    AgUiLifecycleEventFactory,
    DeepAgentAgUiAdapter,
    Identity,
    micro_batch,
)

from . import _runtime_agui, _runtime_streams
from ._runtime_agui import _AgUiStreamDeadlineExceeded
from ._runtime_streams import _validate_timeout
from ._state_schema import validate_state_schema
from .agui_native import (
    AgUiNativeStreamConfig,
    AgUiNativeStreamConfigurationError,
    AgUiNativeStreamInvocation,
)
from .coordination import RunCoordinator
from .deep_agent import CREATE_DEEP_AGENT
from .errors import (
    AgUiSettlementTimeoutError,
    TinkerFinLifecycleError,
)
from .native import NativeStreamPart
from .plan._clarification import create_clarification_binding
from .plan._config import AgentMode, PlanOptions, validate_agent_mode
from .plan.clarification import ClarificationFormBase, DefaultClarificationForm
from .sse import (
    SseBody,
    SseEventIdResolver,
    SseMapper,
    SsePayload,
    SsePreflight,
)

PartT = TypeVar("PartT")

logger = logging.getLogger(__name__)

PartObserver: TypeAlias = Callable[[PartT], Awaitable[None]]
EventObserver = Callable[[BaseEvent], Awaitable[None]]


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
        return await _runtime_streams.__anext__(
            self,
        )

    async def aclose(self) -> None:
        """Close the native source and release coordination idempotently.

        Closure requested from a part-observer call chain preserves delivery of the
        part currently being observed. An external closer cancels an active pull.
        """

        return await _runtime_streams.aclose(
            self,
        )

    def to_sse(
        self,
        *,
        timeout: float | None = None,
        mapper: SseMapper[NativeStreamPart] | None = None,
        event_id_resolver: SseEventIdResolver[NativeStreamPart] | None = None,
    ) -> SseBody[str]:
        """Consume this native object stream as safely framed SSE text."""

        return _runtime_streams.to_sse(
            self,
            timeout=timeout,
            mapper=mapper,
            event_id_resolver=event_id_resolver,
        )

    async def _observe(self, part: PartT) -> None:
        return await _runtime_streams._observe(
            self,
            part,
        )

    async def _start(self) -> None:
        return await _runtime_streams._start(
            self,
        )

    async def _finish(self, error: BaseException | None) -> None:
        return await _runtime_streams._finish(
            self,
            error,
        )

    async def _finish_once(self, error: BaseException | None) -> None:
        return await _runtime_streams._finish_once(
            self,
            error,
        )


class NativeGraphRunStream(GraphRunStream[Mapping[str, object]]):
    """Canonical LangGraph v2 stream with a complete durable codec profile."""

    @property
    def messaging_identity(self) -> Identity:
        """Return the immutable durable run identity."""

        identity = self._identity
        if identity is None:
            raise TinkerFinLifecycleError("a native stream requires an Identity")
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
            private_state_keys=frozenset(),
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
        private_state_keys: frozenset[str] = frozenset(),
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
            private_state_keys=private_state_keys,
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
        return await _runtime_agui.__anext__(
            self,
        )

    async def abort(self) -> list[BaseEvent]:
        """Cancel the active conversion and return one observed cancelled tail.

        Returns:
            The observed cancellation tail, or an empty list after a terminal state
            or prior abort delivery.

        Raises:
            RuntimeError: Called recursively from this stream's event observer before
                the main run reaches a terminal state.
        """

        return await _runtime_agui.abort(
            self,
        )

    async def aclose(self) -> None:
        """Close this stream and its upstream parts idempotently.

        Closure requested from an event-observer call chain preserves delivery of
        the event currently being observed. An external closer cancels an active
        pull before waiting for the shared cleanup.
        """

        return await _runtime_agui.aclose(
            self,
        )

    def to_sse(
        self,
        *,
        mapper: SseMapper[BaseEvent] | None = None,
        event_id_resolver: SseEventIdResolver[BaseEvent] | None = None,
    ) -> SseBody[str]:
        """Consume this AG-UI object stream as safely framed SSE text."""

        return _runtime_agui.to_sse(
            self,
            mapper=mapper,
            event_id_resolver=event_id_resolver,
        )

    async def _close(
        self,
        primary: BaseException | None,
        *,
        active: asyncio.Task[object] | None = None,
    ) -> None:
        return await _runtime_agui._close(
            self,
            primary,
            active=active,
        )

    @staticmethod
    def _close_finished(task: asyncio.Task[None]) -> None:
        """Consume a retained close failure even when no caller waits again."""

        return _runtime_agui._close_finished(
            task,
        )

    async def _close_once(self, active: asyncio.Task[object] | None) -> None:
        return await _runtime_agui._close_once(
            self,
            active,
        )

    async def _observe(self, event: BaseEvent) -> None:
        return await _runtime_agui._observe(
            self,
            event,
        )

    async def _close_upstream(self, primary: BaseException | None) -> None:
        return await _runtime_agui._close_upstream(
            self,
            primary,
        )

    def _record_secondary_error_note(self, note: str) -> None:
        """Retain cleanup evidence across the micro-batch cancellation boundary."""

        return _runtime_agui._record_secondary_error_note(
            self,
            note,
        )

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
        except Exception as error:
            self.error = error
            primary = error
            error_code = (
                "stream_timeout"
                if isinstance(error, _AgUiStreamDeadlineExceeded)
                else self._runtime_error_code
            )
            # The public terminal stays client-safe; trusted host logs retain the
            # causal exception before cleanup can add secondary failure notes.
            logger.error(
                "AG-UI runtime conversion failed",
                extra={
                    "thread_id": self._identity.thread_id,
                    "run_id": self._identity.run_id,
                    "error_code": error_code,
                    "error_type": type(error).__name__,
                },
                exc_info=(type(error), error, error.__traceback__),
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

        return _runtime_agui._decorate_initialization_event(
            self,
            event,
        )

    async def _next_part(self) -> object:
        return await _runtime_agui._next_part(
            self,
        )


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
            raise TinkerFinLifecycleError(
                "a TinkerFin run can create only one object stream"
            )
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
        private_state_keys: frozenset[str] = frozenset(),
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
            private_state_keys: Top-level state channels omitted from public output.
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
            private_state_keys=private_state_keys,
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
    """Globally shareable factory for single-use source and Agent definitions."""

    __slots__ = ("_plan_options", "_run_coordinator", "_state_schema")

    create_deep_agent = CREATE_DEEP_AGENT

    def __init__(
        self,
        *,
        run_coordinator: RunCoordinator | None = None,
        state_schema: type[DeepAgentState] | None = None,
    ) -> None:
        if run_coordinator is not None and not callable(run_coordinator):
            raise TypeError("run_coordinator must be callable or None")
        validate_state_schema(state_schema, source="TinkerFin state_schema")
        self._run_coordinator = run_coordinator
        self._state_schema = state_schema
        self._plan_options: PlanOptions | None = None

    def plan(
        self,
        *,
        enabled: bool = True,
        default_mode: AgentMode = "default",
        gate_model: str | BaseChatModel | None = None,
        planner_model: str | BaseChatModel | None = None,
        clarification_schema: type[ClarificationFormBase] = DefaultClarificationForm,
    ) -> TinkerFin:
        """Return a factory with immutable Plan-capability options.

        The returned factory borrows the same coordinator and global state schema.
        Existing Definitions and this source factory are unchanged.

        Args:
            enabled: Whether subsequent Deep Agent Definitions support Plan runs.
            default_mode: Run mode used when ``new`` or ``new_agui`` omits one.
            gate_model: Optional model dedicated to conservative request routing.
            planner_model: Optional model dedicated to read-only Plan drafting.
            clarification_schema: Concrete host form used by Gate and Planner.

        Returns:
            A separate configured TinkerFin factory.

        Raises:
            TypeError: ``enabled`` or a model value has the wrong type.
            PlanModeConfigurationError: A mode or disabled configuration is invalid.
        """

        if type(enabled) is not bool:
            raise TypeError("enabled must be a bool")
        mode = validate_agent_mode(default_mode, name="default_mode")
        for name, model in (
            ("gate_model", gate_model),
            ("planner_model", planner_model),
        ):
            if model is not None and not isinstance(model, (str, BaseChatModel)):
                raise TypeError(
                    f"{name} must be a model string, BaseChatModel, or None"
                )
            if isinstance(model, str) and not model.strip():
                raise ValueError(f"{name} must not be blank")
        if not enabled and (
            mode != "default"
            or gate_model is not None
            or planner_model is not None
            or clarification_schema is not DefaultClarificationForm
        ):
            from .plan.errors import PlanModeConfigurationError

            raise PlanModeConfigurationError(
                "disabled Plan capability cannot configure a mode, model, or form"
            )
        configured = TinkerFin(
            run_coordinator=self._run_coordinator,
            state_schema=self._state_schema,
        )
        if enabled:
            configured._plan_options = PlanOptions(
                clarification=create_clarification_binding(clarification_schema),
                default_mode=mode,
                gate_model=gate_model,
                planner_model=planner_model,
            )
        return configured

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
            invocation = source_factory._bind_identity(identity)
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
        """Validate a request binding without opening Graph or source resources."""

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
