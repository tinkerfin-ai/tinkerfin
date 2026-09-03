"""Materialize public Graph nodes from index metadata and referenced facts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from pydantic import JsonValue

from ._graph_reducer import (
    ReducedTraceGraphRevision,
    apply_graph_node_mutation,
    effective_graph_nodes,
    graph_node_mutations,
)
from .capture import CapturedValue
from .errors import TraceStoreProtocolError
from .facts import (
    AgentStepFact,
    ContextContributionFact,
    InteractionFact,
    MessageFact,
    ModelCallFact,
    PlanRevisionFact,
    RunFact,
    RuntimeTaskFact,
    SkillFact,
    SubagentFact,
    ToolExecutionFact,
    ToolFact,
    TraceEvent,
    TurnFact,
)
from .graph import (
    TECHNICAL_TRACE_GRAPH_NODE_KINDS,
    TraceGraphFailure,
    TraceGraphLinkIssue,
    TraceGraphNode,
    TraceGraphNodeKind,
    TraceGraphTurn,
)
from .store import TraceGraphNodeRecord


def reduce_trace_graph_records(
    events: tuple[TraceEvent, ...],
    *,
    run_ids: frozenset[str],
) -> tuple[TraceGraphNodeRecord, ...]:
    """Replay canonical Graph mutations and bind their exact Ledger facts."""

    revisions: dict[tuple[str, str], ReducedTraceGraphRevision] = {}
    for mutation in graph_node_mutations(events):
        apply_graph_node_mutation(revisions, mutation)
    by_sequence = {event.trace_seq: event for event in events}

    def event_at(sequence: int | None) -> TraceEvent | None:
        if sequence is None:
            return None
        event = by_sequence.get(sequence)
        if event is None:
            raise TraceStoreProtocolError(
                "Trace Graph locator is outside the replayed Ledger prefix"
            )
        return event

    records: list[TraceGraphNodeRecord] = []
    for node in effective_graph_nodes(revisions.values(), run_ids=run_ids):
        started_event = event_at(node.started_seq)
        updated_event = event_at(node.updated_seq)
        if started_event is None or updated_event is None:
            raise TraceStoreProtocolError("Trace Graph node lacks lifecycle facts")
        records.append(
            TraceGraphNodeRecord(
                node_id=node.node_id,
                structural_parent_id=node.structural_parent_id,
                kind=node.kind,
                status=node.status,
                name=node.name,
                run_id=node.run_id,
                namespace=node.namespace,
                agent_name=node.agent_name,
                provider=node.provider,
                model=node.model,
                started_at=node.started_at,
                first_output_at=node.first_output_at,
                completed_at=node.completed_at,
                started_seq=node.started_seq,
                updated_seq=node.updated_seq,
                request_seq=node.request_seq,
                result_seq=node.result_seq,
                failure_seq=node.failure_seq,
                link_issue=node.link_issue,
                started_event=started_event,
                updated_event=updated_event,
                request_event=event_at(node.request_seq),
                result_event=event_at(node.result_seq),
                failure_event=event_at(node.failure_seq),
            )
        )
    return tuple(records)


def _captured(value: CapturedValue | None) -> tuple[JsonValue | None, bool]:
    if value is None:
        return None, False
    return value.value, value.disposition == "omitted"


def _captured_tool(value: CapturedValue | None) -> tuple[JsonValue | None, bool]:
    """Unwrap an explicitly selected RFC 6901 Tool root value."""

    captured, omitted = _captured(value)
    if isinstance(captured, dict) and set(captured) == {""}:
        return captured[""], omitted
    return captured, omitted


def _failure(
    error_type: str,
    *,
    message: CapturedValue | None = None,
    code: str | None = None,
) -> TraceGraphFailure:
    value, _omitted = _captured(message)
    return TraceGraphFailure(
        error_type=error_type,
        message=value if isinstance(value, str) else None,
        code=code,
    )


def _system_message_content(
    node_id: str,
    fact: ModelCallFact,
) -> tuple[JsonValue | None, bool]:
    request, omitted = _captured(fact.request)
    if omitted or not isinstance(request, dict):
        return None, omitted
    messages = request.get("messages")
    if not isinstance(messages, list):
        raise TraceStoreProtocolError("Model request messages are unavailable")
    _prefix, separator, raw_position = node_id.rpartition(":")
    if not separator or not raw_position.isdecimal():
        raise TraceStoreProtocolError("SystemMessage node identity is invalid")
    position = int(raw_position)
    if position >= len(messages) or not isinstance(messages[position], dict):
        raise TraceStoreProtocolError("SystemMessage position is outside the request")
    message = cast(dict[str, JsonValue], messages[position])
    if message.get("messageType") != "system":
        raise TraceStoreProtocolError("SystemMessage position references another role")
    return message.get("content"), False


def project_trace_graph_node(
    record: TraceGraphNodeRecord,
    *,
    turn_id: str,
    parent_id: str | None,
    relationship_missing: bool,
) -> TraceGraphNode:
    """Build one public node without trusting duplicated index detail fields."""

    start = record.started_event.fact
    update = record.updated_event.fact
    request_fact = None if record.request_event is None else record.request_event.fact
    result_fact = None if record.result_event is None else record.result_event.fact
    failure_fact = None if record.failure_event is None else record.failure_event.fact
    content: JsonValue | None = None
    content_omitted = False
    request: JsonValue | None = None
    request_omitted = False
    result: JsonValue | None = None
    result_omitted = False
    usage: JsonValue | None = None
    response_metadata: JsonValue | None = None
    failure: TraceGraphFailure | None = None
    source_id: str | None = None
    class_name: str | None = None
    hooks: tuple[str, ...] = ()
    source_path: str | None = None

    if record.kind in {
        TraceGraphNodeKind.HUMAN_MESSAGE,
        TraceGraphNodeKind.ASSISTANT_MESSAGE,
    }:
        message_fact = result_fact if isinstance(result_fact, MessageFact) else update
        if not isinstance(message_fact, MessageFact):
            if record.kind is TraceGraphNodeKind.HUMAN_MESSAGE and isinstance(
                start, TurnFact
            ):
                source_id = start.user_message_id
            elif not isinstance(start, ModelCallFact):
                raise TraceStoreProtocolError("Message Graph node fact is invalid")
        else:
            source_id = message_fact.source_message_id
            content, content_omitted = _captured(message_fact.content)
    elif record.kind is TraceGraphNodeKind.SYSTEM_MESSAGE:
        if (
            not isinstance(request_fact, ModelCallFact)
            or request_fact.phase != "started"
        ):
            raise TraceStoreProtocolError("SystemMessage Graph node fact is invalid")
        content, content_omitted = _system_message_content(record.node_id, request_fact)
        source_id = record.node_id.rpartition(":")[2]
    elif record.kind is TraceGraphNodeKind.MODEL:
        if (
            not isinstance(request_fact, ModelCallFact)
            or request_fact.phase != "started"
        ):
            raise TraceStoreProtocolError("Model Graph node start fact is invalid")
        request, request_omitted = _captured(request_fact.request)
        source_id = request_fact.call_id
        if isinstance(result_fact, ModelCallFact):
            usage, _usage_omitted = _captured(result_fact.usage)
            response_metadata, _metadata_omitted = _captured(
                result_fact.response_metadata
            )
        if isinstance(failure_fact, ModelCallFact):
            failure = _failure(
                failure_fact.error_type or "model_error",
                message=failure_fact.error_message,
            )
    elif record.kind is TraceGraphNodeKind.TOOL:
        if isinstance(request_fact, ToolExecutionFact):
            request, request_omitted = _captured_tool(request_fact.input)
            source_id = request_fact.source_tool_call_id
        elif isinstance(request_fact, ToolFact):
            request, request_omitted = _captured_tool(request_fact.content)
            source_id = request_fact.source_tool_call_id
        if isinstance(result_fact, ToolExecutionFact):
            result, result_omitted = _captured_tool(result_fact.output)
        elif isinstance(result_fact, ToolFact):
            result, result_omitted = _captured_tool(result_fact.content)
            source_id = source_id or result_fact.source_tool_call_id
        if isinstance(failure_fact, ToolExecutionFact):
            failure = _failure(
                failure_fact.error_type or "tool_error",
                message=failure_fact.error_message,
            )
        elif isinstance(failure_fact, ToolFact):
            failure = TraceGraphFailure(error_type="tool_error")
    elif record.kind in {
        TraceGraphNodeKind.AGENT,
        TraceGraphNodeKind.MIDDLEWARE,
    }:
        step_start = request_fact if isinstance(request_fact, AgentStepFact) else start
        if not isinstance(step_start, AgentStepFact) or step_start.phase != "started":
            raise TraceStoreProtocolError("Agent Graph node start fact is invalid")
        source_id = step_start.source_task_id
        hooks = () if step_start.hook is None else (step_start.hook,)
        if isinstance(failure_fact, AgentStepFact):
            failure = _failure(
                failure_fact.error_type or "agent_error",
                message=failure_fact.error_message,
            )
    elif record.kind is TraceGraphNodeKind.RUNTIME_TASK:
        if isinstance(request_fact, RuntimeTaskFact):
            source_id = request_fact.source_task_id
            request, request_omitted = _captured(request_fact.input)
        elif isinstance(request_fact, AgentStepFact):
            source_id = request_fact.source_task_id
        if isinstance(result_fact, RuntimeTaskFact):
            result, result_omitted = _captured(result_fact.result)
        if isinstance(failure_fact, RuntimeTaskFact):
            failure = TraceGraphFailure(
                error_type=failure_fact.error_type or "runtime_task_error"
            )
        elif isinstance(failure_fact, AgentStepFact):
            failure = _failure(
                failure_fact.error_type or "runtime_task_error",
                message=failure_fact.error_message,
            )
    elif record.kind is TraceGraphNodeKind.SUBAGENT:
        if not isinstance(request_fact, SubagentFact):
            raise TraceStoreProtocolError("Subagent Graph node start fact is invalid")
        source_id = request_fact.parent_tool_call_id
        request, request_omitted = _captured_tool(request_fact.input)
    elif record.kind is TraceGraphNodeKind.SKILL:
        if not isinstance(request_fact, SkillFact):
            raise TraceStoreProtocolError("Skill Graph node fact is invalid")
        source_id = request_fact.execution_id
        source_path = request_fact.source_path
    elif record.kind in {
        TraceGraphNodeKind.MEMORY,
        TraceGraphNodeKind.GUARDRAIL,
        TraceGraphNodeKind.RETRIEVAL,
        TraceGraphNodeKind.CUSTOM,
    }:
        if isinstance(request_fact, ContextContributionFact):
            request, request_omitted = _captured(request_fact.input)
        if isinstance(result_fact, ContextContributionFact):
            result, result_omitted = _captured(result_fact.output)
        if isinstance(failure_fact, ContextContributionFact):
            failure = TraceGraphFailure(
                error_type=failure_fact.error_type or "context_error"
            )
    elif record.kind is TraceGraphNodeKind.PLAN:
        if not isinstance(result_fact, PlanRevisionFact):
            raise TraceStoreProtocolError("Plan Graph node fact is invalid")
        result, result_omitted = _captured(result_fact.plan)
        source_id = result_fact.revision_id
    elif record.kind is TraceGraphNodeKind.INTERACTION:
        if not isinstance(result_fact, InteractionFact):
            raise TraceStoreProtocolError("Interaction Graph node fact is invalid")
        result, result_omitted = _captured(result_fact.payload)
        source_id = result_fact.source_interaction_id
    elif record.kind is TraceGraphNodeKind.RUN:
        if not isinstance(start, RunFact) or start.phase != "started":
            raise TraceStoreProtocolError("Run Graph node start fact is invalid")
        source_id = start.identity.run_id
        if isinstance(failure_fact, RunFact):
            failure = _failure(
                failure_fact.error_type or "run_error",
                code=failure_fact.code,
            )
    else:  # pragma: no cover - exhaustive enum handling above
        raise TraceStoreProtocolError("Trace Graph node kind has no projection")

    issues = () if record.link_issue is None else (record.link_issue,)
    if relationship_missing:
        inferred = {
            TraceGraphNodeKind.ASSISTANT_MESSAGE: (
                TraceGraphLinkIssue.MISSING_MODEL_OUTPUT
            ),
            TraceGraphNodeKind.TOOL: TraceGraphLinkIssue.MISSING_TOOL_PROPOSAL,
            TraceGraphNodeKind.SKILL: TraceGraphLinkIssue.MISSING_TOOL_EXECUTION,
        }.get(record.kind, TraceGraphLinkIssue.MISSING_PARENT)
        if inferred not in issues:
            issues = (*issues, inferred)
    return TraceGraphNode(
        id=record.node_id,
        turn_id=turn_id,
        parent_id=parent_id,
        structural_parent_id=record.structural_parent_id,
        kind=record.kind,
        status=record.status,
        name=record.name,
        run_id=record.run_id,
        namespace=record.namespace,
        agent_name=record.agent_name,
        provider=record.provider,
        model=record.model,
        source_id=source_id,
        started_at=record.started_at,
        first_output_at=record.first_output_at,
        completed_at=record.completed_at,
        started_seq=record.started_seq,
        updated_seq=record.updated_seq,
        content=content,
        content_omitted=content_omitted,
        request=request,
        request_omitted=request_omitted,
        result=result,
        result_omitted=result_omitted,
        usage=usage,
        response_metadata=response_metadata,
        failure=failure,
        class_name=class_name,
        hooks=hooks,
        source_path=source_path,
        link_issues=issues,
    )


def project_trace_graph_records(
    records: tuple[TraceGraphNodeRecord, ...],
    *,
    turns: tuple[TraceGraphTurn, ...],
    run_turns: Mapping[str, str],
    include_technical_nodes: bool,
    include_ancestor_nodes: bool,
) -> tuple[
    tuple[TraceGraphNode, ...],
    tuple[str, ...],
    tuple[str, ...],
]:
    """Project visible parents and authoritative order from Graph records."""

    turn_by_id = {turn.id: turn for turn in turns}
    records_by_id = {record.node_id: record for record in records}
    visible_records = {
        node_id: record
        for node_id, record in records_by_id.items()
        if include_technical_nodes
        or record.kind not in TECHNICAL_TRACE_GRAPH_NODE_KINDS
    }
    projected: dict[str, TraceGraphNode] = {}
    for node_id, record in visible_records.items():
        turn_id = run_turns.get(record.run_id)
        turn = None if turn_id is None else turn_by_id.get(turn_id)
        if turn is None:
            raise TraceStoreProtocolError(
                "Trace Graph node has no selected Turn ownership"
            )
        parent_id, relationship_missing = _visible_graph_parent(
            record,
            turn=turn,
            records=records_by_id,
            visible_node_ids=frozenset(visible_records),
            include_ancestor_nodes=include_ancestor_nodes,
        )
        projected[node_id] = project_trace_graph_node(
            record,
            turn_id=turn.id,
            parent_id=parent_id,
            relationship_missing=relationship_missing,
        )
    ordered_ids = _ordered_graph_node_ids(turns, projected)
    nodes = tuple(projected[node_id] for node_id in ordered_ids)
    roots = tuple(node.id for node in nodes if node.parent_id is None)
    return nodes, ordered_ids, roots


def _visible_graph_parent(
    record: TraceGraphNodeRecord,
    *,
    turn: TraceGraphTurn,
    records: Mapping[str, TraceGraphNodeRecord],
    visible_node_ids: frozenset[str],
    include_ancestor_nodes: bool,
) -> tuple[str | None, bool]:
    """Resolve the nearest visible ancestor without hiding missing evidence."""

    if record.node_id == turn.root_node_id:
        return None, False
    relationship_missing = record.structural_parent_id is None and record.kind not in {
        TraceGraphNodeKind.HUMAN_MESSAGE,
        TraceGraphNodeKind.RUN,
    }
    candidate = record.structural_parent_id
    visited = {record.node_id}
    while candidate is not None:
        if candidate in visited:
            raise TraceStoreProtocolError("Trace Graph contains a parent cycle")
        visited.add(candidate)
        if candidate in visible_node_ids:
            return candidate, relationship_missing
        parent = records.get(candidate)
        if parent is None:
            relationship_missing = include_ancestor_nodes
            break
        candidate = parent.structural_parent_id
    if turn.root_node_id in visible_node_ids:
        return turn.root_node_id, relationship_missing
    return None, relationship_missing


def _ordered_graph_node_ids(
    turns: tuple[TraceGraphTurn, ...],
    nodes: Mapping[str, TraceGraphNode],
) -> tuple[str, ...]:
    """Return deterministic Turn-first depth-first order for visible nodes."""

    by_turn: dict[str, list[TraceGraphNode]] = {}
    for node in nodes.values():
        by_turn.setdefault(node.turn_id, []).append(node)
    ordered: list[str] = []
    for turn in sorted(turns, key=lambda item: (item.ordinal, item.id)):
        turn_nodes = by_turn.get(turn.id, [])
        node_ids = {node.id for node in turn_nodes}
        children: dict[str, list[TraceGraphNode]] = {}
        roots: list[TraceGraphNode] = []
        for node in turn_nodes:
            if node.parent_id is None or node.parent_id not in node_ids:
                roots.append(node)
            else:
                children.setdefault(node.parent_id, []).append(node)

        def key(item: TraceGraphNode) -> tuple[int, str]:
            return item.started_seq, item.id

        roots.sort(key=key)
        for values in children.values():
            values.sort(key=key)
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: TraceGraphNode) -> None:
            if node.id in visited:
                return
            if node.id in visiting:
                raise TraceStoreProtocolError("Trace Graph contains a visible cycle")
            visiting.add(node.id)
            ordered.append(node.id)
            for child in children.get(node.id, ()):
                visit(child)
            visiting.remove(node.id)
            visited.add(node.id)

        for root in roots:
            visit(root)
        if visited != node_ids:
            raise TraceStoreProtocolError("Trace Graph ordering omitted a node")
    if set(ordered) != set(nodes):
        raise TraceStoreProtocolError("Trace Graph ordering omitted a Turn")
    return tuple(ordered)


__all__ = [
    "project_trace_graph_node",
    "project_trace_graph_records",
    "reduce_trace_graph_records",
]
