"""Single deterministic reducer from committed facts to Graph index mutations."""

from __future__ import annotations

from collections.abc import Iterable, MutableMapping
from dataclasses import dataclass
from datetime import datetime
from typing import TypeAlias, cast

from ._ids import scope_id
from .backend import TraceGraphNodeMutation
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
from .graph import TraceGraphLinkIssue, TraceGraphNodeKind, TraceGraphNodeStatus


@dataclass(slots=True)
class ReducedTraceGraphNode:
    """Hold one payload-free node revision produced by Graph mutations."""

    node_id: str
    structural_parent_id: str | None
    kind: TraceGraphNodeKind
    status: TraceGraphNodeStatus
    name: str
    run_id: str
    namespace: tuple[str, ...]
    agent_name: str | None
    provider: str | None
    model: str | None
    started_at: datetime
    first_output_at: datetime | None
    completed_at: datetime | None
    started_seq: int
    updated_seq: int
    request_seq: int | None
    result_seq: int | None
    failure_seq: int | None
    link_issue: TraceGraphLinkIssue | None


@dataclass(frozen=True, slots=True)
class ReducedTraceGraphTombstone:
    """Hide an inherited logical node in one selected Run lineage."""

    node_id: str
    run_id: str
    updated_seq: int


ReducedTraceGraphRevision: TypeAlias = (
    ReducedTraceGraphNode | ReducedTraceGraphTombstone
)


def _run_node(event: TraceEvent) -> str:
    return f"run:{event.generation}:{event.fact.identity.run_id}"


def _agent_node(run_id: str, namespace: tuple[str, ...]) -> str:
    if namespace:
        return scope_id("subagent", namespace, namespace[-1])
    return scope_id("agent", (), run_id)


def _runtime_task_node(fact: RuntimeTaskFact) -> str:
    return scope_id(
        "runtime-task",
        fact.namespace,
        f"{fact.identity.run_id}:{fact.source_task_id}",
    )


def _tool_node(namespace: tuple[str, ...], source_tool_call_id: str) -> str:
    return scope_id("tool", namespace, source_tool_call_id)


def resolve_graph_link_issue(
    structural_parent_id: str | None,
    link_issue: TraceGraphLinkIssue | None,
) -> TraceGraphLinkIssue | None:
    """Drop a transient missing-parent marker once exact parent evidence exists."""

    if (
        structural_parent_id is not None
        and link_issue is TraceGraphLinkIssue.MISSING_PARENT
    ):
        return None
    return link_issue


def resolve_graph_node_kind(
    current: TraceGraphNodeKind | None,
    incoming: TraceGraphNodeKind | None,
) -> TraceGraphNodeKind | None:
    """Merge the two valid views of one task-backed middleware execution.

    Native task observations and middleware callbacks describe the same execution
    when they share a canonical node identity. Middleware is the more specific
    presentation kind regardless of event arrival order. Every other kind change
    remains a protocol violation.

    Args:
        current: Kind already retained for the canonical node.
        incoming: Kind supplied by the next mutation.

    Returns:
        The single canonical kind for the node.

    Raises:
        TraceStoreProtocolError: If two unrelated kinds claim the same identity.
    """

    if current is None:
        return incoming
    if incoming is None or incoming is current:
        return current
    if {current, incoming} == {
        TraceGraphNodeKind.RUNTIME_TASK,
        TraceGraphNodeKind.MIDDLEWARE,
    }:
        return TraceGraphNodeKind.MIDDLEWARE
    raise TraceStoreProtocolError(
        f"Trace Graph node kind changed from {current.value} to {incoming.value}"
    )


def _terminal_status(value: str) -> TraceGraphNodeStatus:
    return {
        "succeeded": TraceGraphNodeStatus.SUCCEEDED,
        "interrupted": TraceGraphNodeStatus.WAITING,
        "failed": TraceGraphNodeStatus.FAILED,
        "cancelled": TraceGraphNodeStatus.CANCELLED,
        "abandoned": TraceGraphNodeStatus.ABANDONED,
    }.get(value, TraceGraphNodeStatus.UNKNOWN)


def _phase_status(value: str) -> TraceGraphNodeStatus:
    return {
        "completed": TraceGraphNodeStatus.SUCCEEDED,
        "failed": TraceGraphNodeStatus.FAILED,
        "cancelled": TraceGraphNodeStatus.CANCELLED,
        "interrupted": TraceGraphNodeStatus.WAITING,
        "abandoned": TraceGraphNodeStatus.ABANDONED,
        "resolved": TraceGraphNodeStatus.SUCCEEDED,
    }.get(value, TraceGraphNodeStatus.UNKNOWN)


