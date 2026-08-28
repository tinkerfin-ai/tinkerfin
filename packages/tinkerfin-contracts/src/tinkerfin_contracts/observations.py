"""Validated Runtime and Native observations consumed by framework observers."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, TypeAlias

from pydantic import Field, JsonValue, TypeAdapter, model_validator

from ._models import ContractModel, ObservationModel
from .identity import RunIdentity

RunInputKind: TypeAlias = Literal["ordinary", "branch", "resume", "abandon"]
RunMode: TypeAlias = Literal["default", "plan"]
RunTerminalOutcome: TypeAlias = Literal[
    "succeeded",
    "interrupted",
    "failed",
    "cancelled",
    "abandoned",
]
NativeExtraMode: TypeAlias = Literal["updates", "checkpoints", "debug", "custom"]
NativeMessageType: TypeAlias = Literal[
    "human",
    "assistant",
    "assistant_chunk",
    "tool",
    "system",
    "chat",
    "remove",
    "other",
]


class ObservationBoundary(StrEnum):
    """Durability boundaries that a managed observation session must settle."""

    RESUME_CHECKPOINTED = "resume_checkpointed"
    INTERRUPT = "interrupt"
    TERMINAL = "terminal"
    CLOSE = "close"


class RunResumeSummary(ContractModel):
    """Describe one public interaction decision without transport-specific models."""

    interrupt_id: str = Field(min_length=1, max_length=1024)
    status: Literal["resolved", "cancelled"]
    decision: Literal["approve", "edit", "reject", "respond"] | None = None

    @model_validator(mode="after")
    def cancellation_has_no_fabricated_decision(self) -> RunResumeSummary:
        """Keep abandonment distinct from a resolved rejection decision."""

        if self.status == "cancelled" and self.decision is not None:
            raise ValueError("cancelled interactions cannot contain a decision")
        return self


class RunSourceContext(ContractModel):
    """Describe the real input and lineage used to open one Runtime request.

    Input and configuration values are finite JSON snapshots produced by the Runtime.
    Observer implementations must treat them as borrowed immutable evidence and apply
    their own retention policy before storing content.
    """

    identity: RunIdentity
    runtime_profile: str = Field(min_length=1, max_length=1024)
    input_kind: RunInputKind
    parent_run_id: str | None = Field(default=None, min_length=1, max_length=1024)
    mode: RunMode = "default"
    input: JsonValue
    config: JsonValue
    resume: tuple[RunResumeSummary, ...] = ()
    private_state_keys: tuple[str, ...] = ()

    @model_validator(mode="after")
    def identifiers_and_collections_are_canonical(self) -> RunSourceContext:
        """Reject an ambiguous Profile, resume group, or private state set."""

        if self.runtime_profile != self.runtime_profile.strip():
            raise ValueError("runtime_profile must be canonical text")
        resume_ids = tuple(item.interrupt_id for item in self.resume)
        if len(set(resume_ids)) != len(resume_ids):
            raise ValueError("resume interrupt IDs must be unique")
        if len(set(self.private_state_keys)) != len(self.private_state_keys):
            raise ValueError("private state keys must be unique")
        return self


class NativeToolCall(ContractModel):
    """Describe one complete Tool proposal carried by a Native message."""

    id: str = Field(min_length=1, max_length=1024)
    name: str = Field(min_length=1, max_length=1024)
    arguments: dict[str, JsonValue]


class NativeToolCallChunk(ContractModel):
    """Describe one Tool argument fragment in its provider-defined slot."""

    index: int = Field(ge=0)
    id: str | None = Field(default=None, min_length=1, max_length=1024)
    name: str | None = Field(default=None, min_length=1, max_length=1024)
    arguments: str = ""


class NativeMessageRecord(ContractModel):
    """Represent a LangChain message without exposing its concrete Python class."""

    message_type: NativeMessageType
    id: str | None = Field(default=None, min_length=1, max_length=1024)
    name: str | None = Field(default=None, min_length=1, max_length=1024)
    content: JsonValue
    tool_calls: tuple[NativeToolCall, ...] = ()
    tool_call_chunks: tuple[NativeToolCallChunk, ...] = ()
    tool_call_id: str | None = Field(default=None, min_length=1, max_length=1024)
    tool_status: Literal["success", "error"] | None = None
    response_metadata: dict[str, JsonValue] = Field(default_factory=dict)
    usage_metadata: dict[str, JsonValue] | None = None


class NativeInterruptRecord(ContractModel):
    """Preserve one native interrupt ID and its finite public value."""

    id: str = Field(min_length=1, max_length=1024)
    value: JsonValue


class RunStartedObservation(ObservationModel):
    """Record admission of one Runtime request after coordination succeeds."""

    kind: Literal["run.started"] = "run.started"
    identity: RunIdentity


class RunInputObservation(ObservationModel):
    """Record the real ordinary, branch, resume, or abandonment input."""

    kind: Literal["run.input"] = "run.input"
    identity: RunIdentity
    source: RunSourceContext

    @model_validator(mode="after")
    def source_identity_matches_observation(self) -> RunInputObservation:
        """Prevent one observation from binding input owned by another Run."""

        if self.identity != self.source.identity:
            raise ValueError("Run input source identity must match the observation")
        return self


class RunResumeCheckpointedObservation(ObservationModel):
    """Record durable resume evidence before continuation output."""

    kind: Literal["run.resume_checkpointed"] = "run.resume_checkpointed"
    identity: RunIdentity
    marker_id: str = Field(min_length=1, max_length=1024)
    native_interrupt_ids: tuple[str, ...] = Field(min_length=1)


class RunObserverFailedObservation(ObservationModel):
    """Notify healthy observers that another observer terminated the Runtime."""

    kind: Literal["run.observer_failed"] = "run.observer_failed"
    identity: RunIdentity
    observer_name: str = Field(min_length=1, max_length=1024)
    error_type: str = Field(min_length=1, max_length=1024)


class RunTerminalObservation(ObservationModel):
    """Record the exactly-once semantic terminal selected by the Runtime."""

    kind: Literal["run.terminal"] = "run.terminal"
    identity: RunIdentity
    outcome: RunTerminalOutcome
    code: str | None = Field(default=None, min_length=1, max_length=1024)
    error_type: str | None = Field(default=None, min_length=1, max_length=1024)
    interrupt_ids: tuple[str, ...] = ()


class RunClosedObservation(ObservationModel):
    """Record completion of Runtime-owned cleanup after the terminal boundary."""

    kind: Literal["run.closed"] = "run.closed"
    identity: RunIdentity
    outcome: RunTerminalOutcome


class NativeMessageObservation(ObservationModel):
    """Record one validated Native message part with complete graph scope."""

    kind: Literal["native.message"] = "native.message"
    identity: RunIdentity
    namespace: tuple[str, ...]
    message: NativeMessageRecord
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class NativeReasoningObservation(ObservationModel):
    """Record explicitly enabled provider reasoning for one scoped message.

    The Runtime emits this observation only when a caller injects a verified
    provider extractor. Observers must apply an independent retention policy;
    enabling live reasoning does not by itself authorize persistence.
    """

    kind: Literal["native.reasoning"] = "native.reasoning"
    identity: RunIdentity
    namespace: tuple[str, ...]
    message_id: str = Field(min_length=1, max_length=1024)
    extractor: str = Field(min_length=1, max_length=1024)
    content: JsonValue
    snapshot: bool = Field(
        description="Whether content is a complete message snapshot rather than a delta"
    )


class NativeTaskObservation(ObservationModel):
    """Record one validated LangGraph runtime task phase."""

    kind: Literal["native.task"] = "native.task"
    identity: RunIdentity
    namespace: tuple[str, ...]
    phase: Literal["start", "result"]
    task_id: str = Field(min_length=1, max_length=1024)
    name: str = Field(min_length=1, max_length=1024)
    triggers: tuple[str, ...] = ()
    input: JsonValue | None = None
    result: JsonValue | None = None
    error_type: str | None = Field(default=None, min_length=1, max_length=1024)
    interrupts: tuple[NativeInterruptRecord, ...] = ()
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class NativeStateObservation(ObservationModel):
    """Record one validated root or subgraph state snapshot and interrupts."""

    kind: Literal["native.state"] = "native.state"
    identity: RunIdentity
    namespace: tuple[str, ...]
    state: dict[str, JsonValue]
    messages: tuple[NativeMessageRecord, ...] = ()
    interrupts: tuple[NativeInterruptRecord, ...] = ()


class NativeExtraObservation(ObservationModel):
    """Record only safe structural metadata for a declared extra stream mode."""

    kind: Literal["native.extra"] = "native.extra"
    identity: RunIdentity
    namespace: tuple[str, ...]
    mode: NativeExtraMode
    data_type: str = Field(min_length=1, max_length=1024)
    safe_size_bytes: int = Field(ge=0)
    top_level_keys: tuple[str, ...] = ()


NativeObservation: TypeAlias = (
    NativeMessageObservation
    | NativeReasoningObservation
    | NativeTaskObservation
    | NativeStateObservation
    | NativeExtraObservation
)


RuntimeObservation: TypeAlias = Annotated[
    RunStartedObservation
    | RunInputObservation
    | RunResumeCheckpointedObservation
    | RunObserverFailedObservation
    | RunTerminalObservation
    | RunClosedObservation
    | NativeMessageObservation
    | NativeReasoningObservation
    | NativeTaskObservation
    | NativeStateObservation
    | NativeExtraObservation,
    Field(discriminator="kind"),
]

RUNTIME_OBSERVATION_ADAPTER: TypeAdapter[RuntimeObservation] = TypeAdapter(
    RuntimeObservation
)


__all__ = [
    "RUNTIME_OBSERVATION_ADAPTER",
    "NativeExtraMode",
    "NativeExtraObservation",
    "NativeInterruptRecord",
    "NativeMessageObservation",
    "NativeMessageRecord",
    "NativeMessageType",
    "NativeObservation",
    "NativeReasoningObservation",
    "NativeStateObservation",
    "NativeTaskObservation",
    "NativeToolCall",
    "NativeToolCallChunk",
    "ObservationBoundary",
    "RunClosedObservation",
    "RunInputKind",
    "RunInputObservation",
    "RunMode",
    "RunObserverFailedObservation",
    "RunResumeCheckpointedObservation",
    "RunResumeSummary",
    "RunSourceContext",
    "RunStartedObservation",
    "RunTerminalObservation",
    "RunTerminalOutcome",
    "RuntimeObservation",
]
