"""Core semantic fold and deterministic optional Projection extensions."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Literal, Protocol, TypeAlias, TypeVar, cast, runtime_checkable

from pydantic import BaseModel, Field, JsonValue

from tinkerfin_contracts import RunTerminalOutcome

from ._models import TraceModel
from .capture import CapturedValue
from .errors import (
    AmbiguousTraceHead,
    TraceCorruption,
    TraceProjectionFailed,
    TraceRunNotFound,
)
from .facts import (
    InteractionFact,
    MessageFact,
    PlanRevisionFact,
    ReasoningFact,
    RunFact,
    RuntimeTaskFact,
    StateRevisionFact,
    SubagentFact,
    ToolFact,
    TraceEvent,
    TraceSemanticFact,
    TurnFact,
)
from .views import (
    TraceCompleteness,
    TraceInteraction,
    TraceMessage,
    TraceNode,
    TraceReasoning,
    TraceState,
    TraceStatus,
    TraceSummary,
    TraceTree,
)

ProjectionStateT = TypeVar("ProjectionStateT", bound=BaseModel)
ProjectionResultT = TypeVar("ProjectionResultT", bound=BaseModel)
MessageRole: TypeAlias = Literal["user", "assistant", "tool", "system", "other"]
NodeStatus: TypeAlias = Literal[
    "running",
    "waiting",
    "succeeded",
    "failed",
    "cancelled",
    "abandoned",
    "unknown",
]


@runtime_checkable
class TraceProjection(Protocol[ProjectionStateT, ProjectionResultT]):
    """Fold stable semantic facts into one serializable business result."""

    name: str
    state_type: type[ProjectionStateT]
    result_type: type[ProjectionResultT]

    def initial_state(self) -> ProjectionStateT:
        """Return one empty deterministic Projection state."""

        ...

    def apply(
        self,
        state: ProjectionStateT,
        fact: TraceSemanticFact,
    ) -> ProjectionStateT:
        """Return the next state without external side effects."""

        ...

    def finish(self, state: ProjectionStateT) -> ProjectionResultT:
        """Return the current public result for one fixed as-of prefix."""

        ...


RegisteredTraceProjection: TypeAlias = TraceProjection[Any, Any]


@dataclass(slots=True)
class _RunInfo:
    run_id: str
    started_at: datetime
    first_seq: int = 1
    parent_run_id: str | None = None
    input_kind: str | None = None
    turn_id: str | None = None
    terminal: RunTerminalOutcome | None = None
    completed_at: datetime | None = None


@dataclass(slots=True)
class _MutableMessage:
    id: str
    trace_seq: int
    source_id: str | None
    namespace: tuple[str, ...]
    run_id: str
    role: MessageRole
    created_at: datetime
    content: JsonValue | None = None
    content_omitted: bool = False
    name: str | None = None
    tool_call_id: str | None = None
    completed_at: datetime | None = None


@dataclass(slots=True)
class _MutableReasoning:
    id: str
    trace_seq: int
    message_id: str
    namespace: tuple[str, ...]
    run_id: str
    extractor: str
    created_at: datetime
    content: JsonValue | None = None
    content_omitted: bool = False
    completed_at: datetime | None = None


@dataclass(slots=True)
class CoreProjection:
    """Internal complete fold used to build one public TraceThread view."""

    facts: tuple[TraceSemanticFact, ...]
    selected_facts: tuple[TraceSemanticFact, ...]
    selected_run_ids: frozenset[str]
    selected_head: str
    available_heads: tuple[str, ...]
    messages: tuple[TraceMessage, ...]
    reasoning: tuple[TraceReasoning, ...]
    tree: TraceTree
    state: TraceState
    interactions: tuple[TraceInteraction, ...]
    summary: TraceSummary
    has_older: bool

    @property
    def status(self) -> TraceStatus:
        """Return the single cumulative status owned by the summary."""

        return self.summary.status

    @property
    def completeness(self) -> TraceCompleteness:
        """Return the single cumulative completeness value owned by the summary."""

        return self.summary.completeness

    @property
    def message_count(self) -> int:
        """Return the single cumulative message count owned by the summary."""

        return self.summary.message_count

    @property
    def tool_call_count(self) -> int:
        """Return the single cumulative Tool count owned by the summary."""

        return self.summary.tool_call_count


class CoreRunCheckpoint(TraceModel):
    """Incremental lineage, state, and completeness for one semantic Run."""

    run_id: str
    first_seq: int
    last_seq: int
    started_at: datetime
    last_occurred_at: datetime
    parent_run_id: str | None = None
    lineage_bound: bool = False
    input_kind: str | None = None
    turn_id: str | None = None
    terminal: RunTerminalOutcome | None = None
    completed_at: datetime | None = None
    state: TraceState = Field(default_factory=TraceState)
    has_state_changes: bool = False
    messages: tuple[TraceMessage, ...] = ()
    reasoning: tuple[TraceReasoning, ...] = ()
    tree_facts: tuple[TraceSemanticFact, ...] = ()
    tree_sequences: tuple[int, ...] = ()
    has_view_changes: bool = False
    payload_omitted: bool = False
    missing_prefix: bool = False
    interaction_facts: tuple[InteractionFact, ...] = ()
    interaction_sequences: tuple[int, ...] = ()


class CoreTurnCheckpoint(TraceModel):
    """Index one Turn's Run membership and inclusive Ledger sequence range."""

    turn_id: str
    first_seq: int
    last_seq: int
    run_ids: tuple[str, ...]


class CoreProjectionState(TraceModel):
    """Serializable incremental state for bounded core Trace queries.

    The state keeps Turn paging metadata and one independent cumulative snapshot per
    Run. A child inherits its parent snapshot once, then message, reasoning, state, and
    tree transitions advance without sharing mutable sibling materialization.
    """

    generation: str | None = None
    as_of_seq: int = 0
    runs: dict[str, CoreRunCheckpoint] = Field(default_factory=dict)
    heads: tuple[str, ...] = ()
    turns: dict[str, CoreTurnCheckpoint] = Field(default_factory=dict)
    turn_order: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CoreProjectionWindow:
    """Describe the selected lineage and bounded Turn window for one query."""

    selected_head: str
    available_heads: tuple[str, ...]
    selected_run_ids: frozenset[str]
    visible_run_ids: frozenset[str]
    run_turns: Mapping[str, str]
    visible_turns: frozenset[str]
    first_seq: int
    has_older: bool


