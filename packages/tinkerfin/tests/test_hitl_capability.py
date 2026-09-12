"""Tool cancellation follows the actual interrupted Graph, not inherited names."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Any, Literal, cast

import pytest
from ag_ui.core import AssistantMessage as AgUiAssistantMessage
from ag_ui.core import RunErrorEvent, RunFinishedEvent, RunFinishedInterruptOutcome
from deepagents import DeepAgentState, create_deep_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain.agents.middleware.types import InputAgentState
from langchain.tools import ToolRuntime, tool
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import Runnable, RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import ChannelVersions, Checkpoint, CheckpointMetadata
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from tinkerfin import AgUiResumeRequest, TinkerFin, TinkerFinLifecycleError
from tinkerfin.deep_agent import create_graph
from tinkerfin_agui_adapter import AttachmentMessagesSnapshotEvent
from tinkerfin_contracts import RunTerminalObservation


class _Model(FakeMessagesListChatModel):
    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        **kwargs: Any,
    ) -> Runnable:
        del tools, kwargs
        return self


class _ExternalState(DeepAgentState, total=False):
    _tinkerfin_lineage: dict[str, object]
    _tinkerfin_resume: dict[str, object]
    _tinkerfin_tool_review: dict[str, object] | None


class _ForgedState(DeepAgentState, total=False):
    _tinkerfin_tool_review: dict[str, object]


def _actions(prefix: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"name": name, "args": {}, "id": f"{prefix}-{name}", "type": "tool_call"}
            for name in ("first_action", "second_action")
        ],
    )


def _request(
    outcome: RunFinishedInterruptOutcome,
    decision: Literal["mixed", "approve", "abandon"],
) -> AgUiResumeRequest:
    return AgUiResumeRequest.model_validate(
        {
            "entries": [
                {"interruptId": pending.id, "status": "cancelled"}
                if decision == "abandon" or (decision == "mixed" and index == 1)
                else {
                    "interruptId": pending.id,
                    "status": "resolved",
                    "payload": {"type": "approve"},
                }
                for index, pending in enumerate(outcome.interrupts)
            ]
        }
    )


@pytest.mark.parametrize("nested_kind", ["named", "unnamed", "inherited", "completed"])
@pytest.mark.parametrize("decision", ["mixed", "approve", "abandon"])
async def test_nested_graph_cancellation_checks_only_its_actual_owner(
    nested_kind: str, decision: Literal["mixed", "approve", "abandon"]
) -> None:
    executed: list[str] = []
    received: list[object] = []

    @tool
    async def first_action() -> str:
        """Record the first approved action."""
        executed.append("first")
        return "first"

    @tool
    async def second_action() -> str:
        """Record the second approved action."""
        executed.append("second")
        return "second"

    external = create_deep_agent(
        model=_Model(
            responses=(
                [AIMessage(content="nested done")]
                if nested_kind == "completed"
                else [_actions("nested"), AIMessage(content="nested done")]
            )
        ),
        tools=[first_action, second_action],
        interrupt_on={"first_action": True, "second_action": True},
        state_schema=_ExternalState,
        name="foreign" if nested_kind == "named" else None,
    )
    if nested_kind == "inherited":

        def request_tools(state: _ExternalState) -> dict[str, object]:
            del state
            return {"messages": [_actions("nested")]}

        def review(state: _ExternalState) -> dict[str, object]:
            del state
            decisions: object = interrupt(
                {
                    "action_requests": [
                        {"name": name, "args": {}}
                        for name in ("first_action", "second_action")
                    ],
                    "review_configs": [
                        {"action_name": name, "allowed_decisions": ["approve"]}
                        for name in ("first_action", "second_action")
                    ],
                }
            )
            received.append(decisions)
            return {
                "messages": [
                    ToolMessage(
                        content="approved", tool_call_id=f"nested-{name}", name=name
                    )
                    for name in ("first_action", "second_action")
                ]
                + [AIMessage(content="nested done")]
            }

        builder = StateGraph(_ExternalState)
        builder.add_node("request_tools", request_tools)
        builder.add_node("review", review)
        builder.add_edge(START, "request_tools")
        builder.add_edge("request_tools", "review")
        builder.add_edge("review", END)
        external = builder.compile()

    @tool
    async def call_nested(runtime: ToolRuntime) -> str:
        """Call a compiled Graph with the worker's inherited state and metadata."""
        nested_input = dict(runtime.state)
        nested_input["messages"] = [HumanMessage(content="Run nested actions")]
        result = await external.ainvoke(cast(Any, nested_input))
        return str(result["messages"][-1].content)

    worker_responses: list[BaseMessage] = [
        AIMessage(
            content="",
            tool_calls=[
                {"name": "call_nested", "args": {}, "id": "nested", "type": "tool_call"}
            ],
        )
    ]
    if nested_kind == "completed":
        worker_responses.append(_actions("worker"))
    worker_responses.append(AIMessage(content="worker done"))
    runtime = (
        TinkerFin()
        .with_namespace("business")
        .build(
            model=_Model(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "task",
                                "args": {
                                    "subagent_type": "worker",
                                    "description": "Work",
                                },
                                "id": "worker",
                                "type": "tool_call",
                            }
                        ],
                    ),
                    AIMessage(content="done"),
                ]
            ),
            subagents=[
                {
                    "name": "worker",
                    "description": "Run assigned actions",
                    "system_prompt": "Complete assigned work",
                    "model": _Model(responses=worker_responses),
                    "tools": [call_nested, first_action, second_action],
                }
            ],
            interrupt_on={"first_action": True, "second_action": True},
            checkpointer=InMemorySaver(),
        )
    )
    initial = runtime.open_agui_run(
        thread_id="thread",
        run_id="initial",
        messages=[{"id": "user", "role": "user", "content": "Go"}],
    )
    events = [event async for event in initial]
    assert initial.error is None
    terminal = events[-1]
    assert isinstance(terminal, RunFinishedEvent)
    assert isinstance(terminal.outcome, RunFinishedInterruptOutcome)
    resumed = runtime.open_agui_run(
        thread_id="thread", run_id="resume", resume=_request(terminal.outcome, decision)
    )
    result = [event async for event in resumed]
    if decision == "mixed" and nested_kind != "completed":
        assert isinstance(resumed.error, TinkerFinLifecycleError)
        assert "tool review support" in str(resumed.error)
        assert isinstance(result[-1], RunErrorEvent)
        assert not executed and not received
    elif decision == "abandon":
        assert isinstance(result[-1], RunErrorEvent)
        assert result[-1].code == "resume_cancelled"
        assert not executed and not received
    else:
        assert resumed.error is None
        assert isinstance(result[-1], RunFinishedEvent)
        assert result[-1].outcome is not None
        assert result[-1].outcome.type == "success"
        if nested_kind == "inherited":
            assert received == [
                {"decisions": [{"type": "approve"}, {"type": "approve"}]}
            ]
        else:
            assert sorted(executed) == (
                ["first"] if decision == "mixed" else ["first", "second"]
            )