def graph_node_mutations(
    events: tuple[TraceEvent, ...],
) -> tuple[TraceGraphNodeMutation, ...]:
    """Reduce one ordered fact batch into payload-free Graph index changes.

    The same function drives online append and full rebuild. Message content deltas do
    not update the index; only a final reconciliation supplies a detail locator.
    """

    mutations: list[TraceGraphNodeMutation] = []
    for event in events:
        fact = event.fact
        if isinstance(fact, RunFact):
            node_id = _run_node(event)
            if fact.phase == "started":
                mutations.append(
                    TraceGraphNodeMutation(
                        node_id=node_id,
                        updated_seq=event.trace_seq,
                        kind=TraceGraphNodeKind.RUN,
                        status=TraceGraphNodeStatus.RUNNING,
                        name="Run",
                        run_id=fact.identity.run_id,
                        namespace=(),
                        started_at=fact.occurred_at,
                        started_seq=event.trace_seq,
                    )
                )
            elif fact.phase == "terminal" and fact.outcome is not None:
                mutations.append(
                    TraceGraphNodeMutation(
                        node_id=node_id,
                        updated_seq=event.trace_seq,
                        run_id=fact.identity.run_id,
                        status=_terminal_status(fact.outcome),
                        completed_at=fact.occurred_at,
                        result_seq=event.trace_seq,
                        failure_seq=(
                            event.trace_seq
                            if fact.outcome == "failed" and fact.failure_origin
                            else None
                        ),
                    )
                )
            continue
        if isinstance(fact, TurnFact):
            source_message_id = fact.user_message_id or fact.turn_id
            human_id = scope_id("message", (), source_message_id)
            mutations.extend(
                (
                    TraceGraphNodeMutation(
                        node_id=human_id,
                        updated_seq=event.trace_seq,
                        kind=TraceGraphNodeKind.HUMAN_MESSAGE,
                        status=TraceGraphNodeStatus.SUCCEEDED,
                        name="HumanMessage",
                        run_id=fact.identity.run_id,
                        namespace=(),
                        started_at=fact.occurred_at,
                        completed_at=fact.occurred_at,
                        started_seq=event.trace_seq,
                        link_issue=(
                            None
                            if fact.user_message_id is not None
                            else TraceGraphLinkIssue.MISSING_PARENT
                        ),
                    ),
                    TraceGraphNodeMutation(
                        node_id=_run_node(event),
                        updated_seq=event.trace_seq,
                        run_id=fact.identity.run_id,
                        structural_parent_id=human_id,
                    ),
                )
            )
            continue
        if isinstance(fact, MessageFact):
            if fact.role == "tool" or (fact.role == "user" and fact.namespace):
                continue
            if fact.phase == "removed":
                mutations.append(
                    TraceGraphNodeMutation(
                        node_id=fact.message_id,
                        updated_seq=event.trace_seq,
                        run_id=fact.identity.run_id,
                        remove=True,
                    )
                )
                continue
            kind = {
                "user": TraceGraphNodeKind.HUMAN_MESSAGE,
                "assistant": TraceGraphNodeKind.ASSISTANT_MESSAGE,
                "system": TraceGraphNodeKind.SYSTEM_MESSAGE,
            }.get(fact.role)
            if kind is None or fact.phase == "content":
                continue
            parent_id = (
                _tool_node(fact.namespace, fact.tool_call_id)
                if fact.role == "tool" and fact.tool_call_id is not None
                else None
            )
            mutations.append(
                TraceGraphNodeMutation(
                    node_id=fact.message_id,
                    updated_seq=event.trace_seq,
                    kind=kind,
                    status=(
                        TraceGraphNodeStatus.RUNNING
                        if fact.phase == "started"
                        else TraceGraphNodeStatus.SUCCEEDED
                    ),
                    name={
                        "user": "HumanMessage",
                        "assistant": "AssistantMessage",
                        "tool": "ToolMessage",
                        "system": "SystemMessage",
                    }[fact.role],
                    run_id=fact.identity.run_id,
                    structural_parent_id=parent_id,
                    namespace=fact.namespace,
                    started_at=fact.occurred_at,
                    completed_at=(
                        None if fact.phase == "started" else fact.occurred_at
                    ),
                    started_seq=event.trace_seq,
                    result_seq=(event.trace_seq if fact.content is not None else None),
                )
            )
            continue
        if isinstance(fact, AgentStepFact):
            if fact.step_kind == "subagent":
                continue
            node_id = (
                scope_id(
                    "runtime-task",
                    fact.namespace,
                    f"{fact.identity.run_id}:{fact.source_task_id}",
                )
                if fact.step_kind == "middleware" and fact.source_task_id is not None
                else fact.call_id
            )
            semantic_agent = fact.step_kind == "agent"
            kind = (
                TraceGraphNodeKind.AGENT
                if semantic_agent
                else (
                    TraceGraphNodeKind.MIDDLEWARE
                    if fact.step_kind == "middleware"
                    else TraceGraphNodeKind.RUNTIME_TASK
                )
            )
            # Native task identity owns the canonical Agent/Subagent parent. LangChain
            # may report an intermediate chain callback as the raw parent inside the
            # same task, but that callback must not rewrite the merged task node.
            parent_id = (
                _agent_node(fact.identity.run_id, fact.namespace)
                if fact.source_task_id is not None
                else fact.parent_call_id
            )
            if semantic_agent and parent_id is None:
                parent_id = _run_node(event)
            if fact.phase == "started":
                mutations.append(
                    TraceGraphNodeMutation(
                        node_id=node_id,
                        updated_seq=event.trace_seq,
                        kind=kind,
                        status=TraceGraphNodeStatus.RUNNING,
                        name=(fact.agent_name or "Agent")
                        if semantic_agent
                        else fact.name,
                        run_id=fact.identity.run_id,
                        structural_parent_id=parent_id,
                        namespace=fact.namespace,
                        agent_name=fact.agent_name,
                        started_at=fact.occurred_at,
                        started_seq=event.trace_seq,
                        request_seq=(
                            event.trace_seq
                            if kind
                            in {
                                TraceGraphNodeKind.AGENT,
                                TraceGraphNodeKind.MIDDLEWARE,
                            }
                            else None
                        ),
                        link_issue=(
                            None
                            if parent_id is not None or semantic_agent
                            else TraceGraphLinkIssue.MISSING_PARENT
                        ),
                    )
                )
            else:
                mutations.append(
                    TraceGraphNodeMutation(
                        node_id=node_id,
                        updated_seq=event.trace_seq,
                        run_id=fact.identity.run_id,
                        status=_phase_status(fact.phase),
                        completed_at=fact.occurred_at,
                        result_seq=event.trace_seq,
                        failure_seq=(
                            event.trace_seq
                            if fact.phase == "failed" and fact.failure_origin
                            else None
                        ),
                    )
                )
            continue
        if isinstance(fact, RuntimeTaskFact):
            node_id = _runtime_task_node(fact)
            if fact.phase == "started":
                mutations.append(
                    TraceGraphNodeMutation(
                        node_id=node_id,
                        updated_seq=event.trace_seq,
                        kind=TraceGraphNodeKind.RUNTIME_TASK,
                        status=TraceGraphNodeStatus.RUNNING,
                        name=fact.task_name,
                        run_id=fact.identity.run_id,
                        structural_parent_id=_agent_node(
                            fact.identity.run_id,
                            fact.namespace,
                        ),
                        namespace=fact.namespace,
                        started_at=fact.occurred_at,
                        started_seq=event.trace_seq,
                        request_seq=event.trace_seq,
                    )
                )
            else:
                mutations.append(
                    TraceGraphNodeMutation(
                        node_id=node_id,
                        updated_seq=event.trace_seq,
                        run_id=fact.identity.run_id,
                        status=_phase_status(fact.phase),
                        completed_at=(
                            None if fact.phase == "interrupted" else fact.occurred_at
                        ),
                        result_seq=event.trace_seq,
                        failure_seq=(
                            event.trace_seq
                            if fact.phase == "failed" and fact.failure_origin
                            else None
                        ),
                    )
                )
            continue
        if isinstance(fact, ModelCallFact):
            if fact.phase == "started":
                mutations.append(
                    TraceGraphNodeMutation(
                        node_id=fact.call_id,
                        updated_seq=event.trace_seq,
                        kind=TraceGraphNodeKind.MODEL,
                        status=TraceGraphNodeStatus.RUNNING,
                        name=fact.model or "Model",
                        run_id=fact.identity.run_id,
                        structural_parent_id=fact.parent_call_id,
                        namespace=fact.namespace,
                        agent_name=fact.agent_name,
                        provider=fact.provider,
                        model=fact.model,
                        started_at=fact.occurred_at,
                        started_seq=event.trace_seq,
                        request_seq=event.trace_seq,
                        link_issue=(
                            None
                            if fact.parent_call_id is not None
                            else TraceGraphLinkIssue.MISSING_PARENT
                        ),
                    )
                )
                for position in fact.system_message_positions:
                    mutations.append(
                        TraceGraphNodeMutation(
                            node_id=scope_id(
                                "system-message",
                                fact.namespace,
                                f"{fact.call_id}:{position}",
                            ),
                            updated_seq=event.trace_seq,
                            kind=TraceGraphNodeKind.SYSTEM_MESSAGE,
                            status=TraceGraphNodeStatus.SUCCEEDED,
                            name="SystemMessage",
                            run_id=fact.identity.run_id,
                            structural_parent_id=fact.call_id,
                            namespace=fact.namespace,
                            agent_name=fact.agent_name,
                            provider=fact.provider,
                            model=fact.model,
                            started_at=fact.occurred_at,
                            completed_at=fact.occurred_at,
                            started_seq=event.trace_seq,
                            request_seq=event.trace_seq,
                        )
                    )
            elif fact.phase == "first_output":
                mutations.append(
                    TraceGraphNodeMutation(
                        node_id=fact.call_id,
                        updated_seq=event.trace_seq,
                        run_id=fact.identity.run_id,
                        first_output_at=fact.occurred_at,
                    )
                )
                for source_message_id in fact.output_message_ids:
                    mutations.append(
                        TraceGraphNodeMutation(
                            node_id=scope_id(
                                "message",
                                fact.namespace,
                                source_message_id,
                            ),
                            updated_seq=event.trace_seq,
                            kind=TraceGraphNodeKind.ASSISTANT_MESSAGE,
                            status=TraceGraphNodeStatus.RUNNING,
                            name="AssistantMessage",
                            run_id=fact.identity.run_id,
                            structural_parent_id=fact.call_id,
                            namespace=fact.namespace,
                            agent_name=fact.agent_name,
                            started_at=fact.occurred_at,
                            started_seq=event.trace_seq,
                        )
                    )
            else:
                mutations.append(
                    TraceGraphNodeMutation(
                        node_id=fact.call_id,
                        updated_seq=event.trace_seq,
                        run_id=fact.identity.run_id,
                        status=_phase_status(fact.phase),
                        completed_at=fact.occurred_at,
                        result_seq=event.trace_seq,
                        failure_seq=(
                            event.trace_seq
                            if fact.phase == "failed" and fact.failure_origin
                            else None
                        ),
                    )
                )
                if fact.phase == "completed":
                    for source_message_id in fact.output_message_ids:
                        mutations.append(
                            TraceGraphNodeMutation(
                                node_id=scope_id(
                                    "message",
                                    fact.namespace,
                                    source_message_id,
                                ),
                                updated_seq=event.trace_seq,
                                kind=TraceGraphNodeKind.ASSISTANT_MESSAGE,
                                status=TraceGraphNodeStatus.SUCCEEDED,
                                name="AssistantMessage",
                                run_id=fact.identity.run_id,
                                structural_parent_id=fact.call_id,
                                namespace=fact.namespace,
                                agent_name=fact.agent_name,
                                started_at=fact.occurred_at,
                                completed_at=fact.occurred_at,
                                started_seq=event.trace_seq,
                            )
                        )
                    for source_tool_call_id in fact.tool_call_ids:
                        mutations.append(
                            TraceGraphNodeMutation(
                                node_id=_tool_node(
                                    fact.namespace,
                                    source_tool_call_id,
                                ),
                                updated_seq=event.trace_seq,
                                run_id=fact.identity.run_id,
                                structural_parent_id=fact.call_id,
                            )
                        )
            continue
        if isinstance(fact, ToolFact):
            node_id = _tool_node(fact.namespace, fact.source_tool_call_id)
            if fact.phase == "started":
                mutations.append(
                    TraceGraphNodeMutation(
                        node_id=node_id,
                        updated_seq=event.trace_seq,
                        kind=TraceGraphNodeKind.TOOL,
                        status=TraceGraphNodeStatus.WAITING,
                        name=fact.tool_name,
                        run_id=fact.identity.run_id,
                        structural_parent_id=fact.parent_call_id,
                        namespace=fact.namespace,
                        started_at=fact.occurred_at,
                        started_seq=event.trace_seq,
                        link_issue=(
                            None
                            if fact.parent_call_id is not None
                            else TraceGraphLinkIssue.MISSING_PARENT
                        ),
                    )
                )
            elif fact.phase == "arguments" and fact.content is not None:
                mutations.append(
                    TraceGraphNodeMutation(
                        node_id=node_id,
                        updated_seq=event.trace_seq,
                        run_id=fact.identity.run_id,
                        kind=TraceGraphNodeKind.TOOL,
                        status=TraceGraphNodeStatus.WAITING,
                        name=fact.tool_name,
                        structural_parent_id=fact.parent_call_id,
                        namespace=fact.namespace,
                        started_at=fact.occurred_at,
                        started_seq=event.trace_seq,
                        request_seq=event.trace_seq,
                    )
                )
            elif fact.phase == "result":
                mutations.append(
                    TraceGraphNodeMutation(
                        node_id=node_id,
                        updated_seq=event.trace_seq,
                        run_id=fact.identity.run_id,
                        kind=TraceGraphNodeKind.TOOL,
                        status=(
                            TraceGraphNodeStatus.SUCCEEDED
                            if fact.result_status == "success"
                            else TraceGraphNodeStatus.FAILED
                        ),
                        name=fact.tool_name,
                        structural_parent_id=fact.parent_call_id,
                        namespace=fact.namespace,
                        started_at=fact.occurred_at,
                        started_seq=event.trace_seq,
                        completed_at=fact.occurred_at,
                        result_seq=event.trace_seq,
                        failure_seq=(
                            event.trace_seq
                            if fact.result_status == "error" and fact.failure_origin
                            else None
                        ),
                    )
                )
            elif fact.phase in {"cancelled", "abandoned"}:
                mutations.append(
                    TraceGraphNodeMutation(
                        node_id=node_id,
                        updated_seq=event.trace_seq,
                        run_id=fact.identity.run_id,
                        kind=TraceGraphNodeKind.TOOL,
                        status=_phase_status(fact.phase),
                        name=fact.tool_name,
                        structural_parent_id=fact.parent_call_id,
                        namespace=fact.namespace,
                        started_at=fact.occurred_at,
                        started_seq=event.trace_seq,
                        completed_at=fact.occurred_at,
                    )
                )
            continue
        if isinstance(fact, ToolExecutionFact):
            node_id = (
                _tool_node(fact.namespace, fact.source_tool_call_id)
                if fact.source_tool_call_id is not None
                else fact.execution_id
            )
            if fact.phase == "started":
                mutations.append(
                    TraceGraphNodeMutation(
                        node_id=node_id,
                        updated_seq=event.trace_seq,
                        kind=TraceGraphNodeKind.TOOL,
                        status=TraceGraphNodeStatus.RUNNING,
                        name=fact.tool_name,
                        run_id=fact.identity.run_id,
                        structural_parent_id=(
                            fact.parent_call_id
                            if fact.source_tool_call_id is None
                            else None
                        ),
                        namespace=fact.namespace,
                        agent_name=fact.agent_name,
                        started_at=fact.occurred_at,
                        started_seq=event.trace_seq,
                        request_seq=event.trace_seq,
                        link_issue=(
                            TraceGraphLinkIssue.MISSING_TOOL_PROPOSAL
                            if fact.source_tool_call_id is None
                            else None
                        ),
                    )
                )
            else:
                mutations.append(
                    TraceGraphNodeMutation(
                        node_id=node_id,
                        updated_seq=event.trace_seq,
                        run_id=fact.identity.run_id,
                        status=_phase_status(fact.phase),
                        completed_at=fact.occurred_at,
                        result_seq=event.trace_seq,
                        failure_seq=(
                            event.trace_seq
                            if fact.phase == "failed" and fact.failure_origin
                            else None
                        ),
                    )
                )
            continue
        if isinstance(fact, SubagentFact):
            internal_parent_id = None
            if (
                fact.phase == "started"
                and fact.parent_tool_call_id is None
                and fact.parent_execution_id is None
                and fact.namespace
            ):
                _node_name, separator, source_task_id = fact.namespace[-1].partition(
                    ":"
                )
                if separator and source_task_id:
                    # A non-Tool subgraph, such as the Plan planner, still carries its
                    # owning Native task in the locked LangGraph namespace segment.
                    internal_parent_id = scope_id(
                        "runtime-task",
                        fact.namespace[:-1],
                        f"{fact.identity.run_id}:{source_task_id}",
                    )
            parent_id = (
                _tool_node(fact.namespace[:-1], fact.parent_tool_call_id)
                if fact.parent_tool_call_id is not None
                else fact.parent_execution_id or internal_parent_id
            )
            if fact.phase == "started":
                mutations.append(
                    TraceGraphNodeMutation(
                        node_id=fact.subagent_id,
                        updated_seq=event.trace_seq,
                        kind=TraceGraphNodeKind.SUBAGENT,
                        status=TraceGraphNodeStatus.RUNNING,
                        name=fact.agent_name or "Subagent",
                        run_id=fact.identity.run_id,
                        structural_parent_id=parent_id,
                        namespace=fact.namespace,
                        agent_name=fact.agent_name,
                        started_at=fact.occurred_at,
                        started_seq=event.trace_seq,
                        request_seq=event.trace_seq,
                        link_issue=(
                            None
                            if fact.parent_tool_call_id is not None
                            or internal_parent_id is not None
                            else TraceGraphLinkIssue.MISSING_TOOL_PROPOSAL
                        ),
                    )
                )
            else:
                mutations.append(
                    TraceGraphNodeMutation(
                        node_id=fact.subagent_id,
                        updated_seq=event.trace_seq,
                        run_id=fact.identity.run_id,
                        kind=TraceGraphNodeKind.SUBAGENT,
                        status=_terminal_status(fact.status),
                        name=fact.agent_name or "Subagent",
                        structural_parent_id=parent_id,
                        namespace=fact.namespace,
                        agent_name=fact.agent_name,
                        started_at=fact.occurred_at,
                        started_seq=event.trace_seq,
                        completed_at=(
                            fact.occurred_at
                            if fact.status
                            in {"succeeded", "failed", "cancelled", "abandoned"}
                            else None
                        ),
                        result_seq=event.trace_seq,
                    )
                )
            continue
        if isinstance(fact, SkillFact):
            mutations.append(
                TraceGraphNodeMutation(
                    node_id=fact.skill_id,
                    updated_seq=event.trace_seq,
                    kind=TraceGraphNodeKind.SKILL,
                    status=TraceGraphNodeStatus.SUCCEEDED,
                    name=fact.name,
                    run_id=fact.identity.run_id,
                    structural_parent_id=(
                        _tool_node(fact.namespace, fact.source_tool_call_id)
                    ),
                    namespace=fact.namespace,
                    agent_name=fact.agent_name,
                    started_at=fact.occurred_at,
                    completed_at=fact.occurred_at,
                    started_seq=event.trace_seq,
                    request_seq=event.trace_seq,
                    link_issue=(None),
                )
            )
            continue
        if isinstance(fact, ContextContributionFact):
            kind = {
                "memory": TraceGraphNodeKind.MEMORY,
                "guardrail": TraceGraphNodeKind.GUARDRAIL,
                "retrieval": TraceGraphNodeKind.RETRIEVAL,
                "custom": TraceGraphNodeKind.CUSTOM,
            }[fact.context_kind]
            parent_id = fact.parent_call_id or _agent_node(
                fact.identity.run_id,
                fact.namespace,
            )
            if fact.phase == "started":
                mutations.append(
                    TraceGraphNodeMutation(
                        node_id=fact.contribution_id,
                        updated_seq=event.trace_seq,
                        kind=kind,
                        status=TraceGraphNodeStatus.RUNNING,
                        name=fact.name,
                        run_id=fact.identity.run_id,
                        structural_parent_id=parent_id,
                        namespace=fact.namespace,
                        started_at=fact.occurred_at,
                        started_seq=event.trace_seq,
                        request_seq=event.trace_seq,
                    )
                )
            else:
                mutations.append(
                    TraceGraphNodeMutation(
                        node_id=fact.contribution_id,
                        updated_seq=event.trace_seq,
                        run_id=fact.identity.run_id,
                        status=_phase_status(fact.phase),
                        completed_at=fact.occurred_at,
                        result_seq=event.trace_seq,
                        failure_seq=(
                            event.trace_seq
                            if fact.phase == "failed" and fact.failure_origin
                            else None
                        ),
                    )
                )
            continue
        if isinstance(fact, PlanRevisionFact):
            mutations.append(
                TraceGraphNodeMutation(
                    node_id=scope_id(
                        "plan",
                        fact.namespace,
                        fact.identity.run_id,
                    ),
                    updated_seq=event.trace_seq,
                    kind=TraceGraphNodeKind.PLAN,
                    status=(
                        TraceGraphNodeStatus.WAITING
                        if fact.status
                        and fact.status
                        in {"awaiting_input", "awaiting_review", "draft"}
                        else TraceGraphNodeStatus.SUCCEEDED
                    ),
                    name="Plan",
                    run_id=fact.identity.run_id,
                    structural_parent_id=_agent_node(
                        fact.identity.run_id,
                        fact.namespace,
                    ),
                    namespace=fact.namespace,
                    started_at=fact.occurred_at,
                    started_seq=event.trace_seq,
                    result_seq=event.trace_seq,
                )
            )
            continue
        if isinstance(fact, InteractionFact):
            parent_id = (
                _tool_node(fact.namespace, fact.tool_call_ids[0])
                if len(fact.tool_call_ids) == 1
                else _agent_node(fact.identity.run_id, fact.namespace)
            )
            status = (
                TraceGraphNodeStatus.WAITING
                if fact.status == "pending"
                else (
                    TraceGraphNodeStatus.CANCELLED
                    if fact.status == "cancelled"
                    else TraceGraphNodeStatus.SUCCEEDED
                )
            )
            mutations.append(
                TraceGraphNodeMutation(
                    node_id=fact.interaction_id,
                    updated_seq=event.trace_seq,
                    kind=TraceGraphNodeKind.INTERACTION,
                    status=status,
                    name=fact.interaction_kind,
                    run_id=fact.identity.run_id,
                    structural_parent_id=parent_id,
                    namespace=fact.namespace,
                    started_at=fact.occurred_at,
                    completed_at=(
                        None if fact.status == "pending" else fact.occurred_at
                    ),
                    started_seq=event.trace_seq,
                    result_seq=event.trace_seq,
                )
            )
    return _coalesce_graph_mutations(mutations)