def project_core(
    events: tuple[TraceEvent, ...],
    *,
    head_run_id: str | None,
    turn_limit: int,
    active_run_ids: tuple[str, ...],
) -> CoreProjection:
    """Fold one fixed event prefix into current head-scoped semantic views."""

    facts = tuple(event.fact for event in events)
    runs, heads, run_turns, turn_order, missing_prefix_runs = _lineage(facts)
    first_sequences: dict[str, int] = {}
    for event in events:
        run_id = event.fact.identity.run_id
        first_sequences.setdefault(run_id, event.trace_seq)
    for run_id, sequence in first_sequences.items():
        info = runs.get(run_id)
        if info is not None:
            info.first_seq = sequence
    if not heads:
        raise TraceCorruption("Trace has no selectable Run head")
    if head_run_id is None:
        if len(heads) != 1:
            values = tuple(sorted(heads))
            raise AmbiguousTraceHead(
                "Trace has multiple Run heads",
                context={
                    "head_count": len(values),
                    "head_run_ids": ",".join(values),
                },
            )
        selected_head = next(iter(heads))
    else:
        selected_head = _resolve_requested_head(
            runs,
            heads,
            requested=head_run_id,
        )
    lineage = _ancestor_lineage(runs, selected_head)
    selected_events = tuple(
        event for event in events if event.fact.identity.run_id in lineage
    )
    selected_facts = tuple(event.fact for event in selected_events)
    selected_fact_sequences = {
        id(event.fact): event.trace_seq for event in selected_events
    }
    selected_turn_ids = {run_turns[run_id] for run_id in lineage if run_id in run_turns}
    selected_turns = [turn for turn in turn_order if turn in selected_turn_ids]
    visible_turns = set(selected_turns[-turn_limit:])
    visible_runs = {
        run_id for run_id in lineage if run_turns.get(run_id) in visible_turns
    }
    visible_events = tuple(
        event for event in selected_events if event.fact.identity.run_id in visible_runs
    )
    visible_facts = tuple(event.fact for event in visible_events)
    fact_sequences = {id(event.fact): event.trace_seq for event in visible_events}
    completeness = TraceCompleteness(
        missing_prefix=bool(lineage & missing_prefix_runs),
        missing_tail=(
            runs[selected_head].terminal is None and selected_head not in active_run_ids
        ),
        payload_omitted=_payload_omitted(selected_facts),
    )
    messages = _messages(visible_facts, fact_sequences=fact_sequences)
    reasoning = _reasoning(visible_facts, fact_sequences=fact_sequences)
    interactions = _interactions(visible_facts, fact_sequences=fact_sequences)
    state = _state(selected_facts)
    selected_interactions = _interactions(
        selected_facts,
        fact_sequences=selected_fact_sequences,
    )
    pending_interactions = tuple(
        interaction
        for interaction in selected_interactions
        if interaction.status == "pending"
    )
    status = _status_with_pending(
        _status(runs[selected_head], active_run_ids),
        pending_interactions,
    )
    tree = _tree(
        visible_facts,
        trace_sequences=tuple(event.trace_seq for event in visible_events),
        runs=runs,
        run_turns=run_turns,
        visible_turns=visible_turns,
        visible_run_ids=visible_runs,
        active_run_ids=active_run_ids,
    )
    message_count = sum(
        message.role in {"user", "assistant"}
        for message in _messages(
            selected_facts,
            fact_sequences=selected_fact_sequences,
        )
    )
    tool_call_count = sum(
        isinstance(fact, ToolFact) and fact.phase == "started"
        for fact in selected_facts
    )
    summary = TraceSummary(
        status=status,
        completeness=completeness,
        message_count=message_count,
        tool_call_count=tool_call_count,
        pending_interactions=pending_interactions,
        last_occurred_at=max(fact.occurred_at for fact in selected_facts),
    )
    return CoreProjection(
        facts=facts,
        selected_facts=selected_facts,
        selected_run_ids=frozenset(lineage),
        selected_head=selected_head,
        available_heads=tuple(sorted(heads)),
        messages=messages,
        reasoning=reasoning,
        tree=tree,
        state=state,
        interactions=interactions,
        summary=summary,
        has_older=len(selected_turns) > len(visible_turns),
    )


def empty_core_projection_state() -> CoreProjectionState:
    """Return the validated sequence-zero state for a new Trace generation."""

    return CoreProjectionState()


