"""Deep Agents 建图定义与请求级 Runtime"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Callable, Mapping
from functools import wraps
from typing import TYPE_CHECKING, Generic, ParamSpec, TypeVar, cast, overload

from ag_ui.core import RunAgentInput
from deepagents.graph import create_deep_agent as _native_create_deep_agent

from tinkerfin_agui_adapter import AgUiLifecycleEventFactory

from .agui_native import _bind_agui_graph_astream
from .agui_resume import AgUiResumeBinding

if TYPE_CHECKING:
    from .runtime import (
        AgUiEventStream,
        EventObserver,
        GraphRunStream,
        PartObserver,
        TinkerFin,
    )

CreateP = ParamSpec("CreateP")
GraphT = TypeVar("GraphT")
AstreamT = TypeVar("AstreamT", bound=Callable[..., object])
PrincipalT = TypeVar("PrincipalT")


class _StreamClaim:
    __slots__ = ("_claimed",)

    def __init__(self) -> None:
        self._claimed = False

    def ensure_available(self) -> None:
        if self._claimed:
            raise RuntimeError("a TinkerFin run can create only one object stream")

    def claim(self) -> None:
        self.ensure_available()
        self._claimed = True


def _wrap_native_astream(
    astream: AstreamT,
    *,
    tinkerfin: TinkerFin[PrincipalT],
    principal: PrincipalT | None,
    on_part: PartObserver[object] | None,
) -> AstreamT:
    signature = inspect.signature(astream)
    claim = _StreamClaim()

    @wraps(astream)
    def wrapped(*args: object, **kwargs: object) -> GraphRunStream[object]:
        signature.bind(*args, **kwargs)
        claim.claim()

        def source() -> AsyncIterator[object]:
            return cast(AsyncIterator[object], astream(*args, **kwargs))

        return tinkerfin.run(
            source,
            principal=principal,
            on_part=on_part,
        ).astream()

    return cast(AstreamT, wrapped)


def _wrap_agui_astream(
    astream: AstreamT,
    *,
    tinkerfin: TinkerFin[PrincipalT],
    principal: PrincipalT | None,
    on_part: PartObserver[Mapping[str, object]] | None,
    run_input: RunAgentInput,
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
        signature.bind(*args, **kwargs)
        claim.ensure_available()
        if resume is not None:
            graph_input = args[0] if args else kwargs.get("input")
            resume.validate_command(graph_input)
        invocation = _bind_agui_graph_astream(
            native_astream,
            *args,
            **kwargs,
        )
        stream = tinkerfin.run(
            invocation,
            principal=principal,
            on_part=on_part,
        ).astream_agui(
            run_input=run_input,
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
    """保留 LangGraph 原生对象流的单次请求 Runtime"""

    __slots__ = ("astream",)

    def __init__(
        self,
        *,
        astream: AstreamT,
        tinkerfin: TinkerFin[PrincipalT],
        principal: PrincipalT | None,
        on_part: PartObserver[object] | None,
    ) -> None:
        self.astream = _wrap_native_astream(
            astream,
            tinkerfin=tinkerfin,
            principal=principal,
            on_part=on_part,
        )


class DeepAgentAgUiRuntime(Generic[AstreamT]):
    """将 LangGraph 原生对象流转换为 AG-UI 的单次请求 Runtime"""

    __slots__ = ("astream",)

    def __init__(
        self,
        *,
        astream: AstreamT,
        tinkerfin: TinkerFin[PrincipalT],
        principal: PrincipalT | None,
        on_part: PartObserver[Mapping[str, object]] | None,
        run_input: RunAgentInput,
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
            principal=principal,
            on_part=on_part,
            run_input=run_input,
            timeout=timeout,
            settlement_timeout=settlement_timeout,
            expose_reasoning_events=expose_reasoning_events,
            expose_subagent_events=expose_subagent_events,
            resume=resume,
            on_event=on_event,
        )


class DeepAgentDefinition(Generic[GraphT, AstreamT, PrincipalT]):
    """保存原生建图调用，并在每次模式选择时创建一个新 Graph"""

    __slots__ = (
        "_args",
        "_factory",
        "_get_astream",
        "_kwargs",
        "_tinkerfin",
    )

    def __init__(
        self,
        *,
        tinkerfin: TinkerFin[PrincipalT],
        factory: Callable[..., GraphT],
        args: tuple[object, ...],
        kwargs: dict[str, object],
        get_astream: Callable[[GraphT], AstreamT],
    ) -> None:
        self._tinkerfin = tinkerfin
        self._factory = factory
        self._args = args
        self._kwargs = kwargs
        self._get_astream = get_astream

    def new(
        self,
        *,
        principal: PrincipalT | None = None,
        on_part: PartObserver[object] | None = None,
    ) -> DeepAgentRuntime[AstreamT]:
        """创建新 Graph，并绑定一次原生对象流请求"""

        self._tinkerfin._validate_run_binding(
            principal=principal,
            on_part=on_part,
        )
        graph = self._factory(*self._args, **self._kwargs)
        return DeepAgentRuntime(
            astream=self._get_astream(graph),
            tinkerfin=self._tinkerfin,
            principal=principal,
            on_part=on_part,
        )

    def new_agui(
        self,
        *,
        principal: PrincipalT | None = None,
        on_part: PartObserver[Mapping[str, object]] | None = None,
        run_input: RunAgentInput,
        timeout: float | None = None,
        settlement_timeout: float | None = None,
        expose_reasoning_events: bool = False,
        expose_subagent_events: bool = True,
        resume: AgUiResumeBinding | None = None,
        on_event: EventObserver | None = None,
    ) -> DeepAgentAgUiRuntime[AstreamT]:
        """创建新 Graph，并绑定一次 AG-UI 对象流请求"""

        self._tinkerfin._validate_run_binding(
            principal=principal,
            on_part=on_part,
        )
        AgUiLifecycleEventFactory.validate_run_input(run_input)
        if run_input.resume:
            if resume is None:
                raise ValueError(
                    "resume binding is required when run_input.resume is set"
                )
            resume.validate_run_input(run_input)
            bound_run_input = resume.run_input
        elif resume is not None:
            raise ValueError("resume binding requires non-empty run_input.resume")
        else:
            bound_run_input = run_input.model_copy(deep=True)
        graph = self._factory(*self._args, **self._kwargs)
        return DeepAgentAgUiRuntime(
            astream=self._get_astream(graph),
            tinkerfin=self._tinkerfin,
            principal=principal,
            on_part=on_part,
            run_input=bound_run_input,
            timeout=timeout,
            settlement_timeout=settlement_timeout,
            expose_reasoning_events=expose_reasoning_events,
            expose_subagent_events=expose_subagent_events,
            resume=resume,
            on_event=on_event,
        )


class _EnhancedDeepAgentFactory(Generic[CreateP, GraphT, AstreamT]):
    """在实例 descriptor 中保留上游 factory 的参数类型和运行时签名"""

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
        owner: type[TinkerFin[PrincipalT]],
    ) -> _EnhancedDeepAgentFactory[CreateP, GraphT, AstreamT]: ...

    @overload
    def __get__(
        self,
        instance: TinkerFin[PrincipalT],
        owner: type[TinkerFin[PrincipalT]],
    ) -> Callable[
        CreateP,
        DeepAgentDefinition[GraphT, AstreamT, PrincipalT],
    ]: ...

    def __get__(
        self,
        instance: TinkerFin[PrincipalT] | None,
        owner: type[TinkerFin[PrincipalT]],
    ) -> object:
        if instance is None:
            return self

        @wraps(self._factory)
        def create(
            *args: CreateP.args,
            **kwargs: CreateP.kwargs,
        ) -> DeepAgentDefinition[GraphT, AstreamT, PrincipalT]:
            self._signature.bind(*args, **kwargs)
            factory = cast(Callable[..., GraphT], _native_create_deep_agent)
            return DeepAgentDefinition(
                tinkerfin=instance,
                factory=factory,
                args=cast(tuple[object, ...], args),
                kwargs=cast(dict[str, object], kwargs),
                get_astream=self._get_astream,
            )

        return create


CREATE_DEEP_AGENT = _EnhancedDeepAgentFactory(
    _native_create_deep_agent,
    lambda graph: graph.astream,
)


__all__ = [
    "DeepAgentAgUiRuntime",
    "DeepAgentDefinition",
    "DeepAgentRuntime",
]