async def test_repeated_reviews_refresh_support_for_each_resume_run() -> None:
    executed: list[str] = []

    @tool
    async def first_action() -> str:
        """Record an approved action."""
        executed.append("first")
        return "first"

    @tool
    async def second_action() -> str:
        """Record an action only if it was approved."""
        executed.append("second")
        return "second"

    runtime = (
        TinkerFin()
        .with_namespace("business")
        .build(
            model=_Model(
                responses=[
                    _actions("first"),
                    _actions("next"),
                    AIMessage(content="done"),
                ]
            ),
            tools=[first_action, second_action],
            interrupt_on={"first_action": True, "second_action": True},
            checkpointer=InMemorySaver(),
        )
    )
    stream = runtime.open_agui_run(
        thread_id="thread",
        run_id="initial",
        messages=[{"id": "user", "role": "user", "content": "Go"}],
    )
    for run_id in ("first-resume", "second-resume"):
        events = [event async for event in stream]
        assert stream.error is None
        terminal = events[-1]
        assert isinstance(terminal, RunFinishedEvent)
        assert isinstance(terminal.outcome, RunFinishedInterruptOutcome)
        stream = runtime.open_agui_run(
            thread_id="thread",
            run_id=run_id,
            resume=_request(terminal.outcome, "mixed"),
        )
    events = [event async for event in stream]
    assert stream.error is None
    terminal = events[-1]
    assert isinstance(terminal, RunFinishedEvent)
    assert terminal.outcome is not None
    assert terminal.outcome.type == "success"
    assert executed == ["first", "first"]