def advance_core_projection_state(
    state: CoreProjectionState,
    events: tuple[TraceEvent, ...],
) -> CoreProjectionState:
    """Apply one contiguous event batch without refolding an earlier Ledger prefix.

    Args:
        state: Previously validated state ending immediately before ``events``.
        events: Ascending, contiguous events from one Store generation.

    Returns:
        A new serializable state at the final event sequence.

    Raises:
        TraceCorruption: Sequence, generation, lineage, or parent facts conflict.
    """

    if not isinstance(state, CoreProjectionState):
        raise TypeError("state must be a CoreProjectionState")
    runs = {name: value.model_copy(deep=True) for name, value in state.runs.items()}
    heads = set(state.heads)
    turns = {name: value.model_copy(deep=True) for name, value in state.turns.items()}
    turn_order = list(state.turn_order)
    generation = state.generation
    as_of_seq = state.as_of_seq

    def inherited_run(
        run_id: str,
        *,
        occurred_at: datetime,
        trace_seq: int,
        parent_run_id: str | None = None,
    ) -> CoreRunCheckpoint:
        parent = None if parent_run_id is None else runs.get(parent_run_id)
        return CoreRunCheckpoint(
            run_id=run_id,
            first_seq=trace_seq,
            last_seq=trace_seq,
            started_at=occurred_at,
            last_occurred_at=occurred_at,
            parent_run_id=parent_run_id,
            state=(
                TraceState() if parent is None else parent.state.model_copy(deep=True)
            ),
            messages=() if parent is None else parent.messages,
            reasoning=() if parent is None else parent.reasoning,
            tree_facts=() if parent is None else parent.tree_facts,
            tree_sequences=() if parent is None else parent.tree_sequences,
            payload_omitted=False if parent is None else parent.payload_omitted,
            missing_prefix=(parent_run_id is not None and parent is None)
            or (False if parent is None else parent.missing_prefix),
        )

    def bind_parent(
        info: CoreRunCheckpoint,
        parent_run_id: str | None,
    ) -> CoreRunCheckpoint:
        if parent_run_id == info.run_id:
            raise TraceCorruption("Run lineage cannot reference itself")
        if (
            parent_run_id is not None
            and info.parent_run_id is not None
            and parent_run_id != info.parent_run_id
        ):
            raise TraceCorruption("Run lineage parent changed after start")
        if parent_run_id is None or info.parent_run_id == parent_run_id:
            return info
        parent = runs.get(parent_run_id)
        return info.model_copy(
            update={
                "parent_run_id": parent_run_id,
                "state": (
                    info.state
                    if info.has_state_changes or parent is None
                    else parent.state.model_copy(deep=True)
                ),
                "messages": (
                    info.messages
                    if info.has_view_changes or parent is None
                    else parent.messages
                ),
                "reasoning": (
                    info.reasoning
                    if info.has_view_changes or parent is None
                    else parent.reasoning
                ),
                "tree_facts": (
                    info.tree_facts
                    if info.has_view_changes or parent is None
                    else parent.tree_facts
                ),
                "tree_sequences": (
                    info.tree_sequences
                    if info.has_view_changes or parent is None
                    else parent.tree_sequences
                ),
                "payload_omitted": info.payload_omitted
                or (False if parent is None else parent.payload_omitted),
                "missing_prefix": info.missing_prefix
                or parent is None
                or (False if parent is None else parent.missing_prefix),
            }
        )

    def assign_turn(
        info: CoreRunCheckpoint,
        turn_id: str,
        trace_seq: int,
    ) -> CoreRunCheckpoint:
        previous = turns.get(turn_id)
        run_ids = (
            (info.run_id,)
            if previous is None
            else tuple(dict.fromkeys((*previous.run_ids, info.run_id)))
        )
        turns[turn_id] = CoreTurnCheckpoint(
            turn_id=turn_id,
            first_seq=(
                info.first_seq
                if previous is None
                else min(previous.first_seq, info.first_seq)
            ),
            last_seq=(
                trace_seq if previous is None else max(previous.last_seq, trace_seq)
            ),
            run_ids=run_ids,
        )
        if turn_id not in turn_order:
            turn_order.append(turn_id)
        return info.model_copy(update={"turn_id": turn_id})

    for event in events:
        if not isinstance(event, TraceEvent):
            raise TraceCorruption("Core Projection received a non-Trace event")
        if event.trace_seq != as_of_seq + 1:
            raise TraceCorruption("Core Projection event sequence is not contiguous")
        if generation is None:
            generation = event.generation
        elif event.generation != generation:
            raise TraceCorruption("Core Projection crossed a Trace generation")
        as_of_seq = event.trace_seq
        fact = event.fact
        run_id = fact.identity.run_id
        info = runs.get(run_id)

        if isinstance(fact, RunFact) and fact.phase in {"started", "input", "resumed"}:
            parent_run_id = fact.parent_run_id
            candidates = heads - {run_id}
            lineage_already_bound = info is not None and info.lineage_bound
            if (
                parent_run_id is None
                and not lineage_already_bound
                and len(candidates) == 1
            ):
                candidate = next(iter(candidates))
                if runs[candidate].terminal is not None:
                    parent_run_id = candidate
            known_parent_run_id = (
                parent_run_id
                if parent_run_id is not None
                else None
                if info is None
                else info.parent_run_id
            )
            # Runtime lifecycle phases may omit the parent already bound by ``started``.
            # Repeated input evidence must not degrade that lineage, while a continuation
            # with no explicit, inferred, or previously bound parent remains incomplete.
            if known_parent_run_id is None and fact.input_kind in {"resume", "abandon"}:
                info = info or inherited_run(
                    run_id,
                    occurred_at=fact.occurred_at,
                    trace_seq=event.trace_seq,
                )
                info = info.model_copy(update={"missing_prefix": True})
            if info is None:
                info = inherited_run(
                    run_id,
                    occurred_at=fact.occurred_at,
                    trace_seq=event.trace_seq,
                    parent_run_id=parent_run_id,
                )
            else:
                info = bind_parent(info, parent_run_id)
            if (
                fact.input_kind is not None
                and info.input_kind is not None
                and fact.input_kind != info.input_kind
            ):
                raise TraceCorruption("Run input kind changed after start")
            info = info.model_copy(
                update={
                    "input_kind": fact.input_kind or info.input_kind,
                    "lineage_bound": info.lineage_bound or fact.phase == "started",
                    "last_seq": event.trace_seq,
                }
            )
            parent = (
                None if info.parent_run_id is None else runs.get(info.parent_run_id)
            )
            if (
                parent is not None
                and fact.input_kind in {"resume", "abandon"}
                and parent.turn_id is not None
            ):
                info = assign_turn(info, parent.turn_id, event.trace_seq)
            elif info.turn_id is None and fact.input_kind in {"resume", "abandon"}:
                info = assign_turn(
                    info,
                    f"turn-partial:{run_id}",
                    event.trace_seq,
                )
            if info.parent_run_id is not None:
                heads.discard(info.parent_run_id)
            heads.add(run_id)
        elif info is None:
            info = inherited_run(
                run_id,
                occurred_at=fact.occurred_at,
                trace_seq=event.trace_seq,
            )
        else:
            info = info.model_copy(update={"last_seq": event.trace_seq})

        if isinstance(fact, TurnFact):
            info = assign_turn(info, fact.turn_id, event.trace_seq)
        elif isinstance(fact, StateRevisionFact | PlanRevisionFact):
            info = info.model_copy(
                update={
                    "state": _apply_incremental_state(info.state, fact),
                    "has_state_changes": True,
                }
            )
        elif isinstance(fact, RunFact) and fact.phase == "terminal":
            info = info.model_copy(
                update={
                    "terminal": fact.outcome,
                    "completed_at": fact.occurred_at,
                }
            )

        messages = _advance_messages(
            info.messages,
            fact,
            trace_seq=event.trace_seq,
        )
        reasoning = _advance_reasoning(
            info.reasoning,
            fact,
            trace_seq=event.trace_seq,
        )
        tree_facts = info.tree_facts
        tree_sequences = info.tree_sequences
        if isinstance(
            fact, RuntimeTaskFact | ToolFact | SubagentFact | PlanRevisionFact
        ):
            tree_facts = (*tree_facts, fact)
            tree_sequences = (*tree_sequences, event.trace_seq)
        info = info.model_copy(
            update={
                "messages": messages,
                "reasoning": reasoning,
                "tree_facts": tree_facts,
                "tree_sequences": tree_sequences,
                "has_view_changes": info.has_view_changes
                or messages != info.messages
                or reasoning != info.reasoning
                or tree_facts != info.tree_facts,
            }
        )

        if isinstance(fact, InteractionFact):
            info = info.model_copy(
                update={
                    "interaction_facts": (*info.interaction_facts, fact),
                    "interaction_sequences": (
                        *info.interaction_sequences,
                        event.trace_seq,
                    ),
                }
            )

        info = info.model_copy(
            update={
                "last_occurred_at": max(
                    info.last_occurred_at,
                    fact.occurred_at,
                )
            }
        )

        if _payload_omitted((fact,)):
            info = info.model_copy(update={"payload_omitted": True})
        turn_id = info.turn_id
        if turn_id is not None:
            info = assign_turn(info, turn_id, event.trace_seq)
        runs[run_id] = info

    return CoreProjectionState(
        generation=generation,
        as_of_seq=as_of_seq,
        runs=runs,
        heads=tuple(sorted(heads)),
        turns=turns,
        turn_order=tuple(turn_order),
    )


def select_core_projection_window(
    state: CoreProjectionState,
    *,
    head_run_id: str | None,
    turn_limit: int,
) -> CoreProjectionWindow:
    """Select one head lineage and its latest bounded Turn window."""

    if turn_limit < 1:
        raise ValueError("turn_limit must be positive")
    runs = state.runs
    heads = set(state.heads)
    if not heads:
        raise TraceCorruption("Trace has no selectable Run head")
    run_infos = {
        run_id: _RunInfo(
            run_id=run_id,
            started_at=info.started_at,
            parent_run_id=info.parent_run_id,
            input_kind=info.input_kind,
            turn_id=info.turn_id,
            terminal=info.terminal,
            completed_at=info.completed_at,
        )
        for run_id, info in runs.items()
    }
    if head_run_id is None:
        if len(heads) != 1:
            values = tuple(sorted(heads))
            raise AmbiguousTraceHead(
                "Trace has multiple Run heads",
                context={
                    "head_count": len(values),
                    "head_run_ids": ",".join(values),
                },
            )
        selected_head = next(iter(heads))
    else:
        selected_head = _resolve_requested_head(
            run_infos,
            heads,
            requested=head_run_id,
        )
    lineage = _ancestor_lineage(run_infos, selected_head)
    run_turns = {
        run_id: info.turn_id
        for run_id, info in runs.items()
        if info.turn_id is not None
    }
    turn_order = list(state.turn_order)
    for run_id in lineage:
        if run_id not in runs:
            continue
        if run_id in run_turns:
            continue
        parent = runs[run_id].parent_run_id
        visited: set[str] = set()
        while parent is not None and parent not in visited:
            visited.add(parent)
            if parent in run_turns:
                run_turns[run_id] = run_turns[parent]
                break
            parent = None if parent not in runs else runs[parent].parent_run_id
        if run_id not in run_turns:
            partial = f"turn-partial:{run_id}"
            run_turns[run_id] = partial
            if partial not in turn_order:
                turn_order.append(partial)
    selected_turn_ids = {run_turns[run_id] for run_id in lineage if run_id in run_turns}
    selected_turns = [value for value in turn_order if value in selected_turn_ids]
    visible_turns = frozenset(selected_turns[-turn_limit:])
    visible_runs = frozenset(
        run_id
        for run_id in lineage
        if run_id in run_turns and run_turns[run_id] in visible_turns
    )
    first_seq = min(runs[run_id].first_seq for run_id in visible_runs)
    return CoreProjectionWindow(
        selected_head=selected_head,
        available_heads=tuple(sorted(heads)),
        selected_run_ids=frozenset(lineage),
        visible_run_ids=visible_runs,
        run_turns=MappingProxyType(run_turns),
        visible_turns=visible_turns,
        first_seq=first_seq,
        has_older=len(selected_turns) > len(visible_turns),
    )


