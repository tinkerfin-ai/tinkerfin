"""Request-scoped Deep Agents native and AG-UI streaming."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import (
    AsyncIterable,
    AsyncIterator,
    Awaitable,
    Callable,
    Mapping,
    Sequence,
)
from contextlib import AbstractAsyncContextManager
from contextvars import ContextVar
from typing import Any, Generic, TypeAlias, TypeVar

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
from .coordination import RunCoordinator
from .deep_agent import CREATE_DEEP_AGENT
from .errors import (
    AgUiNativeStreamConfigurationError,
    AgUiSettlementTimeoutError,
)
from .native import NativeStreamPart
from .plan._clarification import create_clarification_binding
from .plan._config import (
    DEFAULT_ALLOWED_REVIEW_ACTIONS,
    AgentMode,
    PlanOptions,
    validate_agent_mode,
    validate_allowed_review_actions,
)
from .plan._content import create_plan_content_binding
from .plan._contracts import create_plan_contract_binding
from .plan.clarification import ClarificationFormBase, DefaultClarificationForm
from .plan.clarification_types import ClarificationType
from .plan.models import PlanContentModel, PlanReviewAction, StructuredPlanContent
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


class _GraphRunStream(Generic[PartT]):
    """Internal single-use stream with deterministic upstream cleanup."""

    def __init__(
        self,
        *,
        source_factory: Callable[[], AsyncIterator[PartT]],
        coordination_factory: (Callable[[], AbstractAsyncContextManager[None]] | None),
        on_part: PartObserver[PartT] | None,
        identity: Identity,
    ) -> None:
        """Initialize a lazy, single-use native stream.

        The stream owns the iterator returned by ``source_factory`` and its
        coordination scope, but it never owns the optional observer or identity.
        External close cancels an active pull and retains cleanup until settlement.
        """

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

    def __aiter__(self) -> _GraphRunStream[PartT]:
        """Return this single-use asynchronous iterator."""

        return self

    async def __anext__(self) -> PartT:
        """Pull, observe, and return the next native part."""

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


class NativeGraphRunStream(_GraphRunStream[Mapping[str, object]]):
    """Canonical LangGraph v2 stream with a complete durable codec profile."""

    @property
    def messaging_identity(self) -> Identity:
        """Return the immutable durable run identity."""

        return self._identity

    @property
    def messaging_codec_profile(self) -> str:
        """Return the canonical native persistence profile."""

        return "langgraph.stream-part.v2"

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

        return "agui.event"

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
        parent_run_id: str | None = None,
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
            parent_run_id=parent_run_id,
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
        parent_run_id: str | None = None,
    ) -> None:
        """Initialize one owned AG-UI conversion stream.

        ``parts`` is owned and closed exactly once. Identity, parent lineage, and
        callbacks are borrowed. Timeouts bound caller waits without
        abandoning the retained upstream close task.
        """

        self._lifecycle = AgUiLifecycleEventFactory()
        self._lifecycle.validate_identity(identity)
        self._lifecycle.validate_parent_run_id(parent_run_id, identity=identity)
        self._identity = identity
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
        self._resume_abandoned = False
        self.error: Exception | None = None

    def __aiter__(self) -> AgUiEventStream:
        """Return this single-use AG-UI asynchronous iterator."""

        return self

    async def __anext__(self) -> BaseEvent:
        """Return the next observed and lifecycle-safe AG-UI event."""

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
                self._lifecycle.started(
                    identity=self._identity,
                    parent_run_id=self._parent_run_id,
                )
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
            if self._resume_abandoned:
                self._completed = True
                terminal = True
                yield self._lifecycle.failed(
                    identity=self._identity,
                    message="Agent resume cancelled",
                    code="resume_cancelled",
                    parent_run_id=self._parent_run_id,
                )
                return
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
                        parent_run_id=self._parent_run_id,
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


class TinkerFin:
    """Globally shareable factory for request-scoped Deep Agent definitions."""

    __slots__ = ("_plan_options", "_run_coordinator", "_state_schema")

    create_deep_agent = CREATE_DEEP_AGENT

    def __init__(
        self,
        *,
        run_coordinator: RunCoordinator | None = None,
        state_schema: type[DeepAgentState] | None = None,
    ) -> None:
        """Initialize a shareable factory that borrows global integrations.

        Args:
            run_coordinator: Optional exclusive scope provider for run identities.
            state_schema: Optional global Deep Agent TypedDict contribution.

        Raises:
            TypeError: The coordinator is not callable.
            StateSchemaCompositionError: The state schema is not a valid TypedDict.
        """

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
        planner_model: str | BaseChatModel | None = None,
        clarification_schema: type[ClarificationFormBase] = DefaultClarificationForm,
        clarification_types: Sequence[ClarificationType[Any, Any]] = (),
        content_schema: type[PlanContentModel] = StructuredPlanContent,
        allowed_review_actions: Sequence[
            PlanReviewAction
        ] = DEFAULT_ALLOWED_REVIEW_ACTIONS,
    ) -> TinkerFin:
        """Return a factory with immutable Plan-capability options.

        The returned factory borrows the same coordinator and global state schema.
        Existing Definitions and this source factory are unchanged.

        Args:
            enabled: Whether subsequent Deep Agent Definitions support Plan runs.
            default_mode: Run mode used when ``new`` or ``new_agui`` omits one.
            planner_model: Optional model dedicated to read-only planning.
            clarification_schema: Concrete host form used by the Planner.
            clarification_types: Additional custom semantic question types.
            content_schema: Concrete content model used for drafts and confirmed Plans.
            allowed_review_actions: Ordered decisions accepted for each Plan draft
                review.

        Returns:
            A separate configured TinkerFin factory.

        Raises:
            TypeError: ``enabled``, a model, or a review action has the wrong type.
            PlanModeConfigurationError: A mode or disabled configuration is invalid.
        """

        if type(enabled) is not bool:
            raise TypeError("enabled must be a bool")
        mode = validate_agent_mode(default_mode, name="default_mode")
        actions = validate_allowed_review_actions(allowed_review_actions)
        if isinstance(clarification_types, (str, bytes)) or not isinstance(
            clarification_types, Sequence
        ):
            raise TypeError("clarification_types must be a sequence")
        frozen_clarification_types = tuple(clarification_types)
        for name, model in (("planner_model", planner_model),):
            if model is not None and not isinstance(model, (str, BaseChatModel)):
                raise TypeError(
                    f"{name} must be a model string, BaseChatModel, or None"
                )
            if isinstance(model, str) and not model.strip():
                raise ValueError(f"{name} must not be blank")
        if not enabled and (
            mode != "default"
            or planner_model is not None
            or clarification_schema is not DefaultClarificationForm
            or frozen_clarification_types
            or content_schema is not StructuredPlanContent
            or actions != DEFAULT_ALLOWED_REVIEW_ACTIONS
        ):
            from .plan.errors import PlanModeConfigurationError

            raise PlanModeConfigurationError(
                "disabled Plan capability cannot configure a mode, model, form, "
                "clarification types, content schema, or review actions"
            )
        configured = TinkerFin(
            run_coordinator=self._run_coordinator,
            state_schema=self._state_schema,
        )
        if enabled:
            clarification = create_clarification_binding(
                clarification_schema,
                custom_types=frozen_clarification_types,
            )
            content = create_plan_content_binding(content_schema)
            configured._plan_options = PlanOptions(
                clarification=clarification,
                content=content,
                contracts=create_plan_contract_binding(
                    clarification,
                    content,
                    allowed_review_actions=actions,
                ),
                allowed_review_actions=actions,
                default_mode=mode,
                planner_model=planner_model,
            )
        return configured

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
        on_part: PartObserver[Mapping[str, object]] | None,
    ) -> NativeGraphRunStream:
        """Create one validated canonical v2 stream for a request Runtime."""

        self._validate_run_binding(identity=identity, on_part=on_part)
        coordinator = self._run_coordinator
        return NativeGraphRunStream(
            source_factory=source_factory,
            coordination_factory=(
                None if coordinator is None else lambda: coordinator(identity)
            ),
            identity=identity,
            on_part=on_part,
        )

    def _run_agui(
        self,
        source_factory: Callable[[], AsyncIterator[Mapping[str, object]]],
        *,
        identity: Identity,
        parent_run_id: str | None,
        on_part: PartObserver[Mapping[str, object]] | None,
        timeout: float | None,
        settlement_timeout: float | None,
        expose_reasoning_events: bool,
        expose_subagent_events: bool,
        prior_tool_call_ids: frozenset[str],
        private_state_keys: frozenset[str],
        on_event: EventObserver | None,
    ) -> AgUiEventStream:
        """Convert one framework-bound native source into an AG-UI event stream."""

        if on_event is not None and not callable(on_event):
            raise TypeError("on_event must be an async callable or None")
        return AgUiEventStream(
            parts=self._run_native(
                source_factory,
                identity=identity,
                on_part=on_part,
            ),
            identity=identity,
            timeout=timeout,
            settlement_timeout=settlement_timeout,
            expose_reasoning_events=expose_reasoning_events,
            expose_subagent_events=expose_subagent_events,
            prior_tool_call_ids=prior_tool_call_ids,
            private_state_keys=private_state_keys,
            on_event=on_event,
            parent_run_id=parent_run_id,
        )


__all__ = [
    "AgUiEventStream",
    "AgUiNativeStreamConfigurationError",
    "AgUiSettlementTimeoutError",
    "EventObserver",
    "NativeGraphRunStream",
    "NativeStreamPart",
    "PartObserver",
    "SseBody",
    "SseEventIdResolver",
    "SseMapper",
    "SsePayload",
    "SsePreflight",
    "TinkerFin",
]
