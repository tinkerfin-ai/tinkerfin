"""Canonical execution-graph values exposed by TinkerFin Tracing."""

from __future__ import annotations

from collections.abc import Iterable
from collections.abc import Set as AbstractSet
from datetime import UTC, datetime
from enum import StrEnum
from typing import cast

from pydantic import Field, JsonValue, field_validator, model_validator

from ._models import TraceModel


class TraceGraphNodeKind(StrEnum):
    """Functional node kinds available to graph consumers and filters."""

    HUMAN_MESSAGE = "human_message"
    ASSISTANT_MESSAGE = "assistant_message"
    SYSTEM_MESSAGE = "system_message"
    AGENT = "agent"
    MODEL = "model"
    TOOL = "tool"
    SUBAGENT = "subagent"
    SKILL = "skill"
    MIDDLEWARE = "middleware"
    MEMORY = "memory"
    GUARDRAIL = "guardrail"
    RETRIEVAL = "retrieval"
    CUSTOM = "custom"
    PLAN = "plan"
    INTERACTION = "interaction"
    RUN = "run"
    RUNTIME_TASK = "runtime_task"


TECHNICAL_TRACE_GRAPH_NODE_KINDS = frozenset(
    {
        TraceGraphNodeKind.SYSTEM_MESSAGE,
        TraceGraphNodeKind.MIDDLEWARE,
        TraceGraphNodeKind.RUN,
        TraceGraphNodeKind.RUNTIME_TASK,
    }
)
"""Kinds hidden by the default product-facing graph query."""


class TraceGraphNodeStatus(StrEnum):
    """Current lifecycle status of one canonical graph node."""

    RUNNING = "running"
    WAITING = "waiting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"
    UNKNOWN = "unknown"


class TraceGraphLinkIssue(StrEnum):
    """Explicit missing evidence that prevented one exact graph relationship."""

    MISSING_PARENT = "missing_parent"
    MISSING_MODEL_OUTPUT = "missing_model_output"
    MISSING_TOOL_PROPOSAL = "missing_tool_proposal"
    MISSING_TOOL_EXECUTION = "missing_tool_execution"


class TraceGraphFilter(TraceModel):
    """Select graph nodes through bounded Store-side predicates.

    Technical nodes are excluded by default. Requested ancestors are returned only when
    they are visible under the same technical-node policy; the framework reconnects each
    visible node to its nearest visible ancestor.
    """

    kinds: AbstractSet[TraceGraphNodeKind] = frozenset()
    statuses: AbstractSet[TraceGraphNodeStatus] = frozenset()
    parent_id: str | None = Field(default=None, min_length=1, max_length=2048)
    agent_names: AbstractSet[str] = frozenset()
    middleware_names: AbstractSet[str] = frozenset()
    skill_names: AbstractSet[str] = frozenset()
    providers: AbstractSet[str] = frozenset()
    models: AbstractSet[str] = frozenset()
    namespaces: AbstractSet[tuple[str, ...]] = Field(
        default=frozenset(),
        description="Exact graph scopes; the empty tuple selects the root graph",
    )
    search: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        description=(
            "Literal substring across visible metadata and retained public details; "
            "ASCII queries fold only ASCII A-Z while queries containing non-ASCII "
            "characters are case-sensitive"
        ),
    )
    started_after: datetime | None = Field(
        default=None,
        description="Exclusive UTC lower bound for node start time",
    )
    started_before: datetime | None = Field(
        default=None,
        description="Exclusive UTC upper bound for node start time",
    )
    include_technical_nodes: bool = False
    include_ancestor_nodes: bool = True

    @field_validator(
        "kinds",
        "statuses",
        "agent_names",
        "middleware_names",
        "skill_names",
        "providers",
        "models",
        "namespaces",
        mode="before",
    )
    @classmethod
    def collections_are_frozen(cls, value: object) -> object:
        """Accept ordinary collection literals and freeze them at the boundary."""

        if isinstance(value, (set, frozenset, list, tuple)):
            return frozenset(cast(Iterable[object], value))
        return value

    @field_validator(
        "agent_names",
        "middleware_names",
        "skill_names",
        "providers",
        "models",
    )
    @classmethod
    def names_are_canonical(cls, values: AbstractSet[str]) -> AbstractSet[str]:
        """Reject oversized, blank, or whitespace-aliased filter values."""

        if len(values) > 64:
            raise ValueError("Trace Graph filters accept at most 64 values per field")
        if any(len(value) > 1024 for value in values):
            raise ValueError("Trace Graph filter names accept at most 1024 characters")
        if any(not value or value != value.strip() for value in values):
            raise ValueError("Trace Graph filter names must be canonical text")
        return values

    @field_validator("namespaces")
    @classmethod
    def namespaces_are_bounded(
        cls,
        values: AbstractSet[tuple[str, ...]],
    ) -> AbstractSet[tuple[str, ...]]:
        """Bound graph scopes and reject ambiguous namespace segments."""

        if len(values) > 64:
            raise ValueError("Trace Graph filters accept at most 64 namespaces")
        if any(len(namespace) > 64 for namespace in values):
            raise ValueError("Trace Graph namespaces accept at most 64 segments")
        if any(len(segment) > 1024 for value in values for segment in value):
            raise ValueError("Trace Graph namespace segments are too long")
        if any(
            not segment or segment != segment.strip()
            for value in values
            for segment in value
        ):
            raise ValueError("Trace Graph namespace segments must be canonical text")
        return values

    @field_validator("started_after", "started_before")
    @classmethod
    def times_are_utc(cls, value: datetime | None) -> datetime | None:
        """Require comparable UTC filter boundaries."""

        if value is not None and (
            value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value)
        ):
            raise ValueError("Trace Graph filter times must be aware UTC values")
        return value

    @model_validator(mode="after")
    def time_window_is_ordered(self) -> TraceGraphFilter:
        """Reject an empty or reversed time range."""

        if self.search is not None and self.search != self.search.strip():
            raise ValueError("search must be canonical text")
        if (
            self.started_after is not None
            and self.started_before is not None
            and self.started_after >= self.started_before
        ):
            raise ValueError("started_after must precede started_before")
        return self