def select_prior_run_ids(
    state: CoreProjectionState,
    *,
    parent_run_id: str | None,
) -> frozenset[str]:
    """Select the ancestor lineage used to hydrate a newly opened writer session."""

    selected_parent = select_prior_head_run_id(
        state,
        parent_run_id=parent_run_id,
    )
    if selected_parent is None or selected_parent not in state.runs:
        return frozenset()
    run_infos = {
        run_id: _RunInfo(
            run_id=run_id,
            started_at=info.started_at,
            parent_run_id=info.parent_run_id,
            terminal=info.terminal,
        )
        for run_id, info in state.runs.items()
    }
    return frozenset(_ancestor_lineage(run_infos, selected_parent))


def select_prior_head_run_id(
    state: CoreProjectionState,
    *,
    parent_run_id: str | None,
) -> str | None:
    """Resolve the explicit or sole completed parent used by a new Run."""

    if not state.runs:
        return None
    if parent_run_id is not None:
        return parent_run_id if parent_run_id in state.runs else None
    if len(state.heads) != 1:
        return None
    candidate = state.heads[0]
    return candidate if state.runs[candidate].terminal is not None else None


def _advance_messages(
    current: tuple[TraceMessage, ...],
    fact: TraceSemanticFact,
    *,
    trace_seq: int,
) -> tuple[TraceMessage, ...]:
    """Apply one message or Tool-result fact to a stable ordered entity tuple."""

    values = {message.id: message for message in current}
    order = [message.id for message in current]
    if isinstance(fact, ToolFact) and fact.phase == "result":
        if fact.content is None:
            return current
        for message in current:
            if (
                message.role == "tool"
                and message.namespace == fact.namespace
                and message.tool_call_id == fact.source_tool_call_id
            ):
                values[message.id] = _message_with_content(
                    message,
                    fact.content,
                    replace=True,
                    tool_content=True,
                )
        return tuple(values[item] for item in order if item in values)
    if not isinstance(fact, MessageFact):
        return current
    if fact.phase == "removed":
        values.pop(fact.message_id, None)
        return tuple(values[item] for item in order if item in values)
    message = values.get(fact.message_id)
    if message is None:
        message = TraceMessage(
            id=fact.message_id,
            trace_seq=trace_seq,
            source_id=fact.source_message_id,
            namespace=fact.namespace,
            run_id=fact.identity.run_id,
            role=fact.role,
            name=fact.name,
            tool_call_id=fact.tool_call_id,
            status="streaming",
            created_at=fact.occurred_at,
        )
        values[fact.message_id] = message
        order.append(fact.message_id)
    if fact.content is not None:
        message = _message_with_content(
            message,
            fact.content,
            replace=fact.phase != "content",
        )
    if fact.phase in {"completed", "reconciled"}:
        message = message.model_copy(
            update={"status": "completed", "completed_at": fact.occurred_at}
        )
    values[fact.message_id] = message
    return tuple(values[item] for item in order if item in values)


def _message_with_content(
    message: TraceMessage,
    content: CapturedValue,
    *,
    replace: bool,
    tool_content: bool = False,
) -> TraceMessage:
    if content.disposition == "omitted":
        return message.model_copy(update={"content_omitted": True})
    value = _tool_content_value(content.value) if tool_content else content.value
    return message.model_copy(
        update={
            "content": (value if replace else _append_content(message.content, value))
        }
    )


def _advance_reasoning(
    current: tuple[TraceReasoning, ...],
    fact: TraceSemanticFact,
    *,
    trace_seq: int,
) -> tuple[TraceReasoning, ...]:
    """Apply one reasoning delta, reconciliation, or completion by stable ID."""

    if not isinstance(fact, ReasoningFact):
        return current
    values = {item.id: item for item in current}
    order = [item.id for item in current]
    item = values.get(fact.reasoning_id)
    if item is None:
        item = TraceReasoning(
            id=fact.reasoning_id,
            trace_seq=trace_seq,
            message_id=fact.message_id,
            namespace=fact.namespace,
            run_id=fact.identity.run_id,
            extractor=fact.extractor,
            status="streaming",
            created_at=fact.occurred_at,
        )
        order.append(fact.reasoning_id)
    elif item.message_id != fact.message_id or item.extractor != fact.extractor:
        raise TraceCorruption("Reasoning identity changed inside one stream")
    if fact.content is not None:
        if fact.content.disposition == "omitted":
            item = item.model_copy(update={"content_omitted": True})
        else:
            item = item.model_copy(
                update={
                    "content": (
                        fact.content.value
                        if fact.phase == "reconciled"
                        else _append_content(item.content, fact.content.value)
                    )
                }
            )
    if fact.phase == "completed":
        item = item.model_copy(
            update={"status": "completed", "completed_at": fact.occurred_at}
        )
    values[fact.reasoning_id] = item
    return tuple(values[value] for value in order if value in values)


def _selected_interactions(
    state: CoreProjectionState,
    run_ids: frozenset[str],
) -> tuple[TraceInteraction, ...]:
    """Fold interaction transitions from exactly the requested Run set."""

    entries: list[tuple[int, InteractionFact]] = []
    for run_id, info in state.runs.items():
        if run_id not in run_ids:
            continue
        if len(info.interaction_facts) != len(info.interaction_sequences):
            raise TraceCorruption(
                "Run interaction sequences do not match interaction facts"
            )
        entries.extend(
            zip(
                info.interaction_sequences,
                info.interaction_facts,
                strict=True,
            )
        )
    entries.sort(key=lambda item: item[0])
    facts = tuple(fact for _sequence, fact in entries)
    fact_sequences = {id(fact): sequence for sequence, fact in entries}
    return _interactions(facts, fact_sequences=fact_sequences)


def project_core_checkpoint(
    state: CoreProjectionState,
    *,
    head_run_id: str | None,
    turn_limit: int,
    active_run_ids: tuple[str, ...],
) -> CoreProjection:
    """Build public views from incremental state and one bounded Ledger window."""

    window = select_core_projection_window(
        state,
        head_run_id=head_run_id,
        turn_limit=turn_limit,
    )
    head = state.runs[window.selected_head]
    messages = tuple(
        message for message in head.messages if message.run_id in window.visible_run_ids
    )
    reasoning = tuple(
        item for item in head.reasoning if item.run_id in window.visible_run_ids
    )
    interactions = _selected_interactions(state, window.visible_run_ids)
    if len(head.tree_facts) != len(head.tree_sequences):
        raise TraceCorruption("Run tree sequences do not match structural facts")
    tree_entries = [
        (fact, sequence)
        for fact, sequence in zip(
            head.tree_facts,
            head.tree_sequences,
            strict=True,
        )
        if fact.identity.run_id in window.visible_run_ids
    ]
    tree_facts = tuple(fact for fact, _sequence in tree_entries)
    tree_sequences = tuple(sequence for _fact, sequence in tree_entries)
    runs = {
        run_id: _RunInfo(
            run_id=run_id,
            started_at=info.started_at,
            first_seq=info.first_seq,
            parent_run_id=info.parent_run_id,
            input_kind=info.input_kind,
            turn_id=window.run_turns.get(run_id),
            terminal=info.terminal,
            completed_at=info.completed_at,
        )
        for run_id, info in sorted(
            state.runs.items(),
            key=lambda item: item[1].first_seq,
        )
    }
    completeness = TraceCompleteness(
        missing_prefix=any(
            state.runs[run_id].missing_prefix
            for run_id in window.selected_run_ids
            if run_id in state.runs
        ),
        missing_tail=(head.terminal is None and head.run_id not in active_run_ids),
        payload_omitted=any(
            state.runs[run_id].payload_omitted
            for run_id in window.selected_run_ids
            if run_id in state.runs
        ),
    )
    message_count = sum(
        message.role in {"user", "assistant"} for message in head.messages
    )
    tool_call_count = sum(
        isinstance(fact, ToolFact) and fact.phase == "started"
        for fact in head.tree_facts
    )
    selected_interactions = _selected_interactions(
        state,
        window.selected_run_ids,
    )
    pending_interactions = tuple(
        item for item in selected_interactions if item.status == "pending"
    )
    summary = TraceSummary(
        status=_status_with_pending(
            _status(runs[window.selected_head], active_run_ids),
            pending_interactions,
        ),
        completeness=completeness,
        message_count=message_count,
        tool_call_count=tool_call_count,
        pending_interactions=pending_interactions,
        last_occurred_at=max(
            state.runs[run_id].last_occurred_at
            for run_id in window.selected_run_ids
            if run_id in state.runs
        ),
    )
    return CoreProjection(
        facts=tree_facts,
        selected_facts=tree_facts,
        selected_run_ids=window.selected_run_ids,
        selected_head=window.selected_head,
        available_heads=window.available_heads,
        messages=messages,
        reasoning=reasoning,
        tree=_tree(
            tree_facts,
            trace_sequences=tree_sequences,
            runs=runs,
            run_turns=dict(window.run_turns),
            visible_turns=set(window.visible_turns),
            visible_run_ids=set(window.visible_run_ids),
            active_run_ids=active_run_ids,
        ),
        state=head.state.model_copy(deep=True),
        interactions=interactions,
        summary=summary,
        has_older=window.has_older,
    )


