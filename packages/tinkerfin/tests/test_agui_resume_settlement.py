"""Durable resume checkpoint callback and retry contracts."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any, TypedDict, cast

import pytest
from ag_ui.core import BaseEvent
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

import tinkerfin.deep_agent as deep_agent_module
from tinkerfin import (
    AgUiResumeBinding,
    AgUiResumeCheckpoint,
    Identity,
    TinkerFin,
)
from tinkerfin._agui_lineage_state import LINEAGE_STATE_KEY
from tinkerfin.agui_resume import RESUME_MARKER_STATE_KEY


class _ResumeState(TypedDict, total=False):
    _tinkerfin_lineage: dict[str, object]
    _tinkerfin_resume: dict[str, object]
    result: str


def _install_resume_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[
    MemorySaver,
    list[CompiledStateGraph[Any, Any, Any, Any]],
    list[object],
]:
    saver = MemorySaver()
    graphs: list[CompiledStateGraph[Any, Any, Any, Any]] = []
    executions: list[object] = []

    def build(*_args: object, **_kwargs: object):
        async def reviewed(_state: _ResumeState) -> dict[str, object]:
            answer = interrupt({"question": "continue?"})
            executions.append(answer)
            return {"result": "done"}

        builder = StateGraph(_ResumeState)
        builder.add_node("reviewed", reviewed)
        builder.add_edge(START, "reviewed")
        builder.add_edge("reviewed", END)
        graph = builder.compile(checkpointer=saver)
        graphs.append(graph)
        return graph

    monkeypatch.setattr(deep_agent_module, "_native_create_deep_agent", build)
    return saver, graphs, executions


async def _create_interrupted_parent(
    definition: object,
    graphs: list[CompiledStateGraph[Any, Any, Any, Any]],
) -> str:
    identity = Identity(threadId="thread-1", runId="run-parent")
    runtime = cast(Any, definition).new_agui(
        identity=identity,
    )
    events = [event async for event in runtime.astream({})]
    assert events[-1].type.value == "RUN_FINISHED"
    snapshot = await graphs[-1].aget_state(
        {"configurable": {"thread_id": identity.thread_id}}
    )
    assert len(snapshot.interrupts) == 1
    return snapshot.interrupts[0].id


def _resume_binding(interrupt_id: str) -> tuple[Identity, AgUiResumeBinding]:
    identity = Identity(threadId="thread-1", runId="run-resume")
    return identity, AgUiResumeBinding(
        mode="resume",
        resume_data={"answer": "continue"},
        native_interrupt_ids=(interrupt_id,),
    )


def _assert_private_marker_absent(events: list[BaseEvent]) -> None:
    serialized = json.dumps(
        [
            event.model_dump(mode="json", by_alias=True, exclude_none=False)
            for event in events
        ],
        ensure_ascii=False,
    )
    assert RESUME_MARKER_STATE_KEY not in serialized
    assert LINEAGE_STATE_KEY not in serialized


@pytest.mark.asyncio
async def test_checkpoint_callback_failure_retries_without_reexecuting_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _saver, graphs, executions = _install_resume_graph(monkeypatch)
    definition = TinkerFin().create_deep_agent(model="provider:model", tools=[])
    interrupt_id = await _create_interrupted_parent(definition, graphs)
    identity, binding = _resume_binding(interrupt_id)
    checkpoints: list[AgUiResumeCheckpoint] = []

    async def fail_after_recording(checkpoint: AgUiResumeCheckpoint) -> None:
        checkpoints.append(checkpoint)
        raise RuntimeError("checkpoint observer unavailable")

    failed_runtime = cast(Any, definition).new_agui(
        identity=identity,
        parent_run_id="run-parent",
        resume=binding,
        on_resume_checkpointed=fail_after_recording,
    )
    failed_stream = failed_runtime.astream()
    failed_events = [event async for event in failed_stream]

    assert [event.type.value for event in failed_events] == [
        "RUN_STARTED",
        "RUN_ERROR",
    ]
    assert isinstance(failed_stream.error, RuntimeError)
    assert executions == []
    assert len(checkpoints) == 1
    _assert_private_marker_absent(failed_events)

    trace: list[str] = []

    async def checkpointed(checkpoint: AgUiResumeCheckpoint) -> None:
        checkpoints.append(checkpoint)
        trace.append("checkpointed")

    async def observe(_part: Mapping[str, object]) -> None:
        trace.append("part")

    retry_runtime = cast(Any, definition).new_agui(
        identity=identity,
        parent_run_id="run-parent",
        resume=binding,
        on_resume_checkpointed=checkpointed,
        on_part=observe,
    )
    retry_stream = retry_runtime.astream()
    retry_events = [event async for event in retry_stream]

    assert retry_events[-1].type.value == "RUN_FINISHED"
    assert retry_stream.error is None
    assert executions == [{"answer": "continue"}]
    assert checkpoints[0] == checkpoints[1]
    assert trace[0] == "checkpointed"
    assert "part" in trace
    _assert_private_marker_absent(retry_events)


@pytest.mark.asyncio
async def test_close_during_checkpoint_callback_keeps_exactly_once_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _saver, graphs, executions = _install_resume_graph(monkeypatch)
    definition = TinkerFin().create_deep_agent(model="provider:model", tools=[])
    interrupt_id = await _create_interrupted_parent(definition, graphs)
    identity, binding = _resume_binding(interrupt_id)
    checkpoints: list[AgUiResumeCheckpoint] = []
    checkpoint_entered = asyncio.Event()

    async def checkpointed(checkpoint: AgUiResumeCheckpoint) -> None:
        checkpoints.append(checkpoint)
        checkpoint_entered.set()
        await asyncio.Event().wait()

    runtime = cast(Any, definition).new_agui(
        identity=identity,
        parent_run_id="run-parent",
        resume=binding,
        on_resume_checkpointed=checkpointed,
    )
    stream = runtime.astream()
    first = await anext(stream)
    assert first.type.value == "RUN_STARTED"
    next_event = asyncio.create_task(anext(stream))
    await asyncio.wait_for(checkpoint_entered.wait(), timeout=5)
    assert len(checkpoints) == 1
    assert executions == []
    next_event.cancel()
    outcome = (await asyncio.gather(next_event, return_exceptions=True))[0]
    assert isinstance(outcome, asyncio.CancelledError)
    await stream.aclose()
    assert executions == []

    async def checkpoint_retry(checkpoint: AgUiResumeCheckpoint) -> None:
        checkpoints.append(checkpoint)

    retry = cast(Any, definition).new_agui(
        identity=identity,
        parent_run_id="run-parent",
        resume=binding,
        on_resume_checkpointed=checkpoint_retry,
    )
    events = [event async for event in retry.astream()]

    assert events[-1].type.value == "RUN_FINISHED"
    assert executions == [{"answer": "continue"}]
    assert len(checkpoints) == 2
    assert checkpoints[0] == checkpoints[1]
    _assert_private_marker_absent(events)