class TraceGraphQueryLimits(TraceModel):
    """Bound direct matches, ancestor expansion, and serialized Graph pages."""

    max_direct_nodes: int = Field(default=1000, ge=1, le=10_000)
    max_total_nodes: int = Field(default=4000, ge=1, le=20_000)
    max_page_bytes: int = Field(default=8 * 1024 * 1024, ge=1024)

    @model_validator(mode="after")
    def direct_nodes_fit_total_nodes(self) -> TraceGraphQueryLimits:
        """Keep direct selection inside the complete page node budget."""

        if self.max_direct_nodes > self.max_total_nodes:
            raise ValueError("max_direct_nodes must not exceed max_total_nodes")
        return self


class TraceGraphFailure(TraceModel):
    """Failure retained only by the graph node that owns it."""

    error_type: str = Field(min_length=1, max_length=1024)
    message: str | None = Field(default=None, min_length=1, max_length=4096)
    code: str | None = Field(default=None, min_length=1, max_length=1024)


class TraceGraphTurn(TraceModel):
    """One selected-lineage user turn rooted at its HumanMessage node."""

    id: str = Field(min_length=1, max_length=2048)
    ordinal: int = Field(ge=1)
    root_node_id: str = Field(min_length=1, max_length=2048)
    started_at: datetime

    @field_validator("started_at")
    @classmethod
    def started_at_is_utc(cls, value: datetime) -> datetime:
        """Require a comparable source timestamp for Turn ordering."""

        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("Trace Graph Turn time must be aware UTC")
        return value