def _apply_incremental_state(
    current: TraceState,
    fact: StateRevisionFact | PlanRevisionFact,
) -> TraceState:
    """Apply one state-bearing fact without touching sibling graph scopes."""

    root = dict(current.root)
    subgraphs = {name: dict(value) for name, value in current.subgraphs.items()}
    target = (
        root
        if not fact.namespace
        else subgraphs.setdefault(_namespace_key(fact.namespace), {})
    )
    if isinstance(fact, PlanRevisionFact):
        if fact.plan.disposition == "inline":
            target["tinkerfin_plan"] = fact.plan.value
    else:
        for key in fact.removed_keys:
            target.pop(key, None)
        if fact.changes.disposition == "inline" and isinstance(
            fact.changes.value, dict
        ):
            target.update(fact.changes.value)
    return TraceState(root=root, subgraphs=subgraphs)


def select_prior_events(
    events: tuple[TraceEvent, ...],
    *,
    parent_run_id: str | None,
) -> tuple[TraceEvent, ...]:
    """Select only the ancestor facts that can seed a new Run session."""

    if not events:
        return ()
    facts = tuple(event.fact for event in events)
    runs, heads, _run_turns, _turn_order, _missing_prefix = _lineage(facts)
    selected_parent = parent_run_id
    if selected_parent is None and len(heads) == 1:
        candidate = next(iter(heads))
        if runs[candidate].terminal is not None:
            selected_parent = candidate
    if selected_parent is None or selected_parent not in runs:
        return ()
    lineage = _ancestor_lineage(runs, selected_parent)
    return tuple(event for event in events if event.fact.identity.run_id in lineage)


def evaluate_projection(
    projection: RegisteredTraceProjection,
    facts: tuple[TraceSemanticFact, ...],
) -> BaseModel:
    """Validate every custom Projection state transition and final result."""

    state = advance_projection_state(
        projection,
        projection.initial_state(),
        facts,
    )
    return finish_projection(projection, state)


def advance_projection_state(
    projection: RegisteredTraceProjection,
    state: BaseModel,
    facts: tuple[TraceSemanticFact, ...],
) -> BaseModel:
    """Validate and increment one serializable custom Projection state."""

    try:
        current = projection.state_type.model_validate_json(
            state.model_dump_json(by_alias=True)
        )
        for fact in facts:
            current = projection.apply(current, fact)
            current = projection.state_type.model_validate_json(
                current.model_dump_json(by_alias=True)
            )
        return current
    except Exception as error:
        raise TraceProjectionFailed(
            "Trace Projection failed",
            context={"projection": projection.name},
            diagnostic_context={"error_type": type(error).__name__},
            cause=error,
        ) from error


def finish_projection(
    projection: RegisteredTraceProjection,
    state: BaseModel,
) -> BaseModel:
    """Validate one public result from an already checkpointed Projection state."""

    try:
        validated = projection.state_type.model_validate_json(
            state.model_dump_json(by_alias=True)
        )
        result = projection.finish(validated)
        return projection.result_type.model_validate_json(
            result.model_dump_json(by_alias=True),
        )
    except Exception as error:
        raise TraceProjectionFailed(
            "Trace Projection failed",
            context={"projection": projection.name},
            diagnostic_context={"error_type": type(error).__name__},
            cause=error,
        ) from error


def _lineage(
    facts: tuple[TraceSemanticFact, ...],
) -> tuple[
    dict[str, _RunInfo],
    set[str],
    dict[str, str],
    list[str],
    set[str],
]:
    """Reconstruct the Run DAG, selected heads, Turn ownership, and missing evidence.

    Explicit parent IDs are authoritative. The sole completed head is used only for the
    ordinary linear case where Runtime facts omit an explicit parent. Resume and abandon
    inherit their parent's Turn; unresolved ancestry receives a deterministic partial
    Turn and marks the resulting view incomplete rather than inventing history.
    """

    runs: dict[str, _RunInfo] = {}
    heads: set[str] = set()
    run_turns: dict[str, str] = {}
    turn_order: list[str] = []
    missing_prefix_runs: set[str] = set()
    turn_by_root_run: dict[str, str] = {}
    for fact in facts:
        run_id = fact.identity.run_id
        if isinstance(fact, RunFact) and fact.phase == "started":
            info = runs.setdefault(
                run_id, _RunInfo(run_id=run_id, started_at=fact.occurred_at)
            )
            candidates = heads - {run_id}
            parent = fact.parent_run_id
            if parent is None and len(candidates) == 1:
                candidate = next(iter(candidates))
                if runs[candidate].terminal is not None:
                    parent = candidate
            if parent == run_id:
                raise TraceCorruption("Run lineage cannot reference itself")
            info.parent_run_id = parent
            info.input_kind = fact.input_kind
            if parent is not None:
                heads.discard(parent)
                if parent not in runs:
                    missing_prefix_runs.add(run_id)
            heads.add(run_id)
        elif isinstance(fact, TurnFact):
            turn_by_root_run[run_id] = fact.turn_id
            run_turns[run_id] = fact.turn_id
            if fact.turn_id not in turn_order:
                turn_order.append(fact.turn_id)
        elif isinstance(fact, RunFact) and fact.phase in {"input", "resumed"}:
            info = runs.setdefault(
                run_id,
                _RunInfo(run_id=run_id, started_at=fact.occurred_at),
            )
            if (
                fact.parent_run_id is not None
                and info.parent_run_id is not None
                and fact.parent_run_id != info.parent_run_id
            ):
                raise TraceCorruption("Run lineage parent changed after start")
            if (
                fact.input_kind is not None
                and info.input_kind is not None
                and fact.input_kind != info.input_kind
            ):
                raise TraceCorruption("Run input kind changed after start")
            parent = fact.parent_run_id or info.parent_run_id
            if parent is None:
                candidates = heads - {run_id}
                if len(candidates) == 1:
                    candidate = next(iter(candidates))
                    if runs[candidate].terminal is not None:
                        parent = candidate
                elif fact.input_kind in {"resume", "abandon"} and not candidates:
                    missing_prefix_runs.add(run_id)
            info.parent_run_id = parent
            info.input_kind = fact.input_kind
            if parent is not None:
                heads.discard(parent)
                parent_turn = run_turns.get(parent)
                if parent_turn is not None and fact.input_kind in {"resume", "abandon"}:
                    run_turns[run_id] = parent_turn
                elif parent not in runs:
                    missing_prefix_runs.add(run_id)
            heads.add(run_id)
        elif isinstance(fact, RunFact) and fact.phase == "terminal":
            info = runs.setdefault(
                run_id,
                _RunInfo(run_id=run_id, started_at=fact.occurred_at),
            )
            info.terminal = fact.outcome
            info.completed_at = fact.occurred_at
    for run_id, turn_id in turn_by_root_run.items():
        run_turns[run_id] = turn_id
    for run_id, info in runs.items():
        if run_id in run_turns:
            continue
        parent = info.parent_run_id
        visited: set[str] = set()
        while parent is not None and parent not in visited:
            visited.add(parent)
            if parent in run_turns:
                run_turns[run_id] = run_turns[parent]
                break
            parent_info = runs.get(parent)
            parent = None if parent_info is None else parent_info.parent_run_id
        if run_id not in run_turns:
            partial = f"turn-partial:{run_id}"
            run_turns[run_id] = partial
            if partial not in turn_order:
                turn_order.append(partial)
            missing_prefix_runs.add(run_id)
    return runs, heads, run_turns, turn_order, missing_prefix_runs


