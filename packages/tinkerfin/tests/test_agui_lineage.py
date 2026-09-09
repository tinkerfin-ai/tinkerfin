"""AG-UI canonical identity and durable checkpoint branching contracts."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from operator import add
from typing import Annotated, Any, cast

import pytest
from ag_ui.core import BaseEvent, RunErrorEvent, RunStartedEvent
from langchain.agents.middleware.types import InputAgentState
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import CheckpointTuple
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from tinkerfin import (
    AgUiResumeBinding,
    DeepAgentsV2RuntimeProfile,
    RunIdentity,
    TinkerFin,
)
from tinkerfin._agui_lineage import (
    CHECKPOINT_ROLE_METADATA_KEY,
    NATIVE_CHECKPOINT_ROLE,
    PARENT_RUN_ID_METADATA_KEY,
    RUN_ID_METADATA_KEY,
    RUNTIME_PROFILE_METADATA_KEY,
    _resume_stage_and_source,
)
from tinkerfin._agui_lineage_state import (
    PLANNING_CHECKPOINT_RUN_ID,
    RESUME_MARKER_STATE_KEY,
    lineage_state_update,
)
from tinkerfin.errors import TinkerFinLifecycleError


def test_lineage_marker_uses_the_current_unversioned_contract() -> None:
    payload = lineage_state_update(
        identity=RunIdentity(threadId="thread-1", runId="run-1"),
        parent_run_id=None,
        runtime_profile="deepagents-v2",
        role="native",
    )["_tinkerfin_lineage"]

    assert PLANNING_CHECKPOINT_RUN_ID == "tinkerfin-plan"
    assert set(payload) == {
        "threadId",
        "runId",
        "parentRunId",
        "runtimeProfile",
        "role",
    }


class _BranchState(InputAgentState, total=False):
    history: Annotated[list[str], add]
    _tinkerfin_lineage: dict[str, object]
    _tinkerfin_resume: dict[str, object]


def _install_branch_graph(
    monkeypatch: pytest.MonkeyPatch,
    *,
    executions: list[str] | None = None,
) -> tuple[MemorySaver, list[CompiledStateGraph[Any, Any, Any, Any]]]:
    saver = MemorySaver()
    graphs: list[CompiledStateGraph[Any, Any, Any, Any]] = []

    def build(*_args: object, **_kwargs: object):
        async def record(state: _BranchState) -> dict[str, object]:
            latest = state.get("history", [""])[-1]
            if executions is not None:
                executions.append(latest)
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

    monkeypatch.setattr(
        "tinkerfin.runtime_profile._deepagents_graph.create_deep_agent",
        build,
    )
    return saver, graphs


def _branch_input(value: str) -> _BranchState:
    return {"messages": [], "history": [value]}


class _DifferentFixtureProfile(DeepAgentsV2RuntimeProfile):
    """Give the v2 fixture a distinct integration identity for lineage tests."""

    @property
    def profile_id(self) -> str:
        """Return a non-production identity without pretending to implement v3."""

        return "fixture-different-profile"


class _MappedCheckpointSaver(MemorySaver):
    """Return exact synthetic parent rows for corrupt-ancestry contract tests."""

    def __init__(self, checkpoints: dict[str, CheckpointTuple]) -> None:
        super().__init__()
        self._checkpoints = checkpoints

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        checkpoint_id = config.get("configurable", {}).get("checkpoint_id")
        return (
            self._checkpoints.get(checkpoint_id)
            if isinstance(checkpoint_id, str)
            else None
        )


def _checkpoint_config(checkpoint_id: str) -> RunnableConfig:
    return {
        "configurable": {
            "thread_id": "thread-cycle",
            "checkpoint_ns": "",
            "checkpoint_id": checkpoint_id,
        }
    }


def _committed_resume_checkpoint(
    checkpoint_id: str,
    *,
    parent_id: str | None,
    marker: object,
) -> CheckpointTuple:
    return CheckpointTuple(
        config=_checkpoint_config(checkpoint_id),
        checkpoint=cast(
            Any,
            {"channel_values": {RESUME_MARKER_STATE_KEY: marker}},
        ),
        metadata=cast(Any, {}),
        parent_config=(None if parent_id is None else _checkpoint_config(parent_id)),
        pending_writes=[],
    )


def _resume_marker_fixture() -> tuple[RunIdentity, object]:
    identity = RunIdentity(threadId="thread-cycle", runId="run-resume")
    binding = AgUiResumeBinding(
        mode="resume",
        resume_data={"answer": "continue"},
        native_interrupt_ids=("interrupt-1",),
    )
    marker = binding._marker(identity=identity, parent_run_id="run-parent")
    return identity, marker


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("parents", "head_id"),
    [
        ({"a": "a"}, "a"),
        ({"a": "b", "b": "a"}, "a"),
    ],
)
async def test_resume_ancestry_rejects_checkpoint_cycles(
    parents: dict[str, str],
    head_id: str,
) -> None:
    identity, marker = _resume_marker_fixture()
    rows = {
        checkpoint_id: _committed_resume_checkpoint(
            checkpoint_id,
            parent_id=parent_id,
            marker=cast(Any, marker).model_dump(mode="json", by_alias=True),
        )
        for checkpoint_id, parent_id in parents.items()
    }
    saver = _MappedCheckpointSaver(rows)

    with pytest.raises(TinkerFinLifecycleError, match="checkpoint cycle"):
        await _resume_stage_and_source(
            saver,
            head=rows[head_id],
            identity=identity,
            marker=cast(Any, marker),
            runtime_profile=DeepAgentsV2RuntimeProfile(),
        )


@pytest.mark.asyncio
async def test_resume_ancestry_rejects_an_unbounded_checkpoint_chain() -> None:
    identity, marker = _resume_marker_fixture()
    serialized = cast(Any, marker).model_dump(mode="json", by_alias=True)
    rows = {
        f"checkpoint-{index}": _committed_resume_checkpoint(
            f"checkpoint-{index}",
            parent_id=(None if index == 4096 else f"checkpoint-{index + 1}"),
            marker=serialized,
        )
        for index in range(4097)
    }
    saver = _MappedCheckpointSaver(rows)

    with pytest.raises(TinkerFinLifecycleError, match="safe checkpoint depth"):
        await _resume_stage_and_source(
            saver,
            head=rows["checkpoint-0"],
            identity=identity,
            marker=cast(Any, marker),
            runtime_profile=DeepAgentsV2RuntimeProfile(),
        )


async def _run(
    definition: object,
    *,
    run_id: str,
    value: str | None,
    parent_run_id: str | None = None,
    resume: AgUiResumeBinding | None = None,
) -> tuple[list[BaseEvent], object]:
    identity = RunIdentity(threadId="thread-1", runId=run_id)
    runtime = cast(Any, definition).new_agui(
        identity=identity,
        parent_run_id=parent_run_id,
        resume=resume,
    )
    stream = (
        runtime.astream()
        if resume is not None
        else runtime.astream(_branch_input(cast(str, value)))
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
        graph = await definition.create_graph()
        async for _part in graph.astream(
            _branch_input("other"),
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
async def test_branch_rejects_a_checkpoint_from_another_runtime_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executions: list[str] = []
    saver, _graphs = _install_branch_graph(monkeypatch, executions=executions)
    source = TinkerFin().create_deep_agent(model="provider:model", tools=[])
    await _run(source, run_id="run-profile-source", value="source")

    mismatched = TinkerFin(
        runtime_profile=_DifferentFixtureProfile(),
    ).create_deep_agent(model="provider:model", tools=[])
    events, stream = await _run(
        mismatched,
        run_id="run-profile-child",
        value="must-not-run",
        parent_run_id="run-profile-source",
    )

    assert [event.type.value for event in events] == ["RUN_STARTED", "RUN_ERROR"]
    assert isinstance(cast(Any, stream).error, TinkerFinLifecycleError)
    assert "another Runtime Profile" in str(cast(Any, stream).error)
    assert executions == ["source"]
    child_rows = [
        row
        async for row in saver.alist(
            {"configurable": {"thread_id": "thread-1"}},
            filter={RUN_ID_METADATA_KEY: "run-profile-child"},
        )
    ]
    assert child_rows == []


@pytest.mark.asyncio
async def test_resume_rejects_a_checkpoint_from_another_runtime_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executions: list[str] = []
    saver, _graphs = _install_branch_graph(monkeypatch, executions=executions)
    source = TinkerFin().create_deep_agent(model="provider:model", tools=[])
    interrupted_events, _source_stream = await _run(
        source,
        run_id="run-profile-paused",
        value="pause",
    )
    assert interrupted_events[-1].type.value == "RUN_FINISHED"
    paused_head = await _run_head(saver, run_id="run-profile-paused")
    native_ids = tuple(
        item.id
        for _task_id, _channel, value in paused_head.pending_writes or ()
        if isinstance(value, list)
        for item in value
        if hasattr(item, "id")
    )
    assert native_ids

    mismatched = TinkerFin(
        runtime_profile=_DifferentFixtureProfile(),
    ).create_deep_agent(model="provider:model", tools=[])
    binding = AgUiResumeBinding(
        mode="resume",
        resume_data={"answer": "continue"},
        native_interrupt_ids=native_ids,
    )
    events, stream = await _run(
        mismatched,
        run_id="run-profile-resume",
        value=None,
        parent_run_id="run-profile-paused",
        resume=binding,
    )

    assert [event.type.value for event in events] == ["RUN_STARTED", "RUN_ERROR"]
    assert isinstance(cast(Any, stream).error, TinkerFinLifecycleError)
    assert "another Runtime Profile" in str(cast(Any, stream).error)
    assert executions == ["pause"]
    resumed_rows = [
        row
        async for row in saver.alist(
            {"configurable": {"thread_id": "thread-1"}},
            filter={RUN_ID_METADATA_KEY: "run-profile-resume"},
        )
    ]
    assert resumed_rows == []


@pytest.mark.asyncio
async def test_active_parent_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _saver, graphs = _install_branch_graph(monkeypatch)
    definition = TinkerFin().create_deep_agent(model="provider:model", tools=[])
    await definition.create_graph()
    active_graph = graphs[-1]
    active_identity = RunIdentity(threadId="thread-1", runId="run-active")
    active_stream = active_graph.astream(
        {
            "history": ["active"],
            **lineage_state_update(
                identity=active_identity,
                parent_run_id=None,
                runtime_profile="deepagents-v2",
                role="native",
            ),
        },
        config={
            "configurable": {
                "thread_id": "thread-1",
                RUN_ID_METADATA_KEY: "run-active",
                RUNTIME_PROFILE_METADATA_KEY: "deepagents-v2",
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