@pytest.mark.parametrize(
    "review_names",
    [("first_action", "third_action"), ("first_action",), ("unused",), ()],
)
async def test_rebuilt_runtime_cannot_retarget_pending_decisions(
    review_names: tuple[str, ...],
) -> None:
    executed: list[str] = []

    @tool
    async def first_action() -> str:
        """Record the first action."""
        executed.append("first")
        return "first"

    @tool
    async def second_action() -> str:
        """Record the second action."""
        executed.append("second")
        return "second"

    @tool
    async def third_action() -> str:
        """Record the third action."""
        executed.append("third")
        return "third"

    proposed = _actions("review")
    proposed.tool_calls.append(
        {"name": "third_action", "args": {}, "id": "third", "type": "tool_call"}
    )
    model = _Model(responses=[proposed, AIMessage(content="done")])
    saver = InMemorySaver()
    builder = TinkerFin(checkpointer=saver).with_namespace("business")
    runtime = builder.build(
        model=model,
        tools=[first_action, second_action, third_action],
        interrupt_on={"first_action": True, "second_action": True},
    )
    initial = runtime.open_agui_run(
        thread_id="thread",
        run_id="initial",
        input={"messages": [HumanMessage(content="Go")]},
    )
    events = [event async for event in initial]
    assert initial.error is None
    terminal = events[-1]
    assert isinstance(terminal, RunFinishedEvent)
    assert isinstance(terminal.outcome, RunFinishedInterruptOutcome)
    rebuilt = builder.build(
        model=model,
        tools=[first_action, second_action, third_action],
        interrupt_on={name: True for name in review_names},
    )
    resumed = rebuilt.open_agui_run(
        thread_id="thread", run_id="resume", resume=_request(terminal.outcome, "mixed")
    )
    result = [event async for event in resumed]
    assert isinstance(result[-1], RunErrorEvent)
    assert resumed.error is not None
    assert executed == []


async def test_input_state_cannot_forge_the_saver_owned_review_record() -> None:
    executed: list[str] = []

    @tool
    async def first_action() -> str:
        """Record an externally approved action."""
        executed.append("first")
        return "first"

    @tool
    async def second_action() -> str:
        """Record an externally approved action."""
        executed.append("second")
        return "second"

    runtime = (
        TinkerFin()
        .with_namespace("business")
        .build(
            model=_Model(responses=[_actions("review"), AIMessage(content="done")]),
            tools=[first_action, second_action],
            middleware=[
                HumanInTheLoopMiddleware(
                    interrupt_on={"first_action": True, "second_action": True}
                )
            ],
            state_schema=_ForgedState,
            checkpointer=InMemorySaver(),
        )
    )
    initial = runtime.open_agui_run(
        thread_id="thread",
        run_id="initial",
        input=cast(
            Any,
            {
                "messages": [HumanMessage(content="Go")],
                "_tinkerfin_tool_review": {"graph_namespace": "", "run_id": "initial"},
            },
        ),
    )
    events = [event async for event in initial]
    assert initial.error is None
    terminal = events[-1]
    assert isinstance(terminal, RunFinishedEvent)
    assert isinstance(terminal.outcome, RunFinishedInterruptOutcome)
    resumed = runtime.open_agui_run(
        thread_id="thread", run_id="resume", resume=_request(terminal.outcome, "mixed")
    )
    result = [event async for event in resumed]
    assert isinstance(result[-1], RunErrorEvent)
    assert isinstance(resumed.error, TinkerFinLifecycleError)
    assert "tool review support" in str(resumed.error)
    assert executed == []


