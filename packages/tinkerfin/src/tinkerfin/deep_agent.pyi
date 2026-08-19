# ruff: noqa: F403, F405
# 此文件由 scripts/generate_stubs.py 根据锁定依赖生成，请勿手工维护参数列表
from collections.abc import Mapping, Sequence
from typing import Generic, Literal

from ag_ui.core import RunAgentInput
from deepagents.graph import *
from langchain.agents.middleware.types import InputAgentState
from langchain_core.runnables import RunnableConfig
from langgraph.pregel.main import (
    All,
    DeprecatedKwargs,
    Durability,
    RunControl,
    StreamMode,
)
from langgraph.types import Command
from typing_extensions import TypeVar, Unpack

from .agui_resume import AgUiResumeBinding
from .runtime import AgUiEventStream, EventObserver, GraphRunStream, PartObserver

PrincipalT = TypeVar("PrincipalT", default=object)

class DeepAgentRuntime(Generic[ContextT]):
    def astream(
        self,
        input: InputAgentState | Command | None,
        config: RunnableConfig | None = None,
        *,
        context: ContextT | None = None,
        stream_mode: StreamMode | Sequence[StreamMode] | None = None,
        print_mode: StreamMode | Sequence[StreamMode] = (),
        output_keys: str | Sequence[str] | None = None,
        interrupt_before: All | Sequence[str] | None = None,
        interrupt_after: All | Sequence[str] | None = None,
        durability: Durability | None = None,
        control: RunControl | None = None,
        subgraphs: bool = False,
        debug: bool | None = None,
        version: Literal["v1", "v2"] = "v1",
        **kwargs: Unpack[DeprecatedKwargs],
    ) -> GraphRunStream[object]: ...

class DeepAgentAgUiRuntime(Generic[ContextT]):
    def astream(
        self,
        input: InputAgentState | Command | None,
        config: RunnableConfig | None = None,
        *,
        context: ContextT | None = None,
        stream_mode: StreamMode | Sequence[StreamMode] | None = None,
        print_mode: StreamMode | Sequence[StreamMode] = (),
        output_keys: str | Sequence[str] | None = None,
        interrupt_before: All | Sequence[str] | None = None,
        interrupt_after: All | Sequence[str] | None = None,
        durability: Durability | None = None,
        control: RunControl | None = None,
        subgraphs: bool = False,
        debug: bool | None = None,
        version: Literal["v1", "v2"] = "v1",
        **kwargs: Unpack[DeprecatedKwargs],
    ) -> AgUiEventStream: ...

class DeepAgentDefinition(Generic[ContextT, PrincipalT]):
    def new(
        self,
        *,
        principal: PrincipalT | None = None,
        on_part: PartObserver[object] | None = None,
    ) -> DeepAgentRuntime[ContextT]: ...
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
    ) -> DeepAgentAgUiRuntime[ContextT]: ...

CREATE_DEEP_AGENT: object
