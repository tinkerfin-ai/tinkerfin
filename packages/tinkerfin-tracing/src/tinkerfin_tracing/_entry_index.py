"""Deterministic query-index mutations derived from committed Ledger events."""

from __future__ import annotations

from ._ids import scope_id
from .backend import TraceEntryMutation
from .entries import TraceEntryKind, TraceEntryStatus
from .facts import (
    AgentStepFact,
    ContextContributionFact,
    MiddlewareFact,
    ModelCallFact,
    RunFact,
    RuntimeTaskFact,
    SkillFact,
    SubagentFact,
    ToolExecutionFact,
    ToolFact,
    TraceEvent,
)


def _run_entry(event: TraceEvent) -> str:
    return f"run:{event.generation}:{event.fact.identity.run_id}"


def _agent_parent(event: TraceEvent, namespace: tuple[str, ...]) -> str:
    if not namespace:
        return scope_id("agent", (), event.fact.identity.run_id)
    return scope_id("subagent", namespace, namespace[-1])


def _terminal_status(value: str) -> TraceEntryStatus:
    return {
        "succeeded": TraceEntryStatus.SUCCEEDED,
        "interrupted": TraceEntryStatus.WAITING,
        "failed": TraceEntryStatus.FAILED,
        "cancelled": TraceEntryStatus.CANCELLED,
        "abandoned": TraceEntryStatus.ABANDONED,
    }.get(value, TraceEntryStatus.UNKNOWN)


def _runtime_task_entry(fact: RuntimeTaskFact) -> str:
    """Keep a replayed LangGraph task distinct in each managed Runtime Run."""

    return scope_id(
        "runtime-task",
        fact.namespace,
        f"{fact.identity.run_id}:{fact.task_id}",
    )