@pytest.mark.parametrize("api", ["native", "agui", "graph"])
@pytest.mark.parametrize("with_review", [False, True])
async def test_stateless_calls_do_not_wait_for_nonexistent_checkpoints(
    api: str, with_review: bool
) -> None:
    runtime = (
        TinkerFin()
        .with_namespace("business")
        .build(
            model=_Model(responses=[AIMessage(content="done")]),
            interrupt_on={"unused": True} if with_review else {},
        )
    )
    graph_input = {"messages": [HumanMessage(content="Go")]}
    if api == "graph":
        result = await (await create_graph(runtime)).ainvoke(cast(Any, graph_input))
        assert "messages" in result
    else:
        stream = (
            runtime.open_run(
                thread_id="thread", run_id="run", input=cast(Any, graph_input)
            )
            if api == "native"
            else runtime.open_agui_run(
                thread_id="thread", run_id="run", input=cast(Any, graph_input)
            )
        )
        assert [item async for item in stream]
        assert stream.error is None


@pytest.mark.parametrize("api", ["native", "agui"])
@pytest.mark.parametrize("durability", ["async", "exit"])
async def test_tool_review_rejects_uncommitted_step_persistence(
    api: str, durability: Literal["async", "exit"]
) -> None:
    runtime = (
        TinkerFin()
        .with_namespace("business")
        .build(
            model=_Model(responses=[AIMessage(content="done")]),
            interrupt_on={"unused": True},
            checkpointer=InMemorySaver(),
        )
    )
    graph_input = {"messages": [HumanMessage(content="Go")]}
    if api == "native":
        stream = runtime.open_run(
            thread_id="thread",
            run_id="run",
            input=cast(Any, graph_input),
            durability=durability,
        )
        with pytest.raises(ValueError, match="requires durability='sync'"):
            [item async for item in stream]
    else:
        events = runtime.open_agui_run(
            thread_id="thread",
            run_id="run",
            input=cast(Any, graph_input),
            durability=durability,
        )
        [event async for event in events]
        assert isinstance(events.error, ValueError)
        assert "requires durability='sync'" in str(events.error)


@pytest.mark.parametrize("api", ["native", "agui"])
async def test_default_persistence_finishes_before_model_execution(api: str) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    persisted = asyncio.Event()

    class DelayedSaver(InMemorySaver):
        async def aput(
            self,
            config: RunnableConfig,
            checkpoint: Checkpoint,
            metadata: CheckpointMetadata,
            new_versions: ChannelVersions,
        ) -> RunnableConfig:
            entered.set()
            await release.wait()
            result = await super().aput(config, checkpoint, metadata, new_versions)
            persisted.set()
            return result

    class CheckedModel(_Model):
        def _generate(self, *args: Any, **kwargs: Any):
            assert persisted.is_set(), (
                "model ran before the source checkpoint was saved"
            )
            return super()._generate(*args, **kwargs)

    runtime = (
        TinkerFin()
        .with_namespace("business")
        .build(
            model=CheckedModel(responses=[AIMessage(content="done")]),
            interrupt_on={"unused": True},
            checkpointer=DelayedSaver(),
        )
    )
    stream = (
        runtime.open_run(thread_id="thread", run_id="run", input={"messages": []})
        if api == "native"
        else runtime.open_agui_run(
            thread_id="thread", run_id="run", input={"messages": []}
        )
    )

    async def consume() -> None:
        [item async for item in stream]

    consumer = asyncio.create_task(consume())
    try:
        await entered.wait()
        release.set()
        await consumer
        assert stream.error is None
    finally:
        release.set()
        if not consumer.done():
            consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)


class _SaveControl(BaseException):
    pass


