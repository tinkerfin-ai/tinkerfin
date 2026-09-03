"""Canonical Graph reduction from current semantic facts."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TypeVar

from pydantic import JsonValue

from tinkerfin_contracts import RunIdentity
from tinkerfin_tracing._graph_projection import (
    project_trace_graph_node,
    reduce_trace_graph_records,
)
from tinkerfin_tracing._graph_reducer import (
    apply_graph_node_mutation,
    effective_graph_nodes,
    graph_node_mutations,
    reduce_graph_mutations,
)
from tinkerfin_tracing._ids import scope_id
from tinkerfin_tracing.capture import CapturedValue
from tinkerfin_tracing.facts import (
    AgentStepFact,
    MessageFact,
    ModelCallFact,
    RunFact,
    RuntimeTaskFact,
    SkillFact,
    SubagentFact,
    ToolExecutionFact,
    ToolFact,
    TraceEvent,
    TraceFactBase,
    TraceSemanticFact,
    TurnFact,
)
from tinkerfin_tracing.graph import (
    TraceGraphLinkIssue,
    TraceGraphNodeKind,
    TraceGraphNodeStatus,
)

_START = datetime(2026, 9, 2, tzinfo=UTC)
_IDENTITY = RunIdentity(threadId="thread-graph", runId="run-graph")
FactT = TypeVar("FactT", bound=TraceFactBase)


def _captured(value: JsonValue) -> CapturedValue:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return CapturedValue(
        disposition="inline",
        safe_size_bytes=len(encoded),
        value=value,
    )


def _fact(
    fact_type: type[FactT],
    sequence: int,
    **values: object,
) -> FactT:
    return fact_type.model_validate(
        {
            "sourceObservationId": f"observation-{sequence}",
            "identity": _IDENTITY,
            "occurredAt": _START + timedelta(milliseconds=sequence),
            "monotonicNs": sequence,
            **values,
        }
    )


def _event(sequence: int, fact: TraceSemanticFact) -> TraceEvent:
    return TraceEvent(
        event_id=f"event-{sequence}",
        trace_seq=sequence,
        generation="generation-graph",
        fact=fact,
        persisted_bytes=1,
    )


def test_graph_reducer_roots_each_turn_at_human_message() -> None:
    human_id = scope_id("message", (), "human-1")
    events = (
        _event(
            1,
            _fact(RunFact, 1, phase="started", inputKind="ordinary"),
        ),
        _event(
            2,
            _fact(
                TurnFact,
                2,
                turnId="turn-1",
                userMessageId="human-1",
            ),
        ),
        _event(
            3,
            _fact(
                MessageFact,
                3,
                phase="reconciled",
                messageId=human_id,
                sourceMessageId="human-1",
                role="user",
                content=_captured("value"),
            ),
        ),
    )

    mutations = graph_node_mutations(events)

    human = [item for item in mutations if item.node_id == human_id]
    run = [item for item in mutations if item.kind is TraceGraphNodeKind.RUN]
    assert human[0].kind is TraceGraphNodeKind.HUMAN_MESSAGE
    assert len(human) == 1
    assert human[-1].result_seq == 3
    assert len(run) == 1
    assert (
        next(
            item.structural_parent_id
            for item in mutations
            if item.node_id == run[0].node_id and item.structural_parent_id is not None
        )
        == human_id
    )


def test_graph_reducer_uses_a_functional_name_for_the_main_agent() -> None:
    events = (
        _event(
            1,
            _fact(
                AgentStepFact,
                1,
                phase="started",
                callId="root-callback",
                stepKind="agent",
                name="LangGraph",
            ),
        ),
    )

    (mutation,) = graph_node_mutations(events)

    assert mutation.kind is TraceGraphNodeKind.AGENT
    assert mutation.name == "Agent"


def test_graph_reducer_keeps_internal_user_messages_out_of_the_turn_tree() -> None:
    event = _event(
        1,
        _fact(
            MessageFact,
            1,
            namespace=("create_plan:task-1",),
            phase="reconciled",
            messageId=scope_id(
                "message",
                ("create_plan:task-1",),
                "internal-context",
            ),
            sourceMessageId="internal-context",
            role="user",
            content=_captured("Trusted Plan workflow context"),
        ),
    )

    assert graph_node_mutations((event,)) == ()


def test_graph_reducer_keeps_tool_messages_inside_the_tool_node() -> None:
    event = _event(
        1,
        _fact(
            MessageFact,
            1,
            phase="reconciled",
            messageId=scope_id("message", (), "tool-message"),
            sourceMessageId="tool-message",
            role="tool",
            toolCallId="tool-call",
            content=_captured("result"),
        ),
    )

    assert graph_node_mutations((event,)) == ()


def test_rebuild_reduces_one_node_across_ledger_page_boundaries() -> None:
    started = _event(
        1,
        _fact(
            ToolFact,
            1,
            phase="started",
            toolCallId="tool-1",
            sourceToolCallId="tool-1",
            toolName="search",
        ),
    )
    completed = _event(
        2,
        _fact(
            ToolFact,
            2,
            phase="result",
            toolCallId="tool-1",
            sourceToolCallId="tool-1",
            toolName="search",
            content=_captured({"ok": True}),
            resultStatus="success",
        ),
    )

    mutations = reduce_graph_mutations(
        (*graph_node_mutations((started,)), *graph_node_mutations((completed,)))
    )

    assert len(mutations) == 1
    assert mutations[0].started_seq == 1
    assert mutations[0].updated_seq == 2
    assert mutations[0].result_seq == 2
    assert mutations[0].status is TraceGraphNodeStatus.SUCCEEDED


def test_graph_reducer_merges_runtime_and_callback_task_identity() -> None:
    task_id = "native-task"
    node_id = scope_id("runtime-task", (), f"{_IDENTITY.run_id}:{task_id}")
    events = (
        _event(
            1,
            _fact(
                RuntimeTaskFact,
                1,
                phase="started",
                taskId=scope_id("task", (), task_id),
                sourceTaskId=task_id,
                taskName="model",
            ),
        ),
        _event(
            2,
            _fact(
                AgentStepFact,
                2,
                phase="started",
                callId=node_id,
                parentCallId="intermediate-chain",
                stepKind="model",
                name="model",
                sourceTaskId=task_id,
            ),
        ),
    )

    mutations = graph_node_mutations(events)

    assert {item.node_id for item in mutations} == {node_id}
    assert all(item.kind is TraceGraphNodeKind.RUNTIME_TASK for item in mutations)
    assert mutations[-1].structural_parent_id == scope_id("agent", (), _IDENTITY.run_id)


def test_graph_reducer_rebuilds_historical_task_backed_middleware_identity() -> None:
    task_id = "middleware-task"
    node_id = scope_id("runtime-task", (), f"{_IDENTITY.run_id}:{task_id}")
    events = (
        _event(
            1,
            _fact(
                AgentStepFact,
                1,
                phase="started",
                callId=scope_id("middleware-call", (), "historical-callback"),
                stepKind="middleware",
                name="TodoListMiddleware.after_model",
                sourceTaskId=task_id,
                middlewareName="TodoListMiddleware",
                hook="after_model",
            ),
        ),
        _event(
            2,
            _fact(
                RuntimeTaskFact,
                2,
                phase="started",
                taskId=scope_id("task", (), task_id),
                sourceTaskId=task_id,
                taskName="TodoListMiddleware.after_model",
            ),
        ),
    )

    mutations = graph_node_mutations(events)

    assert {item.node_id for item in mutations} == {node_id}
    assert all(item.kind is TraceGraphNodeKind.MIDDLEWARE for item in mutations)


def test_graph_reducer_links_an_internal_subgraph_to_its_native_parent_task() -> None:
    source_task_id = "plan-task"
    namespace = (f"create_plan:{source_task_id}",)
    event = _event(
        1,
        _fact(
            SubagentFact,
            1,
            namespace=namespace,
            phase="started",
            subagentId=scope_id("subagent", namespace, namespace[-1]),
            status="running",
        ),
    )

    (mutation,) = graph_node_mutations((event,))

    assert mutation.structural_parent_id == scope_id(
        "runtime-task", (), f"{_IDENTITY.run_id}:{source_task_id}"
    )
    assert mutation.link_issue is None


def test_graph_reducer_links_model_system_and_assistant_without_token_updates() -> None:
    model_id = scope_id("model-call", (), "provider-call")
    assistant_id = scope_id("message", (), "assistant-1")
    request = _captured({})
    events = (
        _event(
            1,
            _fact(
                ModelCallFact,
                1,
                phase="started",
                callId=model_id,
                parentCallId=scope_id("agent", (), _IDENTITY.run_id),
                model="deepseek-chat",
                request=request,
                systemMessagePositions=(0,),
                outputMessageIds=(),
            ),
        ),
        _event(
            2,
            _fact(
                ModelCallFact,
                2,
                phase="first_output",
                callId=model_id,
                outputMessageIds=("assistant-1",),
                systemMessagePositions=(),
            ),
        ),
        _event(
            3,
            _fact(
                MessageFact,
                3,
                phase="content",
                messageId=assistant_id,
                sourceMessageId="assistant-1",
                role="assistant",
                content=_captured("value"),
            ),
        ),
        _event(
            4,
            _fact(
                MessageFact,
                4,
                phase="reconciled",
                messageId=assistant_id,
                sourceMessageId="assistant-1",
                role="assistant",
                content=_captured("value"),
            ),
        ),
    )

    mutations = graph_node_mutations(events)

    assert not any(item.updated_seq == 3 for item in mutations)
    assistant = [item for item in mutations if item.node_id == assistant_id]
    assert len(assistant) == 1
    assert assistant[0].structural_parent_id == model_id
    assert assistant[-1].result_seq == 4
    system = next(
        item for item in mutations if item.kind is TraceGraphNodeKind.SYSTEM_MESSAGE
    )
    assert system.structural_parent_id == model_id
    assert system.request_seq == 1


def test_graph_reducer_aggregates_tool_execution_result_and_skill() -> None:
    model_id = scope_id("model-call", (), "provider-call")
    tool_id = scope_id("tool", (), "tool-call")
    execution_id = scope_id("tool-execution", (), "execution")
    events = (
        _event(
            1,
            _fact(
                ToolFact,
                1,
                phase="started",
                toolCallId=tool_id,
                sourceToolCallId="tool-call",
                parentCallId=model_id,
                toolName="read_file",
            ),
        ),
        _event(
            2,
            _fact(
                ToolExecutionFact,
                2,
                phase="started",
                executionId=execution_id,
                sourceToolCallId="tool-call",
                toolName="read_file",
                input=_captured({}),
            ),
        ),
        _event(
            3,
            _fact(
                SkillFact,
                3,
                skillId=scope_id("skill", (), "execution"),
                executionId=execution_id,
                sourceToolCallId="tool-call",
                name="testing",
                sourcePath="/skills/testing/SKILL.md",
            ),
        ),
        _event(
            4,
            _fact(
                ToolExecutionFact,
                4,
                phase="completed",
                executionId=execution_id,
                sourceToolCallId="tool-call",
                toolName="read_file",
                output=_captured({}),
            ),
        ),
    )

    mutations = graph_node_mutations(events)

    tool_mutations = [item for item in mutations if item.node_id == tool_id]
    assert {item.node_id for item in tool_mutations} == {tool_id}
    assert tool_mutations[0].structural_parent_id == model_id
    assert tool_mutations[-1].status is TraceGraphNodeStatus.SUCCEEDED
    skill = next(item for item in mutations if item.kind is TraceGraphNodeKind.SKILL)
    assert skill.structural_parent_id == tool_id


def test_model_completion_resolves_an_earlier_tool_parent_gap() -> None:
    model_id = scope_id("model-call", (), "provider-call")
    tool_id = scope_id("tool", (), "tool-call")
    started = _event(
        1,
        _fact(
            ToolFact,
            1,
            phase="started",
            toolCallId=tool_id,
            sourceToolCallId="tool-call",
            toolName="read_file",
        ),
    )
    linked = _event(
        2,
        _fact(
            ModelCallFact,
            2,
            phase="completed",
            callId=model_id,
            systemMessagePositions=(),
            outputMessageIds=(),
            toolCallIds=("tool-call",),
        ),
    )

    same_batch = next(
        mutation
        for mutation in graph_node_mutations((started, linked))
        if mutation.node_id == tool_id
    )
    split_batches = next(
        mutation
        for mutation in reduce_graph_mutations(
            (
                *graph_node_mutations((started,)),
                *graph_node_mutations((linked,)),
            )
        )
        if mutation.node_id == tool_id
    )

    for mutation in (same_batch, split_batches):
        assert mutation.structural_parent_id == model_id
        assert mutation.link_issue is not TraceGraphLinkIssue.MISSING_PARENT


def test_child_lineage_parent_evidence_does_not_restore_an_ancestor_gap() -> None:
    parent = _event(
        1,
        _fact(
            ToolFact,
            1,
            phase="started",
            toolCallId="tool-call",
            sourceToolCallId="tool-call",
            toolName="read_file",
        ),
    )
    child_identity = RunIdentity(threadId=_IDENTITY.thread_id, runId="run-child")
    child = _event(
        2,
        _fact(
            ToolFact,
            2,
            identity=child_identity,
            phase="started",
            toolCallId="tool-call",
            sourceToolCallId="tool-call",
            parentCallId="child-model",
            toolName="read_file",
        ),
    )
    revisions = {}
    for mutation in graph_node_mutations((parent, child)):
        apply_graph_node_mutation(revisions, mutation)

    nodes = effective_graph_nodes(
        revisions.values(),
        run_ids=frozenset({_IDENTITY.run_id, child_identity.run_id}),
    )

    assert len(nodes) == 1
    assert nodes[0].structural_parent_id == "child-model"
    assert nodes[0].link_issue is None


def test_tool_result_does_not_replace_the_execution_failure_evidence() -> None:
    tool_id = scope_id("tool", (), "tool-call")
    events = (
        _event(
            1,
            _fact(
                ToolFact,
                1,
                phase="started",
                toolCallId=tool_id,
                sourceToolCallId="tool-call",
                parentCallId="model-call",
                toolName="read_file",
            ),
        ),
        _event(
            2,
            _fact(
                ToolExecutionFact,
                2,
                phase="started",
                executionId="execution",
                sourceToolCallId="tool-call",
                toolName="read_file",
                input=_captured({"path": "missing.txt"}),
            ),
        ),
        _event(
            3,
            _fact(
                ToolExecutionFact,
                3,
                phase="failed",
                executionId="execution",
                sourceToolCallId="tool-call",
                toolName="read_file",
                errorType="FileNotFoundError",
                errorMessage=_captured("missing.txt was not found"),
                failureOrigin=True,
            ),
        ),
        _event(
            4,
            _fact(
                ToolFact,
                4,
                phase="result",
                toolCallId=tool_id,
                sourceToolCallId="tool-call",
                parentCallId="model-call",
                toolName="read_file",
                content=_captured("tool error result"),
                resultStatus="error",
            ),
        ),
    )

    record = reduce_trace_graph_records(
        events,
        run_ids=frozenset({_IDENTITY.run_id}),
    )[0]
    node = project_trace_graph_node(
        record,
        turn_id="turn:failure",
        parent_id=None,
        relationship_missing=False,
    )

    assert node.result == "tool error result"
    assert node.failure is not None
    assert node.failure.error_type == "FileNotFoundError"
    assert node.failure.message == "missing.txt was not found"
