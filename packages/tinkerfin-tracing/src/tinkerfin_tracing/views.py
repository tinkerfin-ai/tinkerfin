"""Immutable public Trace views and live semantic update values."""

from __future__ import annotations

from datetime import datetime
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, Field, JsonValue

from ._models import TraceModel
from .facts import TraceEvent, TraceSemanticFact


class TraceMessage(TraceModel):
    """One projected conversation message in chronological order."""

    id: str
    trace_seq: int = Field(ge=1)
    source_id: str | None = None
    namespace: tuple[str, ...] = ()
    run_id: str
    role: Literal["user", "assistant", "tool", "system", "other"]
    content: JsonValue | None = None
    content_omitted: bool = False
    name: str | None = None
    tool_call_id: str | None = None
    status: Literal["streaming", "completed"]
    created_at: datetime
    completed_at: datetime | None = None


class TraceReasoning(TraceModel):
    """One explicitly enabled provider reasoning stream for a scoped message."""

    id: str
    trace_seq: int = Field(ge=1)
    message_id: str
    namespace: tuple[str, ...] = ()
    run_id: str
    extractor: str
    content: JsonValue | None = None
    content_omitted: bool = False
    status: Literal["streaming", "completed"]
    created_at: datetime
    completed_at: datetime | None = None


class TraceNode(TraceModel):
    """One stable flat execution-tree node."""

    id: str
    trace_seq: int = Field(ge=1)
    parent_id: str | None = None
    kind: Literal["turn", "run", "task", "tool", "subagent", "plan"]
    label: str
    run_id: str
    namespace: tuple[str, ...] = ()
    source_id: str | None = None
    status: Literal[
        "running",
        "waiting",
        "succeeded",
        "failed",
        "cancelled",
        "abandoned",
        "unknown",
    ]
    started_at: datetime
    completed_at: datetime | None = None


class TraceTree(TraceModel):
    """Expose convenience traversal over one authoritative flat node tuple."""

    nodes: tuple[TraceNode, ...] = ()

    @property
    def roots(self) -> tuple[TraceNode, ...]:
        """Return top-level Turn nodes in latest-first order."""

        return tuple(node for node in self.nodes if node.parent_id is None)

    def children(self, node_id: str) -> tuple[TraceNode, ...]:
        """Return direct children in their authoritative flat-node order."""

        return tuple(node for node in self.nodes if node.parent_id == node_id)


class TraceInteraction(TraceModel):
    """One approval, clarification, or review interaction.

    ``source_id`` preserves the canonical native interrupt identity. Protocol adapters
    may derive their documented public action IDs from it without forcing the Ledger
    to retain an AG-UI event copy.
    """

    id: str
    trace_seq: int = Field(ge=1)
    source_id: str
    namespace: tuple[str, ...] = ()
    run_id: str
    kind: str
    tool_call_ids: tuple[str, ...] = ()
    status: Literal["pending", "resolved", "cancelled"]
    payload: JsonValue | None = None
    payload_omitted: bool = False
    opened_at: datetime
    resolved_at: datetime | None = None


class TraceState(TraceModel):
    """Latest complete root state plus isolated subgraph state snapshots."""

    root: dict[str, JsonValue] = Field(default_factory=dict)
    subgraphs: dict[str, dict[str, JsonValue]] = Field(default_factory=dict)


class TraceStatus(TraceModel):
    """Current Agent execution status for the selected head lineage."""

    execution: Literal[
        "running",
        "waiting",
        "succeeded",
        "failed",
        "cancelled",
        "abandoned",
        "unknown",
    ]
    head_run_id: str


class TraceCompleteness(TraceModel):
    """Describe structural or capture gaps without overloading execution status."""

    missing_prefix: bool = False
    missing_tail: bool = False
    payload_omitted: bool = False


class TraceEventPage(TraceModel):
    """One fixed-as-of ascending page of safe Ledger events."""

    items: tuple[TraceEvent, ...]
    next_cursor: str | None = None
    page_as_of_seq: int


EntityT = TypeVar("EntityT", bound=BaseModel)


class TraceEntityDelta(TraceModel, Generic[EntityT]):
    """Upsert and remove immutable entities by their stable IDs."""

    upserts: tuple[EntityT, ...] = ()
    removes: tuple[str, ...] = ()


class TraceUpdate(TraceModel):
    """One live semantic update after a committed event batch."""

    as_of_seq: int
    events: tuple[TraceEvent, ...]
    facts: tuple[TraceSemanticFact, ...]
    messages: TraceEntityDelta[TraceMessage]
    reasoning: TraceEntityDelta[TraceReasoning]
    nodes: TraceEntityDelta[TraceNode]
    interactions: TraceEntityDelta[TraceInteraction]
    state: TraceState
    status: TraceStatus
    completeness: TraceCompleteness
    message_count: int = Field(ge=0)
    tool_call_count: int = Field(ge=0)
    projections: dict[str, JsonValue] = Field(default_factory=dict)


__all__ = [
    "TraceCompleteness",
    "TraceEntityDelta",
    "TraceEventPage",
    "TraceInteraction",
    "TraceMessage",
    "TraceNode",
    "TraceReasoning",
    "TraceState",
    "TraceStatus",
    "TraceTree",
    "TraceUpdate",
]
