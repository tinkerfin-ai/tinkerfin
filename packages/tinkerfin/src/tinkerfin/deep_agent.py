"""Deep Agents graph definitions and request-scoped runtimes."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Callable, Mapping
from functools import wraps
from typing import TYPE_CHECKING, Generic, ParamSpec, Protocol, TypeVar, cast, overload

from deepagents import graph as _deepagents_graph
from deepagents.graph import DeepAgentState

from tinkerfin_agui_adapter import Identity

from ._state_schema import compose_deep_agent_base_schema
from .agui_native import _bind_agui_graph_astream, _bind_graph_identity
from .agui_resume import AgUiResumeBinding
from .errors import TinkerFinLifecycleError
from .plan._config import (
    PLAN_MODE_CONFIG_KEY,
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


_native_create_deep_agent = cast(
    Callable[..., object],
    _deepagents_graph.create_deep_agent,  # pyright: ignore[reportUnknownMemberType]
)


def _graph_astream(graph: object) -> Callable[..., object]:
    """Read the callable stream boundary from one compiled upstream graph."""

    return cast(_GraphWithAstream, graph).astream


def _bind_graph_mode(
    bound: inspect.BoundArguments,
    *,
    mode: AgentMode,
) -> inspect.BoundArguments:
    """Bind one immutable run mode to the Graph's reserved configurable key."""

    raw_config = bound.arguments.get("config")
    if raw_config is None:
        config: dict[str, object] = {}
    elif isinstance(raw_config, Mapping):
        config = dict(cast(Mapping[str, object], raw_config))
    else:
        raise TypeError("config must be a mapping or None")
    raw_configurable = config.get("configurable")
    if raw_configurable is None:
        configurable: dict[str, object] = {}
    elif isinstance(raw_configurable, Mapping):
        configurable = dict(cast(Mapping[str, object], raw_configurable))
    else:
        raise TypeError("config.configurable must be a mapping")
    if PLAN_MODE_CONFIG_KEY in configurable:
        raise ValueError(
            f"config.configurable reserves {PLAN_MODE_CONFIG_KEY!r} for TinkerFin"
        )
    configurable[PLAN_MODE_CONFIG_KEY] = mode
    config["configurable"] = configurable
    bound.arguments["config"] = config
    return bound


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
    mode: AgentMode,
    on_part: PartObserver[object] | None,
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
        bound = _bind_graph_mode(bound, mode=mode)
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
        ).astream()

    return cast(AstreamT, wrapped)


def _wrap_agui_astream(
    astream: AstreamT,
    *,
    tinkerfin: TinkerFin,
    identity: Identity,
    mode: AgentMode,
    on_part: PartObserver[Mapping[str, object]] | None,
    timeout: float | None,
    settlement_timeout: float | None,
    expose_reasoning_events: bool,
    expose_subagent_events: bool,
    resume: AgUiResumeBinding | None,
    on_event: EventObserver | None,
) -> AstreamT:
    signature = inspect.signature(astream)
    claim = _StreamClaim()
    native_astream = cast(
        Callable[..., AsyncIterator[Mapping[str, object]]],
        astream,
    )

    @wraps(astream)
    def wrapped(*args: object, **kwargs: object) -> AgUiEventStream:
        bound = _bind_graph_identity(
            signature,
            args,
            kwargs,
            identity=identity,
            require_v2=False,
        )
        bound = _bind_graph_mode(bound, mode=mode)
        claim.ensure_available()
        if resume is not None:
            graph_input = args[0] if args else kwargs.get("input")
            resume.validate_command(graph_input)
        invocation = _bind_agui_graph_astream(
            native_astream,
            *bound.args,
            **bound.kwargs,
        )
        stream = tinkerfin.run(
            invocation,
            identity=identity,
            on_part=on_part,
        ).astream_agui(
            timeout=timeout,
            settlement_timeout=settlement_timeout,
            expose_reasoning_events=expose_reasoning_events,
            expose_subagent_events=expose_subagent_events,
            prior_tool_call_ids=(
                frozenset() if resume is None else resume.prior_tool_call_ids
            ),
            on_event=on_event,
        )
        claim.claim()
        return stream

    return cast(AstreamT, wrapped)


class DeepAgentRuntime(Generic[AstreamT]):
    """Single-request Runtime preserving the native LangGraph object stream."""

    __slots__ = ("astream",)

    def __init__(
        self,
        *,
        astream: AstreamT,
        tinkerfin: TinkerFin,
        identity: Identity,
        mode: AgentMode,
        on_part: PartObserver[object] | None,
    ) -> None:
        self.astream = _wrap_native_astream(
            astream,
            tinkerfin=tinkerfin,
            identity=identity,
            mode=mode,
            on_part=on_part,
        )


