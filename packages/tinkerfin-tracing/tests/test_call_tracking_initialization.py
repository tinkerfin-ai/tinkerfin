"""Call history remains known across failures that precede Agent execution."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import pytest
from langchain.agents.middleware.types import InputAgentState
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import BaseTool
from sqlalchemy.ext.asyncio import create_async_engine

from tinkerfin import DeepAgentDefinition, TinkerFin
from tinkerfin_contracts import (
    NativeTaskObservation,
    RunClosedObservation,
    RunIdentity,
    RunInputObservation,
    RunSourceContext,
    RunStartedObservation,
    RunTerminalObservation,
)
from tinkerfin_tracing import (
    CallTrackingFact,
    InMemoryTraceStore,
    RunFact,
    SqlAlchemyTraceStore,
    TraceGraphFilter,
    TraceGraphNodeKind,
    Tracer,
    TraceStore,
    TraceThreadNotFound,
)


class _LocalModel(FakeMessagesListChatModel):
    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        **kwargs: Any,
    ) -> _LocalModel:
        del tools, kwargs
        return self


@pytest.fixture(
    params=(
        "memory",
        "sqlite",
        pytest.param("mysql", marks=pytest.mark.docker_integration),
    )
)
async def trace_store(
    request: pytest.FixtureRequest, tmp_path: Path
) -> AsyncIterator[TraceStore]:
    if request.param == "memory":
        yield InMemoryTraceStore()
        return
    url = (
        request.getfixturevalue("mysql_admin_url")
        if request.param == "mysql"
        else f"sqlite+aiosqlite:///{tmp_path / 'call-history.db'}"
    )
    assert isinstance(url, str)
    engine = create_async_engine(url)
    try:
        yield SqlAlchemyTraceStore(engine, namespace=f"call-history-{uuid4().hex}")
    finally:
        await engine.dispose()


def _input(identity: RunIdentity) -> InputAgentState:
    return InputAgentState(
        messages=[HumanMessage(id=f"user-{identity.run_id}", content="local test")]
    )


async def _run_healthy(runtime: TinkerFin, identity: RunIdentity) -> None:
    definition = runtime.create_deep_agent(
        model=_LocalModel(
            responses=[
                AIMessage(id=f"assistant-{identity.run_id}", content="local reply")
            ]
        ),
        tools=[],
        system_prompt="Local test",
    )
    stream = await runtime.open_run(identity, agent=definition, input=_input(identity))
    try:
        async for _ in stream:
            pass
    finally:
        await stream.aclose()


@pytest.mark.parametrize("transport", ("native", "agui"))
async def test_initialization_failure_keeps_existing_and_future_call_history(
    trace_store: TraceStore, transport: Literal["native", "agui"]
) -> None:
    tracer = Tracer(store=trace_store)
    runtime = TinkerFin().observe(tracer)
    first = RunIdentity(threadId="call-history", runId="healthy-first")
    failed = RunIdentity(threadId=first.thread_id, runId="initialization-failed")
    last = RunIdentity(threadId=first.thread_id, runId="healthy-last")
    failure = RuntimeError("test-owned initialization failure")

    async def create_agent() -> DeepAgentDefinition[None]:
        raise failure

    await _run_healthy(runtime, first)
    before = await tracer.query(first.thread_id)
    original_models = {
        node.id for node in before.nodes if node.kind is TraceGraphNodeKind.MODEL
    }
    assert len(original_models) == 1
    assert not before.completeness.call_tracking_missing
    if transport == "native":
        native = await runtime.open_run(
            failed, agent=create_agent, input=_input(failed)
        )
        try:
            with pytest.raises(RuntimeError) as captured:
                async for _ in native:
                    raise AssertionError("initialization failure emitted native data")
            assert captured.value is failure
            assert native.error is failure
        finally:
            await native.aclose()
    else:
        agui = await runtime.open_agui_run(
            failed, agent=create_agent, input=_input(failed)
        )
        try:
            lifecycle = [item.type.value async for item in agui]
            assert lifecycle == ["RUN_STARTED", "RUN_ERROR"]
            assert agui.error is failure
        finally:
            await agui.aclose()

    snapshot = await trace_store.snapshot(first.thread_id)
    events = await trace_store.read_events(
        snapshot.key, after_seq=0, as_of_seq=snapshot.as_of_seq, limit=1000
    )
    failed_facts = [event.fact for event in events if event.fact.identity == failed]
    assert not any(isinstance(fact, CallTrackingFact) for fact in failed_facts)
    assert not any(
        fact.kind in {"model.call", "tool.execution", "tool"} for fact in failed_facts
    )
    phases = [fact.phase for fact in failed_facts if isinstance(fact, RunFact)]
    assert phases == ["started", "input", "terminal", "closed"]
    terminal = next(
        fact
        for fact in failed_facts
        if isinstance(fact, RunFact) and fact.phase == "terminal"
    )
    assert terminal.code == "runtime_initialization_error"
    assert terminal.outcome == "failed"
    assert terminal.error_type == "builtins.RuntimeError"

    after_failure = await tracer.query(first.thread_id)
    assert not after_failure.completeness.call_tracking_missing
    assert original_models <= {node.id for node in after_failure.nodes}
    assert any(node.run_id == failed.run_id for node in after_failure.nodes)
    response = await tracer.query(
        first.thread_id,
        where=TraceGraphFilter(model_call_id=next(iter(original_models))),
    )
    assert response.nodes
    assert not response.completeness.call_tracking_missing
    history = await tracer.get(first.thread_id, limit=1)
    assert history.status.execution == "failed"
    assert not history.graph.completeness.call_tracking_missing
    assert history.history_cursor is not None

    await _run_healthy(runtime, last)
    current = await tracer.query(first.thread_id)
    assert not current.completeness.call_tracking_missing
    assert (
        len([node for node in current.nodes if node.kind is TraceGraphNodeKind.MODEL])
        == 2
    )
    assert (await tracer.get(first.thread_id)).status.execution == "succeeded"
    older = await tracer.get(
        first.thread_id, history_cursor=history.history_cursor, limit=100
    )
    assert older.as_of_seq == history.as_of_seq
    assert not older.graph.completeness.call_tracking_missing
    assert not any(node.run_id == last.run_id for node in older.graph.nodes)


@pytest.mark.parametrize("has_native_task", (False, True))
async def test_untracked_execution_failure_remains_unknown_after_a_healthy_run(
    trace_store: TraceStore, has_native_task: bool
) -> None:
    tracer = Tracer(store=trace_store)
    identity = RunIdentity(threadId="untracked", runId="untracked-failure")
    source = RunSourceContext(
        identity=identity,
        runtime_profile="deepagents-v2",
        input_kind="ordinary",
        input={},
        config={},
        call_tracking_enabled=False,
    )
    session = await tracer.open_run(source)
    now = datetime.now(UTC)
    try:
        await session.observe(
            RunStartedObservation(identity=identity, observed_at=now, monotonic_ns=1)
        )
        await session.observe(
            RunInputObservation(
                identity=identity, source=source, observed_at=now, monotonic_ns=2
            )
        )
        if has_native_task:
            await session.observe(
                NativeTaskObservation(
                    identity=identity,
                    namespace=(),
                    phase="start",
                    task_id="untracked-model-task",
                    name="model",
                    input={},
                    observed_at=now,
                    monotonic_ns=3,
                )
            )
        await session.observe(
            RunTerminalObservation(
                identity=identity,
                outcome="failed",
                code="runtime_error",
                error_type="builtins.RuntimeError",
                observed_at=now,
                monotonic_ns=4,
            )
        )
        await session.observe(
            RunClosedObservation(
                identity=identity, outcome="failed", observed_at=now, monotonic_ns=5
            )
        )
    finally:
        await session.aclose()
    assert (await tracer.query(identity.thread_id)).completeness.call_tracking_missing
    assert (
        await tracer.get(identity.thread_id)
    ).graph.completeness.call_tracking_missing
    await _run_healthy(
        TinkerFin().observe(tracer),
        RunIdentity(threadId=identity.thread_id, runId="subsequent-healthy"),
    )
    assert (await tracer.query(identity.thread_id)).completeness.call_tracking_missing
    assert (
        await tracer.get(identity.thread_id)
    ).graph.completeness.call_tracking_missing


@pytest.mark.parametrize("transport", ("native", "agui"))
async def test_cancelled_initialization_preserves_cancellation_and_next_run(
    trace_store: TraceStore, transport: Literal["native", "agui"]
) -> None:
    tracer = Tracer(store=trace_store)
    runtime = TinkerFin().observe(tracer)
    identity = RunIdentity(threadId="cancelled-initialization", runId="cancelled")
    entered = asyncio.Event()

    async def create_agent() -> DeepAgentDefinition[None]:
        entered.set()
        await asyncio.Future[None]()
        raise AssertionError("cancelled initialization continued")

    operation = (
        runtime.open_run(identity, agent=create_agent, input=_input(identity))
        if transport == "native"
        else runtime.open_agui_run(identity, agent=create_agent, input=_input(identity))
    )
    pending = asyncio.create_task(operation)
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        for attempt in range(6):
            if pending.done():
                break
            pending.cancel(f"initialization cancellation {attempt + 1}")
            await asyncio.sleep(0)
        with pytest.raises(asyncio.CancelledError) as captured:
            await pending
        assert captured.value.args == ("initialization cancellation 1",)
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
    with pytest.raises(TraceThreadNotFound):
        await trace_store.snapshot(identity.thread_id)
    await _run_healthy(
        runtime, RunIdentity(threadId=identity.thread_id, runId="after-cancellation")
    )
    assert not (
        await tracer.query(identity.thread_id)
    ).completeness.call_tracking_missing