def entry_mutations(events: tuple[TraceEvent, ...]) -> tuple[TraceEntryMutation, ...]:
    """Return partial index updates that reference, but never copy, event payloads."""

    mutations: list[TraceEntryMutation] = []
    for event in events:
        fact = event.fact
        if isinstance(fact, RunFact):
            entry_id = _run_entry(event)
            if fact.phase == "started":
                mutations.append(
                    TraceEntryMutation(
                        entry_id=entry_id,
                        updated_seq=event.trace_seq,
                        kind=TraceEntryKind.RUN,
                        status=TraceEntryStatus.RUNNING,
                        name=fact.identity.run_id,
                        run_id=fact.identity.run_id,
                        namespace=(),
                        started_at=fact.occurred_at,
                        started_seq=event.trace_seq,
                    )
                )
            elif fact.phase == "terminal" and fact.outcome is not None:
                mutations.append(
                    TraceEntryMutation(
                        entry_id=entry_id,
                        updated_seq=event.trace_seq,
                        status=_terminal_status(fact.outcome),
                        completed_at=fact.occurred_at,
                    )
                )
            continue
        if isinstance(fact, AgentStepFact):
            if fact.step_kind == "subagent":
                continue
            kind = {
                "agent": TraceEntryKind.AGENT,
                "middleware": TraceEntryKind.MIDDLEWARE,
                "model": TraceEntryKind.MODEL,
                "tools": TraceEntryKind.TOOLS,
                "task": TraceEntryKind.TASK,
            }[fact.step_kind]
            if fact.phase == "started":
                mutations.append(
                    TraceEntryMutation(
                        entry_id=fact.call_id,
                        updated_seq=event.trace_seq,
                        kind=kind,
                        status=TraceEntryStatus.RUNNING,
                        name=fact.name,
                        run_id=fact.identity.run_id,
                        parent_id=fact.parent_call_id,
                        namespace=fact.namespace,
                        agent_name=fact.agent_name,
                        started_at=fact.occurred_at,
                        started_seq=event.trace_seq,
                    )
                )
            else:
                mutations.append(
                    TraceEntryMutation(
                        entry_id=fact.call_id,
                        updated_seq=event.trace_seq,
                        status={
                            "completed": TraceEntryStatus.SUCCEEDED,
                            "failed": TraceEntryStatus.FAILED,
                            "cancelled": TraceEntryStatus.CANCELLED,
                            "interrupted": TraceEntryStatus.WAITING,
                            "abandoned": TraceEntryStatus.ABANDONED,
                        }[fact.phase],
                        completed_at=fact.occurred_at,
                    )
                )
            continue
        if isinstance(fact, ModelCallFact):
            if fact.phase == "started":
                # Only callback-correlated parents describe execution structure. An
                # absent parent remains explicit incomplete evidence instead of being
                # attached to a nearby Agent by arrival order.
                mutations.append(
                    TraceEntryMutation(
                        entry_id=fact.call_id,
                        updated_seq=event.trace_seq,
                        kind=TraceEntryKind.PROVIDER,
                        status=TraceEntryStatus.RUNNING,
                        name=fact.model or "Model",
                        run_id=fact.identity.run_id,
                        parent_id=fact.parent_call_id,
                        namespace=fact.namespace,
                        agent_name=fact.agent_name,
                        provider=fact.provider,
                        model=fact.model,
                        started_at=fact.occurred_at,
                        started_seq=event.trace_seq,
                    )
                )
            elif fact.phase == "first_output":
                mutations.append(
                    TraceEntryMutation(
                        entry_id=fact.call_id,
                        updated_seq=event.trace_seq,
                        first_output_at=fact.occurred_at,
                    )
                )
            else:
                mutations.append(
                    TraceEntryMutation(
                        entry_id=fact.call_id,
                        updated_seq=event.trace_seq,
                        status={
                            "completed": TraceEntryStatus.SUCCEEDED,
                            "failed": TraceEntryStatus.FAILED,
                            "cancelled": TraceEntryStatus.CANCELLED,
                            "interrupted": TraceEntryStatus.WAITING,
                            "abandoned": TraceEntryStatus.ABANDONED,
                        }[fact.phase],
                        completed_at=fact.occurred_at,
                    )
                )
                if fact.phase == "completed":
                    for tool_call_id in fact.tool_call_ids:
                        mutations.append(
                            TraceEntryMutation(
                                entry_id=scope_id(
                                    "tool",
                                    fact.namespace,
                                    tool_call_id,
                                ),
                                updated_seq=event.trace_seq,
                                parent_id=fact.call_id,
                            )
                        )
            continue
        if isinstance(fact, ToolFact):
            if fact.phase == "started":
                mutations.append(
                    TraceEntryMutation(
                        entry_id=fact.tool_call_id,
                        updated_seq=event.trace_seq,
                        kind=TraceEntryKind.TOOL_PROPOSAL,
                        status=TraceEntryStatus.WAITING,
                        name=fact.tool_name,
                        run_id=fact.identity.run_id,
                        parent_id=fact.parent_call_id,
                        namespace=fact.namespace,
                        started_at=fact.occurred_at,
                        started_seq=event.trace_seq,
                    )
                )
            elif fact.phase == "arguments" and fact.content is not None:
                mutations.append(
                    TraceEntryMutation(
                        entry_id=fact.tool_call_id,
                        updated_seq=event.trace_seq,
                        started_seq=event.trace_seq,
                    )
                )
            elif fact.phase == "result":
                mutations.append(
                    TraceEntryMutation(
                        entry_id=fact.tool_call_id,
                        updated_seq=event.trace_seq,
                        status=(
                            TraceEntryStatus.SUCCEEDED
                            if fact.result_status == "success"
                            else TraceEntryStatus.FAILED
                        ),
                        completed_at=fact.occurred_at,
                    )
                )
            elif fact.phase in {"cancelled", "abandoned"}:
                mutations.append(
                    TraceEntryMutation(
                        entry_id=fact.tool_call_id,
                        updated_seq=event.trace_seq,
                        status=(
                            TraceEntryStatus.CANCELLED
                            if fact.phase == "cancelled"
                            else TraceEntryStatus.ABANDONED
                        ),
                        completed_at=fact.occurred_at,
                    )
                )
            continue
        if isinstance(fact, ToolExecutionFact):
            if fact.phase == "started":
                # A proposal correlation is projected separately as ``proposal_id``;
                # it must never replace the Tool execution's callback-proven parent.
                mutations.append(
                    TraceEntryMutation(
                        entry_id=fact.execution_id,
                        updated_seq=event.trace_seq,
                        kind=TraceEntryKind.TOOL,
                        status=TraceEntryStatus.RUNNING,
                        name=fact.tool_name,
                        run_id=fact.identity.run_id,
                        parent_id=fact.parent_call_id,
                        namespace=fact.namespace,
                        agent_name=fact.agent_name,
                        started_at=fact.occurred_at,
                        started_seq=event.trace_seq,
                    )
                )
            else:
                mutations.append(
                    TraceEntryMutation(
                        entry_id=fact.execution_id,
                        updated_seq=event.trace_seq,
                        status={
                            "completed": TraceEntryStatus.SUCCEEDED,
                            "failed": TraceEntryStatus.FAILED,
                            "cancelled": TraceEntryStatus.CANCELLED,
                            "interrupted": TraceEntryStatus.WAITING,
                            "abandoned": TraceEntryStatus.ABANDONED,
                        }[fact.phase],
                        completed_at=fact.occurred_at,
                    )
                )
            continue
        if isinstance(fact, SubagentFact):
            if fact.phase == "started":
                parent_namespace = fact.namespace[:-1]
                mutations.append(
                    TraceEntryMutation(
                        entry_id=fact.subagent_id,
                        updated_seq=event.trace_seq,
                        kind=TraceEntryKind.SUBAGENT,
                        status=TraceEntryStatus.RUNNING,
                        name=fact.agent_name or "Subagent",
                        run_id=fact.identity.run_id,
                        parent_id=fact.parent_execution_id
                        or _agent_parent(event, parent_namespace),
                        namespace=fact.namespace,
                        agent_name=fact.agent_name,
                        started_at=fact.occurred_at,
                        started_seq=event.trace_seq,
                    )
                )
            elif fact.phase == "updated":
                mutations.append(
                    TraceEntryMutation(
                        entry_id=fact.subagent_id,
                        updated_seq=event.trace_seq,
                        status={
                            "running": TraceEntryStatus.RUNNING,
                            "waiting": TraceEntryStatus.WAITING,
                            "succeeded": TraceEntryStatus.SUCCEEDED,
                            "failed": TraceEntryStatus.FAILED,
                            "cancelled": TraceEntryStatus.CANCELLED,
                            "abandoned": TraceEntryStatus.ABANDONED,
                        }[fact.status],
                    )
                )
            else:
                mutations.append(
                    TraceEntryMutation(
                        entry_id=fact.subagent_id,
                        updated_seq=event.trace_seq,
                        status={
                            "succeeded": TraceEntryStatus.SUCCEEDED,
                            "failed": TraceEntryStatus.FAILED,
                            "cancelled": TraceEntryStatus.CANCELLED,
                            "abandoned": TraceEntryStatus.ABANDONED,
                        }.get(fact.status, TraceEntryStatus.UNKNOWN),
                        completed_at=fact.occurred_at,
                    )
                )
            continue
        if isinstance(fact, RuntimeTaskFact):
            entry_id = _runtime_task_entry(fact)
            if fact.phase == "started":
                mutations.append(
                    TraceEntryMutation(
                        entry_id=entry_id,
                        updated_seq=event.trace_seq,
                        kind=TraceEntryKind.TASK,
                        status=TraceEntryStatus.RUNNING,
                        name=fact.task_name,
                        run_id=fact.identity.run_id,
                        parent_id=_agent_parent(event, fact.namespace),
                        namespace=fact.namespace,
                        started_at=fact.occurred_at,
                        started_seq=event.trace_seq,
                    )
                )
            else:
                mutations.append(
                    TraceEntryMutation(
                        entry_id=entry_id,
                        updated_seq=event.trace_seq,
                        status={
                            "completed": TraceEntryStatus.SUCCEEDED,
                            "failed": TraceEntryStatus.FAILED,
                            "cancelled": TraceEntryStatus.CANCELLED,
                            "interrupted": TraceEntryStatus.WAITING,
                            "abandoned": TraceEntryStatus.ABANDONED,
                        }[fact.phase],
                        completed_at=(
                            None if fact.phase == "interrupted" else fact.occurred_at
                        ),
                    )
                )
            continue
        if isinstance(fact, MiddlewareFact):
            mutations.append(
                TraceEntryMutation(
                    entry_id=fact.middleware_id,
                    updated_seq=event.trace_seq,
                    kind=TraceEntryKind.MIDDLEWARE,
                    status=TraceEntryStatus.CONFIGURED,
                    name=fact.name,
                    run_id=fact.identity.run_id,
                    parent_id=_agent_parent(event, ()),
                    namespace=(),
                    started_at=fact.occurred_at,
                    completed_at=fact.occurred_at,
                    started_seq=event.trace_seq,
                )
            )
            continue
        if isinstance(fact, SkillFact):
            mutations.append(
                TraceEntryMutation(
                    entry_id=fact.skill_id,
                    updated_seq=event.trace_seq,
                    kind=TraceEntryKind.SKILL,
                    status=TraceEntryStatus.SUCCEEDED,
                    name=fact.name,
                    run_id=fact.identity.run_id,
                    parent_id=fact.execution_id,
                    namespace=fact.namespace,
                    agent_name=fact.agent_name,
                    started_at=fact.occurred_at,
                    completed_at=fact.occurred_at,
                    started_seq=event.trace_seq,
                )
            )
            continue
        if isinstance(fact, ContextContributionFact):
            kind = {
                "memory": TraceEntryKind.MEMORY,
                "guardrail": TraceEntryKind.GUARDRAIL,
                "retrieval": TraceEntryKind.RETRIEVAL,
                "custom": TraceEntryKind.CUSTOM,
            }[fact.context_kind]
            if fact.phase == "started":
                mutations.append(
                    TraceEntryMutation(
                        entry_id=fact.contribution_id,
                        updated_seq=event.trace_seq,
                        kind=kind,
                        status=TraceEntryStatus.RUNNING,
                        name=fact.name,
                        run_id=fact.identity.run_id,
                        parent_id=fact.parent_call_id
                        or _agent_parent(event, fact.namespace),
                        namespace=fact.namespace,
                        started_at=fact.occurred_at,
                        started_seq=event.trace_seq,
                    )
                )
            else:
                mutations.append(
                    TraceEntryMutation(
                        entry_id=fact.contribution_id,
                        updated_seq=event.trace_seq,
                        status={
                            "completed": TraceEntryStatus.SUCCEEDED,
                            "failed": TraceEntryStatus.FAILED,
                            "cancelled": TraceEntryStatus.CANCELLED,
                            "interrupted": TraceEntryStatus.WAITING,
                            "abandoned": TraceEntryStatus.ABANDONED,
                        }[fact.phase],
                        completed_at=fact.occurred_at,
                    )
                )
    return tuple(mutations)


__all__ = ["entry_mutations"]