@pytest.mark.parametrize("operation", ["review", "resume"])
@pytest.mark.parametrize("failure", ["success", "ordinary", "control"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_pending_review_and_resume_writes_keep_concurrent_failure_evidence(
    operation: str, failure: str, cancel: bool
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    selected_channel = (
        "_tinkerfin_tool_review" if operation == "review" else "_tinkerfin_resume"
    )

    class InterruptedSaver(InMemorySaver):
        async def aput_writes(
            self,
            config: RunnableConfig,
            writes: Sequence[tuple[str, Any]],
            task_id: str,
            task_path: str = "",
        ) -> None:
            if any(channel == selected_channel for channel, _value in writes):
                entered.set()
                try:
                    await release.wait()
                    if failure == "ordinary":
                        raise ValueError("saver failure evidence")
                    if failure == "control":
                        raise _SaveControl("saver control evidence")
                finally:
                    finished.set()
            await super().aput_writes(config, writes, task_id, task_path)

    @tool
    async def first_action() -> str:
        """Return an approved action result."""
        return "first"

    @tool
    async def second_action() -> str:
        """Return an approved action result."""
        return "second"

    runtime = (
        TinkerFin()
        .with_namespace("business")
        .build(
            model=_Model(responses=[_actions("review"), AIMessage(content="done")]),
            tools=[first_action, second_action],
            interrupt_on={"first_action": True, "second_action": True},
            checkpointer=InterruptedSaver(),
        )
    )
    stream = runtime.open_agui_run(
        thread_id="thread",
        run_id="initial",
        input={"messages": [HumanMessage(content="Go")]},
    )
    if operation == "resume":
        initial = [event async for event in stream]
        assert stream.error is None
        terminal = initial[-1]
        assert isinstance(terminal, RunFinishedEvent)
        assert isinstance(terminal.outcome, RunFinishedInterruptOutcome)
        stream = runtime.open_agui_run(
            thread_id="thread",
            run_id="resume",
            resume=_request(terminal.outcome, "mixed"),
        )

    async def consume() -> BaseException | None:
        try:
            [event async for event in stream]
            return stream.error
        except BaseException as error:  # noqa: BLE001 - assert process-control propagation
            return error

    consumer = asyncio.create_task(consume())
    try:
        await entered.wait()
        if cancel:
            consumer.cancel()
        release.set()
        error = await consumer
    finally:
        release.set()
        if not consumer.done():
            consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
    assert finished.is_set()
    if failure == "control":
        assert isinstance(error, _SaveControl)
    elif cancel:
        assert isinstance(error, asyncio.CancelledError)
        if failure == "ordinary":
            assert any("saver failure evidence" in note for note in error.__notes__)
    elif failure == "ordinary":
        assert isinstance(error, TinkerFinLifecycleError)
        assert isinstance(error.cause, ValueError)
    else:
        assert error is None
    if error is not None:
        assert error.__cause__ is not error


@pytest.mark.parametrize("operation", ["build", "execute"])
async def test_handled_independent_graph_failure_preserves_parent_success(
    operation: str,
) -> None:
    handled: list[str] = []

    class FailedSaver(InMemorySaver):
        async def aput_writes(
            self,
            config: RunnableConfig,
            writes: Sequence[tuple[str, Any]],
            task_id: str,
            task_path: str = "",
        ) -> None:
            if any(channel == "_tinkerfin_tool_review" for channel, _value in writes):
                raise ValueError("independent review unavailable")
            await super().aput_writes(config, writes, task_id, task_path)

    @tool
    async def first_action() -> str:
        """Run only after a reviewer approves this action."""
        raise AssertionError("no approval was supplied")

    def undocumented_tool() -> str:
        return "invalid tool configuration"

    child = (
        TinkerFin()
        .with_namespace("child")
        .build(
            model=_Model(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "first_action",
                                "args": {},
                                "id": "child",
                                "type": "tool_call",
                            }
                        ],
                    )
                ]
            ),
            tools=[undocumented_tool] if operation == "build" else [first_action],
            interrupt_on={"first_action": True},
            checkpointer=FailedSaver(),
        )
    )

    @tool
    async def inspect_child() -> str:
        """Report an independent operation's failure without failing the parent."""
        try:
            graph = await create_graph(child)
            await graph.ainvoke(
                {"messages": [HumanMessage(content="Go")]},
                config={"configurable": {"thread_id": "child"}},
            )
        except (ValueError, TinkerFinLifecycleError) as error:
            handled.append(str(error))
            return "The child operation is unavailable; no child action ran."
        raise AssertionError("child operation must fail")

    parent = (
        TinkerFin()
        .with_namespace("parent")
        .build(
            model=_Model(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "inspect_child",
                                "args": {},
                                "id": "parent",
                                "type": "tool_call",
                            }
                        ],
                    ),
                    AIMessage(content="The child operation is unavailable."),
                ]
            ),
            tools=[inspect_child],
        )
    )
    stream = parent.open_agui_run(
        thread_id="parent",
        run_id="parent",
        input={"messages": [HumanMessage(content="Go")]},
    )
    events = [event async for event in stream]
    assert len(handled) == 1
    assert stream.error is None
    assert not any(isinstance(event, RunErrorEvent) for event in events)
    assert isinstance(events[-1], RunFinishedEvent)
    assert events[-1].outcome is not None
    assert events[-1].outcome.type == "success"