def _coalesce_graph_mutations(
    mutations: list[TraceGraphNodeMutation],
) -> tuple[TraceGraphNodeMutation, ...]:
    """Collapse one commit batch to at most one write per node revision."""

    ordered_keys: list[tuple[str, str]] = []
    values: dict[tuple[str, str], TraceGraphNodeMutation] = {}
    for mutation in mutations:
        key = (mutation.node_id, mutation.run_id)
        current = values.get(key)
        if current is None:
            ordered_keys.append(key)
            values[key] = mutation
            continue
        if mutation.remove or current.remove:
            values[key] = mutation
            continue
        structural_parent_id = (
            mutation.structural_parent_id or current.structural_parent_id
        )
        values[key] = TraceGraphNodeMutation(
            node_id=mutation.node_id,
            updated_seq=mutation.updated_seq,
            run_id=mutation.run_id,
            kind=resolve_graph_node_kind(current.kind, mutation.kind),
            status=mutation.status or current.status,
            name=current.name or mutation.name,
            structural_parent_id=structural_parent_id,
            namespace=(
                current.namespace
                if current.namespace is not None
                else mutation.namespace
            ),
            agent_name=mutation.agent_name or current.agent_name,
            provider=mutation.provider or current.provider,
            model=mutation.model or current.model,
            started_at=current.started_at or mutation.started_at,
            first_output_at=mutation.first_output_at or current.first_output_at,
            completed_at=mutation.completed_at or current.completed_at,
            started_seq=current.started_seq or mutation.started_seq,
            request_seq=mutation.request_seq or current.request_seq,
            result_seq=mutation.result_seq or current.result_seq,
            failure_seq=mutation.failure_seq or current.failure_seq,
            link_issue=resolve_graph_link_issue(
                structural_parent_id,
                current.link_issue or mutation.link_issue,
            ),
        )
    return tuple(values[key] for key in ordered_keys)


