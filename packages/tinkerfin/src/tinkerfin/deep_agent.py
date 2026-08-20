"""Deep Agents 建图定义与请求级 Runtime"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Callable, Mapping
from functools import wraps
from typing import TYPE_CHECKING, Generic, ParamSpec, TypeVar, cast, overload

from deepagents.graph import create_deep_agent as _native_create_deep_agent

from tinkerfin_agui_adapter import Identity

from .agui_native import _bind_agui_graph_astream, _bind_graph_identity
from .agui_resume import AgUiResumeBinding

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
    tinkerfin: TinkerFin,
    identity: Identity,
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
    """保留 LangGraph 原生对象流的单次请求 Runtime"""

    __slots__ = ("astream",)

    def __init__(
        self,
        *,
        astream: AstreamT,
        tinkerfin: TinkerFin,
        identity: Identity,
        on_part: PartObserver[object] | None,
    ) -> None:
        self.astream = _wrap_native_astream(
            astream,
            tinkerfin=tinkerfin,
            identity=identity,
            on_part=on_part,
        )


class DeepAgentAgUiRuntime(Generic[AstreamT]):
    """将 LangGraph 原生对象流转换为 AG-UI 的单次请求 Runtime"""

    __slots__ = ("astream",)

    def __init__(
        self,
        *,
        astream: AstreamT,
        tinkerfin: TinkerFin,
        identity: Identity,
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
            on_part=on_part,
            timeout=timeout,
            settlement_timeout=settlement_timeout,
            expose_reasoning_events=expose_reasoning_events,
            expose_subagent_events=expose_subagent_events,
            resume=resume,
            on_event=on_event,
        )


class DeepAgentDefinition(Generic[GraphT, AstreamT]):
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
        tinkerfin: TinkerFin,
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
        identity: Identity,
        on_part: PartObserver[object] | None = None,
    ) -> DeepAgentRuntime[AstreamT]:
        """创建新 Graph，并绑定一次原生对象流请求"""

        self._tinkerfin._validate_run_binding(
            identity=identity,
            on_part=on_part,
        )
        graph = self._factory(*self._args, **self._kwargs)
        return DeepAgentRuntime(
            astream=self._get_astream(graph),
            tinkerfin=self._tinkerfin,
            identity=identity,
            on_part=on_part,
        )

    def new_agui(
        self,
        *,
        identity: Identity,
        on_part: PartObserver[Mapping[str, object]] | None = None,
        timeout: float | None = None,
        settlement_timeout: float | None = None,
        expose_reasoning_events: bool = False,
        expose_subagent_events: bool = True,
        resume: AgUiResumeBinding | None = None,
        on_event: EventObserver | None = None,
    ) -> DeepAgentAgUiRuntime[AstreamT]:
        """创建新 Graph，并绑定一次 AG-UI 对象流请求"""

        self._tinkerfin._validate_run_binding(
            identity=identity,
            on_part=on_part,
        )
        if resume is not None:
            resume.validate_identity(identity)
        graph = self._factory(*self._args, **self._kwargs)
        return DeepAgentAgUiRuntime(
            astream=self._get_astream(graph),
            tinkerfin=self._tinkerfin,
            identity=identity,
            on_part=on_part,
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