class DeepAgentAgUiRuntime(Generic[AstreamT]):
    """Single-request Runtime converting native LangGraph objects to AG-UI."""

    __slots__ = ("astream",)

    def __init__(
        self,
        *,
        astream: AstreamT,
        tinkerfin: TinkerFin,
        identity: Identity,
        mode: AgentMode,
        on_part: PartObserver[Mapping[str, object]] | None,
        timeout: float | None,
        settlement_timeout: float | None,
        expose_reasoning_events: bool,
        expose_subagent_events: bool,
        resume: AgUiResumeBinding | None,
        on_event: EventObserver | None,
    ) -> None:
        self.astream = _wrap_agui_astream(
            astream,
            tinkerfin=tinkerfin,
            identity=identity,
            mode=mode,
            on_part=on_part,
            timeout=timeout,
            settlement_timeout=settlement_timeout,
            expose_reasoning_events=expose_reasoning_events,
            expose_subagent_events=expose_subagent_events,
            resume=resume,
            on_event=on_event,
        )


class DeepAgentDefinition(Generic[GraphT, AstreamT]):
    """Store one graph build call and create a fresh Graph for every Runtime."""

    __slots__ = (
        "_args",
        "_factory",
        "_get_astream",
        "_kwargs",
        "_plan_options",
        "_tinkerfin",
    )

    def __init__(
        self,
        *,
        tinkerfin: TinkerFin,
        factory: Callable[..., GraphT],
        args: tuple[object, ...],
        kwargs: dict[str, object],
        get_astream: Callable[[GraphT], AstreamT],
        plan_options: PlanOptions | None,
    ) -> None:
        self._tinkerfin = tinkerfin
        self._factory = factory
        self._args = args
        self._kwargs = kwargs
        self._get_astream = get_astream
        self._plan_options = plan_options

    def new(
        self,
        *,
        identity: Identity,
        mode: AgentMode | None = None,
        on_part: PartObserver[object] | None = None,
    ) -> DeepAgentRuntime[AstreamT]:
        """Create a fresh Graph bound to one native object-stream request."""

        self._tinkerfin._validate_run_binding(
            identity=identity,
            on_part=on_part,
        )
        resolved_mode = resolve_agent_mode(mode, options=self._plan_options)
        graph = self._factory(*self._args, **self._kwargs)
        return DeepAgentRuntime(
            astream=self._get_astream(graph),
            tinkerfin=self._tinkerfin,
            identity=identity,
            mode=resolved_mode,
            on_part=on_part,
        )

    def new_agui(
        self,
        *,
        identity: Identity,
        mode: AgentMode | None = None,
        on_part: PartObserver[Mapping[str, object]] | None = None,
        timeout: float | None = None,
        settlement_timeout: float | None = None,
        expose_reasoning_events: bool = False,
        expose_subagent_events: bool = True,
        resume: AgUiResumeBinding | None = None,
        on_event: EventObserver | None = None,
    ) -> DeepAgentAgUiRuntime[AstreamT]:
        """Create a fresh Graph bound to one AG-UI object-stream request."""

        self._tinkerfin._validate_run_binding(
            identity=identity,
            on_part=on_part,
        )
        if resume is not None:
            resume.validate_identity(identity)
        resolved_mode = resolve_agent_mode(mode, options=self._plan_options)
        graph = self._factory(*self._args, **self._kwargs)
        return DeepAgentAgUiRuntime(
            astream=self._get_astream(graph),
            tinkerfin=self._tinkerfin,
            identity=identity,
            mode=resolved_mode,
            on_part=on_part,
            timeout=timeout,
            settlement_timeout=settlement_timeout,
            expose_reasoning_events=expose_reasoning_events,
            expose_subagent_events=expose_subagent_events,
            resume=resume,
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
            definition_kwargs = cast(dict[str, object], dict(kwargs))
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
            if instance._plan_options is not None:
                from .plan._workflow import prepare_plan_factory

                factory = cast(
                    Callable[..., GraphT],
                    prepare_plan_factory(
                        cast(Callable[..., object], _native_create_deep_agent),
                        self._signature,
                        bound,
                        instance._plan_options,
                    ),
                )
            return DeepAgentDefinition(
                tinkerfin=instance,
                factory=factory,
                args=cast(tuple[object, ...], args),
                kwargs=definition_kwargs,
                get_astream=self._get_astream,
                plan_options=instance._plan_options,
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