class TraceGraphNode(TraceModel):
    """One canonical graph node materialized from indexed Ledger locators."""

    id: str = Field(min_length=1, max_length=2048)
    turn_id: str = Field(min_length=1, max_length=2048)
    parent_id: str | None = Field(default=None, min_length=1, max_length=2048)
    structural_parent_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=2048,
        description="Unfiltered evidence parent before visible-ancestor projection",
    )
    kind: TraceGraphNodeKind
    status: TraceGraphNodeStatus
    name: str = Field(min_length=1, max_length=1024)
    run_id: str = Field(min_length=1, max_length=1024)
    namespace: tuple[str, ...] = ()
    agent_name: str | None = Field(default=None, min_length=1, max_length=1024)
    provider: str | None = Field(default=None, min_length=1, max_length=1024)
    model: str | None = Field(default=None, min_length=1, max_length=1024)
    source_id: str | None = Field(default=None, min_length=1, max_length=2048)
    started_at: datetime
    first_output_at: datetime | None = None
    completed_at: datetime | None = None
    started_seq: int = Field(ge=1)
    updated_seq: int = Field(ge=1)
    content: JsonValue | None = None
    content_omitted: bool = False
    request: JsonValue | None = None
    request_omitted: bool = False
    result: JsonValue | None = None
    result_omitted: bool = False
    usage: JsonValue | None = None
    response_metadata: JsonValue | None = None
    failure: TraceGraphFailure | None = None
    class_name: str | None = Field(default=None, min_length=1, max_length=1024)
    hooks: tuple[str, ...] = ()
    source_path: str | None = Field(default=None, min_length=1, max_length=4096)
    link_issues: tuple[TraceGraphLinkIssue, ...] = ()

    @field_validator("started_at", "first_output_at", "completed_at")
    @classmethod
    def times_are_utc(cls, value: datetime | None) -> datetime | None:
        """Require source timestamps that can be compared across node kinds."""

        if value is not None and (
            value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value)
        ):
            raise ValueError("Trace Graph node times must be aware UTC")
        return value

    @model_validator(mode="after")
    def lifecycle_is_ordered(self) -> TraceGraphNode:
        """Require monotonic sequence and timestamp relationships."""

        if self.updated_seq < self.started_seq:
            raise ValueError("updated_seq cannot precede started_seq")
        if self.first_output_at is not None and self.first_output_at < self.started_at:
            raise ValueError("first output cannot precede node start")
        if self.completed_at is not None and self.completed_at < self.started_at:
            raise ValueError("completion cannot precede node start")
        if len(set(self.link_issues)) != len(self.link_issues):
            raise ValueError("Trace Graph link issues must be unique")
        return self


class TraceGraphFacets(TraceModel):
    """Count each graph-filter dimension after all other dimensions apply."""

    kinds: dict[TraceGraphNodeKind, int] = Field(
        default_factory=lambda: cast(dict[TraceGraphNodeKind, int], {})
    )
    statuses: dict[TraceGraphNodeStatus, int] = Field(
        default_factory=lambda: cast(dict[TraceGraphNodeStatus, int], {})
    )
    agents: dict[str, int] = Field(default_factory=lambda: cast(dict[str, int], {}))
    middleware: dict[str, int] = Field(default_factory=lambda: cast(dict[str, int], {}))
    skills: dict[str, int] = Field(default_factory=lambda: cast(dict[str, int], {}))
    providers: dict[str, int] = Field(default_factory=lambda: cast(dict[str, int], {}))
    models: dict[str, int] = Field(default_factory=lambda: cast(dict[str, int], {}))


class TraceGraphCompleteness(TraceModel):
    """Expose missing call or relationship evidence without inventing nodes."""

    call_tracking_missing: bool = False
    relationship_evidence_missing: bool = False
    details_omitted: bool = False