def apply_graph_node_mutation(
    revisions: MutableMapping[tuple[str, str], ReducedTraceGraphRevision],
    mutation: TraceGraphNodeMutation,
) -> None:
    """Apply one mutation while preserving immutable node identity fields."""

    storage_key = (mutation.node_id, mutation.run_id)
    if mutation.remove:
        revisions[storage_key] = ReducedTraceGraphTombstone(
            node_id=mutation.node_id,
            run_id=mutation.run_id,
            updated_seq=mutation.updated_seq,
        )
        return
    row = revisions.get(storage_key)
    removed_revision = isinstance(row, ReducedTraceGraphTombstone)
    if removed_revision:
        row = None
    if row is None:
        if (
            mutation.kind is None
            or mutation.status is None
            or mutation.name is None
            or mutation.namespace is None
            or mutation.started_at is None
            or mutation.started_seq is None
        ):
            if removed_revision:
                raise TraceStoreProtocolError(
                    "Trace Graph removal revision cannot accept a partial update"
                )
            return
        revisions[storage_key] = ReducedTraceGraphNode(
            node_id=mutation.node_id,
            structural_parent_id=mutation.structural_parent_id,
            kind=mutation.kind,
            status=mutation.status,
            name=mutation.name,
            run_id=mutation.run_id,
            namespace=mutation.namespace,
            agent_name=mutation.agent_name,
            provider=mutation.provider,
            model=mutation.model,
            started_at=mutation.started_at,
            first_output_at=mutation.first_output_at,
            completed_at=mutation.completed_at,
            started_seq=mutation.started_seq,
            updated_seq=mutation.updated_seq,
            request_seq=mutation.request_seq,
            result_seq=mutation.result_seq,
            failure_seq=mutation.failure_seq,
            link_issue=resolve_graph_link_issue(
                mutation.structural_parent_id,
                mutation.link_issue,
            ),
        )
        return
    if not isinstance(row, ReducedTraceGraphNode):  # pragma: no cover - narrowed above
        raise TraceStoreProtocolError("Trace Graph revision type is invalid")
    if mutation.updated_seq < row.updated_seq:
        raise TraceStoreProtocolError("Trace Graph node sequence moved backwards")
    row.kind = cast(
        TraceGraphNodeKind,
        resolve_graph_node_kind(row.kind, mutation.kind),
    )
    if mutation.name is not None and mutation.name != row.name:
        raise TraceStoreProtocolError("Trace Graph node name changed")
    if mutation.run_id != row.run_id:
        raise TraceStoreProtocolError("Trace Graph node Run changed")
    if mutation.namespace is not None and mutation.namespace != row.namespace:
        raise TraceStoreProtocolError("Trace Graph node namespace changed")
    if mutation.structural_parent_id is not None:
        if (
            row.structural_parent_id is not None
            and mutation.structural_parent_id != row.structural_parent_id
        ):
            raise TraceStoreProtocolError("Trace Graph structural parent changed")
        row.structural_parent_id = mutation.structural_parent_id
        row.link_issue = resolve_graph_link_issue(
            row.structural_parent_id,
            row.link_issue,
        )
    if mutation.status is not None:
        row.status = mutation.status
    if mutation.agent_name is not None:
        row.agent_name = mutation.agent_name
    if mutation.provider is not None:
        row.provider = mutation.provider
    if mutation.model is not None:
        row.model = mutation.model
    if mutation.first_output_at is not None:
        row.first_output_at = mutation.first_output_at
    if mutation.completed_at is not None:
        row.completed_at = mutation.completed_at
    if mutation.request_seq is not None:
        row.request_seq = mutation.request_seq
    if mutation.result_seq is not None:
        row.result_seq = mutation.result_seq
    if mutation.failure_seq is not None:
        row.failure_seq = mutation.failure_seq
    resolved_issue = resolve_graph_link_issue(
        row.structural_parent_id,
        mutation.link_issue,
    )
    if resolved_issue is not None and row.link_issue is None:
        row.link_issue = resolved_issue
    row.updated_seq = mutation.updated_seq


