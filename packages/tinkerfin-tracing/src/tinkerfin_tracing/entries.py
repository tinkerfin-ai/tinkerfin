"""Directly filterable execution entries and query values."""

from __future__ import annotations

from collections.abc import Iterable
from collections.abc import Set as AbstractSet
from datetime import UTC, datetime
from enum import StrEnum
from typing import cast

from pydantic import Field, JsonValue, field_validator, model_validator

from ._models import TraceModel
from .views import TraceMessage


class TraceEntryKind(StrEnum):
    """Functional kinds available to server-side Trace filtering."""

    AGENT = "agent"
    RUN = "run"
    MODEL = "model"
    PROVIDER = "provider"
    TOOLS = "tools"
    TOOL_PROPOSAL = "tool_proposal"
    TOOL = "tool"
    SUBAGENT = "subagent"
    SKILL = "skill"
    MIDDLEWARE = "middleware"
    MEMORY = "memory"
    GUARDRAIL = "guardrail"
    RETRIEVAL = "retrieval"
    CUSTOM = "custom"
    TASK = "task"


class TraceEntryStatus(StrEnum):
    """Current status of one queryable Trace entry."""

    CONFIGURED = "configured"
    RUNNING = "running"
    WAITING = "waiting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"
    UNKNOWN = "unknown"


class TraceFilter(TraceModel):
    """Select indexed Trace entries without scanning the semantic Ledger.

    Each repeatable text filter accepts at most 64 canonical values. Individual names
    and graph namespace segments are limited to 1024 characters so custom backends can
    translate every request to bounded database predicates.
    """

    kinds: AbstractSet[TraceEntryKind] = frozenset()
    statuses: AbstractSet[TraceEntryStatus] = frozenset()
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
        description="Literal case-insensitive substring matched against display metadata",
    )
    started_after: datetime | None = Field(
        default=None,
        description="Exclusive UTC lower bound for entry start time",
    )
    started_before: datetime | None = Field(
        default=None,
        description="Exclusive UTC upper bound for entry start time",
    )
    include_ancestors: bool = False

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
        """Accept ordinary collection literals and freeze them at the API boundary."""

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
            raise ValueError("Trace filters accept at most 64 values per field")
        if any(len(value) > 1024 for value in values):
            raise ValueError("Trace filter names accept at most 1024 characters")
        if any(not value or value != value.strip() for value in values):
            raise ValueError("Trace filter names must be canonical non-empty text")
        return values

    @field_validator("namespaces")
    @classmethod
    def namespaces_are_bounded(
        cls,
        values: AbstractSet[tuple[str, ...]],
    ) -> AbstractSet[tuple[str, ...]]:
        """Bound graph scopes and reject ambiguous namespace segments."""

        if len(values) > 64:
            raise ValueError("Trace filters accept at most 64 namespaces")
        if any(len(namespace) > 64 for namespace in values):
            raise ValueError("Trace filter namespaces accept at most 64 segments")
        if any(len(segment) > 1024 for namespace in values for segment in namespace):
            raise ValueError("Trace namespace segments accept at most 1024 characters")
        if any(
            not segment or segment != segment.strip()
            for namespace in values
            for segment in namespace
        ):
            raise ValueError("Trace namespace segments must be canonical text")
        return values

    @field_validator("started_after", "started_before")
    @classmethod
    def times_are_utc(cls, value: datetime | None) -> datetime | None:
        """Require comparable UTC filter boundaries."""

        if value is not None and (
            value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value)
        ):
            raise ValueError("Trace filter times must be aware UTC values")
        return value

    @model_validator(mode="after")
    def time_window_is_ordered(self) -> TraceFilter:
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


class TraceFailure(TraceModel):
    """One direct execution failure retained by its actual owning entry."""

    error_type: str = Field(min_length=1, max_length=1024)
    message: str | None = Field(default=None, min_length=1, max_length=4096)
    code: str | None = Field(default=None, min_length=1, max_length=1024)


class TraceTurn(TraceModel):
    """One selected-lineage user Turn resolved from existing Ledger evidence."""

    id: str = Field(min_length=1, max_length=2048)
    ordinal: int = Field(ge=1)
    started_at: datetime
    user_message: TraceMessage | None = None

    @field_validator("started_at")
    @classmethod
    def started_at_is_utc(cls, value: datetime) -> datetime:
        """Require a comparable source timestamp for Turn ordering."""

        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("Trace Turn time must be aware UTC")
        return value