class TraceGraph(TraceModel):
    """One authoritative ordered graph projection at an exact Ledger tail."""

    turns: tuple[TraceGraphTurn, ...] = ()
    nodes: tuple[TraceGraphNode, ...] = ()
    ordered_node_ids: tuple[str, ...] = ()
    root_node_ids: tuple[str, ...] = ()
    matched_node_ids: tuple[str, ...] = Field(
        description=(
            "Direct filter matches in authoritative order; returned ancestors are "
            "excluded"
        )
    )
    as_of_seq: int = Field(ge=1)
    facets: TraceGraphFacets = Field(default_factory=TraceGraphFacets)
    completeness: TraceGraphCompleteness = Field(default_factory=TraceGraphCompleteness)

    @model_validator(mode="after")
    def references_are_exact(self) -> TraceGraph:
        """Reject duplicate, missing, or non-authoritative ordering references."""

        node_ids = tuple(node.id for node in self.nodes)
        if len(set(node_ids)) != len(node_ids):
            raise ValueError("Trace Graph node IDs must be unique")
        if len(set(self.ordered_node_ids)) != len(self.ordered_node_ids):
            raise ValueError("Trace Graph ordered node IDs must be unique")
        if set(self.ordered_node_ids) != set(node_ids):
            raise ValueError(
                "Trace Graph ordering must contain every node exactly once"
            )
        if len(set(self.root_node_ids)) != len(self.root_node_ids):
            raise ValueError("Trace Graph root node IDs must be unique")
        if not set(self.root_node_ids) <= set(node_ids):
            raise ValueError("Trace Graph roots must reference returned nodes")
        if len(set(self.matched_node_ids)) != len(self.matched_node_ids):
            raise ValueError("Trace Graph matched node IDs must be unique")
        if not set(self.matched_node_ids) <= set(node_ids):
            raise ValueError("Trace Graph matches must reference returned nodes")
        expected_matches = tuple(
            node_id
            for node_id in self.ordered_node_ids
            if node_id in set(self.matched_node_ids)
        )
        if self.matched_node_ids != expected_matches:
            raise ValueError("Trace Graph matches must follow authoritative order")
        if any(
            node.parent_id is not None and node.parent_id not in set(node_ids)
            for node in self.nodes
        ):
            raise ValueError(
                "Trace Graph visible parents must reference returned nodes"
            )
        return self


class TraceGraphPage(TraceGraph):
    """One directly filtered graph page with an optional older-page cursor."""

    next_cursor: str | None = None


class TraceGraphDelta(TraceModel):
    """Replace changed graph values while retaining authoritative order and roots."""

    as_of_seq: int = Field(ge=1)
    next_cursor: str | None
    turn_upserts: tuple[TraceGraphTurn, ...] = ()
    turn_removes: tuple[str, ...] = ()
    node_upserts: tuple[TraceGraphNode, ...] = ()
    node_removes: tuple[str, ...] = ()
    ordered_node_ids: tuple[str, ...] = ()
    root_node_ids: tuple[str, ...] = ()
    matched_node_ids: tuple[str, ...] = Field(
        description="Complete current direct-match set in authoritative order"
    )
    facets: TraceGraphFacets
    completeness: TraceGraphCompleteness

    @model_validator(mode="after")
    def references_follow_authoritative_order(self) -> TraceGraphDelta:
        """Reject duplicate or out-of-order root and match references."""

        ordered_ids = set(self.ordered_node_ids)
        if len(ordered_ids) != len(self.ordered_node_ids):
            raise ValueError("Trace Graph Delta ordered node IDs must be unique")
        if len(set(self.root_node_ids)) != len(self.root_node_ids):
            raise ValueError("Trace Graph Delta root node IDs must be unique")
        if not set(self.root_node_ids) <= ordered_ids:
            raise ValueError("Trace Graph Delta roots must reference ordered nodes")
        if len(set(self.matched_node_ids)) != len(self.matched_node_ids):
            raise ValueError("Trace Graph Delta matched node IDs must be unique")
        if not set(self.matched_node_ids) <= ordered_ids:
            raise ValueError("Trace Graph Delta matches must reference ordered nodes")
        expected_matches = tuple(
            node_id
            for node_id in self.ordered_node_ids
            if node_id in set(self.matched_node_ids)
        )
        if self.matched_node_ids != expected_matches:
            raise ValueError(
                "Trace Graph Delta matches must follow authoritative order"
            )
        return self


def _omit_graph_node_details(node: TraceGraphNode) -> TraceGraphNode:
    return node.model_copy(
        update={
            "content": None,
            "content_omitted": node.content_omitted or node.content is not None,
            "request": None,
            "request_omitted": node.request_omitted or node.request is not None,
            "result": None,
            "result_omitted": node.result_omitted or node.result is not None,
            "usage": None,
            "response_metadata": None,
        },
        deep=True,
    )


def _graph_json_bytes(value: TraceModel) -> int:
    return len(value.model_dump_json(by_alias=True, exclude_none=False).encode())


