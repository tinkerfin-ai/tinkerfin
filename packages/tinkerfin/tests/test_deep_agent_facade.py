"""Public behavior of the deferred Deep Agents runtime façade."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any, TypedDict, cast

import pytest
from ag_ui.core import BaseEvent, RunErrorEvent, RunStartedEvent
from deepagents.graph import create_deep_agent as upstream_create_deep_agent
from langchain.agents.middleware.types import InputAgentState
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.runnables.base import Runnable
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from tinkerfin import (
    AgUiEventStream,
    AgUiResumeBinding,
    AgUiResumeRequest,
    DeepAgentAgUiResumeRuntime,
    DeepAgentAgUiRuntime,
    DeepAgentDefinition,
    DeepAgentRuntime,
    NativeGraphRunStream,
    RunIdentity,
    TinkerFin,
    TinkerFinLifecycleError,
)
from tinkerfin.deep_agent import DeepAgentGraph


@asynccontextmanager
async def _coordinate(_identity: RunIdentity) -> AsyncIterator[None]:
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
        "tinkerfin.runtime_profile._deepagents_graph.create_deep_agent",
        build,
    )
    return calls, graphs


def _call_with_positional_input(
    operation: Callable[..., object],
    graph_input: object,
) -> object:
    return operation(graph_input)


def _definition(
    tinkerfin: TinkerFin,
) -> DeepAgentDefinition[_RuntimeContext]:
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


def _identity(
    *,
    thread_id: str = "thread-1",
    run_id: str = "run-1",
) -> RunIdentity:
    return RunIdentity(threadId=thread_id, runId=run_id)


def _resume_identity(*, run_id: str = "run-resume") -> RunIdentity:
    return _identity(run_id=run_id)


def _resume_binding() -> AgUiResumeBinding:
    return AgUiResumeBinding(
        mode="resume",
        resume_data={"decisions": [{"type": "approve"}]},
        native_interrupt_ids=("interrupt-1",),
    )


def _state_part(value: int = 1) -> Mapping[str, object]:
    return {
        "type": "values",
        "ns": (),
        "data": {"value": value},
        "interrupts": (),
    }


def _real_definition() -> DeepAgentDefinition[None]:
    return TinkerFin().create_deep_agent(
        model=_FakeModel(responses=[AIMessage(content="ok")]),
        tools=[],
    )


@pytest.mark.asyncio
async def test_native_facade_streams_a_real_deep_agent_graph() -> None:
    runtime = _real_definition().new(identity=_identity())
    parts = [
        cast(Mapping[str, object], part)
        async for part in runtime.astream(
            InputAgentState(messages=[HumanMessage(content="hello")]),
        )
    ]
    state_part = next(
        part
        for part in reversed(parts)
        if part["type"] == "values" and part["ns"] == ()
    )
    state = cast(Mapping[str, object], state_part["data"])
    messages = cast(Sequence[BaseMessage], state["messages"])

    assert {part["type"] for part in parts} == {"messages", "tasks", "values"}
    assert isinstance(messages[-1], AIMessage)
    assert messages[-1].content == "ok"


@pytest.mark.asyncio
async def test_definition_creates_one_reusable_direct_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, graphs = _install_builder(
        monkeypatch,
        parts=(
            {
                "type": "messages",
                "ns": (),
                "data": (
                    AIMessage(content="answer", id="message-1"),
                    {"langgraph_node": "model"},
                ),
            },
            _state_part(7),
        ),
    )
    graph = await _definition(TinkerFin()).create_graph()

    assert isinstance(graph, DeepAgentGraph)
    assert len(calls) == 1
    values = [
        part
        async for part in graph.astream(
            _graph_input(),
            _graph_config(),
            stream_mode=("values",),
        )
    ]
    result = await graph.ainvoke(_graph_input(), _graph_config())

    assert [part["type"] for part in values] == ["values"]
    assert result == {"value": 7}
    assert len(calls) == 1
    assert len(graphs) == 1
    assert len(graphs[0].calls) == 2
    for _args, options in graphs[0].calls:
        assert options["version"] == "v2"
        assert options["subgraphs"] is True
        assert options["stream_mode"] == ("messages", "tasks", "values")


@pytest.mark.asyncio
async def test_direct_graph_reuses_runnable_config_and_async_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, graphs = _install_builder(monkeypatch, parts=(_state_part(9),))
    graph = await _definition(TinkerFin()).create_graph()
    configured = graph.with_config({"configurable": {"thread_id": "configured-thread"}})

    configured_result = await configured.ainvoke(_graph_input())
    batch_results = await graph.abatch(
        [_graph_input(), _graph_input()],
        config=[
            {"configurable": {"thread_id": "batch-thread-1"}},
            {"configurable": {"thread_id": "batch-thread-2"}},
        ],
    )

    assert configured_result == {"value": 9}
    assert batch_results == [{"value": 9}, {"value": 9}]
    assert len(calls) == 1
    assert len(graphs) == 1
    thread_ids: set[object] = set()
    for args, _options in graphs[0].calls:
        call_config = cast(Mapping[str, object], args[1])
        configurable = cast(Mapping[str, object], call_config["configurable"])
        thread_ids.add(configurable["thread_id"])
    assert thread_ids == {
        "configured-thread",
        "batch-thread-1",
        "batch-thread-2",
    }


@pytest.mark.asyncio
async def test_tinkerfin_lends_its_default_checkpointer_to_new_definitions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, _graphs = _install_builder(monkeypatch, parts=(_state_part(),))
    checkpointer = MemorySaver()
    definition = _definition(TinkerFin(checkpointer=checkpointer))

    await definition.create_graph()

    assert calls[0][1]["checkpointer"] is checkpointer


@pytest.mark.asyncio
async def test_definition_explicit_checkpointer_overrides_the_factory_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, _graphs = _install_builder(monkeypatch, parts=(_state_part(),))
    default = MemorySaver()
    explicit = MemorySaver()
    definition = TinkerFin(checkpointer=default).create_deep_agent(
        model="provider:model",
        tools=[],
        checkpointer=explicit,
    )

    await definition.create_graph()

    assert calls[0][1]["checkpointer"] is explicit


def test_direct_graph_rejects_synchronous_execution() -> None:
    graph = cast(Any, object.__new__(DeepAgentGraph))

    with pytest.raises(NotImplementedError, match="async execution only"):
        graph.invoke(_graph_input(), _graph_config())
    with pytest.raises(NotImplementedError, match="async execution only"):
        graph.stream(_graph_input(), _graph_config())
    with pytest.raises(NotImplementedError, match="async execution only"):
        graph.batch([_graph_input()], [_graph_config()], return_exceptions=True)
    with pytest.raises(NotImplementedError, match="async execution only"):
        graph.batch_as_completed(
            [_graph_input()],
            [_graph_config()],
            return_exceptions=True,
        )


@pytest.mark.asyncio
async def test_direct_graph_ainvoke_runs_a_real_deep_agent() -> None:
    graph = await _real_definition().create_graph()
    result = await graph.ainvoke(
        InputAgentState(messages=[HumanMessage(content="hello")]),
        _graph_config(),
    )
    messages = cast(Sequence[BaseMessage], result["messages"])

    assert isinstance(messages[-1], AIMessage)
    assert messages[-1].content == "ok"


@pytest.mark.asyncio
async def test_open_run_manages_a_prebuilt_definition() -> None:
    tinkerfin = TinkerFin()
    agent = tinkerfin.create_deep_agent(
        model=_FakeModel(responses=[AIMessage(content="managed")]),
        tools=[],
    )

    stream = await tinkerfin.open_run(
        _identity(),
        agent=agent,
        input=InputAgentState(messages=[HumanMessage(content="hello")]),
        config=_graph_config(),
    )
    parts = [part async for part in stream]
    state = cast(
        Mapping[str, object],
        next(
            part["data"]
            for part in reversed(parts)
            if part["type"] == "values" and part["ns"] == ()
        ),
    )
    messages = cast(Sequence[BaseMessage], state["messages"])

    assert messages[-1].content == "managed"
    assert stream.messaging_identity == _identity()


@pytest.mark.asyncio
async def test_open_run_resolves_an_async_agent_factory_once() -> None:
    tinkerfin = TinkerFin()
    created = 0

    async def create_agent() -> DeepAgentDefinition[None]:
        nonlocal created
        created += 1
        return tinkerfin.create_deep_agent(
            model=_FakeModel(responses=[AIMessage(content="async")]),
            tools=[],
        )

    stream = await tinkerfin.open_run(
        _identity(),
        agent=create_agent,
        input=InputAgentState(messages=[HumanMessage(content="hello")]),
    )
    await anext(stream)
    await stream.aclose()

    assert created == 1


@pytest.mark.asyncio
async def test_open_run_turns_agent_setup_failure_into_an_observed_stream() -> None:
    tinkerfin = TinkerFin()

    async def create_agent() -> DeepAgentDefinition[None]:
        raise RuntimeError("model setup failed")

    stream = await tinkerfin.open_run(
        _identity(),
        agent=create_agent,
        input=_graph_input(),
    )

    with pytest.raises(RuntimeError, match="model setup failed"):
        await anext(stream)
    assert isinstance(stream.error, RuntimeError)


@pytest.mark.asyncio
async def test_open_agui_run_manages_a_prebuilt_definition() -> None:
    identity = _identity()
    tinkerfin = TinkerFin()
    agent = tinkerfin.create_deep_agent(
        model=_FakeModel(responses=[AIMessage(content="managed")]),
        tools=[],
    )

    stream = await tinkerfin.open_agui_run(
        identity,
        agent=agent,
        input=InputAgentState(messages=[HumanMessage(content="hello")]),
    )
    events = [event async for event in stream]

    event_types = [event.type.value for event in events]
    assert stream.messaging_identity is identity
    assert event_types.count("RUN_STARTED") == 1
    assert event_types.count("RUN_FINISHED") == 1
    assert "RUN_ERROR" not in event_types
    assert any(event_type == "TEXT_MESSAGE_CONTENT" for event_type in event_types)


@pytest.mark.asyncio
async def test_open_agui_run_resolves_an_async_agent_factory_once() -> None:
    tinkerfin = TinkerFin()
    created = 0

    async def create_agent() -> DeepAgentDefinition[None]:
        nonlocal created
        created += 1
        return tinkerfin.create_deep_agent(
            model=_FakeModel(responses=[AIMessage(content="async")]),
            tools=[],
        )

    stream = await tinkerfin.open_agui_run(
        _identity(),
        agent=create_agent,
        input=InputAgentState(messages=[HumanMessage(content="hello")]),
    )
    events = [event async for event in stream]

    assert created == 1
    assert events[-1].type.value == "RUN_FINISHED"


@pytest.mark.asyncio
async def test_open_agui_run_turns_agent_setup_failure_into_one_lifecycle() -> None:
    tinkerfin = TinkerFin()

    async def create_agent() -> DeepAgentDefinition[None]:
        raise RuntimeError("model setup failed")

    stream = await tinkerfin.open_agui_run(
        _identity(),
        agent=create_agent,
        input=_graph_input(),
    )
    events = [event async for event in stream]

    assert [event.type.value for event in events] == ["RUN_STARTED", "RUN_ERROR"]
    assert isinstance(stream.error, RuntimeError)


@pytest.mark.asyncio
async def test_open_agui_run_requires_exactly_one_input_or_resume() -> None:
    tinkerfin = TinkerFin()
    definition = _definition(tinkerfin)
    resume = AgUiResumeRequest.model_validate(
        {
            "entries": [
                {
                    "interruptId": "interrupt-1#0",
                    "status": "cancelled",
                }
            ]
        }
    )

    with pytest.raises(ValueError, match="exactly one"):
        await tinkerfin.open_agui_run(
            _identity(),
            agent=definition,
        )
    with pytest.raises(ValueError, match="exactly one"):
        await tinkerfin.open_agui_run(
            _identity(),
            agent=definition,
            input=_graph_input(),
            resume=resume,
        )


@pytest.mark.asyncio
async def test_agui_facade_streams_a_real_deep_agent_graph() -> None:
    identity = _identity()
    runtime = _real_definition().new_agui(identity=identity)
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
    assert events[0].input is None


def test_new_agui_rejects_invalid_identity_before_building_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, _ = _install_builder(monkeypatch)
    definition = _definition(TinkerFin())

    with pytest.raises(ValueError, match="surrounding whitespace"):
        definition.new_agui(
            identity=_identity(thread_id=" thread-1"),
        )

    assert calls == []


def test_new_agui_rejects_self_parent_before_building_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, _ = _install_builder(monkeypatch)
    definition = _definition(TinkerFin())
    identity = _resume_identity()

    with pytest.raises(ValueError, match="must differ"):
        definition.new_agui(
            identity=identity,
            parent_run_id=identity.run_id,
        )

    assert calls == []


@pytest.mark.asyncio
async def test_resume_runtime_rejects_graph_input_without_consuming_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, graphs = _install_builder(monkeypatch, parts=(_state_part(),))
    observed: list[Mapping[str, object]] = []

    async def on_part(part: Mapping[str, object]) -> None:
        observed.append(part)

    identity = _resume_identity()
    binding = _resume_binding()
    runtime = _definition(TinkerFin()).new_agui(
        identity=identity,
        resume=binding,
        on_part=on_part,
    )
    with pytest.raises(TypeError):
        _call_with_positional_input(
            runtime.astream,
            Command(resume={"decisions": [{"type": "reject"}]}),
        )

    assert graphs == []
    assert observed == []

    stream = runtime.astream()
    events = [event async for event in stream]

    assert graphs[0].calls == []
    assert observed == []
    assert [event.type.value for event in events] == ["RUN_STARTED", "RUN_ERROR"]
    assert isinstance(stream.error, TinkerFinLifecycleError)


@pytest.mark.asyncio
async def test_all_cancelled_resume_uses_a_finite_runtime_without_building_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, _graphs = _install_builder(monkeypatch)
    observed_checkpoints: list[object] = []

    async def checkpointed(value: object) -> None:
        observed_checkpoints.append(value)

    runtime = _definition(TinkerFin()).new_agui(
        identity=_resume_identity(),
        parent_run_id="run-parent",
        resume=AgUiResumeBinding(
            mode="abandon",
            native_interrupt_ids=("interrupt-1",),
        ),
        on_resume_checkpointed=checkpointed,
    )

    assert isinstance(runtime, DeepAgentAgUiResumeRuntime)
    assert calls == []
    invalid_astream = cast(Callable[..., object], runtime.astream)
    with pytest.raises(ValueError, match="version"):
        invalid_astream(version="v1")
    events = [event async for event in runtime.astream(config=_graph_config())]

    assert [event.type.value for event in events] == ["RUN_STARTED", "RUN_ERROR"]
    started = cast(RunStartedEvent, events[0])
    assert started.thread_id == "thread-1"
    assert started.parent_run_id == "run-parent"
    assert started.input is None
    terminal = events[-1]
    assert isinstance(terminal, RunErrorEvent)
    assert terminal.code == "resume_cancelled"
    assert observed_checkpoints == []


@pytest.mark.asyncio
async def test_agui_runtime_keeps_identity_before_graph_stream_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_builder(monkeypatch, parts=(_state_part(),))
    identity = _identity(thread_id="public-thread")
    runtime = _definition(TinkerFin()).new_agui(
        identity=identity,
    )

    stream = runtime.astream(_graph_input())
    started = await anext(stream)

    assert isinstance(started, RunStartedEvent)
    assert stream.messaging_identity is identity
    assert started.thread_id == "public-thread"
    assert started.input is None
    await stream.aclose()


@pytest.mark.asyncio
async def test_create_deep_agent_defers_fresh_builds_until_stream_consumption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, graphs = _install_builder(monkeypatch)
    tools: list[Callable[..., object]] = []
    tinkerfin = TinkerFin()

    definition = tinkerfin.create_deep_agent(
        model="provider:model",
        tools=tools,
        system_prompt="system",
    )
    tools.append(lambda: "borrowed-after-definition")

    assert isinstance(definition, DeepAgentDefinition)
    assert calls == []

    native = definition.new(identity=_identity(run_id="native-run"))
    agui_identity = _identity(run_id="agui-run")
    agui = definition.new_agui(
        identity=agui_identity,
    )

    assert isinstance(native, DeepAgentRuntime)
    assert isinstance(agui, DeepAgentAgUiRuntime)
    assert calls == []

    native_stream = native.astream(_graph_input())
    with pytest.raises(StopAsyncIteration):
        await anext(native_stream)
    await native_stream.aclose()
    assert len(calls) == 1

    events = [event async for event in agui.astream(_graph_input())]
    assert [event.type.value for event in events] == [
        "RUN_STARTED",
        "RUN_FINISHED",
    ]
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


def test_new_requires_identity_before_building_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, _ = _install_builder(monkeypatch)
    definition = _definition(TinkerFin(run_coordinator=_coordinate))
    untyped_new = cast(Callable[..., object], definition.new)

    with pytest.raises(TypeError, match="identity"):
        untyped_new()

    assert calls == []


@pytest.mark.asyncio
async def test_native_runtime_forwards_the_bound_call_lazily_and_is_single_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native_part = {"type": "custom", "ns": (), "data": {"value": "native"}}
    _, graphs = _install_builder(monkeypatch, parts=(native_part,))
    observed: list[object] = []

    async def on_part(part: object) -> None:
        observed.append(part)

    identity = _identity()
    runtime = _definition(TinkerFin()).new(identity=identity, on_part=on_part)
    graph_input = _graph_input()
    config = _graph_config()
    stream = runtime.astream(
        graph_input,
        config,
        context={"tenant": "tenant-1"},
        stream_mode=("custom", "values", "messages", "tasks"),
        print_mode="debug",
        output_keys=None,
        interrupt_before=("model",),
        interrupt_after=("tools",),
        durability="sync",
        control=None,
        subgraphs=True,
        debug=True,
    )

    assert isinstance(stream, NativeGraphRunStream)
    assert stream.messaging_identity is identity
    assert graphs == []
    assert observed == []
    assert await anext(stream) is native_part
    assert len(graphs) == 1
    assert observed == [native_part]
    assert config == {"configurable": {"thread_id": "thread-1"}}
    assert graphs[0].calls == [
        (
            (
                graph_input,
                {
                    "configurable": {
                        "thread_id": "thread-1",
                        "_tinkerfin_runtime_profile": "deepagents-v2",
                    }
                },
            ),
            {
                "context": {"tenant": "tenant-1"},
                "stream_mode": ("messages", "tasks", "values", "custom"),
                "print_mode": "debug",
                "output_keys": None,
                "interrupt_before": ("model",),
                "interrupt_after": ("tools",),
                "durability": "sync",
                "control": None,
                "subgraphs": True,
                "debug": True,
                "version": "v2",
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
    part = {"type": "custom", "ns": (), "data": {"value": "native"}}
    _, graphs = _install_builder(monkeypatch, parts=(part,))
    runtime = _definition(TinkerFin()).new(identity=_identity())
    invalid_astream = cast(Callable[..., object], runtime.astream)

    with pytest.raises(TypeError):
        invalid_astream()

    stream = runtime.astream(_graph_input())
    assert await anext(stream) is part
    await stream.aclose()
    assert len(graphs[0].calls) == 1
    assert graphs[0].calls[0][0][1] == {
        "configurable": {
            "thread_id": "thread-1",
            "_tinkerfin_runtime_profile": "deepagents-v2",
        }
    }
    assert graphs[0].calls[0][1]["version"] == "v2"
    assert graphs[0].calls[0][1]["stream_mode"] == (
        "messages",
        "tasks",
        "values",
    )
    assert graphs[0].calls[0][1]["subgraphs"] is True


@pytest.mark.asyncio
async def test_native_v1_and_conflicting_thread_fail_before_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    part = _state_part()
    _, graphs = _install_builder(monkeypatch, parts=(part,))
    runtime = _definition(TinkerFin()).new(identity=_identity())
    invalid_astream = cast(Callable[..., object], runtime.astream)

    with pytest.raises(ValueError, match="requires version='v2'"):
        invalid_astream(_graph_input(), version="v1")
    with pytest.raises(ValueError, match="stream_mode"):
        invalid_astream(_graph_input(), stream_mode="messages")
    with pytest.raises(ValueError, match="stream_mode"):
        invalid_astream(_graph_input(), stream_mode=())
    with pytest.raises(ValueError, match="subgraphs"):
        invalid_astream(_graph_input(), subgraphs=False)
    with pytest.raises(ValueError, match="output_keys"):
        invalid_astream(_graph_input(), output_keys="messages")
    with pytest.raises(ValueError, match="must equal identity.thread_id"):
        invalid_astream(
            _graph_input(),
            {"configurable": {"thread_id": "thread-other"}},
        )

    assert graphs == []
    stream = runtime.astream(_graph_input(), _graph_config())
    assert await anext(stream) == part
    assert len(graphs) == 1
    await stream.aclose()


@pytest.mark.asyncio
async def test_agui_runtime_defaults_reserved_options_and_stays_lazy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, graphs = _install_builder(monkeypatch, parts=(_state_part(),))
    identity = _identity()
    runtime = _definition(TinkerFin()).new_agui(
        identity=identity,
    )
    stream = runtime.astream(
        _graph_input(),
        _graph_config(),
    )

    assert isinstance(stream, AgUiEventStream)
    assert graphs == []
    assert (await anext(stream)).type.value == "RUN_STARTED"
    assert len(graphs) == 1
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
    identity = _identity()
    runtime = _definition(TinkerFin()).new_agui(
        identity=identity,
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
        ({"output_keys": "messages"}, "output_keys"),
    ],
)
def test_invalid_agui_reserved_options_fail_before_every_stream_side_effect(
    monkeypatch: pytest.MonkeyPatch,
    options: dict[str, object],
    expected: str,
) -> None:
    coordination: list[RunIdentity] = []
    observed: list[object] = []

    @asynccontextmanager
    async def coordinate(identity: RunIdentity) -> AsyncIterator[None]:
        coordination.append(identity)
        yield

    async def on_part(part: object) -> None:
        observed.append(part)

    _, graphs = _install_builder(monkeypatch, parts=(_state_part(),))
    identity = _identity()
    runtime = _definition(TinkerFin(run_coordinator=coordinate)).new_agui(
        identity=identity,
        on_part=on_part,
    )
    invalid_astream = cast(Callable[..., object], runtime.astream)

    with pytest.raises((TypeError, ValueError), match=expected):
        invalid_astream(_graph_input(), **options)

    assert graphs == []
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
    identity = _identity()
    runtime = _definition(TinkerFin()).new_agui(
        identity=identity,
        on_part=on_part,
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
        "tinkerfin.runtime_profile._deepagents_graph.create_deep_agent",
        lambda *_args, **_kwargs: graph,
    )
    tinkerfin = TinkerFin()
    definition = _definition(tinkerfin)
    native = definition.new(identity=_identity(run_id="native-run"))
    agui_identity = _identity(run_id="agui-run")
    agui = definition.new_agui(
        identity=agui_identity,
    )
    upstream = inspect.signature(CompiledStateGraph.astream)
    bound_upstream = upstream.replace(
        parameters=tuple(upstream.parameters.values())[1:]
    )

    assert inspect.signature(tinkerfin.create_deep_agent) == inspect.signature(
        upstream_create_deep_agent
    )
    assert inspect.signature(native.astream) == bound_upstream
    assert inspect.signature(agui.astream) == bound_upstream


def test_runtime_astream_is_single_use_for_agui(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_builder(monkeypatch)
    identity = _identity()
    runtime = _definition(TinkerFin()).new_agui(
        identity=identity,
    )
    stream = runtime.astream(_graph_input())

    assert isinstance(stream, AgUiEventStream)
    with pytest.raises(RuntimeError, match="only one object stream"):
        runtime.astream(_graph_input())