class TraceEntry(TraceModel):
    """One current execution entry materialized from referenced Ledger facts."""

    id: str = Field(min_length=1, max_length=2048)
    turn_id: str = Field(min_length=1, max_length=2048)
    parent_id: str | None = Field(default=None, min_length=1, max_length=2048)
    proposal_id: str | None = Field(default=None, min_length=1, max_length=2048)
    kind: TraceEntryKind
    status: TraceEntryStatus
    name: str = Field(min_length=1, max_length=1024)
    run_id: str = Field(min_length=1, max_length=1024)
    namespace: tuple[str, ...] = ()
    agent_name: str | None = Field(default=None, min_length=1, max_length=1024)
    provider: str | None = Field(default=None, min_length=1, max_length=1024)
    model: str | None = Field(default=None, min_length=1, max_length=1024)
    source_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=2048,
        description="Original runtime or provider identifier retained for correlation",
    )
    started_at: datetime
    first_output_at: datetime | None = None
    completed_at: datetime | None = None
    started_seq: int = Field(
        ge=1,
        description="Ledger sequence containing authoritative entry request details",
    )
    updated_seq: int = Field(
        ge=1,
        description="Ledger sequence containing the current lifecycle update",
    )
    request: JsonValue | None = None
    request_omitted: bool = False
    result: JsonValue | None = None
    result_omitted: bool = False
    usage: JsonValue | None = None
    response_metadata: JsonValue | None = None
    failure: TraceFailure | None = None
    class_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=1024,
        description="Qualified middleware implementation class when visible",
    )
    hooks: tuple[str, ...] = ()
    source_path: str | None = Field(
        default=None,
        min_length=1,
        max_length=4096,
        description="Exact configured Skill instruction path that was read successfully",
    )

    @field_validator("started_at", "first_output_at", "completed_at")
    @classmethod
    def times_are_utc(cls, value: datetime | None) -> datetime | None:
        """Require source timestamps that can be compared across entry kinds."""

        if value is not None and (
            value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value)
        ):
            raise ValueError("Trace entry times must be aware UTC values")
        return value

    @model_validator(mode="after")
    def lifecycle_is_ordered(self) -> TraceEntry:
        """Require monotonic sequence and timestamp relationships."""

        if self.updated_seq < self.started_seq:
            raise ValueError("updated_seq cannot precede started_seq")
        if self.first_output_at is not None and self.first_output_at < self.started_at:
            raise ValueError("first output cannot precede entry start")
        if self.completed_at is not None and self.completed_at < self.started_at:
            raise ValueError("completion cannot precede entry start")
        return self


class TraceFacets(TraceModel):
    """Count each filter dimension after applying every other active dimension."""

    kinds: dict[TraceEntryKind, int] = Field(
        default_factory=lambda: dict[TraceEntryKind, int]()
    )
    statuses: dict[TraceEntryStatus, int] = Field(
        default_factory=lambda: dict[TraceEntryStatus, int]()
    )
    agents: dict[str, int] = Field(default_factory=dict)
    middleware: dict[str, int] = Field(default_factory=dict)
    skills: dict[str, int] = Field(default_factory=dict)
    providers: dict[str, int] = Field(default_factory=dict)
    models: dict[str, int] = Field(default_factory=dict)


class TraceEntryCompleteness(TraceModel):
    """Expose whether selected history predates Runtime call observation."""

    call_tracking_missing: bool = False
    execution_tree_missing: bool = False


class TraceEntryPage(TraceModel):
    """One stable indexed Trace entry page and its current Facets."""

    turns: tuple[TraceTurn, ...] = ()
    items: tuple[TraceEntry, ...]
    next_cursor: str | None = None
    as_of_seq: int = Field(ge=1)
    facets: TraceFacets
    completeness: TraceEntryCompleteness


class TraceEntryDelta(TraceModel):
    """Update one filtered result set after indexed Ledger commits."""

    as_of_seq: int = Field(ge=1)
    turn_upserts: tuple[TraceTurn, ...] = ()
    turn_removes: tuple[str, ...] = ()
    upserts: tuple[TraceEntry, ...] = ()
    removes: tuple[str, ...] = ()
    facets: TraceFacets
    completeness: TraceEntryCompleteness


__all__ = [
    "TraceEntry",
    "TraceEntryCompleteness",
    "TraceEntryDelta",
    "TraceEntryKind",
    "TraceEntryPage",
    "TraceEntryStatus",
    "TraceFacets",
    "TraceFailure",
    "TraceFilter",
    "TraceTurn",
]