def bound_graph_page(
    page: TraceGraphPage,
    *,
    max_bytes: int,
) -> TraceGraphPage:
    """Prefer complete structure and explicitly omit details before rejecting a page."""

    if _graph_json_bytes(page) <= max_bytes:
        return page
    bounded = page.model_copy(
        update={
            "nodes": tuple(_omit_graph_node_details(node) for node in page.nodes),
            "completeness": page.completeness.model_copy(
                update={"details_omitted": True}
            ),
        },
        deep=True,
    )
    if _graph_json_bytes(bounded) > max_bytes:
        from .errors import TraceQuotaExceeded

        raise TraceQuotaExceeded(
            "Trace Graph page exceeds max_page_bytes",
            context={"resource": "graph_page_bytes"},
        )
    return bounded


def bound_graph(
    graph: TraceGraph,
    *,
    max_bytes: int,
) -> TraceGraph:
    """Prefer complete structure and omit details before rejecting a Graph."""

    if _graph_json_bytes(graph) <= max_bytes:
        return graph
    bounded = graph.model_copy(
        update={
            "nodes": tuple(_omit_graph_node_details(node) for node in graph.nodes),
            "completeness": graph.completeness.model_copy(
                update={"details_omitted": True}
            ),
        },
        deep=True,
    )
    if _graph_json_bytes(bounded) > max_bytes:
        from .errors import TraceQuotaExceeded

        raise TraceQuotaExceeded(
            "Trace Graph exceeds max_page_bytes",
            context={"resource": "graph_page_bytes"},
        )
    return bounded


def _bound_graph_delta(
    delta: TraceGraphDelta,
    *,
    max_bytes: int,
) -> TraceGraphDelta:
    """Apply the same deterministic detail omission to live Graph updates."""

    if _graph_json_bytes(delta) <= max_bytes:
        return delta
    bounded = delta.model_copy(
        update={
            "node_upserts": tuple(
                _omit_graph_node_details(node) for node in delta.node_upserts
            ),
            "completeness": delta.completeness.model_copy(
                update={"details_omitted": True}
            ),
        },
        deep=True,
    )
    if _graph_json_bytes(bounded) > max_bytes:
        from .errors import TraceQuotaExceeded

        raise TraceQuotaExceeded(
            "Trace Graph update exceeds max_page_bytes",
            context={"resource": "graph_page_bytes"},
        )
    return bounded


def graph_delta(
    previous: TraceGraph,
    current: TraceGraph,
    *,
    max_bytes: int,
) -> TraceGraphDelta:
    """Return the bounded canonical replacement delta between two Graphs."""

    previous_nodes = {node.id: node for node in previous.nodes}
    current_nodes = {node.id: node for node in current.nodes}
    previous_turns = {turn.id: turn for turn in previous.turns}
    current_turns = {turn.id: turn for turn in current.turns}
    return _bound_graph_delta(
        TraceGraphDelta(
            as_of_seq=current.as_of_seq,
            next_cursor=(
                current.next_cursor if isinstance(current, TraceGraphPage) else None
            ),
            turn_upserts=tuple(
                turn
                for turn_id, turn in current_turns.items()
                if previous_turns.get(turn_id) != turn
            ),
            turn_removes=tuple(
                turn_id for turn_id in previous_turns if turn_id not in current_turns
            ),
            node_upserts=tuple(
                node
                for node_id, node in current_nodes.items()
                if previous_nodes.get(node_id) != node
            ),
            node_removes=tuple(
                node_id for node_id in previous_nodes if node_id not in current_nodes
            ),
            ordered_node_ids=current.ordered_node_ids,
            root_node_ids=current.root_node_ids,
            matched_node_ids=current.matched_node_ids,
            facets=current.facets,
            completeness=current.completeness,
        ),
        max_bytes=max_bytes,
    )


__all__ = [
    "TECHNICAL_TRACE_GRAPH_NODE_KINDS",
    "TraceGraph",
    "TraceGraphCompleteness",
    "TraceGraphDelta",
    "TraceGraphFacets",
    "TraceGraphFailure",
    "TraceGraphFilter",
    "TraceGraphLinkIssue",
    "TraceGraphNode",
    "TraceGraphNodeKind",
    "TraceGraphNodeStatus",
    "TraceGraphPage",
    "TraceGraphQueryLimits",
    "TraceGraphTurn",
]
