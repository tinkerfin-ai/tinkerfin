"""Regression contracts for lineage, privacy, Tool, Plan, and interaction facts."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import JsonValue

from tinkerfin_contracts import (
    NativeExtraObservation,
    NativeInterruptRecord,
    NativeMessageObservation,
    NativeMessageRecord,
    NativeStateObservation,
    NativeTaskObservation,
    NativeToolCall,
    NativeToolCallChunk,
    RunClosedObservation,
    RunIdentity,
    RunInputKind,
    RunInputObservation,
    RunObservationSession,
    RunResumeCheckpointedObservation,
    RunResumeSummary,
    RunSourceContext,
    RunStartedObservation,
    RunTerminalObservation,
    RunTerminalOutcome,
    ToolExecutionObservation,
)
from tinkerfin_tracing import (
    CapturePolicy,
    InteractionFact,
    MessageFact,
    NativeExtraFact,
    PlanRevisionFact,
    RunFact,
    RuntimeTaskFact,
    StateRevisionFact,
    SubagentFact,
    ToolCaptureRule,
    ToolExecutionFact,
    ToolFact,
    ToolTraceCapture,
    TraceCaptureRejected,
    TraceLimits,
    Tracer,
)


def _context(
    *,
    run_id: str,
    input_kind: RunInputKind = "ordinary",
    parent_run_id: str | None = None,
    private_state_keys: tuple[str, ...] = (),
    resume: tuple[RunResumeSummary, ...] = (),
) -> RunSourceContext:
    return RunSourceContext(
        identity=RunIdentity(threadId="thread-semantic", runId=run_id),
        runtime_profile="deepagents-v2",
        input_kind=input_kind,
        parent_run_id=parent_run_id,
        input={
            "messages": [
                {
                    "role": "user",
                    "id": f"user-{run_id}",
                    "content": f"request {run_id}",
                }
            ]
        },
        config={},
        private_state_keys=private_state_keys,
        resume=resume,
    )


async def _start(
    tracer: Tracer,
    context: RunSourceContext,
) -> RunObservationSession:
    session = await tracer.open_run(context)
    now = datetime.now(UTC)
    await session.observe(
        RunStartedObservation(
            identity=context.identity,
            observed_at=now,
            monotonic_ns=1,
        )
    )
    await session.observe(
        RunInputObservation(
            identity=context.identity,
            source=context,
            observed_at=now,
            monotonic_ns=2,
        )
    )
    return session


async def _finish(
    session: RunObservationSession,
    context: RunSourceContext,
    *,
    outcome: RunTerminalOutcome = "succeeded",
) -> None:
    now = datetime.now(UTC)
    await session.observe(
        RunTerminalObservation(
            identity=context.identity,
            outcome=outcome,
            observed_at=now,
            monotonic_ns=90,
        )
    )
    await session.observe(
        RunClosedObservation(
            identity=context.identity,
            outcome=outcome,
            observed_at=now,
            monotonic_ns=91,
        )
    )
    await session.aclose()


async def _record_state_run(
    tracer: Tracer,
    *,
    run_id: str,
    value: str,
    shared: int,
    parent_run_id: str | None = None,
    input_kind: RunInputKind = "ordinary",
) -> None:
    context = _context(
        run_id=run_id,
        parent_run_id=parent_run_id,
        input_kind=input_kind,
    )
    session = await _start(tracer, context)
    await session.observe(
        NativeStateObservation(
            identity=context.identity,
            namespace=(),
            state={"value": value, "shared": shared},
            observed_at=datetime.now(UTC),
            monotonic_ns=3,
        )
    )
    await _finish(session, context)


async def test_branch_state_hydrates_only_the_selected_ancestor_lineage() -> None:
    tracer = Tracer()
    await _record_state_run(tracer, run_id="root", value="root", shared=1)
    await _record_state_run(
        tracer,
        run_id="branch-a",
        value="a",
        shared=2,
        parent_run_id="root",
        input_kind="branch",
    )
    await _record_state_run(
        tracer,
        run_id="branch-b",
        value="b",
        shared=2,
        parent_run_id="root",
        input_kind="branch",
    )

    branch = await tracer.get("thread-semantic", head_run_id="branch-b")

    assert branch.state.root == {"value": "b", "shared": 2}


async def test_tool_allowlist_is_applied_to_the_complete_arguments_snapshot() -> None:
    tracer = Tracer(
        capture_policy=CapturePolicy.public_safe(
            tool_rules=(
                ToolCaptureRule(
                    tool_name="write_todos",
                    argument_paths=("/todos/0/content",),
                ),
            )
        )
    )
    context = _context(run_id="tool-args")
    session = await _start(tracer, context)
    arguments: dict[str, JsonValue] = {
        "todos": [{"content": "Inspect", "status": "pending"}]
    }
    encoded_arguments = json.dumps(arguments, separators=(",", ":"))
    split = len(encoded_arguments) // 2
    await session.observe(
        NativeMessageObservation(
            identity=context.identity,
            namespace=(),
            message=NativeMessageRecord(
                message_type="assistant_chunk",
                id="assistant-tool",
                content="",
                tool_call_chunks=(
                    NativeToolCallChunk(
                        index=0,
                        id="call-todos",
                        name="write_todos",
                        arguments=encoded_arguments[:split],
                    ),
                ),
            ),
            observed_at=datetime.now(UTC),
            monotonic_ns=3,
        )
    )
    await session.observe(
        NativeMessageObservation(
            identity=context.identity,
            namespace=(),
            message=NativeMessageRecord(
                message_type="assistant_chunk",
                id="assistant-tool",
                content="",
                tool_call_chunks=(
                    NativeToolCallChunk(
                        index=0,
                        arguments=encoded_arguments[split:],
                    ),
                ),
                tool_calls=(
                    NativeToolCall(
                        id="call-todos",
                        name="write_todos",
                        arguments=arguments,
                    ),
                ),
            ),
            observed_at=datetime.now(UTC),
            monotonic_ns=4,
        )
    )
    await _finish(session, context)
    events = (await (await tracer.get("thread-semantic")).events(limit=100)).items
    argument_facts = [
        event.fact
        for event in events
        if isinstance(event.fact, ToolFact) and event.fact.phase == "arguments"
    ]

    assert len(argument_facts) == 1
    assert argument_facts[0].content is not None
    assert argument_facts[0].content.value == {"/todos/0/content": "Inspect"}


async def test_root_tool_capture_populates_bounded_node_input_and_result() -> None:
    tracer = Tracer()
    context = _context(run_id="tool-root-capture")
    session = await _start(tracer, context)
    await session.observe(
        NativeMessageObservation(
            identity=context.identity,
            namespace=(),
            message=NativeMessageRecord(
                message_type="assistant",
                id="assistant-root-capture",
                content="",
                tool_calls=(
                    NativeToolCall(
                        id="call-root-capture",
                        name="search",
                        arguments={"query": "public", "token": "private"},
                    ),
                ),
            ),
            observed_at=datetime.now(UTC),
            monotonic_ns=3,
        )
    )
    await session.observe(
        NativeMessageObservation(
            identity=context.identity,
            namespace=(),
            message=NativeMessageRecord(
                message_type="tool",
                id="tool-root-capture-result",
                name="search",
                content={"answer": "done", "api_key": "private"},
                tool_call_id="call-root-capture",
                tool_status="success",
            ),
            observed_at=datetime.now(UTC),
            monotonic_ns=4,
        )
    )
    await _finish(session, context)

    thread = await tracer.get("thread-semantic")
    node = next(item for item in thread.tree.nodes if item.kind == "tool")
    result = next(item for item in thread.messages if item.role == "tool")

    assert node.input == {
        "query": "public",
        "token": {"$type": "redacted"},
    }
    assert node.input_omitted is False
    assert node.result == {
        "answer": "done",
        "api_key": {"$type": "redacted"},
    }
    assert node.result_omitted is False
    assert result.content == node.result


async def test_disabled_tool_emits_no_tool_or_result_message_facts() -> None:
    tracer = Tracer(
        capture_policy=CapturePolicy.public_history(
            tool_overrides={"private_tool": ToolTraceCapture.disabled()}
        )
    )
    context = _context(run_id="disabled-tool")
    session = await _start(tracer, context)
    await session.observe(
        NativeMessageObservation(
            identity=context.identity,
            namespace=(),
            message=NativeMessageRecord(
                message_type="assistant",
                id="assistant-disabled-tool",
                content="",
                tool_calls=(
                    NativeToolCall(
                        id="call-disabled-tool",
                        name="private_tool",
                        arguments={"secret": "private"},
                    ),
                ),
            ),
            observed_at=datetime.now(UTC),
            monotonic_ns=3,
        )
    )
    await session.observe(
        ToolExecutionObservation(
            identity=context.identity,
            phase="started",
            execution_id="execution-disabled-tool",
            tool_call_id="call-disabled-tool",
            tool_name="private_tool",
            input={"secret": "private"},
            observed_at=datetime.now(UTC),
            monotonic_ns=4,
        )
    )
    await session.observe(
        ToolExecutionObservation(
            identity=context.identity,
            phase="completed",
            execution_id="execution-disabled-tool",
            tool_call_id="call-disabled-tool",
            tool_name="private_tool",
            output={"secret": "private"},
            observed_at=datetime.now(UTC),
            monotonic_ns=5,
        )
    )
    await session.observe(
        NativeMessageObservation(
            identity=context.identity,
            namespace=(),
            message=NativeMessageRecord(
                message_type="tool",
                id="disabled-tool-result",
                name="private_tool",
                content={"secret": "private"},
                tool_call_id="call-disabled-tool",
            ),
            observed_at=datetime.now(UTC),
            monotonic_ns=6,
        )
    )
    await _finish(session, context)

    thread = await tracer.get("thread-semantic")
    events = (await thread.events(limit=100)).items

    assert not any(isinstance(event.fact, ToolFact) for event in events)
    assert not any(isinstance(event.fact, ToolExecutionFact) for event in events)
    assert not any(
        isinstance(event.fact, MessageFact) and event.fact.role == "tool"
        for event in events
    )
    assert not any(node.kind == "tool" for node in thread.tree.nodes)


async def test_subgraph_tool_message_uses_its_scoped_tool_name_for_capture() -> None:
    tracer = Tracer(
        capture_policy=CapturePolicy.public_safe(
            tool_rules=(ToolCaptureRule(tool_name="search", result_paths=("/public",)),)
        )
    )
    context = _context(run_id="subgraph-tool")
    session = await _start(tracer, context)
    namespace = ("tools:subagent",)
    await session.observe(
        NativeMessageObservation(
            identity=context.identity,
            namespace=namespace,
            message=NativeMessageRecord(
                message_type="assistant",
                id="assistant-subgraph",
                content="",
                tool_calls=(
                    NativeToolCall(id="call-search", name="search", arguments={}),
                ),
            ),
            observed_at=datetime.now(UTC),
            monotonic_ns=3,
        )
    )
    await session.observe(
        NativeMessageObservation(
            identity=context.identity,
            namespace=namespace,
            message=NativeMessageRecord(
                message_type="tool",
                id="tool-subgraph",
                content={"public": "kept", "private": "hidden"},
                tool_call_id="call-search",
            ),
            observed_at=datetime.now(UTC),
            monotonic_ns=4,
        )
    )
    await _finish(session, context)
    thread = await tracer.get("thread-semantic")
    tool_message = next(
        message for message in thread.messages if message.role == "tool"
    )
    events = (await thread.events(limit=100)).items

    assert tool_message.content == {"/public": "kept"}
    assert all(
        event.fact.content is None
        for event in events
        if isinstance(event.fact, MessageFact) and event.fact.role == "tool"
    )
    assert (
        sum(
            1
            for event in events
            if isinstance(event.fact, ToolFact)
            and event.fact.phase == "result"
            and event.fact.content is not None
        )
        == 1
    )


async def test_runtime_task_payloads_are_structural_metadata_only() -> None:
    tracer = Tracer()
    context = _context(run_id="tasks")
    session = await _start(tracer, context)
    now = datetime.now(UTC)
    await session.observe(
        NativeTaskObservation(
            identity=context.identity,
            namespace=(),
            phase="start",
            task_id="task-1",
            name="model",
            triggers=("branch:to:model",),
            input={"messages": [{"content": "duplicated state"}]},
            observed_at=now,
            monotonic_ns=3,
        )
    )
    await session.observe(
        NativeTaskObservation(
            identity=context.identity,
            namespace=(),
            phase="result",
            task_id="task-1",
            name="model",
            result={"messages": [{"content": "duplicated result"}]},
            observed_at=now,
            monotonic_ns=4,
        )
    )
    await _finish(session, context)
    events = (await (await tracer.get("thread-semantic")).events(limit=100)).items
    tasks = [event.fact for event in events if isinstance(event.fact, RuntimeTaskFact)]

    assert tasks[0].input is not None
    assert tasks[0].input.disposition == "inline"
    assert tasks[0].input.value == {
        "$type": "structural_metadata",
        "dataType": "object",
        "sourceSafeSizeBytes": 45,
        "topLevelKeys": ["messages"],
    }
    assert tasks[1].result is not None
    assert tasks[1].result.disposition == "inline"
    assert tasks[1].result.value == {
        "$type": "structural_metadata",
        "dataType": "object",
        "sourceSafeSizeBytes": 46,
        "topLevelKeys": ["messages"],
    }
    assert "duplicated state" not in json.dumps(
        [event.model_dump(mode="json", by_alias=True) for event in events]
    )
    assert "duplicated result" not in json.dumps(
        [event.model_dump(mode="json", by_alias=True) for event in events]
    )


async def test_private_channels_do_not_affect_task_or_extra_structural_metadata() -> (
    None
):
    tracer = Tracer()
    context = _context(
        run_id="private-structural-metadata",
        private_state_keys=("_private_runtime",),
    )
    session = await _start(tracer, context)
    now = datetime.now(UTC)
    await session.observe(
        NativeTaskObservation(
            identity=context.identity,
            namespace=(),
            phase="start",
            task_id="private-task",
            name="model",
            triggers=("branch:to:model",),
            input={"public": "visible", "_private_runtime": "x" * 500},
            observed_at=now,
            monotonic_ns=3,
        )
    )
    await session.observe(
        NativeExtraObservation(
            identity=context.identity,
            namespace=(),
            mode="debug",
            data_type="builtins.dict",
            safe_size_bytes=10_000,
            top_level_keys=("_private_runtime", "public"),
            observed_at=now,
            monotonic_ns=4,
        )
    )
    await _finish(session, context)

    events = (await (await tracer.get("thread-semantic")).events(limit=100)).items
    task = next(
        event.fact for event in events if isinstance(event.fact, RuntimeTaskFact)
    )
    extra = next(
        event.fact for event in events if isinstance(event.fact, NativeExtraFact)
    )
    encoded = "".join(event.model_dump_json(by_alias=True) for event in events)

    assert task.input is not None
    assert task.input.value == {
        "$type": "structural_metadata",
        "dataType": "object",
        "sourceSafeSizeBytes": 20,
        "topLevelKeys": ["public"],
    }
    assert extra.top_level_keys == ("public",)
    assert "safeSizeBytes" not in extra.model_dump(mode="json", by_alias=True)
    assert "_private_runtime" not in encoded
    assert "x" * 100 not in encoded


async def test_interrupted_runtime_task_remains_waiting_in_the_execution_tree() -> None:
    tracer = Tracer()
    context = _context(run_id="interrupted-task")
    session = await _start(tracer, context)
    now = datetime.now(UTC)
    await session.observe(
        NativeTaskObservation(
            identity=context.identity,
            namespace=(),
            phase="start",
            task_id="task-review",
            name="review_plan",
            triggers=("branch:to:review_plan",),
            input={},
            observed_at=now,
            monotonic_ns=3,
        )
    )
    await session.observe(
        NativeTaskObservation(
            identity=context.identity,
            namespace=(),
            phase="result",
            task_id="task-review",
            name="review_plan",
            result={},
            interrupts=(
                NativeInterruptRecord(
                    id="interrupt-review",
                    value={"kind": "plan_review"},
                ),
            ),
            observed_at=now,
            monotonic_ns=4,
        )
    )
    await _finish(session, context, outcome="interrupted")

    thread = await tracer.get("thread-semantic")
    task = next(node for node in thread.tree.nodes if node.kind == "task")
    facts = [
        event.fact
        for event in (await thread.events(limit=100)).items
        if isinstance(event.fact, RuntimeTaskFact)
    ]

    assert facts[-1].interrupt_ids == ("interrupt-review",)
    assert task.status == "waiting"
    assert task.completed_at is None


async def test_parent_task_result_completes_its_direct_subagent_before_interrupt() -> (
    None
):
    tracer = Tracer()
    context = _context(run_id="completed-subagent")
    session = await _start(tracer, context)
    now = datetime.now(UTC)
    parent_task_id = "parent-task"
    namespace = (f"create_plan:{parent_task_id}",)
    await session.observe(
        NativeTaskObservation(
            identity=context.identity,
            namespace=(),
            phase="start",
            task_id=parent_task_id,
            name="create_plan",
            triggers=("branch:to:create_plan",),
            input={},
            observed_at=now,
            monotonic_ns=3,
        )
    )
    await session.observe(
        NativeTaskObservation(
            identity=context.identity,
            namespace=namespace,
            phase="start",
            task_id="child-task",
            name="model",
            triggers=("branch:to:model",),
            input={},
            observed_at=now,
            monotonic_ns=4,
        )
    )
    await session.observe(
        NativeTaskObservation(
            identity=context.identity,
            namespace=namespace,
            phase="result",
            task_id="child-task",
            name="model",
            result={},
            observed_at=now,
            monotonic_ns=5,
        )
    )
    await session.observe(
        NativeTaskObservation(
            identity=context.identity,
            namespace=(),
            phase="result",
            task_id=parent_task_id,
            name="create_plan",
            result={},
            observed_at=now,
            monotonic_ns=6,
        )
    )
    await _finish(session, context, outcome="interrupted")

    thread = await tracer.get("thread-semantic")
    facts = [
        event.fact
        for event in (await thread.events(limit=100)).items
        if isinstance(event.fact, SubagentFact)
    ]
    node = next(node for node in thread.tree.nodes if node.kind == "subagent")

    assert [fact.phase for fact in facts] == ["started", "completed"]
    assert facts[-1].status == "succeeded"
    assert node.status == "succeeded"
    assert node.completed_at is not None


async def test_verified_task_tool_adds_subagent_identity_and_input() -> None:
    tracer = Tracer(
        capture_policy=CapturePolicy.public_safe(
            tool_rules=(
                ToolCaptureRule(
                    tool_name="task",
                    argument_paths=("",),
                    result_paths=("",),
                ),
            )
        )
    )
    context = _context(run_id="verified-subagent")
    session = await _start(tracer, context)
    now = datetime.now(UTC)
    task_arguments: dict[str, JsonValue] = {
        "description": "Research the current contract",
        "subagent_type": "researcher",
    }
    task_input: list[JsonValue] = [
        {
            "name": "task",
            "args": task_arguments,
            "id": "call-task",
            "type": "tool_call",
        }
    ]
    await session.observe(
        NativeMessageObservation(
            identity=context.identity,
            namespace=(),
            message=NativeMessageRecord(
                message_type="assistant",
                id="assistant-task-call",
                content="",
                tool_calls=(
                    NativeToolCall(
                        id="call-task",
                        name="task",
                        arguments=task_arguments,
                    ),
                ),
            ),
            observed_at=now,
            monotonic_ns=3,
        )
    )
    await session.observe(
        NativeTaskObservation(
            identity=context.identity,
            namespace=(),
            phase="start",
            task_id="parent-task",
            name="tools",
            triggers=("branch:to:tools",),
            input=task_input,
            observed_at=now,
            monotonic_ns=4,
        )
    )
    namespace = ("tools:parent-task",)
    await session.observe(
        NativeTaskObservation(
            identity=context.identity,
            namespace=namespace,
            phase="start",
            task_id="child-model",
            name="model",
            input={},
            observed_at=now,
            monotonic_ns=5,
        )
    )
    await session.observe(
        NativeTaskObservation(
            identity=context.identity,
            namespace=(),
            phase="result",
            task_id="parent-task",
            name="tools",
            result={},
            observed_at=now,
            monotonic_ns=6,
        )
    )
    await session.observe(
        NativeMessageObservation(
            identity=context.identity,
            namespace=(),
            message=NativeMessageRecord(
                message_type="tool",
                id="task-result",
                name="task",
                content="Research complete",
                tool_call_id="call-task",
                tool_status="success",
            ),
            observed_at=now,
            monotonic_ns=7,
        )
    )
    await _finish(session, context)

    thread = await tracer.get("thread-semantic")
    facts = [
        event.fact
        for event in (await thread.events(limit=100)).items
        if isinstance(event.fact, SubagentFact)
    ]
    subagent = next(node for node in thread.tree.nodes if node.kind == "subagent")
    parent_tool = next(
        node
        for node in thread.tree.nodes
        if node.kind == "tool" and node.label == "task"
    )

    assert facts[0].parent_tool_call_id == "call-task"
    assert facts[0].input is not None
    assert subagent.source_id == "call-task"
    assert subagent.label == "researcher"
    assert subagent.input == task_arguments
    assert parent_tool.result == "Research complete"


async def test_grouped_parallel_task_result_completes_every_direct_subagent() -> None:
    """Indexed child namespaces must retain the exact owning parent task ID."""

    tracer = Tracer(
        capture_policy=CapturePolicy.public_safe(
            tool_rules=(
                ToolCaptureRule(
                    tool_name="task",
                    argument_paths=("",),
                    result_paths=("",),
                ),
            )
        )
    )
    context = _context(run_id="parallel-subagents")
    session = await _start(tracer, context)
    now = datetime.now(UTC)
    parent_task_id = "parallel-parent"
    calls: list[JsonValue] = [
        {
            "name": "task",
            "args": {
                "description": "Inspect the first independent area",
                "subagent_type": "researcher",
            },
            "id": "call-task-a",
            "type": "tool_call",
        },
        {
            "name": "task",
            "args": {
                "description": "Inspect the second independent area",
                "subagent_type": "general-purpose",
            },
            "id": "call-task-b",
            "type": "tool_call",
        },
    ]
    await session.observe(
        NativeTaskObservation(
            identity=context.identity,
            namespace=(),
            phase="start",
            task_id=parent_task_id,
            name="tools",
            input=calls,
            observed_at=now,
            monotonic_ns=3,
        )
    )
    for index in range(2):
        await session.observe(
            NativeTaskObservation(
                identity=context.identity,
                namespace=(f"tools:{parent_task_id}:{index}",),
                phase="start",
                task_id=f"child-{index}",
                name="model",
                input={},
                observed_at=now,
                monotonic_ns=4 + index,
            )
        )
    await session.observe(
        NativeTaskObservation(
            identity=context.identity,
            namespace=(),
            phase="result",
            task_id=parent_task_id,
            name="tools",
            result={},
            observed_at=now,
            monotonic_ns=6,
        )
    )
    await _finish(session, context)

    thread = await tracer.get("thread-semantic")
    subagents = [node for node in thread.tree.nodes if node.kind == "subagent"]

    assert len(subagents) == 2
    assert {node.source_id for node in subagents} == {"call-task-a", "call-task-b"}
    assert {node.status for node in subagents} == {"succeeded"}
    assert all(node.completed_at is not None for node in subagents)


async def test_private_state_and_provider_reasoning_never_enter_the_ledger() -> None:
    tracer = Tracer()
    context = _context(
        run_id="privacy",
        private_state_keys=("_private_runtime",),
    ).model_copy(
        update={
            "input": {
                "messages": [
                    {"role": "user", "id": "user-privacy", "content": "visible"}
                ],
                "_private_runtime": "private-input-value",
            }
        }
    )
    session = await _start(tracer, context)
    now = datetime.now(UTC)
    await session.observe(
        NativeTaskObservation(
            identity=context.identity,
            namespace=(),
            phase="result",
            task_id="privacy-task",
            name="model",
            result={
                "additional_kwargs": {
                    "reasoning_content": "provider-private-reasoning"
                },
                "reasoning_content": "business-reasoning",
                "password": "credential-value",
            },
            observed_at=now,
            monotonic_ns=3,
        )
    )
    await session.observe(
        NativeStateObservation(
            identity=context.identity,
            namespace=(),
            state={
                "public": "visible-state",
                "_private_runtime": "private-state-value",
            },
            observed_at=now,
            monotonic_ns=4,
        )
    )
    await _finish(session, context)
    page = await (await tracer.get("thread-semantic")).events(limit=100)
    encoded = page.model_dump_json(by_alias=True)

    assert "provider-private-reasoning" not in encoded
    assert "private-input-value" not in encoded
    assert "private-state-value" not in encoded
    assert "credential-value" not in encoded
    assert "business-reasoning" not in encoded
    assert "visible-state" in encoded


async def test_plan_payload_is_stored_once_and_still_projects_into_state() -> None:
    tracer = Tracer()
    context = _context(run_id="plan")
    session = await _start(tracer, context)
    plan = {"status": "awaiting_review", "revision": 2, "draft": "visible"}
    await session.observe(
        NativeStateObservation(
            identity=context.identity,
            namespace=(),
            state={"tinkerfin_plan": plan, "other": "value"},
            observed_at=datetime.now(UTC),
            monotonic_ns=3,
        )
    )
    await _finish(session, context)
    thread = await tracer.get("thread-semantic")
    events = (await thread.events(limit=100)).items
    plan_facts = [
        event.fact for event in events if isinstance(event.fact, PlanRevisionFact)
    ]
    state_facts = [
        event.fact for event in events if isinstance(event.fact, StateRevisionFact)
    ]

    assert len(plan_facts) == 1
    assert plan_facts[0].plan.value == plan
    assert state_facts[0].changes.value == {"other": "value"}
    assert thread.state.root == {"tinkerfin_plan": plan, "other": "value"}


async def test_interaction_resolves_across_resume_and_keeps_one_turn() -> None:
    tracer = Tracer()
    initial = _context(run_id="interrupt")
    first = await _start(tracer, initial)
    await first.observe(
        NativeStateObservation(
            identity=initial.identity,
            namespace=(),
            state={"todos": [{"content": "Wait", "status": "pending"}]},
            interrupts=(
                NativeInterruptRecord(
                    id="interrupt-1",
                    value={"kind": "plan_review", "message": "Review"},
                ),
            ),
            observed_at=datetime.now(UTC),
            monotonic_ns=3,
        )
    )
    await _finish(first, initial, outcome="interrupted")
    interrupted = await tracer.get("thread-semantic")
    assert [item.source_id for item in interrupted.summary.pending_interactions] == [
        "interrupt-1"
    ]

    resumed = _context(
        run_id="resume",
        input_kind="resume",
        parent_run_id="interrupt",
        resume=(
            RunResumeSummary(
                interrupt_id="interrupt-1",
                status="resolved",
                decision="approve",
            ),
        ),
    )
    second = await _start(tracer, resumed)
    await second.observe(
        RunResumeCheckpointedObservation(
            identity=resumed.identity,
            marker_id="secret-derived-checkpoint-marker",
            native_interrupt_ids=("interrupt-1",),
            observed_at=datetime.now(UTC),
            monotonic_ns=3,
        )
    )
    await second.observe(
        NativeStateObservation(
            identity=resumed.identity,
            namespace=(),
            state={"todos": [{"content": "Continue", "status": "completed"}]},
            observed_at=datetime.now(UTC),
            monotonic_ns=4,
        )
    )
    await _finish(second, resumed)
    thread = await tracer.get("thread-semantic")
    events = (await thread.events(limit=100)).items
    interaction_facts = [
        event.fact for event in events if isinstance(event.fact, InteractionFact)
    ]
    resolved_seq = next(
        event.trace_seq
        for event in events
        if isinstance(event.fact, InteractionFact) and event.fact.phase == "resolved"
    )
    checkpoint_seq = next(
        event.trace_seq
        for event in events
        if getattr(event.fact, "phase", None) == "resume_checkpointed"
    )

    assert [fact.phase for fact in interaction_facts] == ["opened", "resolved"]
    assert resolved_seq < checkpoint_seq
    assert "secret-derived-checkpoint-marker" not in "".join(
        event.model_dump_json(by_alias=True) for event in events
    )
    assert thread.interactions[0].status == "resolved"
    assert thread.summary.pending_interactions == ()
    assert len(thread.tree.roots) == 1
    assert len([node for node in thread.tree.nodes if node.kind == "run"]) == 2
    assert any(
        isinstance(event.fact, MessageFact)
        and event.fact.role == "user"
        and event.fact.source_message_id == "user-interrupt"
        for event in events
    )


async def test_root_and_colliding_subgraph_paths_remain_separate_state_scopes() -> None:
    tracer = Tracer()
    context = _context(run_id="state-scopes")
    session = await _start(tracer, context)
    now = datetime.now(UTC)
    for index, (namespace, value) in enumerate(
        (
            ((), "root"),
            (("a/b",), "single-component"),
            (("a", "b"), "two-components"),
        ),
        start=3,
    ):
        await session.observe(
            NativeStateObservation(
                identity=context.identity,
                namespace=namespace,
                state={"value": value},
                observed_at=now,
                monotonic_ns=index,
            )
        )
    await _finish(session, context)
    state = (await tracer.get("thread-semantic")).state

    assert state.root == {"value": "root"}
    assert state.subgraphs == {
        '["a/b"]': {"value": "single-component"},
        '["a","b"]': {"value": "two-components"},
    }


async def test_tool_review_interaction_applies_the_tool_argument_allowlist() -> None:
    tracer = Tracer(
        capture_policy=CapturePolicy.public_safe(
            tool_rules=(
                ToolCaptureRule(
                    tool_name="search",
                    argument_paths=("/query",),
                    include_review_description=True,
                ),
            )
        )
    )
    context = _context(run_id="interaction-tool")
    session = await _start(tracer, context)
    await session.observe(
        NativeStateObservation(
            identity=context.identity,
            namespace=(),
            state={},
            messages=(
                NativeMessageRecord(
                    message_type="assistant",
                    id="assistant-tool-review",
                    content="",
                    tool_calls=(
                        NativeToolCall(
                            id="call-search-review",
                            name="search",
                            arguments={
                                "query": "public query",
                                "token": "private token",
                            },
                        ),
                    ),
                ),
            ),
            interrupts=(
                NativeInterruptRecord(
                    id="tool-review",
                    value={
                        "action_requests": [
                            {
                                "name": "search",
                                "args": {
                                    "query": "public query",
                                    "token": "private token",
                                },
                                "description": "Review search",
                            }
                        ],
                        "review_configs": [
                            {
                                "action_name": "search",
                                "allowed_decisions": ["approve", "reject"],
                            }
                        ],
                    },
                ),
            ),
            observed_at=datetime.now(UTC),
            monotonic_ns=3,
        )
    )
    await _finish(session, context, outcome="interrupted")
    thread = await tracer.get("thread-semantic")
    interaction = thread.interactions[0]
    encoded = (await thread.events(limit=100)).model_dump_json(by_alias=True)

    assert isinstance(interaction.payload, dict)
    assert interaction.source_id == "tool-review"
    assert interaction.tool_call_ids == ("call-search-review",)
    actions = interaction.payload["action_requests"]
    assert isinstance(actions, list)
    action = actions[0]
    assert isinstance(action, dict)
    arguments = action["arguments"]
    assert isinstance(arguments, dict)
    assert arguments["value"] == {"/query": "public query"}
    assert action["description"] == "Review search"
    assert "private token" not in encoded


async def test_same_name_review_actions_keep_exact_checkpoint_tool_ids() -> None:
    tracer = Tracer(
        capture_policy=CapturePolicy.public_safe(
            tool_rules=(
                ToolCaptureRule(tool_name="write_file", argument_paths=("/file_path",)),
            )
        )
    )
    context = _context(run_id="same-name-review")
    session = await _start(tracer, context)
    await session.observe(
        NativeStateObservation(
            identity=context.identity,
            namespace=(),
            state={},
            messages=(
                NativeMessageRecord(
                    message_type="assistant",
                    id="assistant-same-name-review",
                    content="",
                    tool_calls=(
                        NativeToolCall(
                            id="call-unreviewed",
                            name="write_todos",
                            arguments={"todos": []},
                        ),
                        NativeToolCall(
                            id="call-write-a",
                            name="write_file",
                            arguments={"file_path": "/a.txt"},
                        ),
                        NativeToolCall(
                            id="call-write-b",
                            name="write_file",
                            arguments={"file_path": "/b.txt"},
                        ),
                    ),
                ),
            ),
            interrupts=(
                NativeInterruptRecord(
                    id="same-name-interrupt",
                    value={
                        "action_requests": [
                            {"name": "write_file", "args": {"file_path": "/a.txt"}},
                            {"name": "write_file", "args": {"file_path": "/b.txt"}},
                        ],
                        "review_configs": [
                            {
                                "action_name": "write_file",
                                "allowed_decisions": ["approve", "reject"],
                            },
                            {
                                "action_name": "write_file",
                                "allowed_decisions": ["approve", "reject"],
                            },
                        ],
                    },
                ),
            ),
            observed_at=datetime.now(UTC),
            monotonic_ns=3,
        )
    )
    await _finish(session, context, outcome="interrupted")

    interaction = (await tracer.get("thread-semantic")).interactions[0]
    assert interaction.tool_call_ids == ("call-write-a", "call-write-b")


async def test_tool_review_ignores_a_completed_historical_duplicate() -> None:
    tracer = Tracer()
    context = _context(run_id="historical-duplicate-review")
    session = await _start(tracer, context)
    arguments: dict[str, JsonValue] = {"file_path": "/same.txt"}
    review_value: JsonValue = {
        "action_requests": [{"name": "write_file", "args": arguments}],
        "review_configs": [
            {
                "action_name": "write_file",
                "allowed_decisions": ["approve"],
            }
        ],
    }
    await session.observe(
        NativeStateObservation(
            identity=context.identity,
            namespace=(),
            state={},
            messages=(
                NativeMessageRecord(
                    message_type="assistant",
                    id="assistant-completed-history",
                    content="",
                    tool_calls=(
                        NativeToolCall(
                            id="call-completed-history",
                            name="write_file",
                            arguments=arguments,
                        ),
                    ),
                ),
                NativeMessageRecord(
                    message_type="tool",
                    id="result-completed-history",
                    name="write_file",
                    content="done",
                    tool_call_id="call-completed-history",
                    tool_status="success",
                ),
                NativeMessageRecord(
                    message_type="assistant",
                    id="assistant-current-review",
                    content="",
                    tool_calls=(
                        NativeToolCall(
                            id="call-current-review",
                            name="write_file",
                            arguments=arguments,
                        ),
                    ),
                ),
            ),
            interrupts=(
                NativeInterruptRecord(
                    id="current-review",
                    value=review_value,
                ),
            ),
            observed_at=datetime.now(UTC),
            monotonic_ns=3,
        )
    )
    await _finish(session, context, outcome="interrupted")

    interaction = (await tracer.get("thread-semantic")).interactions[0]
    assert interaction.tool_call_ids == ("call-current-review",)


async def test_resumed_tool_result_does_not_repeat_pre_interrupt_lifecycle() -> None:
    tracer = Tracer()
    initial = _context(run_id="tool-interrupt")
    first = await _start(tracer, initial)
    await first.observe(
        NativeMessageObservation(
            identity=initial.identity,
            namespace=(),
            message=NativeMessageRecord(
                message_type="assistant",
                id="assistant-tool-interrupt",
                content="",
                tool_calls=(
                    NativeToolCall(
                        id="call-resumed-tool",
                        name="write_file",
                        arguments={"path": "/workspace/report.md"},
                    ),
                ),
            ),
            observed_at=datetime.now(UTC),
            monotonic_ns=3,
        )
    )
    await first.observe(
        NativeStateObservation(
            identity=initial.identity,
            namespace=(),
            state={},
            interrupts=(
                NativeInterruptRecord(
                    id="resume-tool-interrupt",
                    value={"kind": "tool_approval"},
                ),
            ),
            observed_at=datetime.now(UTC),
            monotonic_ns=4,
        )
    )
    await _finish(first, initial, outcome="interrupted")

    resumed = _context(
        run_id="tool-resume",
        input_kind="resume",
        parent_run_id="tool-interrupt",
        resume=(
            RunResumeSummary(
                interrupt_id="resume-tool-interrupt",
                status="resolved",
                decision="approve",
            ),
        ),
    )
    second = await _start(tracer, resumed)
    await second.observe(
        NativeMessageObservation(
            identity=resumed.identity,
            namespace=(),
            message=NativeMessageRecord(
                message_type="tool",
                id="resumed-tool-result",
                name="write_file",
                content="written",
                tool_call_id="call-resumed-tool",
                tool_status="success",
            ),
            observed_at=datetime.now(UTC),
            monotonic_ns=3,
        )
    )
    await _finish(second, resumed)
    events = (await (await tracer.get("thread-semantic")).events(limit=100)).items
    tool_facts = [
        event.fact
        for event in events
        if isinstance(event.fact, ToolFact)
        and event.fact.source_tool_call_id == "call-resumed-tool"
    ]

    assert [fact.phase for fact in tool_facts] == [
        "started",
        "arguments",
        "completed",
        "result",
    ]
    assert tool_facts[-1].identity.run_id == "tool-resume"


async def test_tool_review_arguments_are_metadata_only_without_an_allowlist() -> None:
    tracer = Tracer(capture_policy=CapturePolicy.public_safe())
    context = _context(run_id="interaction-default")
    session = await _start(tracer, context)
    await session.observe(
        NativeStateObservation(
            identity=context.identity,
            namespace=(),
            state={},
            messages=(
                NativeMessageRecord(
                    message_type="assistant",
                    id="assistant-default-review",
                    content="",
                    tool_calls=(
                        NativeToolCall(
                            id="call-execute-sql",
                            name="execute_sql",
                            arguments={"query": "SELECT private_value"},
                        ),
                    ),
                ),
            ),
            interrupts=(
                NativeInterruptRecord(
                    id="default-review",
                    value={
                        "action_requests": [
                            {
                                "name": "execute_sql",
                                "args": {"query": "SELECT private_value"},
                                "description": "SELECT private_value",
                            }
                        ],
                        "review_configs": [
                            {
                                "action_name": "execute_sql",
                                "allowed_decisions": ["approve", "reject"],
                            }
                        ],
                    },
                ),
            ),
            observed_at=datetime.now(UTC),
            monotonic_ns=3,
        )
    )
    await _finish(session, context, outcome="interrupted")
    thread = await tracer.get("thread-semantic")
    payload = thread.interactions[0].payload
    encoded = (await thread.events(limit=100)).model_dump_json(by_alias=True)

    assert isinstance(payload, dict)
    actions = payload["action_requests"]
    assert isinstance(actions, list)
    action = actions[0]
    assert isinstance(action, dict)
    arguments = action["arguments"]
    assert isinstance(arguments, dict)
    assert arguments["disposition"] == "omitted"
    assert arguments["reason"] == "tool_content_metadata_only"
    assert "SELECT private_value" not in encoded


async def test_oversized_pending_interaction_fails_before_an_unrecoverable_pause() -> (
    None
):
    limits = TraceLimits(
        max_event_bytes=1024,
        max_thread_bytes=64 * 1024,
        max_tracer_bytes=64 * 1024,
        terminal_reserve_bytes_per_run=4 * 1024,
    )
    tracer = Tracer(limits=limits)
    context = _context(run_id="oversized-interaction")
    session = await _start(tracer, context)

    with pytest.raises(TraceCaptureRejected, match="Pending interaction payload"):
        await session.observe(
            NativeStateObservation(
                identity=context.identity,
                namespace=(),
                state={},
                interrupts=(
                    NativeInterruptRecord(
                        id="oversized-interaction",
                        value={"kind": "input_required", "message": "x" * 4096},
                    ),
                ),
                observed_at=datetime.now(UTC),
                monotonic_ns=3,
            )
        )

    await session.aclose()


async def _omitted_tool_result_fingerprint(secret: str) -> str:
    tracer = Tracer(capture_policy=CapturePolicy.public_safe())
    context = _context(run_id="fingerprint")
    session = await _start(tracer, context)
    message = NativeMessageRecord(
        message_type="tool",
        id="private-tool-message",
        name="search",
        content=secret,
        tool_call_id="private-call",
        tool_status="success",
    )
    now = datetime.now(UTC)
    await session.observe(
        NativeMessageObservation(
            identity=context.identity,
            namespace=(),
            message=message,
            observed_at=now,
            monotonic_ns=3,
        )
    )
    await session.observe(
        NativeStateObservation(
            identity=context.identity,
            namespace=(),
            state={},
            messages=(message,),
            observed_at=now,
            monotonic_ns=4,
        )
    )
    await _finish(session, context)
    events = (await (await tracer.get("thread-semantic")).events(limit=100)).items
    return next(
        fact.fingerprint
        for event in events
        if isinstance((fact := event.fact), MessageFact)
        and fact.source_message_id == "private-tool-message"
        and fact.fingerprint is not None
    )


async def test_omitted_tool_values_do_not_participate_in_message_digests() -> None:
    first = await _omitted_tool_result_fingerprint("secret-A")
    second = await _omitted_tool_result_fingerprint("secret-B")

    assert first == second


async def test_removed_message_can_be_readded_once_with_the_same_id() -> None:
    tracer = Tracer()
    context = _context(run_id="message-readd")
    session = await _start(tracer, context)
    now = datetime.now(UTC)
    first = NativeMessageRecord(
        message_type="assistant",
        id="reused-message",
        content="first",
    )
    second = first.model_copy(update={"content": "second"})
    await session.observe(
        NativeStateObservation(
            identity=context.identity,
            namespace=(),
            state={},
            messages=(first,),
            observed_at=now,
            monotonic_ns=3,
        )
    )
    await session.observe(
        NativeStateObservation(
            identity=context.identity,
            namespace=(),
            state={},
            messages=(),
            observed_at=now,
            monotonic_ns=4,
        )
    )
    await session.observe(
        NativeStateObservation(
            identity=context.identity,
            namespace=(),
            state={},
            messages=(second,),
            observed_at=now,
            monotonic_ns=5,
        )
    )
    await _finish(session, context)
    messages = (await tracer.get("thread-semantic")).messages

    reused = [message for message in messages if message.source_id == "reused-message"]
    assert len(reused) == 1
    assert reused[0].content == "second"


async def test_resume_resolves_the_original_subgraph_interaction_scope() -> None:
    tracer = Tracer()
    initial = _context(run_id="subgraph-interrupt")
    first = await _start(tracer, initial)
    namespace = ("tools:subagent",)
    await first.observe(
        NativeStateObservation(
            identity=initial.identity,
            namespace=namespace,
            state={},
            interrupts=(
                NativeInterruptRecord(
                    id="subgraph-interrupt-id",
                    value={"kind": "tool_approval"},
                ),
            ),
            observed_at=datetime.now(UTC),
            monotonic_ns=3,
        )
    )
    await _finish(first, initial, outcome="interrupted")

    resumed = _context(
        run_id="subgraph-resume",
        input_kind="resume",
        parent_run_id="subgraph-interrupt",
        resume=(
            RunResumeSummary(
                interrupt_id="subgraph-interrupt-id",
                status="resolved",
                decision="approve",
            ),
        ),
    )
    second = await _start(tracer, resumed)
    await _finish(second, resumed)
    interactions = (await tracer.get("thread-semantic")).interactions

    assert len(interactions) == 1
    assert interactions[0].namespace == namespace
    assert interactions[0].status == "resolved"


async def test_native_resume_resolves_one_unambiguous_pending_interrupt() -> None:
    tracer = Tracer()
    initial = _context(run_id="native-interrupt")
    first = await _start(tracer, initial)
    await first.observe(
        NativeStateObservation(
            identity=initial.identity,
            namespace=(),
            state={},
            interrupts=(
                NativeInterruptRecord(
                    id="native-only-interrupt",
                    value={"kind": "input_required"},
                ),
            ),
            observed_at=datetime.now(UTC),
            monotonic_ns=3,
        )
    )
    await _finish(first, initial, outcome="interrupted")

    resumed = _context(
        run_id="native-resume",
        input_kind="resume",
        parent_run_id="native-interrupt",
    )
    second = await _start(tracer, resumed)
    await _finish(second, resumed)
    thread = await tracer.get("thread-semantic")
    run_facts = [
        event.fact
        for event in (await thread.events(limit=100)).items
        if isinstance(event.fact, RunFact)
        and event.fact.identity.run_id == "native-resume"
    ]

    assert thread.interactions[0].status == "resolved"
    assert next(
        fact for fact in run_facts if fact.phase == "resumed"
    ).interrupt_ids == ("native-only-interrupt",)


async def test_turn_uses_only_the_authoritative_top_level_messages_channel() -> None:
    tracer = Tracer()
    context = RunSourceContext(
        identity=RunIdentity(threadId="thread-semantic", runId="authoritative-user"),
        runtime_profile="deepagents-v2",
        input_kind="ordinary",
        input={
            "messages": [
                {
                    "role": "user",
                    "id": "real-user",
                    "content": "real request",
                }
            ],
            "business": {
                "role": "user",
                "id": "fake-user",
                "content": "private business value",
            },
        },
        config={},
    )
    session = await _start(tracer, context)
    await _finish(session, context)

    thread = await tracer.get("thread-semantic")
    encoded = "".join(
        event.model_dump_json(by_alias=True)
        for event in (await thread.events(limit=100)).items
    )

    assert [(message.source_id, message.content) for message in thread.messages] == [
        ("real-user", "real request")
    ]
    assert "fake-user" not in encoded
    assert "private business value" not in encoded


async def test_private_state_filter_removes_only_declared_top_level_channels() -> None:
    tracer = Tracer()
    context = _context(
        run_id="top-level-private-state",
        private_state_keys=("_tinkerfin_resume",),
    )
    session = await _start(tracer, context)
    await session.observe(
        NativeStateObservation(
            identity=context.identity,
            namespace=(),
            state={
                "_tinkerfin_resume": "private runtime value",
                "business": {"_tinkerfin_resume": "public business value"},
            },
            observed_at=datetime.now(UTC),
            monotonic_ns=3,
        )
    )
    await _finish(session, context)

    thread = await tracer.get("thread-semantic")
    encoded = "".join(
        event.model_dump_json(by_alias=True)
        for event in (await thread.events(limit=100)).items
    )

    assert thread.state.root == {
        "business": {"_tinkerfin_resume": "public business value"}
    }
    assert "private runtime value" not in encoded


async def test_terminal_fact_does_not_duplicate_an_unbounded_interrupt_batch() -> None:
    tracer = Tracer()
    context = _context(run_id="large-interrupt-terminal")
    session = await _start(tracer, context)
    now = datetime.now(UTC)
    interrupt_ids = tuple(f"interrupt-{index}-{'x' * 880}" for index in range(1200))

    await session.observe(
        RunTerminalObservation(
            identity=context.identity,
            outcome="interrupted",
            interrupt_ids=interrupt_ids,
            observed_at=now,
            monotonic_ns=90,
        )
    )
    await session.observe(
        RunClosedObservation(
            identity=context.identity,
            outcome="interrupted",
            observed_at=now,
            monotonic_ns=91,
        )
    )
    await session.aclose()

    thread = await tracer.get("thread-semantic")
    run_facts = [
        event.fact
        for event in (await thread.events(limit=100)).items
        if isinstance(event.fact, RunFact) and event.fact.phase == "terminal"
    ]
    assert thread.status.execution == "waiting"
    assert thread.completeness.missing_tail is False
    assert run_facts[0].interrupt_ids == ()


async def test_maximal_run_identity_remains_usable_by_the_tracer() -> None:
    run_id = "r" * 1024
    tracer = Tracer()
    context = RunSourceContext(
        identity=RunIdentity(threadId="thread-maximal-identity", runId=run_id),
        runtime_profile="deepagents-v2",
        input_kind="ordinary",
        input={"messages": []},
        config={},
    )
    session = await _start(tracer, context)
    await _finish(session, context)

    thread = await tracer.get("thread-maximal-identity")
    assert thread.status.head_run_id == run_id
    assert thread.status.execution == "succeeded"


async def test_concurrent_runs_have_disjoint_bounded_source_observation_ids() -> None:
    tracer = Tracer()
    first_context = _context(run_id="concurrent-source-a")
    second_context = _context(run_id="concurrent-source-b")
    first = await _start(tracer, first_context)
    second = await _start(tracer, second_context)
    await _finish(first, first_context)
    await _finish(second, second_context)

    first_events = (
        await (
            await tracer.get(
                "thread-semantic",
                head_run_id="concurrent-source-a",
            )
        ).events(limit=100)
    ).items
    second_events = (
        await (
            await tracer.get(
                "thread-semantic",
                head_run_id="concurrent-source-b",
            )
        ).events(limit=100)
    ).items
    first_ids = {event.fact.source_observation_id for event in first_events}
    second_ids = {event.fact.source_observation_id for event in second_events}

    assert first_ids.isdisjoint(second_ids)
    assert all(len(source_id) < 256 for source_id in first_ids | second_ids)


async def test_native_remove_message_removes_the_target_without_waiting_for_state() -> (
    None
):
    tracer = Tracer()
    context = _context(run_id="remove-message")
    session = await _start(tracer, context)
    now = datetime.now(UTC)
    await session.observe(
        NativeMessageObservation(
            identity=context.identity,
            namespace=(),
            message=NativeMessageRecord(
                message_type="assistant",
                id="remove-target",
                content="temporary",
            ),
            observed_at=now,
            monotonic_ns=3,
        )
    )
    await session.observe(
        NativeMessageObservation(
            identity=context.identity,
            namespace=(),
            message=NativeMessageRecord(
                message_type="remove",
                id="remove-target",
                content="",
            ),
            observed_at=now,
            monotonic_ns=4,
        )
    )
    await _finish(session, context)
    thread = await tracer.get("thread-semantic")

    assert all(message.source_id != "remove-target" for message in thread.messages)
