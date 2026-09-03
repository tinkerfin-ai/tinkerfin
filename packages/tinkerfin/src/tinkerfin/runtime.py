"""Request-scoped Deep Agents native and AG-UI streaming."""

from __future__ import annotations

import asyncio
import inspect
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
from typing import TYPE_CHECKING, Any, Generic, TypeAlias, TypeVar, cast

from deepagents.graph import DeepAgentState
from langchain.agents.middleware.types import InputAgentState
from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command

from tinkerfin_contracts import (
    RunIdentity,
    RunSourceContext,
    RunTerminalOutcome,
    RuntimeObserver,
)
from tinkerfin_native_stream import (
    NativeStreamContractError,
    NativeStreamFrame,
    NativeValuesStreamPart,
)

from . import _runtime_streams
from ._observation import RuntimeObservationHub, observer_tuple, source_context
from ._optional_dependencies import require_agui
from ._runtime_streams import _validate_timeout
from ._state_schema import validate_state_schema
from ._tasks import join_task
from .coordination import RunCoordinator
from .deep_agent import CREATE_DEEP_AGENT, DeepAgentDefinition
from .errors import (
    AgUiSettlementTimeoutError,
    TinkerFinLifecycleError,
    TinkerFinStreamProtocolError,
)
from .native import NativeStreamPart
from .native_driver import (
    NativeStreamDriver,
)
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
from .runtime_profile import DeepAgentsRuntimeProfile, DeepAgentsV2RuntimeProfile
from .sse import (
    SseBody,
    SseEventIdResolver,
    SseMapper,
    SsePayload,
    SsePreflight,
)

PartT = TypeVar("PartT")

if TYPE_CHECKING:
    from ag_ui.core import BaseEvent

    from .agui_resume import (
        AgUiResumeBinding,
        AgUiResumeCheckpointObserver,
        AgUiResumeInitializationFailureObserver,
        AgUiResumeRequest,
    )

    EventObserver: TypeAlias = Callable[[BaseEvent], Awaitable[None]]
else:
    # Runtime annotations must stay importable without the AG-UI extra. Static analysis
    # uses the exact BaseEvent callback above; only the runtime alias is protocol-neutral.
    EventObserver: TypeAlias = Callable[[object], Awaitable[None]]

PartObserver: TypeAlias = Callable[[PartT], Awaitable[None]]
NativeFrameResolver = Callable[[object], NativeStreamFrame]
_CheckpointSaver: TypeAlias = (
    BaseCheckpointSaver[int] | BaseCheckpointSaver[float] | BaseCheckpointSaver[str]
)
if TYPE_CHECKING:
    _DefinitionAny: TypeAlias = DeepAgentDefinition[Any]
    _AgentSource: TypeAlias = (
        _DefinitionAny
        | Callable[
            [],
            _DefinitionAny | Awaitable[_DefinitionAny],
        ]
    )
else:
    # The implementation class carries private Graph/Astream generics while the public
    # generated stub intentionally exposes only ContextT. Runtime annotations remain
    # inert here; static analysis sees the precise branch above.
    _DefinitionAny: TypeAlias = DeepAgentDefinition
    _AgentSource: TypeAlias = object


def _translate_agui_conversion_error(error: Exception) -> Exception:
    """Translate Adapter-owned failures before they escape the Runtime facade.

    Native validation already uses the Runtime's stable error family on normal Profile
    paths. This fallback protects direct or initialization streams that do not carry a
    frame resolver, and also keeps later Adapter correlation failures from leaking a
    dependency-specific exception through TinkerFin.
    """

    require_agui()
    from tinkerfin_agui_adapter import AgUiAdapterError

    if not isinstance(error, AgUiAdapterError):
        return error
    native_cause = error.cause
    if isinstance(native_cause, NativeStreamContractError):
        translated = _runtime_streams._native_contract_error(native_cause)
    else:
        translated = TinkerFinStreamProtocolError(
            "AG-UI conversion violates the current Runtime contract",
            context=error.context,
            diagnostic_context={"adapter_code": error.code.value},
            cause=error,
        )
    return translated.with_traceback(error.__traceback__)


