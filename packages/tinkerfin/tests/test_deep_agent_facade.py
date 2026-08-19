"""Public behavior of the deferred Deep Agents runtime façade."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any, TypedDict, cast

import pytest
from ag_ui.core import BaseEvent, RunAgentInput, RunStartedEvent
from langchain.agents.middleware.types import InputAgentState
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.runnables.base import Runnable
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

import tinkerfin.deep_agent as deep_agent_module
from tinkerfin import (
    AgUiEventStream,
    AgUiNativeStreamConfigurationError,
    AgUiResumeBinding,
    DeepAgentAgUiRuntime,
    DeepAgentDefinition,
    DeepAgentRuntime,
    GraphRunStream,
    TinkerFin,
)


@asynccontextmanager
async def _coordinate(_principal: str) -> AsyncIterator[None]:
    yield


class _RuntimeContext(TypedDict):
    tenant: str


class _FakeModel(FakeMessagesListChatModel):
    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable:
        del tools, tool_choice, kwargs
        return self


class _RecordingGraph:
    def __init__(
        self,
        *,
        parts: tuple[object, ...] = (),
        source_error: Exception | None = None,
    ) -> None:
        self.parts = parts
        self.source_error = source_error
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.opened = 0
        self.pulled = 0
        self.closed = 0

    def astream(
        self,
        *args: object,
        **options: object,
    ) -> AsyncIterator[object]:
        self.calls.append((args, options))

        async def source() -> AsyncIterator[object]:
            self.opened += 1
            try:
                for part in self.parts:
                    self.pulled += 1
                    yield part
                if self.source_error is not None:
                    raise self.source_error
            finally:
                self.closed += 1

        return source()


setattr(
    _RecordingGraph.astream,
    "__signature__",
    inspect.signature(CompiledStateGraph.astream),
)


def _install_builder(
    monkeypatch: pytest.MonkeyPatch,
    *,
    parts: tuple[object, ...] = (),
    source_error: Exception | None = None,
) -> tuple[
    list[tuple[tuple[object, ...], dict[str, object]]],
    list[_RecordingGraph],
]:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    graphs: list[_RecordingGraph] = []

    def build(*args: object, **kwargs: object) -> _RecordingGraph:
        calls.append((args, kwargs))
        graph = _RecordingGraph(parts=parts, source_error=source_error)
        graphs.append(graph)
        return graph

    monkeypatch.setattr(
        deep_agent_module,
        "_native_create_deep_agent",
        build,
        raising=False,
    )
    return calls, graphs


def _definition(
    tinkerfin: TinkerFin[str],
) -> DeepAgentDefinition[_RuntimeContext, str]:
    return tinkerfin.create_deep_agent(
        model="provider:model",
        tools=[],
        system_prompt="system",
        context_schema=_RuntimeContext,
    )


def _graph_input() -> InputAgentState:
    return InputAgentState(messages=[])


def _graph_config() -> RunnableConfig:
    return {"configurable": {"thread_id": "thread-1"}}


def _run_input(
    *,
    thread_id: str = "thread-1",
    run_id: str = "run-1",
    parent_run_id: str | None = None,
    resume: list[dict[str, object]] | None = None,
) -> RunAgentInput:
    return RunAgentInput.model_validate(
        {
            "threadId": thread_id,
            "runId": run_id,
            "parentRunId": parent_run_id,
            "state": {"draft": True},
            "messages": [{"id": "message-1", "role": "user", "content": "hello"}],
            "tools": [
                {
                    "name": "lookup",
                    "description": "Look up one value",
                    "parameters": {"type": "object"},
                }
            ],
            "context": [{"description": "tenant", "value": "acme"}],
            "forwardedProps": {"model": "test-model"},
            "resume": resume,
        }
    )


def _resume_input(*, run_id: str = "run-resume") -> RunAgentInput:
    return _run_input(
        run_id=run_id,
        parent_run_id="run-interrupted",
        resume=[
            {
                "interruptId": "interrupt-1",
                "status": "resolved",
                "payload": {"type": "approve"},
            }
        ],
    )


def _resume_binding(run_input: RunAgentInput) -> AgUiResumeBinding:
    return AgUiResumeBinding(
        run_input=run_input,
        command=Command(resume={"decisions": [{"type": "approve"}]}),
    )


def _state_part(value: int = 1) -> Mapping[str, object]:
    return {
        "type": "values",
        "ns": (),
        "data": {"value": value},
        "interrupts": (),
    }


def _real_definition() -> DeepAgentDefinition[None, object]:
    return TinkerFin[object]().create_deep_agent(
        model=_FakeModel(responses=[AIMessage(content="ok")]),
        tools=[],
    )


@pytest.mark.asyncio
async def test_native_facade_streams_a_real_deep_agent_graph() -> None:
    runtime = _real_definition().new()
    parts = [
        cast(Mapping[str, object], part)
        async for part in runtime.astream(
            InputAgentState(messages=[HumanMessage(content="hello")]),
            stream_mode="values",
            version="v2",
        )
    ]
    state = cast(Mapping[str, object], parts[-1]["data"])
    messages = cast(Sequence[BaseMessage], state["messages"])

    assert parts[-1]["type"] == "values"
    assert isinstance(messages[-1], AIMessage)
    assert messages[-1].content == "ok"


@pytest.mark.asyncio
async def test_agui_facade_streams_a_real_deep_agent_graph() -> None:
    run_input = _run_input()
    runtime = _real_definition().new_agui(run_input=run_input)
    events = [
        event
        async for event in runtime.astream(
            InputAgentState(messages=[HumanMessage(content="hello")])
        )
    ]

    assert events[0].type.value == "RUN_STARTED"
    assert events[-1].type.value == "RUN_FINISHED"
    assert [event.type.value for event in events].count("RUN_STARTED") == 1
    assert [event.type.value for event in events].count("RUN_FINISHED") == 1
    assert any(event.type.value == "TEXT_MESSAGE_CONTENT" for event in events)
    assert isinstance(events[0], RunStartedEvent)
    assert events[0].input == run_input
    assert events[0].input is not run_input


def test_new_agui_rejects_invalid_input_before_building_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, _ = _install_builder(monkeypatch)
    definition = _definition(TinkerFin[str]())

    with pytest.raises(ValueError, match="thread_id must be non-blank"):
        definition.new_agui(run_input=_run_input(thread_id=" thread-1"))

    assert calls == []


def test_new_agui_requires_resume_binding_before_building_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, _ = _install_builder(monkeypatch)
    definition = _definition(TinkerFin[str]())

    with pytest.raises(ValueError, match="resume binding is required"):
        definition.new_agui(run_input=_resume_input())

    assert calls == []


def test_new_agui_rejects_binding_for_other_input_before_building_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, _ = _install_builder(monkeypatch)
    definition = _definition(TinkerFin[str]())
    run_input = _resume_input()
    other_binding = _resume_binding(_resume_input(run_id="run-other"))

    with pytest.raises(ValueError, match="different run_input"):
        definition.new_agui(run_input=run_input, resume=other_binding)

    assert calls == []


@pytest.mark.asyncio
async def test_resume_command_mismatch_does_not_consume_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, graphs = _install_builder(monkeypatch, parts=(_state_part(),))
    observed: list[Mapping[str, object]] = []

    async def on_part(part: Mapping[str, object]) -> None:
        observed.append(part)

    run_input = _resume_input()
    binding = _resume_binding(run_input)
    runtime = _definition(TinkerFin[str]()).new_agui(
        run_input=run_input,
        resume=binding,
        on_part=on_part,
    )

    with pytest.raises(ValueError, match="resume binding command"):
        runtime.astream(Command(resume={"decisions": [{"type": "reject"}]}))

    assert graphs[0].calls == []
    assert observed == []

    events = [event async for event in runtime.astream(binding.command)]

    assert graphs[0].calls
    assert observed == [_state_part()]
    assert events[0].type.value == "RUN_STARTED"
    assert events[-1].type.value == "RUN_FINISHED"


@pytest.mark.asyncio
async def test_agui_runtime_snapshots_run_input_before_graph_stream_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_builder(monkeypatch, parts=(_state_part(),))
    run_input = _run_input()
    runtime = _definition(TinkerFin[str]()).new_agui(run_input=run_input)
    cast(dict[str, object], run_input.state)["draft"] = False

    stream = runtime.astream(_graph_input())
    cast(dict[str, object], run_input.forwarded_props)["model"] = "tampered"
    started = await anext(stream)

    assert isinstance(started, RunStartedEvent)
    assert started.input is not None
    assert started.input.state == {"draft": True}
    assert started.input.forwarded_props == {"model": "test-model"}
    await stream.aclose()


def test_create_deep_agent_defers_and_reuses_fresh_graph_builds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, graphs = _install_builder(monkeypatch)
    tools: list[Callable[..., object]] = []
    tinkerfin = TinkerFin[str]()

    definition = tinkerfin.create_deep_agent(
        model="provider:model",
        tools=tools,
        system_prompt="system",
    )
    tools.append(lambda: "borrowed-after-definition")

    assert isinstance(definition, DeepAgentDefinition)
    assert calls == []

    native = definition.new()
    agui = definition.new_agui(run_input=_run_input())

    assert isinstance(native, DeepAgentRuntime)
    assert isinstance(agui, DeepAgentAgUiRuntime)
    assert len(calls) == 2
    assert len(graphs) == 2
    assert graphs[0] is not graphs[1]
    assert calls[0][1]["tools"] is tools
    assert calls[1][1]["tools"] is tools


def test_factory_parameter_binding_fails_without_building_a_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, _ = _install_builder(monkeypatch)
    create = cast(Callable[..., object], TinkerFin().create_deep_agent)

    with pytest.raises(TypeError):
        create(unknown_option=True)

    assert calls == []


@pytest.mark.parametrize(
    ("tinkerfin", "principal", "expected"),
    [
        (TinkerFin[str](), "user-1", "requires a run_coordinator"),
        (
            TinkerFin[str](run_coordinator=_coordinate),
            None,
            "principal is required",
        ),
    ],
)
def test_new_validates_principal_before_building_graph(
    monkeypatch: pytest.MonkeyPatch,
    tinkerfin: TinkerFin[str],
    principal: str | None,
    expected: str,
) -> None:
    calls, _ = _install_builder(monkeypatch)
    definition = _definition(tinkerfin)

    with pytest.raises(ValueError, match=expected):
        definition.new(principal=principal)

    assert calls == []


@pytest.mark.asyncio
async def test_native_runtime_forwards_the_bound_call_lazily_and_is_single_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native_part = object()
    _, graphs = _install_builder(monkeypatch, parts=(native_part,))
    observed: list[object] = []

    async def on_part(part: object) -> None:
        observed.append(part)

    runtime = _definition(TinkerFin[str]()).new(on_part=on_part)
    graph_input = _graph_input()
    config = _graph_config()
    stream = runtime.astream(
        graph_input,
        config,
        context={"tenant": "tenant-1"},
        stream_mode="custom",
        print_mode="debug",
        output_keys="messages",
        interrupt_before=("model",),
        interrupt_after=("tools",),
        durability="sync",
        control=None,
        subgraphs=False,
        debug=True,
        version="v1",
    )

    assert isinstance(stream, GraphRunStream)
    assert graphs[0].calls == []
    assert observed == []
    assert await anext(stream) is native_part
    assert observed == [native_part]
    assert graphs[0].calls == [
        (
            (graph_input, config),
            {
                "context": {"tenant": "tenant-1"},
                "stream_mode": "custom",
                "print_mode": "debug",
                "output_keys": "messages",
                "interrupt_before": ("model",),
                "interrupt_after": ("tools",),
                "durability": "sync",
                "control": None,
                "subgraphs": False,
                "debug": True,
                "version": "v1",
            },
        )
    ]
    with pytest.raises(RuntimeError, match="only one object stream"):
        runtime.astream(graph_input, config)
    await stream.aclose()
    assert graphs[0].closed == 1


@pytest.mark.asyncio
async def test_native_invalid_binding_does_not_claim_the_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    part = object()
    _, graphs = _install_builder(monkeypatch, parts=(part,))
    runtime = _definition(TinkerFin[str]()).new()
    invalid_astream = cast(Callable[..., object], runtime.astream)

    with pytest.raises(TypeError):
        invalid_astream()

    stream = runtime.astream(_graph_input())
    assert await anext(stream) is part
    await stream.aclose()
    assert len(graphs[0].calls) == 1


@pytest.mark.asyncio
async def test_agui_runtime_defaults_reserved_options_and_stays_lazy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, graphs = _install_builder(monkeypatch, parts=(_state_part(),))
    runtime = _definition(TinkerFin[str]()).new_agui(
        run_input=_run_input(),
    )
    stream = runtime.astream(
        _graph_input(),
        _graph_config(),
    )

    assert isinstance(stream, AgUiEventStream)
    assert graphs[0].calls == []
    assert (await anext(stream)).type.value == "RUN_STARTED"
    assert graphs[0].calls == []
    assert (await anext(stream)).type.value == "STATE_SNAPSHOT"
    assert graphs[0].calls[0][1] == {
        "stream_mode": ("messages", "tasks", "values"),
        "version": "v2",
        "subgraphs": True,
    }
    assert (await anext(stream)).type.value == "RUN_FINISHED"


@pytest.mark.asyncio
async def test_agui_runtime_normalizes_explicit_modes_and_forwards_other_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, graphs = _install_builder(monkeypatch, parts=(_state_part(),))
    runtime = _definition(TinkerFin[str]()).new_agui(
        run_input=_run_input(),
    )
    stream = runtime.astream(
        _graph_input(),
        stream_mode=("custom", "values", "messages", "debug", "tasks"),
        print_mode="updates",
        context={"tenant": "tenant-1"},
        version="v2",
        subgraphs=True,
    )

    assert (await anext(stream)).type.value == "RUN_STARTED"
    assert (await anext(stream)).type.value == "STATE_SNAPSHOT"
    assert graphs[0].calls[0][1] == {
        "print_mode": "updates",
        "context": {"tenant": "tenant-1"},
        "stream_mode": ("messages", "tasks", "values", "custom", "debug"),
        "version": "v2",
        "subgraphs": True,
    }
    await stream.aclose()


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({"stream_mode": None}, "stream_mode"),
        ({"stream_mode": "messages"}, "missing required"),
        (
            {"stream_mode": ("messages", "tasks", "values", "messages")},
            "duplicate",
        ),
        (
            {"stream_mode": ("messages", "tasks", "values", "unknown")},
            "unsupported",
        ),
        ({"version": "v1"}, "version"),
        ({"subgraphs": False}, "subgraphs"),
    ],
)
def test_invalid_agui_reserved_options_fail_before_every_stream_side_effect(
    monkeypatch: pytest.MonkeyPatch,
    options: dict[str, object],
    expected: str,
) -> None:
    coordination: list[str] = []
    observed: list[object] = []

    @asynccontextmanager
    async def coordinate(principal: str) -> AsyncIterator[None]:
        coordination.append(principal)
        yield

    async def on_part(part: object) -> None:
        observed.append(part)

    _, graphs = _install_builder(monkeypatch, parts=(_state_part(),))
    runtime = _definition(TinkerFin[str](run_coordinator=coordinate)).new_agui(
        principal="user-1",
        on_part=on_part,
        run_input=_run_input(),
    )
    invalid_astream = cast(Callable[..., object], runtime.astream)

    with pytest.raises(AgUiNativeStreamConfigurationError, match=expected):
        invalid_astream(_graph_input(), **options)

    assert graphs[0].calls == []
    assert graphs[0].opened == 0
    assert coordination == []
    assert observed == []

    valid = runtime.astream(_graph_input())
    assert isinstance(valid, AgUiEventStream)


@pytest.mark.asyncio
async def test_agui_runtime_preserves_observer_order_and_error_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace: list[str] = []

    async def on_part(part: object) -> None:
        assert part == _state_part()
        trace.append("part")

    async def on_event(event: BaseEvent) -> None:
        trace.append(f"event:{event.type.value}")

    _, graphs = _install_builder(
        monkeypatch,
        parts=(_state_part(),),
        source_error=RuntimeError("native failed"),
    )
    runtime = _definition(TinkerFin[str]()).new_agui(
        on_part=on_part,
        run_input=_run_input(),
        on_event=on_event,
    )
    stream = runtime.astream(_graph_input())
    events = [event async for event in stream]

    assert [event.type.value for event in events] == [
        "RUN_STARTED",
        "STATE_SNAPSHOT",
        "RUN_ERROR",
    ]
    assert trace == [
        "event:RUN_STARTED",
        "part",
        "event:STATE_SNAPSHOT",
        "event:RUN_ERROR",
    ]
    assert isinstance(stream.error, RuntimeError)
    assert graphs[0].closed == 1


def test_runtime_wrappers_preserve_upstream_parameter_information(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class State(TypedDict):
        value: int

    builder = StateGraph(State)
    builder.add_node("identity", lambda state: state)
    builder.add_edge(START, "identity")
    builder.add_edge("identity", END)
    graph = builder.compile()
    monkeypatch.setattr(
        deep_agent_module,
        "_native_create_deep_agent",
        lambda *args, **kwargs: graph,
    )
    tinkerfin = TinkerFin[str]()
    definition = _definition(tinkerfin)
    native = definition.new()
    agui = definition.new_agui(run_input=_run_input())
    upstream = inspect.signature(CompiledStateGraph.astream)
    bound_upstream = upstream.replace(
        parameters=tuple(upstream.parameters.values())[1:]
    )

    from deepagents.graph import create_deep_agent

    assert inspect.signature(tinkerfin.create_deep_agent) == inspect.signature(
        create_deep_agent
    )
    assert inspect.signature(native.astream) == bound_upstream
    assert inspect.signature(agui.astream) == bound_upstream


def test_runtime_astream_is_single_use_for_agui(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_builder(monkeypatch)
    runtime = _definition(TinkerFin[str]()).new_agui(
        run_input=_run_input(),
    )
    stream = runtime.astream(_graph_input())

    assert isinstance(stream, AgUiEventStream)
    with pytest.raises(RuntimeError, match="only one object stream"):
        runtime.astream(_graph_input())