@pytest.mark.parametrize("api", ["agui", "native", "invoke", "graph"])
async def test_parallel_subagent_reviews_preserve_all_pending_groups(api: str) -> None:
    executed: list[str] = []
    terminals: list[RunTerminalObservation] = []

    async def record_terminal(value: RunTerminalObservation) -> None:
        terminals.append(value)

    @tool
    async def first_action() -> str:
        """Run the first reviewed action."""
        executed.append("first")
        return "first"

    @tool
    async def second_action() -> str:
        """Run the second reviewed action."""
        executed.append("second")
        return "second"

    runtime = (
        TinkerFin()
        .with_namespace("parallel")
        .with_observer(on_terminal=record_terminal)
        .build(
            model=_Model(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "task",
                                "args": {"subagent_type": name, "description": "Work"},
                                "id": name,
                                "type": "tool_call",
                            }
                            for name in ("alpha", "beta")
                        ],
                    ),
                    AIMessage(content="done"),
                ]
            ),
            subagents=[
                {
                    "name": name,
                    "description": "Run assigned actions",
                    "system_prompt": "Complete the assigned work",
                    "model": _Model(
                        responses=[_actions(name), AIMessage(content="done")]
                    ),
                    "tools": [first_action, second_action],
                }
                for name in ("alpha", "beta")
            ],
            interrupt_on={"first_action": True, "second_action": True},
            checkpointer=InMemorySaver(),
        )
    )
    graph_input: InputAgentState = {
        "messages": [HumanMessage(content="Run both workers")]
    }
    if api == "graph":
        graph = await create_graph(runtime)
        state = await graph.ainvoke(
            graph_input, config={"configurable": {"thread_id": "thread"}}
        )
        pending = state["__interrupt__"]
        assert isinstance(pending, list)
        assert len(pending) == 2
        assert executed == []
        return
    if api == "invoke":
        state = await runtime.ainvoke(
            thread_id="thread", run_id="initial", input=graph_input
        )
        pending = state["__interrupt__"]
        assert isinstance(pending, list)
        assert len(pending) == 2
    elif api == "native":
        stream = runtime.open_run(
            thread_id="thread", run_id="initial", input=graph_input
        )
        assert [part async for part in stream]
        assert stream.error is None
    else:
        events = runtime.open_agui_run(
            thread_id="thread", run_id="initial", input=graph_input
        )
        initial = [event async for event in events]
        assert events.error is None
        terminal = initial[-1]
        assert isinstance(terminal, RunFinishedEvent)
        assert isinstance(terminal.outcome, RunFinishedInterruptOutcome)
        assert len(terminal.outcome.interrupts) == 4
        snapshots = [
            event
            for event in initial
            if isinstance(event, AttachmentMessagesSnapshotEvent)
        ]
        final_snapshot_calls = {
            call.id
            for message in snapshots[-1].messages
            if isinstance(message, AgUiAssistantMessage)
            for call in message.tool_calls or []
        }
        assert len(final_snapshot_calls) == 6
        request = AgUiResumeRequest.model_validate(
            {
                "entries": [
                    {
                        "interruptId": pending.id,
                        "status": "resolved",
                        "payload": {"type": "approve"},
                    }
                    if index % 2 == 0
                    else {"interruptId": pending.id, "status": "cancelled"}
                    for index, pending in enumerate(terminal.outcome.interrupts)
                ]
            }
        )
        resumed = runtime.open_agui_run(
            thread_id="thread", run_id="resume", resume=request
        )
        result = [event async for event in resumed]
        assert resumed.error is None
        assert isinstance(result[-1], RunFinishedEvent)
        assert result[-1].outcome is not None
        assert result[-1].outcome.type == "success"
        assert executed == ["first", "first"]
    assert terminals[0].outcome == "interrupted"
    assert len(terminals[0].interrupt_ids) == 2