def _ancestor_lineage(runs: dict[str, _RunInfo], head: str) -> set[str]:
    lineage: set[str] = set()
    current: str | None = head
    while current is not None:
        if current in lineage:
            raise TraceCorruption("Run lineage contains a cycle")
        lineage.add(current)
        info = runs.get(current)
        current = None if info is None else info.parent_run_id
    return lineage


def _resolve_requested_head(
    runs: dict[str, _RunInfo],
    heads: set[str],
    *,
    requested: str,
) -> str:
    """Resolve a requested historical Run to one unambiguous current descendant head.

    Selecting an ancestor follows its only surviving branch. Multiple descendants must
    be chosen explicitly, and a disconnected Run is corruption rather than an empty
    branch.
    """

    if requested in heads:
        return requested
    if requested not in runs:
        raise TraceRunNotFound(
            "Selected Run does not exist in this Trace generation",
            context={"head_run_id": requested},
        )
    descendants = {head for head in heads if requested in _ancestor_lineage(runs, head)}
    if len(descendants) == 1:
        return next(iter(descendants))
    if len(descendants) > 1:
        values = tuple(sorted(descendants))
        raise AmbiguousTraceHead(
            "Selected Trace branch has multiple Run heads",
            context={
                "head_count": len(values),
                "head_run_ids": ",".join(values),
            },
        )
    raise TraceCorruption(
        "Selected Run is disconnected from every current Trace head",
        context={"head_run_id": requested},
    )


def _messages(
    facts: tuple[TraceSemanticFact, ...],
    *,
    fact_sequences: Mapping[int, int],
) -> tuple[TraceMessage, ...]:
    """Fold message deltas while joining Tool result content by scoped call ID.

    Tool result bodies have one authoritative ``ToolFact`` copy. A corresponding Tool
    message references that result during projection, avoiding duplicate persistence
    while preserving normal message order and replay removal semantics.
    """

    values: dict[str, _MutableMessage] = {}
    order: list[str] = []
    tool_messages: dict[tuple[tuple[str, ...], str], _MutableMessage] = {}
    tool_results: dict[tuple[tuple[str, ...], str], CapturedValue] = {}
    for fact in facts:
        if isinstance(fact, ToolFact) and fact.phase == "result":
            if fact.content is None:
                continue
            tool_key = (fact.namespace, fact.source_tool_call_id)
            tool_results[tool_key] = fact.content
            message = tool_messages.get(tool_key)
            if message is not None:
                _apply_message_content(
                    message,
                    fact.content,
                    replace=True,
                    tool_content=True,
                )
            continue
        if not isinstance(fact, MessageFact):
            continue
        if fact.phase == "removed":
            values.pop(fact.message_id, None)
            if fact.message_id in order:
                order.remove(fact.message_id)
            continue
        message = values.get(fact.message_id)
        if message is None:
            message = _MutableMessage(
                id=fact.message_id,
                trace_seq=fact_sequences[id(fact)],
                source_id=fact.source_message_id,
                namespace=fact.namespace,
                run_id=fact.identity.run_id,
                role=fact.role,
                created_at=fact.occurred_at,
                name=fact.name,
                tool_call_id=fact.tool_call_id,
            )
            values[fact.message_id] = message
            order.append(fact.message_id)
            if fact.role == "tool" and fact.tool_call_id is not None:
                tool_key = (fact.namespace, fact.tool_call_id)
                tool_messages[tool_key] = message
                result = tool_results.get(tool_key)
                if result is not None:
                    _apply_message_content(
                        message,
                        result,
                        replace=True,
                        tool_content=True,
                    )
        if fact.content is not None:
            _apply_message_content(
                message,
                fact.content,
                replace=fact.phase != "content",
            )
        if fact.phase in {"completed", "reconciled"}:
            message.completed_at = fact.occurred_at
    return tuple(
        TraceMessage(
            id=message.id,
            trace_seq=message.trace_seq,
            source_id=message.source_id,
            namespace=message.namespace,
            run_id=message.run_id,
            role=message.role,
            content=message.content,
            content_omitted=message.content_omitted,
            name=message.name,
            tool_call_id=message.tool_call_id,
            status="completed" if message.completed_at is not None else "streaming",
            created_at=message.created_at,
            completed_at=message.completed_at,
        )
        for message_id in order
        if (message := values.get(message_id)) is not None
    )


def _apply_message_content(
    message: _MutableMessage,
    content: CapturedValue,
    *,
    replace: bool,
    tool_content: bool = False,
) -> None:
    value = _tool_content_value(content.value) if tool_content else content.value
    if content.disposition == "omitted":
        message.content_omitted = True
    elif replace:
        message.content = value
    else:
        message.content = _append_content(message.content, value)


def _tool_content_value(value: JsonValue | None) -> JsonValue | None:
    """Unwrap an explicitly selected RFC 6901 root Tool value."""

    if isinstance(value, dict) and set(value) == {""}:
        return value[""]
    return value


def _updated_node_detail(
    current: JsonValue | None,
    current_omitted: bool,
    capture: CapturedValue | None,
    *,
    tool_content: bool = False,
) -> tuple[JsonValue | None, bool]:
    """Apply one capture envelope without confusing omission with JSON null."""

    if capture is None:
        return current, current_omitted
    if capture.disposition == "omitted":
        return None, True
    value = _tool_content_value(capture.value) if tool_content else capture.value
    return value, False