def effective_graph_nodes(
    revisions: Iterable[ReducedTraceGraphRevision],
    *,
    run_ids: frozenset[str],
) -> tuple[ReducedTraceGraphNode, ...]:
    """Merge selected-lineage revisions into one current node per stable ID."""

    grouped: dict[str, list[ReducedTraceGraphRevision]] = {}
    for revision in revisions:
        if revision.run_id in run_ids:
            grouped.setdefault(revision.node_id, []).append(revision)
    effective: list[ReducedTraceGraphNode] = []
    for node_id, values in grouped.items():
        ordered = sorted(values, key=lambda item: item.updated_seq)
        latest = ordered[-1]
        if isinstance(latest, ReducedTraceGraphTombstone):
            continue
        nodes = [item for item in ordered if isinstance(item, ReducedTraceGraphNode)]
        origin = min(nodes, key=lambda item: item.started_seq)

        def latest_value(name: str) -> object | None:
            return next(
                (
                    value
                    for item in reversed(nodes)
                    if (value := getattr(item, name)) is not None
                ),
                None,
            )

        structural_parent_id = cast(
            str | None,
            latest_value("structural_parent_id"),
        )
        effective.append(
            ReducedTraceGraphNode(
                node_id=node_id,
                structural_parent_id=structural_parent_id,
                kind=latest.kind,
                status=latest.status,
                name=latest.name,
                run_id=latest.run_id,
                namespace=latest.namespace,
                agent_name=cast(str | None, latest_value("agent_name")),
                provider=cast(str | None, latest_value("provider")),
                model=cast(str | None, latest_value("model")),
                started_at=origin.started_at,
                first_output_at=min(
                    (
                        item.first_output_at
                        for item in nodes
                        if item.first_output_at is not None
                    ),
                    default=None,
                ),
                completed_at=cast(
                    datetime | None,
                    latest_value("completed_at"),
                ),
                started_seq=origin.started_seq,
                updated_seq=latest.updated_seq,
                request_seq=max(
                    (
                        item.request_seq
                        for item in nodes
                        if item.request_seq is not None
                    ),
                    default=None,
                ),
                result_seq=max(
                    (item.result_seq for item in nodes if item.result_seq is not None),
                    default=None,
                ),
                failure_seq=max(
                    (
                        item.failure_seq
                        for item in nodes
                        if item.failure_seq is not None
                    ),
                    default=None,
                ),
                link_issue=resolve_graph_link_issue(
                    structural_parent_id,
                    cast(
                        TraceGraphLinkIssue | None,
                        latest_value("link_issue"),
                    ),
                ),
            )
        )
    return tuple(effective)