class _GraphRunStream(Generic[PartT]):
    """Internal single-use stream with deterministic upstream cleanup."""

    def __init__(
        self,
        *,
        source_factory: Callable[[], AsyncIterator[PartT]],
        source_preflight: Callable[[], Awaitable[object]] | None,
        coordination_factory: (Callable[[], AbstractAsyncContextManager[None]] | None),
        on_part: PartObserver[PartT] | None,
        identity: RunIdentity,
        observation: RuntimeObservationHub,
        stream_driver: NativeStreamDriver,
        on_settle: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Initialize a lazy, single-use native stream.

        The stream owns the iterator returned by ``source_factory`` and its coordination
        scope. An optional retained preflight settles asynchronous source construction
        before the source iterator is opened. It never owns the observer or identity.
        External close cancels an active pull and retains cleanup until settlement.
        """

        self._source_factory = source_factory
        self._source_preflight = source_preflight
        self._coordination_factory = coordination_factory
        self._on_part = on_part
        self._identity = identity
        self._observation = observation
        self._stream_driver = stream_driver
        self._on_settle = on_settle
        self._native_frame: tuple[object, NativeStreamFrame] | None = None
        self._last_root_interrupt_ids: tuple[str, ...] = ()
        self._source: AsyncIterator[PartT] | None = None
        self._coordination: AbstractAsyncContextManager[None] | None = None
        self._started = False
        self._closed = False
        self._ready_error: Exception | None = None
        self._ready_error_delivered = False
        self._active_task: asyncio.Task[object] | None = None
        self._observer_lineage = ContextVar(
            f"tinkerfin_graph_part_observer_lineage_{id(self)}",
            default=False,
        )
        self._active_observers = 0
        self._finish_task: asyncio.Task[None] | None = None
        self.error: Exception | None = None

    def _take_frame(self, part: object) -> NativeStreamFrame:
        """Consume the one canonical sidecar belonging to a delivered raw part.

        The sidecar is single-use because AG-UI, Native SSE, and Messaging are
        alternative consumers of one stream. A missing or mismatched sidecar is a
        lifecycle violation; reparsing the raw object here would reintroduce hidden
        third-party profile knowledge outside the selected Driver.

        Raises:
            TinkerFinLifecycleError: The part was not just delivered by this stream or
                another consumer already claimed its canonical frame.
        """

        binding = self._native_frame
        self._native_frame = None
        if binding is None or binding[0] is not part:
            raise TinkerFinLifecycleError(
                "the canonical Native frame is unavailable for this delivered part"
            )
        return binding[1]

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

    async def _ready(self) -> None:
        """Open the managed Observation and source boundary without pulling output."""

        return await _runtime_streams.ready(self)

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

    async def _finish(
        self,
        error: BaseException | None,
        *,
        outcome: RunTerminalOutcome | None = None,
    ) -> None:
        return await _runtime_streams._finish(
            self,
            error,
            outcome=outcome,
        )

    async def _finish_once(
        self,
        error: BaseException | None,
        outcome: RunTerminalOutcome | None,
    ) -> None:
        return await _runtime_streams._finish_once(
            self,
            error,
            outcome,
        )


class NativeGraphRunStream(_GraphRunStream[Mapping[str, object]]):
    """Raw upstream stream with one Driver-owned canonical replay sidecar."""

    @property
    def messaging_identity(self) -> RunIdentity:
        """Return the immutable durable run identity."""

        return self._identity

    @property
    def messaging_codec_profile(self) -> str:
        """Return the canonical native persistence profile."""

        return "tinkerfin.native-stream"

    @property
    def messaging_source_type(self) -> type[Mapping[str, object]]:
        """Return the declared live LangGraph envelope class."""

        return Mapping

    @property
    def messaging_codec_input_type(self) -> type[NativeStreamPart]:
        """Return the Driver-owned finite model accepted by the Native codec."""

        return NativeStreamPart

    def messaging_codec_input(
        self,
        item: Mapping[str, object],
    ) -> NativeStreamPart:
        """Claim the exact canonical replay model paired with a live item.

        Messaging calls this structural hook after pulling ``item`` and before the
        next source pull. The method transfers the already normalized frame sidecar;
        it never validates or serializes the upstream LangGraph mapping again.

        Args:
            item: Raw object most recently yielded by this source.

        Returns:
            The immutable finite replay model produced by the selected Driver.

        Raises:
            TinkerFinLifecycleError: The item does not own the pending frame or the
                frame was already consumed.
        """

        return self._take_frame(item).replay

    @property
    def messaging_replay_type(self) -> type[NativeStreamPart]:
        """Return the decoded durable replay class."""

        return NativeStreamPart


class AgUiEventStream:
    """Own one observed AG-UI stream and its independently budgeted close task."""

    @property
    def messaging_cancel_waits_for_first_item(self) -> bool:
        """Declare that durable cancellation must not overtake ``RUN_STARTED``.

        Messaging integrations use this structural fact to keep the first public
        lifecycle event ahead of a cancellation tail. Advanced sources can still
        configure their own explicit first-item barrier.
        """

        return True

    @property
    def messaging_codec_profile(self) -> str:
        """Return the canonical AG-UI event persistence profile."""

        return "agui.event"

    @property
    def messaging_source_type(self) -> type[BaseEvent]:
        """Return the declared live AG-UI event base class."""

        require_agui()
        from ag_ui.core import BaseEvent

        return BaseEvent

    @property
    def messaging_replay_type(self) -> type[BaseEvent]:
        """Return the decoded durable event base class."""

        require_agui()
        from ag_ui.core import BaseEvent

        return BaseEvent

    @property
    def messaging_identity(self) -> RunIdentity:
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

    def __init__(
        self,
        *,
        parts: AsyncIterable[object],
        identity: RunIdentity,
        expose_reasoning_events: bool,
        expose_subagent_events: bool,
        prior_tool_call_ids: frozenset[str],
        private_state_keys: frozenset[str] = frozenset(),
        native_frame_resolver: NativeFrameResolver | None = None,
        timeout: float | None,
        settlement_timeout: float | None = None,
        on_event: EventObserver | None,
        parent_run_id: str | None = None,
    ) -> None:
        """Initialize one owned AG-UI conversion stream.

        ``parts`` is owned and closed exactly once. RunIdentity, parent lineage, and
        callbacks are borrowed. Timeouts bound caller waits without
        abandoning the retained upstream close task.

        Args:
            parts: Single-use Native source transferred to this stream.
            identity: Canonical public and Graph Run identity.
            expose_reasoning_events: Whether verified public reasoning is emitted.
            expose_subagent_events: Whether validated subagent events are emitted.
            prior_tool_call_ids: Scoped calls emitted before a resumed request.
            private_state_keys: Runtime-owned state channels omitted from AG-UI.
            native_frame_resolver: Optional single-normalization sidecar resolver.
            timeout: Optional total Native pull deadline in seconds.
            settlement_timeout: Optional caller wait for protected close settlement.
            on_event: Optional borrowed observer awaited before event delivery.
            parent_run_id: Optional branch or resume source in the same thread.

        Raises:
            TypeError: Identity, parent lineage, timeout, or callbacks are invalid.
            ModuleNotFoundError: The AG-UI extra is not installed.
        """

        require_agui()
        from tinkerfin_agui_adapter import (
            AgUiLifecycleEventFactory,
            DeepAgentAgUiAdapter,
            micro_batch,
        )

        from . import _runtime_agui

        self._runtime_agui = _runtime_agui
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
        self._start_parts = parts._ready if isinstance(parts, _GraphRunStream) else None
        self._upstream_closed = False
        self._adapter = DeepAgentAgUiAdapter(
            identity=identity,
            expose_reasoning_events=expose_reasoning_events,
            expose_subagent_events=expose_subagent_events,
            prior_tool_call_ids=prior_tool_call_ids,
            private_state_keys=private_state_keys,
        )
        self._native_frame_resolver = native_frame_resolver
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

        return await self._runtime_agui.__anext__(
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

        return await self._runtime_agui.abort(
            self,
        )

    async def aclose(self) -> None:
        """Close this stream and its upstream parts idempotently.

        Closure requested from an event-observer call chain preserves delivery of
        the event currently being observed. An external closer cancels an active
        pull before waiting for the shared cleanup.
        """

        return await self._runtime_agui.aclose(
            self,
        )

    async def _ready(self) -> None:
        """Open the managed Native boundary without consuming an AG-UI event."""

        start_parts = self._start_parts
        if start_parts is not None:
            await start_parts()

    def to_sse(
        self,
        *,
        mapper: SseMapper[BaseEvent] | None = None,
        event_id_resolver: SseEventIdResolver[BaseEvent] | None = None,
    ) -> SseBody[str]:
        """Consume this AG-UI object stream as safely framed SSE text."""

        return self._runtime_agui.to_sse(
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
        return await self._runtime_agui._close(
            self,
            primary,
            active=active,
        )

    def _close_finished(self, task: asyncio.Task[None]) -> None:
        """Consume a retained close failure even when no caller waits again."""

        return self._runtime_agui._close_finished(
            task,
        )

    async def _close_once(self, active: asyncio.Task[object] | None) -> None:
        return await self._runtime_agui._close_once(
            self,
            active,
        )

    async def _observe(self, event: BaseEvent) -> None:
        return await self._runtime_agui._observe(
            self,
            event,
        )

    async def _close_upstream(self, primary: BaseException | None) -> None:
        return await self._runtime_agui._close_upstream(
            self,
            primary,
        )

    def _record_secondary_error_note(self, note: str) -> None:
        """Retain cleanup evidence across the micro-batch cancellation boundary."""

        return self._runtime_agui._record_secondary_error_note(
            self,
            note,
        )

    async def _convert(self) -> AsyncIterator[BaseEvent]:
        primary: BaseException | None = None
        terminal = False
        initial_event = self._decorate_initialization_event(
            self._lifecycle.started(
                identity=self._identity,
                parent_run_id=self._parent_run_id,
            )
        )
        try:
            start_parts = self._start_parts
            if start_parts is not None:
                await start_parts()
            self._main_started = True
            yield initial_event
            while True:
                try:
                    part = await self._next_part()
                except StopAsyncIteration:
                    break
                if self._aborted:
                    return
                resolver = self._native_frame_resolver
                events = (
                    self._adapter.process(part)
                    if resolver is None
                    else self._adapter.process_frame(resolver(part))
                )
                for event in events:
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
        except Exception as error:  # noqa: BLE001 - Runtime owns terminal conversion
            error = _translate_agui_conversion_error(error)
            self.error = error
            primary = error
            error_code = (
                "stream_timeout"
                if isinstance(error, self._runtime_agui._AgUiStreamDeadlineExceeded)
                else self._runtime_error_code
            )
            if not self._main_started:
                self._main_started = True
                yield initial_event
            try:
                await self._close_upstream(error)
            except asyncio.CancelledError as cancellation:
                primary = cancellation
                raise
            for event in self._adapter.abort():
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

        return self._runtime_agui._decorate_initialization_event(
            self,
            event,
        )

    async def _next_part(self) -> object:
        return await self._runtime_agui._next_part(
            self,
        )


class TinkerFin:
    """Globally shareable factory for request-scoped Deep Agent definitions."""

    __slots__ = (
        "_checkpointer",
        "_family",
        "_observers",
        "_plan_options",
        "_run_coordinator",
        "_runtime_profile",
        "_state_schema",
    )

    create_deep_agent = CREATE_DEEP_AGENT

    def __init__(
        self,
        *,
        checkpointer: _CheckpointSaver | None = None,
        run_coordinator: RunCoordinator | None = None,
        state_schema: type[DeepAgentState] | None = None,
        runtime_profile: DeepAgentsRuntimeProfile | None = None,
    ) -> None:
        """Initialize a shareable factory that borrows global integrations.

        Args:
            checkpointer: Optional borrowed default saver inherited by Definitions that
                do not provide one explicitly. TinkerFin never sets up or closes it.
            run_coordinator: Optional exclusive scope provider for run identities.
            state_schema: Optional global Deep Agent TypedDict contribution.
            runtime_profile: Complete Deep Agents integration selected before any
                Definition or Run is created. The default is the locked v2 Profile.

        Raises:
            TypeError: The checkpointer, coordinator, or Runtime Profile has the wrong
                type.
            ValueError: The Runtime Profile ID is not canonical.
            StateSchemaCompositionError: The state schema is not a valid TypedDict.
        """

        if checkpointer is not None and not isinstance(
            checkpointer, BaseCheckpointSaver
        ):
            raise TypeError("checkpointer must be a BaseCheckpointSaver or None")
        if run_coordinator is not None and not callable(run_coordinator):
            raise TypeError("run_coordinator must be callable or None")
        if runtime_profile is not None and not isinstance(
            runtime_profile,
            DeepAgentsRuntimeProfile,
        ):
            raise TypeError(
                "runtime_profile must implement DeepAgentsRuntimeProfile or be None"
            )
        resolved_profile = runtime_profile or DeepAgentsV2RuntimeProfile()
        profile_id = resolved_profile.profile_id
        if (
            not isinstance(profile_id, str)
            or not profile_id
            or profile_id != profile_id.strip()
        ):
            raise ValueError("runtime_profile.profile_id must be canonical text")
        if not isinstance(resolved_profile.create_agent_signature, inspect.Signature):
            raise TypeError(
                "runtime_profile.create_agent_signature must be a Signature"
            )
        if not isinstance(resolved_profile.astream_signature, inspect.Signature):
            raise TypeError("runtime_profile.astream_signature must be a Signature")
        validate_state_schema(state_schema, source="TinkerFin state_schema")
        self._checkpointer = checkpointer
        self._family = object()
        self._run_coordinator = run_coordinator
        self._state_schema = state_schema
        self._runtime_profile = resolved_profile
        self._observers: tuple[RuntimeObserver, ...] = ()
        self._plan_options: PlanOptions | None = None

    @property
    def runtime_profile(self) -> DeepAgentsRuntimeProfile:
        """Return the immutable borrowed integration used by future Definitions."""

        return self._runtime_profile

    def observe(self, observer: RuntimeObserver) -> TinkerFin:
        """Return a factory with one additional ordered Runtime observer.

        The source factory, existing Definitions, and registered Observer objects are
        unchanged. The same Observer instance cannot be registered twice because that
        would duplicate one logical observation stream.

        Args:
            observer: Cross-package Observer that opens one session per Runtime Run.

        Returns:
            A separate configured TinkerFin factory.

        Raises:
            TypeError: The object does not implement ``RuntimeObserver``.
            ValueError: The same Observer instance is already registered.
        """

        observers = observer_tuple((*self._observers, observer))
        configured = TinkerFin(
            checkpointer=self._checkpointer,
            run_coordinator=self._run_coordinator,
            state_schema=self._state_schema,
            runtime_profile=self._runtime_profile,
        )
        configured._plan_options = self._plan_options
        configured._observers = observers
        configured._family = self._family
        return configured

    async def open_run(
        self,
        identity: RunIdentity,
        *,
        agent: _AgentSource,
        input: InputAgentState | Command[object] | None,
        mode: AgentMode | None = None,
        config: RunnableConfig | None = None,
        context: object | None = None,
        on_native_part: PartObserver[Mapping[str, object]] | None = None,
        **stream_options: object,
    ) -> NativeGraphRunStream:
        """Open one managed native Run without exposing Definition/Runtime ordering.

        ``agent`` may be a reusable Definition or a synchronous/asynchronous callable
        that creates one after host-specific model, Sandbox, or tool preparation. Graph
        construction uses the selected Profile's async boundary exactly once. The
        returned stream has already committed Run start and input observations but has
        not pulled model output. Setup exceptions become a managed native stream that
        records failed Observation and raises when consumed; process-control exceptions
        continue to propagate.

        Args:
            identity: Canonical identity shared by Runtime, Graph, Observation, and
                optional durable delivery.
            agent: Existing Definition or callable returning one from this configured
                TinkerFin family.
            input: Ordinary Deep Agent state, native LangGraph resume command, or ``None``.
            mode: Optional default or Plan route for new input.
            config: Optional LangGraph execution configuration.
            context: Optional context declared by the Definition.
            on_native_part: Optional async observer after validation and Runtime
                Observation, before delivery.
            **stream_options: Advanced Profile-supported Graph stream options.

        Returns:
            A single-use managed native object stream.

        Raises:
            TypeError: Identity, agent, callback, or Definition shape is invalid.
            ValueError: The Definition belongs to another configured TinkerFin family.
            BaseException: Caller cancellation or process control interrupts setup.
        """

        from .plan._state import PLAN_PRIVATE_STATE_KEYS

        self._validate_run_binding(identity=identity, on_part=on_native_part)
        if not isinstance(agent, DeepAgentDefinition) and not callable(agent):
            raise TypeError("agent must be a DeepAgentDefinition or callable")
        requested_mode = (
            self._plan_options.default_mode
            if mode is None and self._plan_options is not None
            else ("default" if mode is None else mode)
        )
        resolved_mode = validate_agent_mode(requested_mode, name="mode")
        try:
            definition = await self._resolve_agent(cast(_AgentSource, agent))
            if not definition._belongs_to(self._family):
                raise ValueError(
                    "agent must come from this configured TinkerFin family"
                )
            stream = await definition._open_native_run(
                identity=identity,
                input=input,
                mode=mode,
                config=config,
                context=context,
                on_part=on_native_part,
                stream_options=stream_options,
            )
            await stream._ready()
            return stream
        except Exception as error:  # noqa: BLE001 - Runtime owns failed Observation
            setup_error = error
            input_kind = (
                "resume"
                if isinstance(input, Command) and input.resume is not None
                else "ordinary"
            )
            source = source_context(
                identity=identity,
                runtime_profile=self._runtime_profile.profile_id,
                input_kind=input_kind,
                parent_run_id=None,
                mode=resolved_mode,
                graph_input=input,
                config={} if config is None else config,
                private_state_keys=frozenset(
                    {"_tinkerfin_lineage", "_tinkerfin_resume"}
                )
                | PLAN_PRIVATE_STATE_KEYS,
            )
            observation = self._observation_hub(source)

            async def failed_source() -> AsyncIterator[Mapping[str, object]]:
                if False:  # pragma: no cover - establish async iterator shape
                    yield {}
                raise setup_error

            failed_stream = self._run_native(
                failed_source,
                identity=identity,
                on_part=on_native_part,
                observation=observation,
            )
            await failed_stream._ready()
            return failed_stream

    async def ainvoke(
        self,
        identity: RunIdentity,
        *,
        agent: _AgentSource,
        input: InputAgentState | Command[object] | None,
        mode: AgentMode | None = None,
        config: RunnableConfig | None = None,
        context: object | None = None,
        on_native_part: PartObserver[Mapping[str, object]] | None = None,
        **stream_options: object,
    ) -> Mapping[str, object]:
        """Run one managed invocation and return its final root state.

        This facade consumes the same Profile-owned canonical source as
        :meth:`open_run`, so Runtime observations, model and Tool callbacks,
        cancellation, and terminal settlement do not depend on whether the caller wants
        a stream or one final result.

        Args:
            identity: Canonical identity shared by Runtime, Graph, and Observation.
            agent: Existing Definition or lazy Definition factory from this family.
            input: Ordinary Deep Agent state, native resume command, or ``None``.
            mode: Optional default or Plan route.
            config: Optional LangGraph configuration.
            context: Optional context declared by the Definition.
            on_native_part: Optional observer after validation and Observation.
            **stream_options: Advanced options supported by the selected Profile.

        Returns:
            A defensive copy of the latest root values state. Interrupted output retains
            the current normalized ``__interrupt__`` list.

        Raises:
            TinkerFinLifecycleError: The managed source emits no root values boundary.
            BaseException: Profile binding, execution, observation, or cleanup fails.
        """

        stream = await self.open_run(
            identity,
            agent=agent,
            input=input,
            mode=mode,
            config=config,
            context=context,
            on_native_part=on_native_part,
            **stream_options,
        )
        state: dict[str, object] | None = None
        async for part in stream:
            canonical = self._runtime_profile.stream_driver.validate(part)
            if not isinstance(canonical, NativeValuesStreamPart) or canonical.ns != ():
                continue
            state = dict(canonical.data)
            if canonical.interrupts:
                state["__interrupt__"] = list(canonical.interrupts)
            else:
                state.pop("__interrupt__", None)
        if state is None:
            raise TinkerFinLifecycleError(
                "managed invocation completed without a root values boundary"
            )
        return state

    async def _resolve_agent(
        self,
        agent: _AgentSource,
    ) -> _DefinitionAny:
        """Resolve one existing or lazily created Definition exactly once."""

        if isinstance(agent, DeepAgentDefinition):
            return cast(_DefinitionAny, agent)
        resolved = agent()
        if inspect.isawaitable(resolved):
            resolved = await resolved
        if not isinstance(resolved, DeepAgentDefinition):
            raise TypeError("agent callable must return DeepAgentDefinition")
        return resolved

    async def open_agui_run(
        self,
        identity: RunIdentity,
        *,
        agent: _AgentSource,
        input: InputAgentState | None = None,
        resume: AgUiResumeRequest | None = None,
        parent_run_id: str | None = None,
        mode: AgentMode | None = None,
        config: RunnableConfig | None = None,
        context: object | None = None,
        on_resume_saved: AgUiResumeCheckpointObserver | None = None,
        on_resume_not_saved: AgUiResumeInitializationFailureObserver | None = None,
        resume_checkpointer: _CheckpointSaver | None = None,
        stream_timeout: float | None = None,
        cleanup_timeout: float | None = None,
        include_reasoning_events: bool = False,
        include_subagent_events: bool = True,
        on_native_part: PartObserver[Mapping[str, object]] | None = None,
        on_agui_event: EventObserver | None = None,
        **stream_options: object,
    ) -> AgUiEventStream:
        """Open one managed ordinary or resumed AG-UI Run through a single Graph.

        The facade owns asynchronous Agent preparation, Graph construction,
        authoritative checkpoint resolution, durable resume settlement, failed lifecycle
        conversion, and stream creation. Before returning, it commits Run start and input
        observations without consuming the first public event. Existing Definitions and
        low-level Runtime APIs remain available for advanced orchestration.

        Args:
            identity: Canonical identity shared by Runtime, Graph, Observation, AG-UI,
                checkpoint lineage, and optional durable delivery.
            agent: Existing Definition or synchronous/asynchronous callable returning one
                from this configured TinkerFin family.
            input: Ordinary Deep Agent input; mutually exclusive with ``resume``.
            resume: Untrusted AG-UI decisions resolved from the Definition checkpointer;
                mutually exclusive with ``input``.
            parent_run_id: Optional branch or interrupted source in the same thread.
            mode: Optional default or Plan route for new input.
            config: Optional LangGraph execution configuration.
            context: Optional context declared by the Definition.
            on_resume_saved: Optional idempotent callback after the exact marker is
                saver-readable and Trace Observation is forced.
            on_resume_not_saved: Optional idempotent settlement invoked only when no
                prepared or accepted marker exists.
            resume_checkpointer: Optional authoritative saver for a lazy resumed Agent
                that intentionally overrides this TinkerFin factory's default. The
                callable must return a Definition borrowing this exact object.
            stream_timeout: Optional total native pull deadline in seconds.
            cleanup_timeout: Optional caller wait for protected stream cleanup.
            include_reasoning_events: Whether verified public reasoning emits AG-UI
                reasoning events.
            include_subagent_events: Whether validated subagent events are emitted.
            on_native_part: Optional observer after native validation and Observation.
            on_agui_event: Optional observer before each public AG-UI event is delivered.
            **stream_options: Advanced Profile-supported Graph stream options.

        Returns:
            A single-use AG-UI event stream with one main lifecycle.

        Raises:
            TypeError: Identity, agent, request, or callback shape is invalid.
            ValueError: Input/resume selection or configured factory ownership is invalid.
            BaseException: Caller cancellation or process control interrupts setup.
        """

        require_agui()
        from .agui_resume import AgUiResumeRequest as AgUiResumeRequestType

        self._validate_run_binding(identity=identity, on_part=on_native_part)
        if not isinstance(agent, DeepAgentDefinition) and not callable(agent):
            raise TypeError("agent must be a DeepAgentDefinition or callable")
        if (input is None) == (resume is None):
            raise ValueError("exactly one of input or resume must be provided")
        if resume is not None and not isinstance(resume, AgUiResumeRequestType):
            raise TypeError("resume must be an AgUiResumeRequest or None")
        if resume_checkpointer is not None and not isinstance(
            resume_checkpointer,
            BaseCheckpointSaver,
        ):
            raise TypeError("resume_checkpointer must be a BaseCheckpointSaver or None")
        if resume_checkpointer is not None and resume is None:
            raise ValueError("resume_checkpointer requires a resume request")
        for name, callback in (
            ("on_resume_saved", on_resume_saved),
            ("on_resume_not_saved", on_resume_not_saved),
            ("on_agui_event", on_agui_event),
        ):
            if callback is not None and not callable(callback):
                raise TypeError(f"{name} must be an async callable or None")
        if (
            resume is not None
            and resume_checkpointer is None
            and self._checkpointer is None
            and not isinstance(agent, DeepAgentDefinition)
        ):
            raise ValueError(
                "resume with a lazy agent requires TinkerFin(checkpointer=...)"
            )
        lazy_agent = not isinstance(agent, DeepAgentDefinition)
        settlement_checkpointer = (
            None
            if resume is None
            else (
                (
                    resume_checkpointer
                    if resume_checkpointer is not None
                    else self._checkpointer
                )
                if lazy_agent
                else agent._resume_checkpointer()
            )
        )
        if (
            resume is not None
            and not lazy_agent
            and resume_checkpointer is not None
            and resume_checkpointer is not settlement_checkpointer
        ):
            raise ValueError(
                "resume_checkpointer must be the Definition's effective checkpointer"
            )
        settlement_authority_proven = settlement_checkpointer is not None
        requested_mode = (
            self._plan_options.default_mode
            if mode is None and self._plan_options is not None
            else ("default" if mode is None else mode)
        )
        resolved_mode = validate_agent_mode(requested_mode, name="mode")
        definition: _DefinitionAny | None = None
        try:
            definition = await self._resolve_agent(cast(_AgentSource, agent))
            if resume is not None and lazy_agent:
                effective_checkpointer = definition._resume_checkpointer()
                if effective_checkpointer is not settlement_checkpointer:
                    settlement_authority_proven = False
                    raise ValueError(
                        "lazy resume agent must use the declared resume_checkpointer"
                    )
            if not definition._belongs_to(self._family):
                raise ValueError(
                    "agent must come from this configured TinkerFin family"
                )
            stream = await definition._open_agui_run(
                identity=identity,
                input=input,
                resume_request=resume,
                parent_run_id=parent_run_id,
                mode=mode,
                config=config,
                context=context,
                on_resume_checkpointed=on_resume_saved,
                on_resume_initialization_failed=on_resume_not_saved,
                timeout=stream_timeout,
                settlement_timeout=cleanup_timeout,
                expose_reasoning_events=include_reasoning_events,
                expose_subagent_events=include_subagent_events,
                on_part=on_native_part,
                on_event=on_agui_event,
                stream_options=stream_options,
            )
            await stream._ready()
            return stream
        # Resume claim settlement covers every setup exit before a request Runtime can
        # install its own marker guard. Ordinary failures become one AG-UI error
        # lifecycle; cancellation and process control settle first and then propagate.
        except BaseException as error:
            if (
                resume is not None
                and on_resume_not_saved is not None
                and settlement_authority_proven
            ):
                try:
                    await self._settle_resume_not_saved(
                        identity=identity,
                        checkpointer=settlement_checkpointer,
                        callback=on_resume_not_saved,
                    )
                except BaseException as settlement_error:
                    if isinstance(error, Exception) and not isinstance(
                        settlement_error,
                        Exception,
                    ):
                        settlement_error.add_note(
                            f"AG-UI setup also failed: {type(error).__name__}: {error}"
                        )
                        raise settlement_error from error
                    error.add_note(
                        "resume not-saved settlement also failed: "
                        f"{type(settlement_error).__name__}: {settlement_error}"
                    )
            if not isinstance(error, Exception):
                raise
            failed_stream = self.failed_agui_run(
                error,
                identity=identity,
                parent_run_id=parent_run_id,
                mode=resolved_mode,
                input=input,
                config=config,
                resume_request=resume,
            )
            await failed_stream._ready()
            return failed_stream

    async def _settle_resume_not_saved(
        self,
        *,
        identity: RunIdentity,
        checkpointer: object | None,
        callback: AgUiResumeInitializationFailureObserver,
    ) -> None:
        """Probe and settle one pre-marker claim as a single owned operation."""

        from ._agui_lineage import agui_resume_marker_is_durable

        if checkpointer is None:
            raise TinkerFinLifecycleError(
                "resume marker state is unknowable without a concrete checkpointer"
            )

        async def settle() -> None:
            if await agui_resume_marker_is_durable(
                checkpointer,
                identity=identity,
                runtime_profile=self._runtime_profile,
            ):
                return
            await callback()

        task = asyncio.create_task(
            settle(),
            name="tinkerfin-resume-not-saved-settlement",
        )
        await join_task(task)

    def failed_agui_run(
        self,
        error: Exception,
        *,
        identity: RunIdentity,
        parent_run_id: str | None = None,
        mode: AgentMode = "default",
        input: object = None,
        config: object = None,
        resume: AgUiResumeBinding | None = None,
        resume_request: AgUiResumeRequest | None = None,
    ) -> AgUiEventStream:
        """Create one observed AG-UI lifecycle for a pre-Graph setup failure.

        Hosts call this boundary after accepting a semantic run but failing to build
        its model, Sandbox, Definition, or Graph. The Runtime records the real input,
        failed terminal, and close without requiring host code to invoke Observer
        methods directly. A framework-owned resume request preserves the real input
        kind when checkpoint resolution itself fails before a private binding exists.

        Args:
            error: Original setup failure retained as trusted causal evidence.
            identity: Canonical identity already accepted by the host.
            parent_run_id: Optional branch or resume lineage within the same thread.
            mode: Requested default or Plan route.
            input: Ordinary Graph input available before setup failed.
            config: Graph configuration available before setup failed.
            resume: Validated resume binding when the failed request was a resume.
            resume_request: Untrusted framework-owned resume intent when binding
                resolution failed. It is never converted into a native command or
                treated as validated checkpoint evidence.

        Returns:
            A single-use AG-UI stream with one standard initialization error terminal.

        Raises:
            TypeError: An argument has the wrong public type.
            ValueError: Parent lineage, mode, or resume arguments are invalid.
        """

        require_agui()
        from tinkerfin_agui_adapter import AgUiLifecycleEventFactory

        from .agui_resume import AgUiResumeBinding as AgUiResumeBindingType
        from .agui_resume import AgUiResumeRequest as AgUiResumeRequestType

        if not isinstance(error, Exception):
            raise TypeError("error must be an Exception")
        self._validate_run_binding(identity=identity, on_part=None)
        AgUiLifecycleEventFactory.validate_parent_run_id(
            parent_run_id,
            identity=identity,
        )
        resolved_mode = validate_agent_mode(mode, name="mode")
        if resume is not None and not isinstance(resume, AgUiResumeBindingType):
            raise TypeError("resume must be an AgUiResumeBinding or None")
        if resume_request is not None and not isinstance(
            resume_request,
            AgUiResumeRequestType,
        ):
            raise TypeError("resume_request must be an AgUiResumeRequest or None")
        if resume is not None and resume_request is not None:
            raise ValueError("resume and resume_request are mutually exclusive")
        input_kind = (
            resume.mode
            if resume is not None
            else (
                "resume"
                if resume_request is not None
                else ("branch" if parent_run_id is not None else "ordinary")
            )
        )
        source_input = (
            resume.model_dump(mode="json", by_alias=True)
            if resume is not None
            else (
                resume_request.model_dump(mode="json", by_alias=True)
                if resume_request is not None
                else input
            )
        )
        context = source_context(
            identity=identity,
            runtime_profile=self._runtime_profile.profile_id,
            input_kind=input_kind,
            parent_run_id=parent_run_id,
            mode=resolved_mode,
            graph_input=source_input,
            config={} if config is None else config,
            private_state_keys=frozenset(),
            resume=() if resume is None else resume._observation_summaries(),
        )
        observation = self._observation_hub(context)

        async def failed_parts() -> AsyncIterator[Mapping[str, object]]:
            if False:  # pragma: no cover - supplies the async iterator shape
                yield {}
            raise error

        stream = self._run_agui(
            failed_parts,
            identity=identity,
            parent_run_id=parent_run_id,
            on_part=None,
            timeout=None,
            settlement_timeout=None,
            expose_reasoning_events=False,
            expose_subagent_events=True,
            prior_tool_call_ids=frozenset(),
            private_state_keys=frozenset(),
            on_event=None,
            observation=observation,
        )
        stream._runtime_error_code = "runtime_initialization_error"
        stream._initialization_failed = True
        return stream

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
            checkpointer=self._checkpointer,
            run_coordinator=self._run_coordinator,
            state_schema=self._state_schema,
            runtime_profile=self._runtime_profile,
        )
        configured._observers = self._observers
        configured._family = self._family
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
        identity: RunIdentity | None,
        on_part: object | None,
    ) -> None:
        """Validate a request binding without opening Graph or source resources."""

        coordinator = self._run_coordinator
        if identity is not None and not isinstance(identity, RunIdentity):
            raise TypeError("identity must be a RunIdentity or None")
        if coordinator is not None and identity is None:
            raise ValueError("identity is required when run_coordinator is configured")
        if on_part is not None and not callable(on_part):
            raise TypeError("on_part must be an async callable or None")

    def _bind_native_invocation(
        self,
        signature: inspect.Signature,
        args: tuple[object, ...],
        options: Mapping[str, object],
        *,
        identity: RunIdentity,
    ) -> inspect.BoundArguments:
        """Delegate the complete upstream call contract to the selected Profile."""

        return self._runtime_profile.stream_driver.bind_invocation(
            signature,
            args,
            options,
            identity=identity,
            runtime_profile=self._runtime_profile.profile_id,
        )

    def _run_native(
        self,
        source_factory: Callable[[], AsyncIterator[Mapping[str, object]]],
        *,
        identity: RunIdentity,
        on_part: PartObserver[Mapping[str, object]] | None,
        observation: RuntimeObservationHub,
        on_settle: Callable[[], Awaitable[None]] | None = None,
        source_preflight: Callable[[], Awaitable[object]] | None = None,
    ) -> NativeGraphRunStream:
        """Create one canonical stream bound to the selected Runtime Profile."""

        self._validate_run_binding(identity=identity, on_part=on_part)
        coordinator = self._run_coordinator
        return NativeGraphRunStream(
            source_factory=source_factory,
            source_preflight=source_preflight,
            coordination_factory=(
                None if coordinator is None else lambda: coordinator(identity)
            ),
            identity=identity,
            on_part=on_part,
            observation=observation,
            stream_driver=self._runtime_profile.stream_driver,
            on_settle=on_settle,
        )

    def _run_agui(
        self,
        source_factory: Callable[[], AsyncIterator[Mapping[str, object]]],
        *,
        identity: RunIdentity,
        parent_run_id: str | None,
        on_part: PartObserver[Mapping[str, object]] | None,
        timeout: float | None,
        settlement_timeout: float | None,
        expose_reasoning_events: bool,
        expose_subagent_events: bool,
        prior_tool_call_ids: frozenset[str],
        private_state_keys: frozenset[str],
        on_event: EventObserver | None,
        observation: RuntimeObservationHub,
        on_settle: Callable[[], Awaitable[None]] | None = None,
        source_preflight: Callable[[], Awaitable[object]] | None = None,
    ) -> AgUiEventStream:
        """Convert one framework-bound native source into an AG-UI event stream."""

        if on_event is not None and not callable(on_event):
            raise TypeError("on_event must be an async callable or None")
        native = self._run_native(
            source_factory,
            identity=identity,
            on_part=on_part,
            observation=observation,
            on_settle=on_settle,
            source_preflight=source_preflight,
        )
        return AgUiEventStream(
            parts=native,
            identity=identity,
            timeout=timeout,
            settlement_timeout=settlement_timeout,
            expose_reasoning_events=expose_reasoning_events,
            expose_subagent_events=expose_subagent_events,
            prior_tool_call_ids=prior_tool_call_ids,
            private_state_keys=private_state_keys,
            on_event=on_event,
            parent_run_id=parent_run_id,
            native_frame_resolver=native._take_frame,
        )

    def _observation_hub(self, context: RunSourceContext) -> RuntimeObservationHub:
        """Create one lazy request-scoped fan-out over frozen Observer registration."""

        return RuntimeObservationHub(context=context, observers=self._observers)


__all__ = [
    "AgUiEventStream",
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