def _reasoning(
    facts: tuple[TraceSemanticFact, ...],
    *,
    fact_sequences: Mapping[int, int],
) -> tuple[TraceReasoning, ...]:
    """Fold authorized reasoning deltas without obscuring explicit omission.

    Reconciled facts replace the accumulated snapshot; ordinary facts append. An omitted
    value records only its disposition, and identity/extractor drift inside one stream
    is treated as Ledger corruption.
    """

    values: dict[str, _MutableReasoning] = {}
    order: list[str] = []
    for fact in facts:
        if not isinstance(fact, ReasoningFact):
            continue
        reasoning = values.get(fact.reasoning_id)
        if reasoning is None:
            reasoning = _MutableReasoning(
                id=fact.reasoning_id,
                trace_seq=fact_sequences[id(fact)],
                message_id=fact.message_id,
                namespace=fact.namespace,
                run_id=fact.identity.run_id,
                extractor=fact.extractor,
                created_at=fact.occurred_at,
            )
            values[fact.reasoning_id] = reasoning
            order.append(fact.reasoning_id)
        elif (
            reasoning.message_id != fact.message_id
            or reasoning.extractor != fact.extractor
        ):
            raise TraceCorruption("Reasoning identity changed inside one stream")
        if fact.content is not None:
            if fact.content.disposition == "omitted":
                reasoning.content_omitted = True
            elif fact.phase == "reconciled":
                reasoning.content = fact.content.value
            else:
                reasoning.content = _append_content(
                    reasoning.content,
                    fact.content.value,
                )
        if fact.phase == "completed":
            reasoning.completed_at = fact.occurred_at
    return tuple(
        TraceReasoning(
            id=reasoning.id,
            trace_seq=reasoning.trace_seq,
            message_id=reasoning.message_id,
            namespace=reasoning.namespace,
            run_id=reasoning.run_id,
            extractor=reasoning.extractor,
            content=reasoning.content,
            content_omitted=reasoning.content_omitted,
            status="completed" if reasoning.completed_at is not None else "streaming",
            created_at=reasoning.created_at,
            completed_at=reasoning.completed_at,
        )
        for reasoning_id in order
        if (reasoning := values.get(reasoning_id)) is not None
    )


def _append_content(
    current: JsonValue | None, delta: JsonValue | None
) -> JsonValue | None:
    if delta is None:
        return current
    if current is None:
        return delta
    if isinstance(current, str) and isinstance(delta, str):
        return current + delta
    if isinstance(current, list) and isinstance(delta, list):
        return [*current, *delta]
    return delta


def _interactions(
    facts: tuple[TraceSemanticFact, ...],
    *,
    fact_sequences: Mapping[int, int],
) -> tuple[TraceInteraction, ...]:
    """Fold opened and resolved interactions while retaining their first-open time."""

    values: dict[str, TraceInteraction] = {}
    order: list[str] = []
    for fact in facts:
        if not isinstance(fact, InteractionFact):
            continue
        previous = values.get(fact.interaction_id)
        if previous is None:
            order.append(fact.interaction_id)
        opened_at = previous.opened_at if previous is not None else fact.occurred_at
        payload = (
            fact.payload.value
            if fact.payload is not None and fact.payload.disposition == "inline"
            else (previous.payload if previous is not None else None)
        )
        values[fact.interaction_id] = TraceInteraction(
            id=fact.interaction_id,
            trace_seq=(
                fact_sequences[id(fact)] if previous is None else previous.trace_seq
            ),
            source_id=fact.source_interaction_id,
            namespace=fact.namespace,
            run_id=fact.identity.run_id,
            kind=fact.interaction_kind,
            tool_call_ids=(
                fact.tool_call_ids
                if fact.tool_call_ids
                else (() if previous is None else previous.tool_call_ids)
            ),
            status=fact.status,
            payload=payload,
            payload_omitted=(
                fact.payload is not None and fact.payload.disposition == "omitted"
            ),
            opened_at=opened_at,
            resolved_at=(fact.occurred_at if fact.phase == "resolved" else None),
        )
    return tuple(values[item] for item in order if item in values)


def _state(facts: tuple[TraceSemanticFact, ...]) -> TraceState:
    """Apply state and Plan revisions without mixing root and subgraph scopes."""

    root: dict[str, JsonValue] = {}
    subgraphs: dict[str, dict[str, JsonValue]] = {}
    for fact in facts:
        if isinstance(fact, PlanRevisionFact):
            target = (
                root
                if not fact.namespace
                else subgraphs.setdefault(_namespace_key(fact.namespace), {})
            )
            if fact.plan.disposition == "inline":
                target["tinkerfin_plan"] = fact.plan.value
            continue
        if not isinstance(fact, StateRevisionFact):
            continue
        target = (
            root
            if not fact.namespace
            else subgraphs.setdefault(_namespace_key(fact.namespace), {})
        )
        for key in fact.removed_keys:
            target.pop(key, None)
        if fact.changes.disposition == "inline" and isinstance(
            fact.changes.value, dict
        ):
            target.update(fact.changes.value)
    return TraceState(root=root, subgraphs=subgraphs)


