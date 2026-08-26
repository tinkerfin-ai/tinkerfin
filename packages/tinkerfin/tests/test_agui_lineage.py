"""AG-UI canonical identity and durable checkpoint branching contracts."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from operator import add
from typing import Annotated, Any, TypedDict, cast

import pytest
from ag_ui.core import BaseEvent, RunErrorEvent, RunStartedEvent
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

import tinkerfin.deep_agent as deep_agent_module
from tinkerfin import AgUiResumeBinding, Identity, TinkerFin
from tinkerfin._agui_lineage import (
    CHECKPOINT_ROLE_METADATA_KEY,
    NATIVE_CHECKPOINT_ROLE,
    PARENT_RUN_ID_METADATA_KEY,
    RUN_ID_METADATA_KEY,
)
from tinkerfin._agui_lineage_state import (
    PLANNING_CHECKPOINT_RUN_ID,
    lineage_state_update,
)
from tinkerfin.errors import TinkerFinLifecycleError


class _BranchState(TypedDict, total=False):
    history: Annotated[list[str], add]
    _tinkerfin_lineage: dict[str, object]
    _tinkerfin_resume: dict[str, object]


def _install_branch_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[MemorySaver, list[CompiledStateGraph[Any, Any, Any, Any]]]:
    saver = MemorySaver()
    graphs: list[CompiledStateGraph[Any, Any, Any, Any]] = []

    def build(*_args: object, **_kwargs: object):
        async def record(state: _BranchState) -> dict[str, object]:
            latest = state.get("history", [""])[-1]
            if latest == "fail":
                raise RuntimeError("branch fixture failure")
            if latest == "pause":
                interrupt({"kind": "branch-fixture"})
            return {}

        builder = StateGraph(_BranchState)
        builder.add_node("record", record)
        builder.add_edge(START, "record")
        builder.add_edge("record", END)
        graph = builder.compile(checkpointer=saver)
        graphs.append(graph)
        return graph

    monkeypatch.setattr(deep_agent_module, "_native_create_deep_agent", build)
    return saver, graphs


async def _run(
    definition: object,
    *,
    run_id: str,
    value: str | None,
    parent_run_id: str | None = None,
    resume: AgUiResumeBinding | None = None,
) -> tuple[list[BaseEvent], object]:
    identity = Identity(threadId="thread-1", runId=run_id)
    runtime = cast(Any, definition).new_agui(
        identity=identity,
        parent_run_id=parent_run_id,
        resume=resume,
    )
    stream = (
        runtime.astream()
        if resume is not None
        else runtime.astream({"history": [cast(str, value)]})
    )
    return [event async for event in stream], stream


async def _run_head(
    saver: MemorySaver,
    *,
    run_id: str,
):
    rows = [
        row
        async for row in saver.alist(
            {
                "configurable": {
                    "thread_id": "thread-1",
                    "checkpoint_ns": "",
                }
            },
            filter={RUN_ID_METADATA_KEY: run_id},
        )
    ]
    assert rows
    return rows[0]


@pytest.mark.asyncio
async def test_parent_run_id_creates_a_real_branch_with_one_canonical_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saver, graphs = _install_branch_graph(monkeypatch)
    definition = TinkerFin().create_deep_agent(model="provider:model", tools=[])

    await _run(definition, run_id="run-a", value="A")
    await _run(definition, run_id="run-b", value="B")
    events, _stream = await _run(
        definition,
        run_id="run-c",
        value="C",
        parent_run_id="run-a",
    )

    b_head = await _run_head(saver, run_id="run-b")
    c_head = await _run_head(saver, run_id="run-c")
    b_state = await graphs[1].aget_state(b_head.config)
    c_state = await graphs[2].aget_state(c_head.config)
    started = cast(RunStartedEvent, events[0])

    assert b_state.values["history"] == ["A", "B"]
    assert c_state.values["history"] == ["A", "C"]
    assert started.thread_id == "thread-1"
    assert started.run_id == "run-c"
    assert started.parent_run_id == "run-a"
    assert started.input is None
    metadata = dict[str, object](c_head.metadata)
    assert metadata[CHECKPOINT_ROLE_METADATA_KEY] == NATIVE_CHECKPOINT_ROLE
    assert metadata[PARENT_RUN_ID_METADATA_KEY] == "run-a"


@pytest.mark.asyncio
async def test_lineage_uses_portable_run_index_without_private_metadata_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saver, _graphs = _install_branch_graph(monkeypatch)
    definition = TinkerFin().create_deep_agent(model="provider:model", tools=[])
    await _run(definition, run_id="run-parent", value="parent")
    original_alist = MemorySaver.alist
    indexed_run_ids: list[object] = []

    async def strict_alist(
        self: MemorySaver,
        config: Any,
        *,
        filter: dict[str, Any] | None = None,
        before: Any = None,
        limit: int | None = None,
    ):
        if filter is not None:
            raise ValueError("private metadata filters are unsupported")
        indexed_run_ids.append(config["configurable"].get("run_id"))
        async for checkpoint in original_alist(
            self,
            config,
            filter=None,
            before=before,
            limit=limit,
        ):
            yield checkpoint

    monkeypatch.setattr(MemorySaver, "alist", strict_alist)
    events, stream = await _run(
        definition,
        run_id="run-child",
        value="child",
        parent_run_id="run-parent",
    )

    assert events[-1].type.value == "RUN_FINISHED"
    assert cast(Any, stream).error is None
    assert indexed_run_ids == ["run-parent", PLANNING_CHECKPOINT_RUN_ID]


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_run_id", ["missing", "run-other-thread"])
async def test_missing_or_cross_execution_parent_fails_before_graph_invocation(
    monkeypatch: pytest.MonkeyPatch,
    parent_run_id: str,
) -> None:
    saver, _graphs = _install_branch_graph(monkeypatch)
    definition = TinkerFin().create_deep_agent(model="provider:model", tools=[])
    if parent_run_id == "run-other-thread":
        graph = cast(Any, definition)._build_astream("default").__self__
        async for _part in graph.astream(
            {"history": ["other"]},
            config={
                "configurable": {
                    "thread_id": "other-thread",
                    RUN_ID_METADATA_KEY: parent_run_id,
                    CHECKPOINT_ROLE_METADATA_KEY: NATIVE_CHECKPOINT_ROLE,
                }
            },
            stream_mode="values",
            version="v2",
        ):
            pass

    events, stream = await _run(
        definition,
        run_id="run-child",
        value="child",
        parent_run_id=parent_run_id,
    )

    assert [event.type.value for event in events] == ["RUN_STARTED", "RUN_ERROR"]
    assert isinstance(events[-1], RunErrorEvent)
    assert isinstance(cast(Any, stream).error, TinkerFinLifecycleError)
    child_rows = [
        row
        async for row in saver.alist(
            {"configurable": {"thread_id": "thread-1"}},
            filter={RUN_ID_METADATA_KEY: "run-child"},
        )
    ]
    assert child_rows == []


@pytest.mark.asyncio
async def test_failed_and_ambiguous_parents_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _saver, _graphs = _install_branch_graph(monkeypatch)
    definition = TinkerFin().create_deep_agent(model="provider:model", tools=[])

    failed_events, _failed_stream = await _run(
        definition,
        run_id="run-failed",
        value="fail",
    )
    assert failed_events[-1].type.value == "RUN_ERROR"
    child_events, child_stream = await _run(
        definition,
        run_id="run-after-failure",
        value="child",
        parent_run_id="run-failed",
    )
    assert child_events[-1].type.value == "RUN_ERROR"
    assert "failed checkpoint" in str(cast(Any, child_stream).error)

    await _run(definition, run_id="run-root", value="root")
    await _run(
        definition,
        run_id="run-duplicate",
        value="left",
        parent_run_id="run-root",
    )
    await _run(
        definition,
        run_id="run-duplicate",
        value="right",
        parent_run_id="run-root",
    )
    ambiguous_events, ambiguous_stream = await _run(
        definition,
        run_id="run-after-duplicate",
        value="child",
        parent_run_id="run-duplicate",
    )
    assert ambiguous_events[-1].type.value == "RUN_ERROR"
    assert "ambiguous" in str(cast(Any, ambiguous_stream).error)


@pytest.mark.asyncio
async def test_interrupted_parent_requires_the_exact_resume_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saver, _graphs = _install_branch_graph(monkeypatch)
    definition = TinkerFin().create_deep_agent(model="provider:model", tools=[])
    interrupted_events, _stream = await _run(
        definition,
        run_id="run-paused",
        value="pause",
    )
    assert interrupted_events[-1].type.value == "RUN_FINISHED"
    paused_head = await _run_head(saver, run_id="run-paused")
    native_ids = frozenset(
        item.id
        for _task_id, _channel, value in paused_head.pending_writes or ()
        if isinstance(value, list)
        for item in value
        if hasattr(item, "id")
    )
    assert native_ids

    wrong = AgUiResumeBinding(
        mode="resume",
        resume_data={"answer": "continue"},
        native_interrupt_ids=("wrong-interrupt",),
    )
    wrong_events, wrong_stream = await _run(
        definition,
        run_id="run-wrong",
        value=None,
        parent_run_id="run-paused",
        resume=wrong,
    )
    assert wrong_events[-1].type.value == "RUN_ERROR"
    assert "exact interrupts" in str(cast(Any, wrong_stream).error)

    binding = AgUiResumeBinding(
        mode="resume",
        resume_data={"answer": "continue"},
        native_interrupt_ids=tuple(native_ids),
    )
    resumed_events, resumed_stream = await _run(
        definition,
        run_id="run-resume",
        value=None,
        parent_run_id="run-paused",
        resume=binding,
    )
    assert resumed_events[-1].type.value == "RUN_FINISHED"
    assert cast(Any, resumed_stream).error is None


@pytest.mark.asyncio
async def test_active_parent_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _saver, graphs = _install_branch_graph(monkeypatch)
    definition = TinkerFin().create_deep_agent(model="provider:model", tools=[])
    cast(Any, definition)._build_astream("default")
    active_graph = graphs[-1]
    active_identity = Identity(threadId="thread-1", runId="run-active")
    active_stream = active_graph.astream(
        {
            "history": ["active"],
            **lineage_state_update(
                identity=active_identity,
                parent_run_id=None,
                role="native",
            ),
        },
        config={
            "configurable": {
                "thread_id": "thread-1",
                RUN_ID_METADATA_KEY: "run-active",
                CHECKPOINT_ROLE_METADATA_KEY: NATIVE_CHECKPOINT_ROLE,
            }
        },
        stream_mode="values",
        version="v2",
        durability="sync",
    )
    assert isinstance(active_stream, AsyncGenerator)
    await anext(active_stream)
    try:
        events, stream = await _run(
            definition,
            run_id="run-after-active",
            value="child",
            parent_run_id="run-active",
        )
        assert events[-1].type.value == "RUN_ERROR"
        assert "active parent" in str(cast(Any, stream).error)
    finally:
        await active_stream.aclose()
