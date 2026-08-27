"""Deep Agents graph definitions and request-scoped runtimes."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from functools import wraps
from typing import (
    TYPE_CHECKING,
    Any,
    Generic,
    ParamSpec,
    Protocol,
    TypeVar,
    cast,
    overload,
)

from deepagents import graph as _deepagents_graph
from deepagents.graph import DeepAgentState
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, StateSnapshot

from tinkerfin_agui_adapter import AgUiLifecycleEventFactory, Identity

from ._agui_lineage import bind_agui_lineage, verify_agui_resume_marker
from ._agui_lineage_state import LINEAGE_STATE_KEY, lineage_state_update
from ._hitl import prepare_hitl_factory_overrides
from ._state_schema import compose_deep_agent_base_schema
from ._tasks import join_task
from .agui_native import _bind_agui_graph_astream, _bind_graph_identity
from .agui_resume import (
    RESUME_MARKER_STATE_KEY,
    AgUiResumeBinding,
    AgUiResumeCheckpointObserver,
)
from .errors import TinkerFinLifecycleError
from .plan._config import (
    AgentMode,
    PlanOptions,
    resolve_agent_mode,
)

if TYPE_CHECKING:
    from .runtime import (
        AgUiEventStream,
        EventObserver,
        NativeGraphRunStream,
        PartObserver,
        TinkerFin,
    )

CreateP = ParamSpec("CreateP")
GraphT = TypeVar("GraphT")
AstreamT = TypeVar("AstreamT", bound=Callable[..., object])


class _GraphWithAstream(Protocol):
    astream: Callable[..., object]


class _PlanNativeGraph(Protocol):
    checkpointer: object

    def astream(
        self,
        *args: object,
        **kwargs: object,
    ) -> AsyncIterator[Mapping[str, object]]: ...

    async def aget_state(
        self,
        config: RunnableConfig,
        *,
        subgraphs: bool = False,
    ) -> StateSnapshot: ...

    async def aupdate_state(
        self,
        config: RunnableConfig,
        values: Mapping[str, object],
        as_node: str | None = None,
        task_id: str | None = None,
    ) -> RunnableConfig: ...


_native_create_deep_agent = cast(
    Callable[..., object],
    _deepagents_graph.create_deep_agent,  # pyright: ignore[reportUnknownMemberType]
)


def _graph_astream(graph: object) -> Callable[..., object]:
    """Read the callable stream boundary from one compiled upstream graph."""

    return cast(_GraphWithAstream, graph).astream


class _StreamClaim:
    __slots__ = ("_claimed",)

    def __init__(self) -> None:
        self._claimed = False

    def ensure_available(self) -> None:
        if self._claimed:
            raise TinkerFinLifecycleError(
                "a TinkerFin run can create only one object stream"
            )

    def claim(self) -> None:
        self.ensure_available()
        self._claimed = True


def _wrap_native_astream(
    astream: AstreamT,
    *,
    tinkerfin: TinkerFin,
    identity: Identity,
    on_part: PartObserver[Mapping[str, object]] | None,
) -> AstreamT:
    signature = inspect.signature(astream)
    claim = _StreamClaim()

    @wraps(astream)
    def wrapped(*args: object, **kwargs: object) -> NativeGraphRunStream:
        bound = _bind_graph_identity(
            signature,
            args,
            kwargs,
            identity=identity,
            require_v2=True,
        )
        claim.claim()

        def source() -> AsyncIterator[Mapping[str, object]]:
            return cast(
                AsyncIterator[Mapping[str, object]],
                astream(*bound.args, **bound.kwargs),
            )

        return tinkerfin._run_native(
            source,
            identity=identity,
            on_part=on_part,
        )

    return cast(AstreamT, wrapped)


def _create_graph_agui_stream(
    *,
    native_astream: Callable[..., AsyncIterator[Mapping[str, object]]],
    bound: inspect.BoundArguments,
    claim: _StreamClaim,
    tinkerfin: TinkerFin,
    identity: Identity,
    parent_run_id: str | None,
    on_part: PartObserver[Mapping[str, object]] | None,
    timeout: float | None,
    settlement_timeout: float | None,
    expose_reasoning_events: bool,
    expose_subagent_events: bool,
    private_state_keys: frozenset[str],
    resume: AgUiResumeBinding | None,
    on_resume_checkpointed: AgUiResumeCheckpointObserver | None,
    on_event: EventObserver | None,
) -> AgUiEventStream:
    """Create one lazy AG-UI stream from an already bound Graph call."""

    claim.ensure_available()
    if resume is not None:
        durability = bound.arguments.get("durability")
        if durability not in (None, "sync"):
            raise TinkerFinLifecycleError("AG-UI resume requires durability='sync'")
    _bind_agui_graph_astream(native_astream, *bound.args, **bound.kwargs)

    async def source() -> AsyncIterator[Mapping[str, object]]:
        raw_config = bound.arguments.get("config")
        if not isinstance(raw_config, Mapping):
            raise TypeError("bound Graph config must be a mapping")
        resolution = await bind_agui_lineage(
            native_astream,
            cast(RunnableConfig, raw_config),
            identity=identity,
            parent_run_id=parent_run_id,
            resume=resume,
        )
        bound.arguments["config"] = resolution.config
        lineage_update = lineage_state_update(
            identity=identity,
            parent_run_id=parent_run_id,
            role="native",
        )
        if resume is not None:
            bound.arguments["input"] = (
                None
                if resolution.resume_checkpointed
                else resume._invocation_command(
                    identity=identity,
                    parent_run_id=parent_run_id,
                    state_update=lineage_update,
                )
            )
            bound.arguments["durability"] = "sync"
            if resolution.resume_checkpointed:
                await verify_agui_resume_marker(
                    native_astream,
                    resolution.config,
                    identity=identity,
                    parent_run_id=parent_run_id,
                    resume=resume,
                )
                if on_resume_checkpointed is not None:
                    await on_resume_checkpointed(
                        resume._checkpoint(
                            identity=identity,
                            parent_run_id=parent_run_id,
                        )
                    )
        else:
            raw_input = bound.arguments.get("input")
            if not isinstance(raw_input, Mapping):
                raise TypeError("ordinary AG-UI Graph input must be a mapping")
            bound.arguments["input"] = {
                **cast(Mapping[str, object], raw_input),
                **lineage_update,
            }
        invocation = _bind_agui_graph_astream(
            native_astream,
            *bound.args,
            **bound.kwargs,
        )
        native = invocation()
        try:
            if resume is not None and not resolution.resume_checkpointed:
                try:
                    first = await anext(native)
                except StopAsyncIteration as error:
                    raise TinkerFinLifecycleError(
                        "resume ended before durable marker verification"
                    ) from error
                await verify_agui_resume_marker(
                    native_astream,
                    resolution.config,
                    identity=identity,
                    parent_run_id=parent_run_id,
                    resume=resume,
                )
                if on_resume_checkpointed is not None:
                    await on_resume_checkpointed(
                        resume._checkpoint(
                            identity=identity,
                            parent_run_id=parent_run_id,
                        )
                    )
                yield first
            async for part in native:
                yield part
        finally:
            close = getattr(native, "aclose", None)
            if callable(close):
                # The Graph iterator is owned by this request source. Settle its close
                # independently so cancellation of the consumer cannot leak it.
                async def close_native() -> None:
                    await cast(Callable[[], Awaitable[object]], close)()

                close_task = asyncio.create_task(
                    close_native(),
                    name="tinkerfin-deep-agent-native-close",
                )
                await join_task(close_task)

    stream = tinkerfin._run_agui(
        source,
        identity=identity,
        parent_run_id=parent_run_id,
        on_part=on_part,
        timeout=timeout,
        settlement_timeout=settlement_timeout,
        expose_reasoning_events=expose_reasoning_events,
        expose_subagent_events=expose_subagent_events,
        prior_tool_call_ids=(
            frozenset() if resume is None else frozenset(resume.prior_tool_call_ids)
        ),
        private_state_keys=private_state_keys,
        on_event=on_event,
    )
    claim.claim()
    return stream


def _wrap_agui_astream(
    astream: AstreamT,
    *,
    tinkerfin: TinkerFin,
    identity: Identity,
    parent_run_id: str | None,
    on_part: PartObserver[Mapping[str, object]] | None,
    timeout: float | None,
    settlement_timeout: float | None,
    expose_reasoning_events: bool,
    expose_subagent_events: bool,
    private_state_keys: frozenset[str],
    on_event: EventObserver | None,
) -> AstreamT:
    """Preserve the Graph input signature for an ordinary AG-UI run."""

    signature = inspect.signature(astream)
    claim = _StreamClaim()
    native_astream = cast(
        Callable[..., AsyncIterator[Mapping[str, object]]],
        astream,
    )

    @wraps(astream)
    def wrapped(*args: object, **kwargs: object) -> AgUiEventStream:
        graph_input = args[0] if args else kwargs.get("input")
        if graph_input is None or isinstance(graph_input, Command):
            raise ValueError(
                "ordinary AG-UI runs require Graph state input; use resume= for resume"
            )
        bound = _bind_graph_identity(
            signature,
            args,
            kwargs,
            identity=identity,
            require_v2=False,
        )
        return _create_graph_agui_stream(
            native_astream=native_astream,
            bound=bound,
            claim=claim,
            tinkerfin=tinkerfin,
            identity=identity,
            parent_run_id=parent_run_id,
            on_part=on_part,
            timeout=timeout,
            settlement_timeout=settlement_timeout,
            expose_reasoning_events=expose_reasoning_events,
            expose_subagent_events=expose_subagent_events,
            private_state_keys=private_state_keys,
            resume=None,
            on_resume_checkpointed=None,
            on_event=on_event,
        )

    return cast(AstreamT, wrapped)


def _resume_astream_signature(signature: inspect.Signature) -> inspect.Signature:
    """Remove Graph input and make config keyword-only for a resume Runtime."""

    parameters = list(signature.parameters.values())
    if parameters and parameters[0].name == "self":
        parameters.pop(0)
    if not parameters or parameters[0].name != "input":
        raise TypeError("Graph astream must expose its input parameter")
    parameters.pop(0)
    parameters = [
        (
            parameter.replace(kind=inspect.Parameter.KEYWORD_ONLY)
            if parameter.name == "config"
            else parameter
        )
        for parameter in parameters
    ]
    return signature.replace(parameters=parameters)


_COMPILED_ASTREAM = cast(
    Callable[..., object],
    cast(
        object,
        CompiledStateGraph.astream,  # pyright: ignore[reportUnknownMemberType]
    ),
)
_COMPILED_ASTREAM_SIGNATURE = inspect.signature(_COMPILED_ASTREAM)
_BOUND_COMPILED_ASTREAM_SIGNATURE = _COMPILED_ASTREAM_SIGNATURE.replace(
    parameters=tuple(_COMPILED_ASTREAM_SIGNATURE.parameters.values())[1:]
)


def _flatten_bound_options(
    bound: inspect.BoundArguments,
    signature: inspect.Signature,
) -> dict[str, object]:
    """Restore keyword options after validating the public resume signature."""

    options: dict[str, object] = {}
    for name, value in bound.arguments.items():
        parameter = signature.parameters[name]
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            options.update(cast(Mapping[str, object], value))
        else:
            options[name] = value
    return options


def _wrap_agui_resume_astream(
    astream: Callable[..., object] | None,
    *,
    tinkerfin: TinkerFin,
    identity: Identity,
    parent_run_id: str | None,
    on_part: PartObserver[Mapping[str, object]] | None,
    timeout: float | None,
    settlement_timeout: float | None,
    expose_reasoning_events: bool,
    expose_subagent_events: bool,
    private_state_keys: frozenset[str],
    resume: AgUiResumeBinding,
    on_resume_checkpointed: AgUiResumeCheckpointObserver | None,
    on_event: EventObserver | None,
) -> Callable[..., AgUiEventStream]:
    """Bind native resume input internally and expose only Graph options."""

    native_signature = (
        _BOUND_COMPILED_ASTREAM_SIGNATURE
        if astream is None
        else inspect.signature(astream)
    )
    public_signature = _resume_astream_signature(native_signature)
    claim = _StreamClaim()

    def wrapped(*args: object, **kwargs: object) -> AgUiEventStream:
        public_bound = public_signature.bind(*args, **kwargs)
        options = _flatten_bound_options(public_bound, public_signature)
        bound = _bind_graph_identity(
            native_signature,
            (None,),
            options,
            identity=identity,
            require_v2=False,
        )
        if resume.mode == "abandon":
            claim.ensure_available()

            def validation_only_astream(
                *_args: object,
                **_options: object,
            ) -> AsyncIterator[Mapping[str, object]]:
                raise AssertionError("validation-only astream must not be invoked")

            _bind_agui_graph_astream(
                validation_only_astream,
                *bound.args,
                **bound.kwargs,
            )

            async def empty_source() -> AsyncIterator[Mapping[str, object]]:
                if False:  # pragma: no cover - supplies the async iterator shape
                    yield {}

            stream = tinkerfin._run_agui(
                empty_source,
                identity=identity,
                parent_run_id=parent_run_id,
                on_part=on_part,
                timeout=timeout,
                settlement_timeout=settlement_timeout,
                expose_reasoning_events=expose_reasoning_events,
                expose_subagent_events=expose_subagent_events,
                prior_tool_call_ids=frozenset(),
                private_state_keys=private_state_keys,
                on_event=on_event,
            )
            stream._resume_abandoned = True
            claim.claim()
            return stream
        if astream is None:  # pragma: no cover - guarded by Definition construction
            raise RuntimeError("resume Runtime has no Graph stream")
        return _create_graph_agui_stream(
            native_astream=cast(
                Callable[..., AsyncIterator[Mapping[str, object]]],
                astream,
            ),
            bound=bound,
            claim=claim,
            tinkerfin=tinkerfin,
            identity=identity,
            parent_run_id=parent_run_id,
            on_part=on_part,
            timeout=timeout,
            settlement_timeout=settlement_timeout,
            expose_reasoning_events=expose_reasoning_events,
            expose_subagent_events=expose_subagent_events,
            private_state_keys=private_state_keys,
            resume=resume,
            on_resume_checkpointed=on_resume_checkpointed,
            on_event=on_event,
        )

    if astream is not None:
        wraps(astream)(wrapped)
    setattr(wrapped, "__signature__", public_signature)
    return wrapped


class DeepAgentRuntime(Generic[AstreamT]):
    """Single-request Runtime preserving the native LangGraph object stream."""

    __slots__ = ("astream",)

    def __init__(
        self,
        *,
        astream: AstreamT,
        tinkerfin: TinkerFin,
        identity: Identity,
        on_part: PartObserver[Mapping[str, object]] | None,
    ) -> None:
        """Bind a fresh native graph stream to one canonical identity."""

        self.astream = _wrap_native_astream(
            astream,
            tinkerfin=tinkerfin,
            identity=identity,
            on_part=on_part,
        )


class DeepAgentAgUiRuntime(Generic[AstreamT]):
    """Single-request AG-UI Runtime requiring one explicit Graph input."""

    __slots__ = ("astream",)

    def __init__(
        self,
        *,
        astream: AstreamT,
        tinkerfin: TinkerFin,
        identity: Identity,
        parent_run_id: str | None,
        on_part: PartObserver[Mapping[str, object]] | None,
        timeout: float | None,
        settlement_timeout: float | None,
        expose_reasoning_events: bool,
        expose_subagent_events: bool,
        private_state_keys: frozenset[str],
        on_event: EventObserver | None,
    ) -> None:
        """Bind a fresh graph to one canonical ordinary AG-UI request."""

        self.astream = _wrap_agui_astream(
            astream,
            tinkerfin=tinkerfin,
            identity=identity,
            parent_run_id=parent_run_id,
            on_part=on_part,
            timeout=timeout,
            settlement_timeout=settlement_timeout,
            expose_reasoning_events=expose_reasoning_events,
            expose_subagent_events=expose_subagent_events,
            private_state_keys=private_state_keys,
            on_event=on_event,
        )


class DeepAgentAgUiResumeRuntime(Generic[AstreamT]):
    """Single-request AG-UI Runtime whose native resume input is framework-owned."""

    __slots__ = ("astream",)

    def __init__(
        self,
        *,
        astream: AstreamT | None,
        tinkerfin: TinkerFin,
        identity: Identity,
        parent_run_id: str | None,
        on_part: PartObserver[Mapping[str, object]] | None,
        timeout: float | None,
        settlement_timeout: float | None,
        expose_reasoning_events: bool,
        expose_subagent_events: bool,
        private_state_keys: frozenset[str],
        resume: AgUiResumeBinding,
        on_resume_checkpointed: AgUiResumeCheckpointObserver | None,
        on_event: EventObserver | None,
    ) -> None:
        """Bind one validated resume without exposing its native Command."""

        self.astream = _wrap_agui_resume_astream(
            astream,
            tinkerfin=tinkerfin,
            identity=identity,
            parent_run_id=parent_run_id,
            on_part=on_part,
            timeout=timeout,
            settlement_timeout=settlement_timeout,
            expose_reasoning_events=expose_reasoning_events,
            expose_subagent_events=expose_subagent_events,
            private_state_keys=private_state_keys,
            resume=resume,
            on_resume_checkpointed=on_resume_checkpointed,
            on_event=on_event,
        )


class DeepAgentDefinition(Generic[GraphT, AstreamT]):
    """Store one graph build call and create a fresh Graph for every Runtime."""

    __slots__ = (
        "_args",
        "_factory",
        "_get_astream",
        "_kwargs",
        "_plan_factory",
        "_plan_options",
        "_private_state_keys",
        "_tinkerfin",
        "_uncontracted_external_subagents",
    )

    def __init__(
        self,
        *,
        tinkerfin: TinkerFin,
        factory: Callable[..., GraphT],
        args: tuple[object, ...],
        kwargs: dict[str, object],
        get_astream: Callable[[GraphT], AstreamT],
        plan_factory: Callable[..., object] | None,
        plan_options: PlanOptions | None,
        private_state_keys: frozenset[str],
        uncontracted_external_subagents: frozenset[str],
    ) -> None:
        """Freeze graph construction inputs while borrowing shared resources.

        The Definition opens no graph or external resource. Each ``new`` or
        ``new_agui`` call builds a fresh graph while reusing caller-owned model,
        checkpointer, Store, backend, and coordinator objects captured here.
        """

        self._tinkerfin = tinkerfin
        self._factory = factory
        self._args = args
        self._kwargs = kwargs
        self._get_astream = get_astream
        self._plan_factory = plan_factory
        self._plan_options = plan_options
        self._private_state_keys = private_state_keys
        self._uncontracted_external_subagents = uncontracted_external_subagents

    def _build_astream(self, mode: AgentMode) -> AstreamT:
        """Build the native Graph and add only request-local Plan routing."""

        native = self._factory(*self._args, **self._kwargs)
        native_astream = self._get_astream(native)
        plan_factory = self._plan_factory
        if plan_factory is None:
            return native_astream
        plan_options = self._plan_options
        if plan_options is None:
            raise RuntimeError("Plan factory requires frozen Plan options")

        from .plan._runtime import PlanCapableGraphRuntime
        from .plan._workflow import PlanningWorkflowGraph

        runtime = PlanCapableGraphRuntime(
            options=plan_options,
            native=cast(_PlanNativeGraph, native),
            planning_factory=lambda: cast(
                PlanningWorkflowGraph[Any],
                plan_factory(*self._args, **self._kwargs),
            ),
            prefer_plan=mode == "plan",
        )
        return cast(AstreamT, runtime.astream)

    def new(
        self,
        *,
        identity: Identity,
        mode: AgentMode | None = None,
        on_part: PartObserver[Mapping[str, object]] | None = None,
    ) -> DeepAgentRuntime[AstreamT]:
        """Create a fresh Graph bound to one native object-stream request."""

        self._tinkerfin._validate_run_binding(
            identity=identity,
            on_part=on_part,
        )
        resolved_mode = resolve_agent_mode(mode, options=self._plan_options)
        return DeepAgentRuntime(
            astream=self._build_astream(resolved_mode),
            tinkerfin=self._tinkerfin,
            identity=identity,
            on_part=on_part,
        )

    def new_agui(
        self,
        *,
        identity: Identity,
        parent_run_id: str | None = None,
        mode: AgentMode | None = None,
        on_part: PartObserver[Mapping[str, object]] | None = None,
        timeout: float | None = None,
        settlement_timeout: float | None = None,
        expose_reasoning_events: bool = False,
        expose_subagent_events: bool = True,
        resume: AgUiResumeBinding | None = None,
        on_resume_checkpointed: AgUiResumeCheckpointObserver | None = None,
        on_event: EventObserver | None = None,
    ) -> DeepAgentAgUiRuntime[AstreamT] | DeepAgentAgUiResumeRuntime[AstreamT]:
        """Create one canonical ordinary or resume AG-UI Runtime.

        Args:
            identity: Canonical identity shared by lifecycle, Graph, checkpoint, and
                durable delivery.
            parent_run_id: Optional branch or resume source within the same thread.
            mode: Optional request-local default or Plan route.
            on_part: Optional observer for validated native stream parts.
            timeout: Optional total native-part pull deadline in seconds.
            settlement_timeout: Optional close-settlement wait per caller.
            expose_reasoning_events: Whether verified public reasoning emits events.
            expose_subagent_events: Whether validated subgraph events are emitted.
            resume: Optional binding built from complete trusted AG-UI resume facts.
            on_resume_checkpointed: Optional idempotent callback invoked after the exact
                resume marker is readable and before continuation output.
            on_event: Optional observer awaited before each public event is delivered.

        Returns:
            An ordinary Runtime requiring Graph input, or a resume Runtime whose input
            is already owned by the framework.

        Raises:
            TypeError: An identity, parent, binding, or callback has the wrong type.
            ValueError: Parent lineage or callback/binding combination is invalid.
            TinkerFinLifecycleError: Mixed cancellation reaches an unsupported external
                subagent contract.
        """

        self._tinkerfin._validate_run_binding(
            identity=identity,
            on_part=on_part,
        )
        AgUiLifecycleEventFactory.validate_parent_run_id(
            parent_run_id,
            identity=identity,
        )
        if resume is not None and not isinstance(resume, AgUiResumeBinding):
            raise TypeError("resume must be an AgUiResumeBinding or None")
        if on_resume_checkpointed is not None and not callable(on_resume_checkpointed):
            raise TypeError("on_resume_checkpointed must be an async callable or None")
        if (
            resume is not None
            and resume.mode == "resume"
            and resume.contains_cancellations
        ):
            unsupported = (
                set(resume.source_agent_names) & self._uncontracted_external_subagents
            )
            if resume.unidentified_external_source or unsupported:
                raise TinkerFinLifecycleError(
                    "mixed Tool cancellation reached an external subagent without "
                    "the TinkerFin HITL contract",
                    context={"unsupported_subagents": ",".join(sorted(unsupported))},
                )
        if resume is None and on_resume_checkpointed is not None:
            raise ValueError("on_resume_checkpointed requires an AgUiResumeBinding")
        resolved_mode = resolve_agent_mode(mode, options=self._plan_options)
        if resume is not None:
            return DeepAgentAgUiResumeRuntime(
                astream=(
                    None
                    if resume.mode == "abandon"
                    else self._build_astream(resolved_mode)
                ),
                tinkerfin=self._tinkerfin,
                identity=identity,
                parent_run_id=parent_run_id,
                on_part=on_part,
                timeout=timeout,
                settlement_timeout=settlement_timeout,
                expose_reasoning_events=expose_reasoning_events,
                expose_subagent_events=expose_subagent_events,
                private_state_keys=self._private_state_keys,
                resume=resume,
                on_resume_checkpointed=on_resume_checkpointed,
                on_event=on_event,
            )
        return DeepAgentAgUiRuntime(
            astream=self._build_astream(resolved_mode),
            tinkerfin=self._tinkerfin,
            identity=identity,
            parent_run_id=parent_run_id,
            on_part=on_part,
            timeout=timeout,
            settlement_timeout=settlement_timeout,
            expose_reasoning_events=expose_reasoning_events,
            expose_subagent_events=expose_subagent_events,
            private_state_keys=self._private_state_keys,
            on_event=on_event,
        )


class _EnhancedDeepAgentFactory(Generic[CreateP, GraphT, AstreamT]):
    """Preserve upstream factory types and its bound runtime signature."""

    __slots__ = ("_factory", "_get_astream", "_signature")

    def __init__(
        self,
        factory: Callable[CreateP, GraphT],
        get_astream: Callable[[GraphT], AstreamT],
    ) -> None:
        self._factory = factory
        self._get_astream = get_astream
        self._signature = inspect.signature(factory)

    @overload
    def __get__(
        self,
        instance: None,
        owner: type[TinkerFin],
    ) -> _EnhancedDeepAgentFactory[CreateP, GraphT, AstreamT]: ...

    @overload
    def __get__(
        self,
        instance: TinkerFin,
        owner: type[TinkerFin],
    ) -> Callable[
        CreateP,
        DeepAgentDefinition[GraphT, AstreamT],
    ]: ...

    def __get__(
        self,
        instance: TinkerFin | None,
        owner: type[TinkerFin],
    ) -> object:
        if instance is None:
            return self

        @wraps(self._factory)
        def create(
            *args: CreateP.args,
            **kwargs: CreateP.kwargs,
        ) -> DeepAgentDefinition[GraphT, AstreamT]:
            bound = self._signature.bind(*args, **kwargs)
            bound.apply_defaults()
            definition_kwargs = dict(kwargs)
            hitl = prepare_hitl_factory_overrides(
                cast(Mapping[str, object], bound.arguments)
            )
            definition_kwargs.update(
                {
                    "interrupt_on": hitl.interrupt_on,
                    "middleware": hitl.middleware,
                    "permissions": hitl.permissions,
                    "subagents": hitl.subagents,
                }
            )
            definition_state = cast(
                type[DeepAgentState] | None,
                bound.arguments.get("state_schema"),
            )
            composed_state = compose_deep_agent_base_schema(
                instance._state_schema,
                definition_state,
            )
            if composed_state is not None:
                definition_kwargs["state_schema"] = composed_state
            factory = cast(Callable[..., GraphT], _native_create_deep_agent)
            plan_factory: Callable[..., object] | None = None
            private_state_keys = frozenset({LINEAGE_STATE_KEY, RESUME_MARKER_STATE_KEY})
            if instance._plan_options is not None:
                from .plan._state import PLAN_PRIVATE_STATE_KEYS
                from .plan._workflow import prepare_plan_factory

                plan_factory = prepare_plan_factory(
                    self._signature,
                    instance._plan_options,
                )
                private_state_keys |= PLAN_PRIVATE_STATE_KEYS
            return DeepAgentDefinition(
                tinkerfin=instance,
                factory=factory,
                args=cast(tuple[object, ...], args),
                kwargs=definition_kwargs,
                get_astream=self._get_astream,
                plan_factory=plan_factory,
                plan_options=instance._plan_options,
                private_state_keys=private_state_keys,
                uncontracted_external_subagents=(hitl.uncontracted_external_subagents),
            )

        return create


CREATE_DEEP_AGENT = _EnhancedDeepAgentFactory(
    _native_create_deep_agent,
    _graph_astream,
)


__all__ = [
    "DeepAgentAgUiRuntime",
    "DeepAgentDefinition",
    "DeepAgentRuntime",
]