def _namespace_key(namespace: tuple[str, ...]) -> str:
    return json.dumps(
        namespace,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _status(info: _RunInfo, active_run_ids: tuple[str, ...]) -> TraceStatus:
    # Runtime terminal facts are authoritative before the Store writer finishes its
    # final cleanup. A short-lived active fence must not hide an already committed
    # Agent outcome or leave followers waiting for a non-existent status-only event.
    if info.terminal == "succeeded":
        execution: NodeStatus = "succeeded"
    elif info.terminal == "interrupted":
        execution = "waiting"
    elif info.terminal == "failed":
        execution = "failed"
    elif info.terminal == "cancelled":
        execution = "cancelled"
    elif info.terminal == "abandoned":
        execution = "abandoned"
    elif info.run_id in active_run_ids:
        execution = "running"
    else:
        execution = "unknown"
    return TraceStatus(execution=execution, head_run_id=info.run_id)


def _status_with_pending(
    status: TraceStatus,
    pending_interactions: tuple[TraceInteraction, ...],
) -> TraceStatus:
    """Keep unresolved user input authoritative over non-failure Run progress.

    A Runtime can publish an interaction immediately before its terminal observation,
    and older durable evidence can contain a success terminal paired with the still-open
    interaction. Active execution remains running so a later root snapshot can resolve
    it. A success or missing-tail outcome waits instead, while failed, cancelled, and
    abandoned outcomes remain terminal.
    """

    if pending_interactions and status.execution in {"succeeded", "unknown"}:
        return status.model_copy(update={"execution": "waiting"})
    return status


def _tree(
    facts: tuple[TraceSemanticFact, ...],
    *,
    trace_sequences: tuple[int, ...],
    runs: dict[str, _RunInfo],
    run_turns: dict[str, str],
    visible_turns: set[str],
    visible_run_ids: set[str],
    active_run_ids: tuple[str, ...],
) -> TraceTree:
    """Build the selected execution tree from scoped Runtime, Tool, and Plan facts.

    Namespace-prefix ownership attaches nested work to the nearest verified subagent.
    Open nodes are reconciled against the Run terminal only after all facts are folded:
    interrupted work waits, failed work fails, and evidence missing a completion remains
    distinguishable from a fabricated success.
    """

    if len(facts) != len(trace_sequences):
        raise TraceCorruption("Tree fact sequences do not match structural facts")
    nodes: dict[str, TraceNode] = {}
    subagent_ids = {
        fact.namespace: fact.subagent_id
        for fact in facts
        if isinstance(fact, SubagentFact)
    }
    turn_runs: dict[str, list[_RunInfo]] = {}
    for run_id, info in runs.items():
        if run_id not in visible_run_ids:
            continue
        turn_id = run_turns.get(run_id)
        if turn_id not in visible_turns:
            continue
        turn_runs.setdefault(turn_id, []).append(info)
    for turn_id, infos in reversed(tuple(turn_runs.items())):
        first = infos[0]
        latest = infos[-1]
        nodes[turn_id] = TraceNode(
            id=turn_id,
            trace_seq=min(info.first_seq for info in infos),
            kind="turn",
            label="Turn",
            run_id=latest.run_id,
            status=_run_node_status(latest, active_run_ids),
            started_at=first.started_at,
            completed_at=latest.completed_at,
        )
    for run_id, info in runs.items():
        if run_id not in visible_run_ids:
            continue
        turn_id = run_turns.get(run_id)
        if turn_id not in visible_turns:
            continue
        node_id = f"run:{run_id}"
        nodes[node_id] = TraceNode(
            id=node_id,
            trace_seq=info.first_seq,
            parent_id=turn_id,
            kind="run",
            label=run_id,
            run_id=run_id,
            source_id=run_id,
            status=_run_node_status(info, active_run_ids),
            started_at=info.started_at,
            completed_at=info.completed_at,
        )
    for fact, trace_seq in zip(facts, trace_sequences, strict=True):
        run_parent = f"run:{fact.identity.run_id}"
        scoped_parent = _nearest_subagent_parent(
            fact.namespace,
            run_parent=run_parent,
            subagent_ids=subagent_ids,
        )
        if isinstance(fact, RuntimeTaskFact):
            node_id = fact.task_id
            previous = nodes.get(node_id)
            input_value, input_omitted = _updated_node_detail(
                None if previous is None else previous.input,
                False if previous is None else previous.input_omitted,
                fact.input,
            )
            result_value, result_omitted = _updated_node_detail(
                None if previous is None else previous.result,
                False if previous is None else previous.result_omitted,
                fact.result,
            )
            nodes[node_id] = TraceNode(
                id=node_id,
                trace_seq=previous.trace_seq if previous else trace_seq,
                parent_id=scoped_parent,
                kind="task",
                label=fact.task_name,
                run_id=fact.identity.run_id,
                namespace=fact.namespace,
                source_id=fact.source_task_id,
                input=input_value,
                input_omitted=input_omitted,
                result=result_value,
                result_omitted=result_omitted,
                status=(
                    "running"
                    if fact.phase == "started"
                    else (
                        "failed"
                        if fact.error_type
                        else ("waiting" if fact.interrupt_ids else "succeeded")
                    )
                ),
                started_at=(previous.started_at if previous else fact.occurred_at),
                completed_at=(
                    fact.occurred_at
                    if fact.phase == "completed" and not fact.interrupt_ids
                    else None
                ),
            )
        elif isinstance(fact, ToolFact):
            node_id = fact.tool_call_id
            previous = nodes.get(node_id)
            input_value, input_omitted = _updated_node_detail(
                None if previous is None else previous.input,
                False if previous is None else previous.input_omitted,
                fact.content if fact.phase == "arguments" else None,
                tool_content=True,
            )
            result_value, result_omitted = _updated_node_detail(
                None if previous is None else previous.result,
                False if previous is None else previous.result_omitted,
                fact.content if fact.phase == "result" else None,
                tool_content=True,
            )
            nodes[node_id] = TraceNode(
                id=node_id,
                trace_seq=previous.trace_seq if previous else trace_seq,
                parent_id=scoped_parent,
                kind="tool",
                label=fact.tool_name,
                run_id=fact.identity.run_id,
                namespace=fact.namespace,
                source_id=fact.source_tool_call_id,
                input=input_value,
                input_omitted=input_omitted,
                result=result_value,
                result_omitted=result_omitted,
                status=(
                    "running"
                    if fact.phase in {"started", "arguments", "completed"}
                    else ("failed" if fact.result_status == "error" else "succeeded")
                ),
                started_at=previous.started_at if previous else fact.occurred_at,
                completed_at=(fact.occurred_at if fact.phase == "result" else None),
            )
        elif isinstance(fact, SubagentFact):
            previous = nodes.get(fact.subagent_id)
            input_value, input_omitted = _updated_node_detail(
                None if previous is None else previous.input,
                False if previous is None else previous.input_omitted,
                fact.input,
                tool_content=True,
            )
            nodes[fact.subagent_id] = TraceNode(
                id=fact.subagent_id,
                trace_seq=previous.trace_seq if previous else trace_seq,
                parent_id=_nearest_subagent_parent(
                    fact.namespace,
                    run_parent=run_parent,
                    subagent_ids=subagent_ids,
                    strict=True,
                ),
                kind="subagent",
                label=fact.agent_name or "Subagent",
                run_id=fact.identity.run_id,
                namespace=fact.namespace,
                source_id=(
                    fact.parent_tool_call_id
                    or (None if previous is None else previous.source_id)
                ),
                input=input_value,
                input_omitted=input_omitted,
                status=_node_status(fact.status),
                started_at=previous.started_at if previous else fact.occurred_at,
                completed_at=(fact.occurred_at if fact.phase == "completed" else None),
            )
        elif isinstance(fact, PlanRevisionFact):
            nodes[fact.revision_id] = TraceNode(
                id=fact.revision_id,
                trace_seq=(
                    nodes[fact.revision_id].trace_seq
                    if fact.revision_id in nodes
                    else trace_seq
                ),
                parent_id=scoped_parent,
                kind="plan",
                label=("Plan" if fact.revision is None else f"Plan r{fact.revision}"),
                run_id=fact.identity.run_id,
                namespace=fact.namespace,
                status=_plan_status(fact.status),
                started_at=fact.occurred_at,
            )
    for node_id, node in tuple(nodes.items()):
        if node.status != "running" or node.kind not in {"task", "tool", "subagent"}:
            continue
        info = runs[node.run_id]
        if info.terminal is None:
            continue
        if node.kind == "subagent":
            status = _node_status(info.terminal)
            completed_at = info.completed_at
        else:
            status = _unfinished_work_status(info.terminal)
            completed_at = None
        nodes[node_id] = node.model_copy(
            update={"status": status, "completed_at": completed_at}
        )
    return TraceTree(nodes=tuple(nodes.values()))


def _nearest_subagent_parent(
    namespace: tuple[str, ...],
    *,
    run_parent: str,
    subagent_ids: Mapping[tuple[str, ...], str],
    strict: bool = False,
) -> str:
    matches = [
        (len(scope), subagent_id)
        for scope, subagent_id in subagent_ids.items()
        if len(scope) <= len(namespace)
        and (not strict or len(scope) < len(namespace))
        and namespace[: len(scope)] == scope
    ]
    return max(matches)[1] if matches else run_parent


def _plan_status(value: str | None) -> NodeStatus:
    if value in {"awaiting_clarification", "awaiting_review", "awaiting_input"}:
        return "waiting"
    if value == "approved":
        return "succeeded"
    if value == "cancelled":
        return "cancelled"
    if value == "planning":
        return "running"
    return "unknown"


def _unfinished_work_status(value: str | None) -> NodeStatus:
    if value == "interrupted":
        return "waiting"
    if value == "failed":
        return "failed"
    if value == "cancelled":
        return "cancelled"
    if value == "abandoned":
        return "abandoned"
    return "unknown"


def _node_status(value: str | None) -> NodeStatus:
    if value == "succeeded":
        return "succeeded"
    if value == "interrupted":
        return "waiting"
    if value == "failed":
        return "failed"
    if value == "cancelled":
        return "cancelled"
    if value == "abandoned":
        return "abandoned"
    if value == "running":
        return "running"
    return "unknown"


def _run_node_status(
    info: _RunInfo,
    active_run_ids: tuple[str, ...],
) -> NodeStatus:
    if info.terminal is not None:
        return _node_status(info.terminal)
    return "running" if info.run_id in active_run_ids else "unknown"


def _payload_omitted(facts: tuple[TraceSemanticFact, ...]) -> bool:
    for fact in facts:
        dumped = cast(object, fact.model_dump(mode="python"))
        if _contains_omitted(dumped):
            return True
    return False


def _contains_omitted(value: object) -> bool:
    if isinstance(value, dict):
        mapping = cast(Mapping[object, object], value)
        if mapping.get("disposition") == "omitted":
            return True
        return any(_contains_omitted(item) for item in mapping.values())
    if isinstance(value, list | tuple):
        return any(_contains_omitted(item) for item in cast(Sequence[object], value))
    return False


__all__ = ["CoreProjection", "TraceProjection", "evaluate_projection", "project_core"]
