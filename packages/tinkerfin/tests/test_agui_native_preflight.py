"""Strict preflight contracts for high-level AG-UI native runs."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError, fields
from typing import TypedDict, cast, get_type_hints

import pytest
from langgraph.graph import START, StateGraph
from langgraph.types import StreamMode

from tinkerfin import (
    AgUiNativeStreamConfig,
    AgUiNativeStreamConfigurationError,
    AgUiNativeStreamInvocation,
    TinkerFin,
)


class _RecordingGraph:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.iterator_opened = 0
        self.iterator_pulled = 0
        self.iterator_closed = 0

    def astream(
        self,
        graph_input: object,
        graph_config: object | None = None,
        **options: object,
    ) -> AsyncIterator[Mapping[str, object]]:
        self.calls.append(
            {
                "graph_input": graph_input,
                "graph_config": graph_config,
                "options": options,
            }
        )

        async def parts() -> AsyncIterator[Mapping[str, object]]:
            self.iterator_opened += 1
            try:
                self.iterator_pulled += 1
                yield {
                    "type": "values",
                    "ns": (),
                    "data": {"answer": 42},
                    "interrupts": (),
                }
            finally:
                self.iterator_closed += 1

        return parts()


class _GraphState(TypedDict):
    value: int


async def _increment(state: _GraphState) -> dict[str, int]:
    return {"value": state["value"] + 1}


def _config(
    *,
    extra_modes: tuple[StreamMode, ...] = (),
) -> AgUiNativeStreamConfig:
    return AgUiNativeStreamConfig(extra_modes=extra_modes)


def test_agui_native_config_is_an_immutable_precise_value() -> None:
    config = _config()

    assert tuple(field.name for field in fields(config)) == ("extra_modes",)
    assert get_type_hints(AgUiNativeStreamConfig) == {
        "extra_modes": tuple[StreamMode, ...],
    }
    with pytest.raises(FrozenInstanceError):
        setattr(config, "extra_modes", ("custom",))


@pytest.mark.parametrize(
    ("extra_modes", "expected"),
    [
        (("messages",), "required"),
        (("tasks",), "required"),
        (("values",), "required"),
        (("custom", "custom"), "duplicate"),
        (cast(tuple[StreamMode, ...], ("unknown",)), "unsupported"),
        (cast(tuple[StreamMode, ...], (1,)), "unsupported"),
    ],
)
def test_invalid_agui_native_config_fails_before_every_runtime_side_effect(
    extra_modes: tuple[StreamMode, ...],
    expected: str,
) -> None:
    graph = _RecordingGraph()
    coordinator_entries = 0
    observed_parts: list[object] = []

    @asynccontextmanager
    async def coordinate(_: str) -> AsyncIterator[None]:
        nonlocal coordinator_entries
        coordinator_entries += 1
        yield

    async def on_part(part: object) -> None:
        observed_parts.append(part)

    invocation = _config(extra_modes=extra_modes).bind(
        graph.astream,
        {"messages": []},
        {"configurable": {"thread_id": "thread-1"}},
    )

    with pytest.raises(AgUiNativeStreamConfigurationError, match=expected):
        TinkerFin(run_coordinator=coordinate).run(
            invocation,
            principal="user-1",
            on_part=on_part,
        )

    assert graph.calls == []
    assert graph.iterator_opened == 0
    assert graph.iterator_pulled == 0
    assert graph.iterator_closed == 0
    assert coordinator_entries == 0
    assert observed_parts == []


@pytest.mark.parametrize(
    "extra_modes",
    [
        cast(tuple[StreamMode, ...], ["custom"]),
    ],
)
def test_agui_native_config_rejects_noncanonical_mode_collections(
    extra_modes: tuple[StreamMode, ...],
) -> None:
    graph = _RecordingGraph()
    invocation = _config(extra_modes=extra_modes).bind(graph.astream, {})

    with pytest.raises(AgUiNativeStreamConfigurationError, match="extra_modes"):
        TinkerFin().run(invocation)

    assert graph.calls == []


def test_invalid_strict_config_precedes_principal_validation() -> None:
    graph = _RecordingGraph()

    @asynccontextmanager
    async def coordinate(_: str) -> AsyncIterator[None]:
        yield

    invocation = _config(extra_modes=("messages",)).bind(graph.astream, {})

    with pytest.raises(AgUiNativeStreamConfigurationError, match="required"):
        TinkerFin(run_coordinator=coordinate).run(invocation)

    assert graph.calls == []


@pytest.mark.parametrize("reserved", ["stream_mode", "version", "subgraphs"])
def test_agui_native_invocation_rejects_config_option_shadowing(
    reserved: str,
) -> None:
    graph = _RecordingGraph()
    invocation = _config().bind(
        graph.astream,
        {},
        **{reserved: "caller-shadow"},
    )

    with pytest.raises(
        AgUiNativeStreamConfigurationError,
        match="cannot override stream configuration",
    ):
        TinkerFin().run(invocation)

    assert graph.calls == []


@pytest.mark.asyncio
async def test_valid_agui_native_config_is_forwarded_exactly_and_stays_lazy() -> None:
    graph = _RecordingGraph()
    coordination: list[str] = []
    observed_parts: list[object] = []
    graph_input = {"messages": []}
    graph_config = {"configurable": {"thread_id": "thread-1"}}
    config = _config(extra_modes=("updates", "checkpoints", "debug", "custom"))

    @asynccontextmanager
    async def coordinate(principal: str) -> AsyncIterator[None]:
        coordination.append(f"enter:{principal}")
        try:
            yield
        finally:
            coordination.append(f"exit:{principal}")

    async def on_part(part: object) -> None:
        observed_parts.append(part)

    invocation = config.bind(
        graph.astream,
        graph_input,
        graph_config,
        context={"tenant": "tenant-1"},
        debug=False,
    )
    assert isinstance(invocation, AgUiNativeStreamInvocation)
    run = TinkerFin(run_coordinator=coordinate).run(
        invocation,
        principal="user-1",
        on_part=on_part,
    )
    events = run.astream_agui(thread_id="thread-1", run_id="run-1")

    assert graph.calls == []
    assert coordination == []
    assert observed_parts == []

    started = await anext(events)
    assert started.type.value == "RUN_STARTED"
    assert graph.calls == []
    assert coordination == []

    state = await anext(events)
    assert state.type.value == "STATE_SNAPSHOT"
    assert graph.calls == [
        {
            "graph_input": graph_input,
            "graph_config": graph_config,
            "options": {
                "context": {"tenant": "tenant-1"},
                "debug": False,
                "stream_mode": (
                    "messages",
                    "tasks",
                    "values",
                    "updates",
                    "checkpoints",
                    "debug",
                    "custom",
                ),
                "version": "v2",
                "subgraphs": True,
            },
        }
    ]
    assert coordination == ["enter:user-1"]
    assert observed_parts == [
        {
            "type": "values",
            "ns": (),
            "data": {"answer": 42},
            "interrupts": (),
        }
    ]

    finished = await anext(events)
    assert finished.type.value == "RUN_FINISHED"
    with pytest.raises(StopAsyncIteration):
        await anext(events)
    assert coordination == ["enter:user-1", "exit:user-1"]
    assert graph.iterator_opened == 1
    assert graph.iterator_pulled == 1
    assert graph.iterator_closed == 1


@pytest.mark.asyncio
async def test_strict_agui_run_preserves_pull_backpressure_and_cancellation_cleanup() -> (
    None
):
    second_pull_started = asyncio.Event()
    release_second = asyncio.Event()
    iterator_closed = asyncio.Event()
    coordination: list[str] = []

    def astream(**options: object) -> AsyncIterator[Mapping[str, object]]:
        assert options == {
            "stream_mode": ("messages", "tasks", "values"),
            "version": "v2",
            "subgraphs": True,
        }

        async def parts() -> AsyncIterator[Mapping[str, object]]:
            try:
                yield {
                    "type": "values",
                    "ns": (),
                    "data": {"step": 1},
                    "interrupts": (),
                }
                second_pull_started.set()
                await release_second.wait()
                yield {
                    "type": "values",
                    "ns": (),
                    "data": {"step": 2},
                    "interrupts": (),
                }
            finally:
                iterator_closed.set()

        return parts()

    @asynccontextmanager
    async def coordinate(principal: str) -> AsyncIterator[None]:
        coordination.append(f"enter:{principal}")
        try:
            yield
        finally:
            coordination.append(f"exit:{principal}")

    invocation = _config().bind(astream)
    events = (
        TinkerFin(run_coordinator=coordinate)
        .run(
            invocation,
            principal="user-1",
        )
        .astream_agui(thread_id="thread-1", run_id="run-1")
    )

    assert (await anext(events)).type.value == "RUN_STARTED"
    assert (await anext(events)).type.value == "STATE_SNAPSHOT"
    assert not second_pull_started.is_set()

    pending = asyncio.create_task(anext(events))
    await second_pull_started.wait()
    assert not pending.done()
    pending.cancel("request disconnected")
    with pytest.raises(asyncio.CancelledError, match="request disconnected"):
        await pending

    assert iterator_closed.is_set()
    assert coordination == ["enter:user-1", "exit:user-1"]


@pytest.mark.asyncio
async def test_strict_agui_binding_runs_a_real_compiled_langgraph_v2_stream() -> None:
    builder = StateGraph(_GraphState)
    builder.add_node("increment", _increment)
    builder.add_edge(START, "increment")
    graph = builder.compile()
    invocation = _config().bind(graph.astream, {"value": 1})
    stream = (
        TinkerFin()
        .run(invocation)
        .astream_agui(
            thread_id="thread-1",
            run_id="run-1",
        )
    )

    events = [event async for event in stream]

    event_types = [event.type.value for event in events]
    assert event_types[0] == "RUN_STARTED"
    assert "STATE_SNAPSHOT" in event_types
    assert event_types[-1] == "RUN_FINISHED"
    assert "RUN_ERROR" not in event_types
    assert stream.error is None
