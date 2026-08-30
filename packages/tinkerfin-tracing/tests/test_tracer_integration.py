"""End-to-end Runtime Observer, semantic Ledger, query, follow, and Projection tests."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from datetime import UTC, datetime
from importlib.metadata import version
from typing import Any, cast

import pytest
from ag_ui.core import RunFinishedEvent
from langchain.agents.middleware.types import InputAgentState
from langchain.tools import tool
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Interrupt
from pydantic import BaseModel

from tinkerfin import (
    DeepAgentDefinition,
    DeepAgentsV2RuntimeProfile,
    DeepSeekReasoningExtractor,
    RunObservationError,
    TinkerFin,
)
from tinkerfin_contracts import (
    RunClosedObservation,
    RunIdentity,
    RunInputKind,
    RunInputObservation,
    RunSourceContext,
    RunStartedObservation,
    RunTerminalObservation,
    RuntimeObservation,
)
from tinkerfin_tracing import (
    AmbiguousTraceHead,
    FactCountProjection,
    FactCountResult,
    InMemoryTraceStore,
    MessageFact,
    ReasoningCapturePolicy,
    ReasoningFact,
    RunFact,
    StateRevisionFact,
    ToolFact,
    TraceEvent,
    TraceLimits,
    TraceProjectionFailed,
    Tracer,
    TraceSemanticFact,
    TraceThreadKey,
    TurnFact,
)
from tinkerfin_tracing.projection import project_core


class _Graph:
    def __init__(self, parts: Sequence[Mapping[str, object]]) -> None:
        self.parts = tuple(parts)

    async def astream(
        self,
        *_args: object,
        **_options: object,
    ) -> AsyncIterator[Mapping[str, object]]:
        for part in self.parts:
            yield part


class _SubagentToolBindingModel(FakeMessagesListChatModel):
    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        **kwargs: Any,
    ) -> _SubagentToolBindingModel:
        del tools, kwargs
        return self


@tool
def reviewed_child_tool(value: str) -> str:
    """Return a value after the child Tool review."""

    return value


class _ReadCountingStore(InMemoryTraceStore):
    def __init__(self) -> None:
        super().__init__()
        self.read_calls: list[tuple[int, int, int]] = []

    async def read_events(
        self,
        key: TraceThreadKey,
        *,
        after_seq: int,
        as_of_seq: int,
        limit: int,
    ) -> tuple[TraceEvent, ...]:
        self.read_calls.append((after_seq, as_of_seq, limit))
        return await super().read_events(
            key,
            after_seq=after_seq,
            as_of_seq=as_of_seq,
            limit=limit,
        )


setattr(
    _Graph.astream,
    "__signature__",
    inspect.signature(CompiledStateGraph.astream),
)


def _definition(
    monkeypatch: pytest.MonkeyPatch,
    graph: _Graph,
    *,
    tracer: Tracer,
    runtime_profile: DeepAgentsV2RuntimeProfile | None = None,
) -> DeepAgentDefinition[None]:
    def build(*_args: object, **_kwargs: object) -> _Graph:
        return graph

    monkeypatch.setattr(
        "tinkerfin.runtime_profile._deepagents_graph.create_deep_agent",
        build,
    )
    tinkerfin = (
        TinkerFin()
        if runtime_profile is None
        else TinkerFin(runtime_profile=runtime_profile)
    )
    factory = cast(
        Callable[..., object],
        tinkerfin.observe(tracer).create_deep_agent,
    )
    definition = factory(model="provider:model", tools=[])
    if not isinstance(definition, DeepAgentDefinition):
        raise TypeError("patched factory must return DeepAgentDefinition")
    return definition


def _parts() -> tuple[Mapping[str, object], ...]:
    tool_call = {
        "name": "write_todos",
        "args": {
            "todos": [
                {"content": "Inspect", "status": "in_progress"},
                {"content": "Verify", "status": "pending"},
            ]
        },
        "id": "call-write-todos",
        "type": "tool_call",
    }
    return (
        {
            "type": "messages",
            "ns": (),
            "data": (
                AIMessageChunk(id="assistant-1", content="hello "),
                {"langgraph_node": "model", "lc_agent_name": "main"},
            ),
        },
        {
            "type": "messages",
            "ns": (),
            "data": (
                AIMessageChunk(
                    id="assistant-1",
                    content="world",
                    tool_call_chunks=[
                        {
                            "name": "write_todos",
                            "args": (
                                '{"todos":[{"content":"Inspect","status":'
                                '"in_progress"},{"content":"Verify","status":"pending"}]}'
                            ),
                            "id": "call-write-todos",
                            "index": 0,
                            "type": "tool_call_chunk",
                        }
                    ],
                ),
                {"langgraph_node": "model", "lc_agent_name": "main"},
            ),
        },
        {
            "type": "tasks",
            "ns": (),
            "data": {
                "id": "task-tools",
                "name": "tools",
                "input": [tool_call],
                "triggers": ("branch:to:tools",),
            },
        },
        {
            "type": "messages",
            "ns": (),
            "data": (
                ToolMessage(
                    id="tool-message-1",
                    name="write_todos",
                    content="Updated todo list",
                    tool_call_id="call-write-todos",
                ),
                {"langgraph_node": "tools", "lc_agent_name": "main"},
            ),
        },
        {
            "type": "tasks",
            "ns": (),
            "data": {
                "id": "task-tools",
                "name": "tools",
                "error": None,
                "result": {"messages": []},
                "interrupts": [],
            },
        },
        {
            "type": "values",
            "ns": (),
            "data": {
                "messages": [
                    HumanMessage(id="user-1", content="do work"),
                    AIMessage(
                        id="assistant-1",
                        content="hello world",
                        tool_calls=[tool_call],
                    ),
                    ToolMessage(
                        id="tool-message-1",
                        name="write_todos",
                        content="Updated todo list",
                        tool_call_id="call-write-todos",
                    ),
                ],
                "todos": tool_call["args"]["todos"],
            },
            "interrupts": (),
        },
    )


async def test_runtime_trace_projects_messages_tree_state_todos_and_safe_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracer = Tracer(projections=(FactCountProjection(),))
    definition = _definition(monkeypatch, _Graph(_parts()), tracer=tracer)
    runtime = definition.new(identity=RunIdentity(threadId="thread-1", runId="run-1"))

    delivered = [
        part
        async for part in runtime.astream(
            InputAgentState(messages=[HumanMessage(id="user-1", content="do work")]),
            config=cast(RunnableConfig, {"configurable": {"thread_id": "thread-1"}}),
        )
    ]
    thread = await tracer.get("thread-1", projections=("fact_counts",))

    assert delivered == list(_parts())
    assert [(message.role, message.content) for message in thread.messages] == [
        ("user", "do work"),
        ("assistant", "hello world"),
        ("tool", "Updated todo list"),
    ]
    assert thread.messages[2].content_omitted is False
    assert [message.trace_seq for message in thread.messages] == sorted(
        message.trace_seq for message in thread.messages
    )
    assert thread.state.root["todos"] == [
        {"content": "Inspect", "status": "in_progress"},
        {"content": "Verify", "status": "pending"},
    ]
    assert any(
        node.kind == "tool" and node.label == "write_todos"
        for node in thread.tree.nodes
    )
    assistant = next(
        message for message in thread.messages if message.role == "assistant"
    )
    tool = next(node for node in thread.tree.nodes if node.kind == "tool")
    assert assistant.trace_seq < tool.trace_seq
    assert thread.status.execution == "succeeded"
    assert thread.completeness.payload_omitted is False
    result = thread.projections["fact_counts"]
    assert isinstance(result, FactCountResult)
    assert result.counts["tool"] == 4
    page = await thread.events(limit=100)
    turn = next(event.fact for event in page.items if isinstance(event.fact, TurnFact))
    tool_facts = [
        event.fact for event in page.items if isinstance(event.fact, ToolFact)
    ]
    assistant_facts = [
        event.fact
        for event in page.items
        if isinstance(event.fact, MessageFact)
        and event.fact.source_message_id == "assistant-1"
    ]
    state_facts = [
        event.fact for event in page.items if isinstance(event.fact, StateRevisionFact)
    ]
    run_facts = [event.fact for event in page.items if isinstance(event.fact, RunFact)]
    assert turn.user_message_id == "user-1"
    assert [fact.phase for fact in tool_facts] == [
        "started",
        "arguments",
        "completed",
        "result",
    ]
    assert [fact.phase for fact in assistant_facts] == [
        "started",
        "content",
        "content",
        "completed",
    ]
    assert all(
        fact.content is None or fact.content.value != "hello world"
        for fact in assistant_facts
    )
    assert all(fact.namespace == () for fact in tool_facts)
    assert any(
        isinstance(fact.changes.value, dict) and "todos" in fact.changes.value
        for fact in state_facts
    )
    assert [fact.phase for fact in run_facts] == [
        "started",
        "input",
        "terminal",
        "closed",
    ]
    assert all(event.fact.kind not in {"agui", "messaging"} for event in page.items)


async def test_propagated_subagent_interrupt_uses_the_deepest_trace_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = ("tools:subagent-task",)
    interrupt = Interrupt(
        id="child-review",
        value={
            "action_requests": [
                {
                    "name": "write_file",
                    "args": {"file_path": "/result.txt", "content": "result"},
                }
            ],
            "review_configs": [
                {
                    "action_name": "write_file",
                    "allowed_decisions": ["approve", "reject"],
                }
            ],
        },
    )
    child_message = AIMessage(
        id="child-assistant",
        content="",
        tool_calls=[
            {
                "name": "write_file",
                "args": {"file_path": "/result.txt", "content": "result"},
                "id": "child-write",
                "type": "tool_call",
            }
        ],
    )
    root_message = AIMessage(
        id="root-assistant",
        content="",
        tool_calls=[
            {
                "name": "task",
                "args": {
                    "description": "Write the reviewed result",
                    "subagent_type": "general-purpose",
                },
                "id": "parent-task",
                "type": "tool_call",
            }
        ],
    )
    parts: tuple[Mapping[str, object], ...] = (
        {
            "type": "values",
            "ns": namespace,
            "data": {
                "messages": [child_message],
            },
            "interrupts": (interrupt,),
        },
        {
            "type": "values",
            "ns": (),
            "data": {
                "messages": [
                    HumanMessage(id="user", content="Delegate"),
                    root_message,
                ],
            },
            "interrupts": (interrupt,),
        },
    )
    tracer = Tracer()
    definition = _definition(monkeypatch, _Graph(parts), tracer=tracer)
    runtime = definition.new(
        identity=RunIdentity(threadId="thread-child-review", runId="run-review")
    )

    delivered = [
        part
        async for part in runtime.astream(
            InputAgentState(messages=[HumanMessage(id="user", content="Delegate")]),
            config=cast(
                RunnableConfig,
                {"configurable": {"thread_id": "thread-child-review"}},
            ),
        )
    ]
    thread = await tracer.get("thread-child-review")

    assert delivered == list(parts)
    assert [
        (interaction.namespace, interaction.source_id, interaction.status)
        for interaction in thread.interactions
    ] == [(namespace, "child-review", "pending")]
    assert thread.status.execution == "waiting"


async def test_locked_subagent_hitl_remains_an_interrupt_with_tracing() -> None:
    assert version("deepagents") == "0.7.5"
    assert version("langgraph") == "1.2.10"
    model = _SubagentToolBindingModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "task",
                        "args": {
                            "description": "Run the reviewed child Tool",
                            "subagent_type": "general-purpose",
                        },
                        "id": "parent-task-call",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "reviewed_child_tool",
                        "args": {"value": "child"},
                        "id": "child-reviewed-call",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="child done"),
            AIMessage(content="root done"),
        ]
    )
    tracer = Tracer(store=InMemoryTraceStore())
    definition = (
        TinkerFin()
        .observe(tracer)
        .create_deep_agent(
            model=model,
            tools=[reviewed_child_tool],
            interrupt_on={"reviewed_child_tool": {"allowed_decisions": ["approve"]}},
            checkpointer=InMemorySaver(),
        )
    )
    identity = RunIdentity(threadId="thread-real-child-review", runId="run-review")
    stream = definition.new_agui(identity=identity).astream(
        {"messages": [HumanMessage(content="Delegate", id="user")]}
    )
    events = [event async for event in stream]
    terminal = events[-1]
    thread = await tracer.get(identity.thread_id)

    assert isinstance(terminal, RunFinishedEvent)
    assert terminal.outcome is not None
    assert terminal.outcome.type == "interrupt"
    assert len(terminal.outcome.interrupts) == 1
    assert stream.error is None
    assert len(thread.interactions) == 1
    assert thread.interactions[0].namespace
    assert thread.interactions[0].status == "pending"
    assert thread.status.execution == "waiting"


async def _record_run(
    tracer: Tracer,
    *,
    run_id: str,
    parent_run_id: str | None = None,
    input_kind: RunInputKind = "ordinary",
) -> None:
    identity = RunIdentity(threadId="thread-lineage", runId=run_id)
    context = RunSourceContext(
        identity=identity,
        runtime_profile="deepagents-v2",
        input_kind=input_kind,
        parent_run_id=parent_run_id,
        input={"messages": [{"role": "user", "id": f"user-{run_id}"}]},
        config={},
    )
    session = await tracer.open_run(context)
    now = datetime.now(UTC)
    observations: tuple[RuntimeObservation, ...] = (
        RunStartedObservation(identity=identity, observed_at=now, monotonic_ns=1),
        RunInputObservation(
            identity=identity,
            source=context,
            observed_at=now,
            monotonic_ns=2,
        ),
        RunTerminalObservation(
            identity=identity,
            outcome="succeeded",
            observed_at=now,
            monotonic_ns=3,
        ),
        RunClosedObservation(
            identity=identity,
            outcome="succeeded",
            observed_at=now,
            monotonic_ns=4,
        ),
    )
    for observation in observations:
        await session.observe(observation)
    await session.aclose()


async def test_branches_require_an_explicit_head_and_window_loads_older_turns() -> None:
    tracer = Tracer()
    await _record_run(tracer, run_id="root")
    await _record_run(tracer, run_id="branch-a", parent_run_id="root")
    await _record_run(tracer, run_id="branch-b", parent_run_id="root")

    with pytest.raises(AmbiguousTraceHead):
        await tracer.get("thread-lineage")
    branch = await tracer.get("thread-lineage", head_run_id="branch-a", limit=1)
    assert branch.head_run_id == "branch-a"
    assert branch.has_older is True
    assert len(branch.tree.roots) == 1
    await branch.load_older(limit=1)
    assert branch.has_older is False
    assert len(branch.tree.roots) == 2


async def test_incremental_core_checkpoint_reads_only_the_visible_turn_window() -> None:
    store = _ReadCountingStore()
    tracer = Tracer(store=store)
    for index in range(8):
        await _record_run(tracer, run_id=f"bounded-{index}")

    store.read_calls.clear()
    thread = await tracer.get("thread-lineage", limit=2)
    query_calls = tuple(store.read_calls)
    snapshot = await store.snapshot("thread-lineage")
    full_events = await store.read_events(
        snapshot.key,
        after_seq=0,
        as_of_seq=snapshot.as_of_seq,
        limit=1000,
    )
    baseline = project_core(
        full_events,
        head_run_id=None,
        turn_limit=2,
        active_run_ids=snapshot.active_run_ids,
    )

    assert query_calls == ()
    assert thread.messages == baseline.messages
    assert thread.tree == baseline.tree
    assert thread.state == baseline.state
    assert thread.status == baseline.status
    assert thread.completeness == baseline.completeness


async def test_history_cursor_expands_one_fixed_prefix_after_new_commits() -> None:
    tracer = Tracer()
    for index in range(5):
        await _record_run(tracer, run_id=f"history-{index}")
    first = await tracer.get("thread-lineage", limit=2)
    cursor = first.history_cursor
    assert cursor is not None

    await _record_run(tracer, run_id="history-newer")
    older = await tracer.get(
        "thread-lineage",
        history_cursor=cursor,
        limit=2,
    )

    assert older.as_of_seq == first.as_of_seq
    assert len(older.tree.roots) == 4
    assert all(node.run_id != "history-newer" for node in older.tree.nodes)


async def test_event_pages_are_fixed_as_of_and_follow_returns_semantic_deltas() -> None:
    tracer = Tracer()
    await _record_run(tracer, run_id="first")
    thread = await tracer.get("thread-lineage")
    first_page = await thread.events(limit=2)
    assert first_page.next_cursor is not None

    follow = thread.follow()
    waiting = asyncio.ensure_future(anext(follow))
    await _record_run(tracer, run_id="second")
    update = await asyncio.wait_for(waiting, timeout=2)

    second_page = await thread.events(cursor=first_page.next_cursor, limit=100)
    assert all(
        item.trace_seq <= first_page.page_as_of_seq for item in second_page.items
    )
    assert update.as_of_seq > thread.as_of_seq
    assert any(fact.kind == "turn.started" for fact in update.facts)
    assert update.status.head_run_id == "second"
    await follow.aclose()


class _ProjectionState(BaseModel):
    count: int = 0


class _ProjectionResult(BaseModel):
    count: int


class _CountingProjection:
    name = "incremental-count"
    state_type = _ProjectionState
    result_type = _ProjectionResult

    def __init__(self) -> None:
        self.apply_calls = 0

    def initial_state(self) -> _ProjectionState:
        return _ProjectionState()

    def apply(
        self,
        state: _ProjectionState,
        fact: TraceSemanticFact,
    ) -> _ProjectionState:
        del fact
        self.apply_calls += 1
        return _ProjectionState(count=state.count + 1)

    def finish(self, state: _ProjectionState) -> _ProjectionResult:
        return _ProjectionResult(count=state.count)


class _FailingProjection:
    name = "failing"
    state_type = _ProjectionState
    result_type = _ProjectionResult

    def initial_state(self) -> _ProjectionState:
        return _ProjectionState()

    def apply(
        self,
        state: _ProjectionState,
        fact: object,
    ) -> _ProjectionState:
        del state, fact
        raise RuntimeError("business projection failed")

    def finish(self, state: _ProjectionState) -> _ProjectionResult:
        return _ProjectionResult(count=state.count)


async def test_custom_projection_checkpoint_reuses_state_and_derives_child_runs() -> (
    None
):
    projection = _CountingProjection()
    tracer = Tracer(projections=(projection,))
    await _record_run(tracer, run_id="projection-root")

    first = await tracer.get(
        "thread-lineage",
        projections=(projection.name,),
    )
    first_calls = projection.apply_calls
    assert first_calls > 0
    assert first.projections[projection.name] == _ProjectionResult(count=first_calls)

    projection.apply_calls = 0
    await tracer.get("thread-lineage", projections=(projection.name,))
    assert projection.apply_calls == 0

    await _record_run(tracer, run_id="projection-child")
    projection.apply_calls = 0
    child = await tracer.get("thread-lineage", projections=(projection.name,))
    snapshot = await tracer.store.snapshot("thread-lineage")
    assert 0 < projection.apply_calls < snapshot.as_of_seq
    result = child.projections[projection.name]
    assert isinstance(result, _ProjectionResult)
    assert result.count == snapshot.as_of_seq


async def test_optional_projection_failure_does_not_change_the_ledger() -> None:
    tracer = Tracer(projections=(_FailingProjection(),))
    await _record_run(tracer, run_id="only")

    with pytest.raises(TraceProjectionFailed):
        await tracer.get("thread-lineage", projections=("failing",))
    healthy = await tracer.get("thread-lineage")
    assert healthy.status.execution == "succeeded"
    assert (await healthy.events(limit=100)).items


async def test_requested_projection_names_must_be_unique() -> None:
    tracer = Tracer(projections=(FactCountProjection(),))
    await _record_run(tracer, run_id="only")

    with pytest.raises(ValueError, match="names must be unique"):
        await tracer.get(
            "thread-lineage",
            projections=("fact_counts", "fact_counts"),
        )


async def test_tracer_rejects_invalid_extension_boundaries_before_io() -> None:
    invalid: Any = object()

    with pytest.raises(TypeError, match="store must implement"):
        Tracer(store=invalid)
    with pytest.raises(TypeError, match="limits must be"):
        Tracer(limits=invalid)
    with pytest.raises(TypeError, match="capture_policy must be"):
        Tracer(capture_policy=invalid)
    with pytest.raises(TypeError, match="context must be"):
        await Tracer().open_run(invalid)


async def test_live_runtime_objects_strip_private_state_reasoning_and_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed_message = AIMessage(
        id="assistant-private",
        content="visible answer",
        additional_kwargs={"reasoning_content": "provider-private-state"},
    )
    parts: tuple[Mapping[str, object], ...] = (
        {
            "type": "messages",
            "ns": (),
            "data": (
                AIMessageChunk(
                    id="assistant-private",
                    content="visible answer",
                    additional_kwargs={"reasoning_content": "provider-private-message"},
                ),
                {"langgraph_node": "model"},
            ),
        },
        {
            "type": "tasks",
            "ns": (),
            "data": {
                "id": "task-private",
                "name": "model",
                "error": None,
                "result": {"messages": [completed_message]},
                "interrupts": [],
            },
        },
        {
            "type": "values",
            "ns": (),
            "data": {
                "messages": [
                    HumanMessage(id="user-private", content="question"),
                    completed_message,
                ],
                "_tinkerfin_lineage": {"secret": "private-lineage"},
                "_tinkerfin_resume": {"secret": "private-resume"},
                "reasoning_content": "business-state-value",
                "password": "credential-state-value",
            },
            "interrupts": (),
        },
    )
    tracer = Tracer()
    definition = _definition(monkeypatch, _Graph(parts), tracer=tracer)
    runtime = definition.new(
        identity=RunIdentity(threadId="thread-private", runId="run-private")
    )

    assert [
        part
        async for part in runtime.astream(
            InputAgentState(
                messages=[HumanMessage(id="user-private", content="question")]
            ),
            config=cast(
                RunnableConfig,
                {"configurable": {"thread_id": "thread-private"}},
            ),
        )
    ] == list(parts)
    thread = await tracer.get("thread-private")
    page = await thread.events(limit=100)
    encoded = page.model_dump_json(by_alias=True)

    assert "provider-private-message" not in encoded
    assert "provider-private-state" not in encoded
    assert "private-lineage" not in encoded
    assert "private-resume" not in encoded
    assert "credential-state-value" not in encoded
    assert thread.state.root["reasoning_content"] == "business-state-value"
    assert thread.state.root["password"] == {"$type": "redacted"}


async def test_runtime_driver_and_tracer_require_both_reasoning_opt_ins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed_message = AIMessage(
        id="assistant-reasoning",
        content="visible answer",
        additional_kwargs={"reasoning_content": "first second"},
    )
    parts: tuple[Mapping[str, object], ...] = (
        {
            "type": "messages",
            "ns": (),
            "data": (
                AIMessageChunk(
                    id="assistant-reasoning",
                    content="visible answer",
                    additional_kwargs={"reasoning_content": "first"},
                ),
                {"langgraph_node": "model"},
            ),
        },
        {
            "type": "messages",
            "ns": (),
            "data": (
                AIMessageChunk(
                    id="assistant-reasoning",
                    content="",
                    additional_kwargs={"reasoning_content": " second"},
                ),
                {"langgraph_node": "model"},
            ),
        },
        {
            "type": "values",
            "ns": (),
            "data": {
                "messages": [completed_message],
                "reasoning_content": "business value",
            },
            "interrupts": (),
        },
    )
    tracer = Tracer(reasoning_capture_policy=ReasoningCapturePolicy.content())
    definition = _definition(
        monkeypatch,
        _Graph(parts),
        tracer=tracer,
        runtime_profile=DeepAgentsV2RuntimeProfile(
            reasoning_extractors=(DeepSeekReasoningExtractor(),)
        ),
    )
    runtime = definition.new(
        identity=RunIdentity(threadId="thread-reasoning", runId="run-reasoning")
    )

    assert [
        part
        async for part in runtime.astream(
            InputAgentState(messages=[]),
            config=cast(
                RunnableConfig,
                {"configurable": {"thread_id": "thread-reasoning"}},
            ),
        )
    ] == list(parts)
    thread = await tracer.get("thread-reasoning")
    reasoning_facts = [
        item.fact
        for item in (await thread.events(limit=100)).items
        if isinstance(item.fact, ReasoningFact)
    ]

    assert [fact.phase for fact in reasoning_facts] == [
        "content",
        "content",
        "completed",
    ]
    assert thread.reasoning[0].content == "first second"
    assert thread.reasoning[0].status == "completed"
    assert thread.state.root["reasoning_content"] == "business value"


async def test_trace_quota_failure_terminates_the_agent_run_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limits = TraceLimits(
        max_event_bytes=8 * 1024,
        max_thread_events=16,
        max_thread_bytes=512 * 1024,
        max_tracer_threads=2,
        max_tracer_bytes=1024 * 1024,
        terminal_reserve_events_per_run=2,
        terminal_reserve_bytes_per_run=16 * 1024,
    )
    tracer = Tracer(limits=limits)
    parts: tuple[Mapping[str, object], ...] = tuple(
        {
            "type": "messages",
            "ns": (),
            "data": (
                AIMessageChunk(id=f"message-{index}", content="content"),
                {"langgraph_node": "model"},
            ),
        }
        for index in range(20)
    )
    definition = _definition(monkeypatch, _Graph(parts), tracer=tracer)
    runtime = definition.new(
        identity=RunIdentity(threadId="thread-quota", runId="run-quota")
    )

    with pytest.raises(RunObservationError):
        _ = [
            part
            async for part in runtime.astream(
                InputAgentState(
                    messages=[HumanMessage(id="user-quota", content="question")]
                )
            )
        ]

    thread = await tracer.get("thread-quota")
    assert thread.status.execution == "unknown"
    assert thread.completeness.missing_tail is True