def reduce_graph_mutations(
    mutations: Iterable[TraceGraphNodeMutation],
) -> tuple[TraceGraphNodeMutation, ...]:
    """Collapse sequential rebuild mutations to one complete revision per key."""

    revisions: dict[tuple[str, str], ReducedTraceGraphRevision] = {}
    for mutation in mutations:
        apply_graph_node_mutation(revisions, mutation)
    reduced: list[TraceGraphNodeMutation] = []
    for revision in revisions.values():
        if isinstance(revision, ReducedTraceGraphTombstone):
            reduced.append(
                TraceGraphNodeMutation(
                    node_id=revision.node_id,
                    updated_seq=revision.updated_seq,
                    run_id=revision.run_id,
                    remove=True,
                )
            )
            continue
        reduced.append(
            TraceGraphNodeMutation(
                node_id=revision.node_id,
                updated_seq=revision.updated_seq,
                run_id=revision.run_id,
                kind=revision.kind,
                status=revision.status,
                name=revision.name,
                structural_parent_id=revision.structural_parent_id,
                namespace=revision.namespace,
                agent_name=revision.agent_name,
                provider=revision.provider,
                model=revision.model,
                started_at=revision.started_at,
                first_output_at=revision.first_output_at,
                completed_at=revision.completed_at,
                started_seq=revision.started_seq,
                request_seq=revision.request_seq,
                result_seq=revision.result_seq,
                failure_seq=revision.failure_seq,
                link_issue=revision.link_issue,
            )
        )
    return tuple(reduced)


__all__ = [
    "ReducedTraceGraphNode",
    "ReducedTraceGraphRevision",
    "ReducedTraceGraphTombstone",
    "apply_graph_node_mutation",
    "effective_graph_nodes",
    "graph_node_mutations",
    "reduce_graph_mutations",
    "resolve_graph_link_issue",
    "resolve_graph_node_kind",
]
