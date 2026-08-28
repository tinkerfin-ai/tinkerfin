"""Current semantic facts stored in the Trace Ledger."""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import Field, TypeAdapter, model_validator

from tinkerfin_contracts import RunIdentity, RunInputKind, RunTerminalOutcome

from ._models import TimedTraceModel, TraceModel
from .capture import CapturedValue


class TraceFactBase(TimedTraceModel):
    """Attach stable source identity and graph scope to one semantic fact."""

    source_observation_id: str = Field(min_length=1, max_length=1024)
    identity: RunIdentity
    namespace: tuple[str, ...] = ()


class TurnFact(TraceFactBase):
    """Start one user task that can span multiple resumed Run segments."""

    kind: Literal["turn.started"] = "turn.started"
    turn_id: str = Field(min_length=1, max_length=2048)
    user_message_id: str | None = Field(default=None, min_length=1, max_length=1024)
    parent_run_id: str | None = Field(default=None, min_length=1, max_length=1024)


class RunFact(TraceFactBase):
    """Describe one Runtime lifecycle phase without transport state."""

    kind: Literal["run"] = "run"
    phase: Literal[
        "started",
        "input",
        "resumed",
        "resume_checkpointed",
        "observer_failed",
        "terminal",
        "closed",
    ]
    input_kind: RunInputKind | None = None
    parent_run_id: str | None = Field(default=None, min_length=1, max_length=1024)
    outcome: RunTerminalOutcome | None = None
    code: str | None = Field(default=None, min_length=1, max_length=1024)
    error_type: str | None = Field(default=None, min_length=1, max_length=1024)
    observer_name: str | None = Field(default=None, min_length=1, max_length=1024)
    interrupt_ids: tuple[str, ...] = ()
    input: CapturedValue | None = None
    config: CapturedValue | None = None

    @model_validator(mode="after")
    def phase_fields_are_consistent(self) -> RunFact:
        """Require the evidence needed to interpret each lifecycle phase."""

        if self.phase == "started" and self.input_kind is None:
            raise ValueError("started Run facts require input_kind")
        if self.phase == "input" and self.input_kind not in {"ordinary", "branch"}:
            raise ValueError("input Run facts require ordinary or branch input_kind")
        if self.phase == "resumed" and self.input_kind not in {"resume", "abandon"}:
            raise ValueError("resumed Run facts require resume or abandon input_kind")
        if self.phase in {"input", "resumed"} and (
            self.input is None or self.config is None
        ):
            raise ValueError("Run input facts require input and config capture")
        if self.input_kind == "branch" and self.parent_run_id is None:
            raise ValueError("branch Run facts require parent_run_id")
        if self.phase == "resume_checkpointed" and not self.interrupt_ids:
            raise ValueError("resume checkpoint facts require interrupt_ids")
        if self.interrupt_ids and self.phase not in {"resumed", "resume_checkpointed"}:
            raise ValueError(
                "interrupt_ids belong only to resumed and resume checkpoint facts"
            )
        if self.phase == "observer_failed" and (
            self.observer_name is None or self.error_type is None
        ):
            raise ValueError(
                "observer failure facts require observer_name and error_type"
            )
        if self.phase in {"terminal", "closed"} and self.outcome is None:
            raise ValueError("terminal and closed Run facts require outcome")
        return self


class MessageFact(TraceFactBase):
    """Append, reconcile, complete, or remove one scoped conversation message."""

    kind: Literal["message"] = "message"
    phase: Literal["started", "content", "completed", "reconciled", "removed"]
    message_id: str = Field(min_length=1, max_length=2048)
    source_message_id: str | None = Field(default=None, min_length=1, max_length=1024)
    role: Literal["user", "assistant", "tool", "system", "other"]
    content: CapturedValue | None = None
    fingerprint: str | None = Field(default=None, min_length=64, max_length=64)
    name: str | None = Field(default=None, min_length=1, max_length=1024)
    tool_call_id: str | None = Field(default=None, min_length=1, max_length=1024)

    @model_validator(mode="after")
    def content_phase_is_explicit(self) -> MessageFact:
        """Require content ownership or an explicit Tool-result reference."""

        tool_result_reference = (
            self.phase == "reconciled"
            and self.role == "tool"
            and self.tool_call_id is not None
        )
        if (
            self.phase in {"content", "reconciled"}
            and self.content is None
            and not tool_result_reference
        ):
            raise ValueError("message content facts require a capture envelope")
        return self


class ReasoningFact(TraceFactBase):
    """Append, reconcile, or complete explicitly enabled provider reasoning."""

    kind: Literal["reasoning"] = "reasoning"
    phase: Literal["content", "reconciled", "completed"]
    reasoning_id: str = Field(min_length=1, max_length=2048)
    message_id: str = Field(min_length=1, max_length=2048)
    source_message_id: str = Field(min_length=1, max_length=1024)
    extractor: str = Field(min_length=1, max_length=1024)
    content: CapturedValue | None = None

    @model_validator(mode="after")
    def content_phase_is_explicit(self) -> ReasoningFact:
        """Require retained or explicitly omitted content for content phases."""

        if self.phase in {"content", "reconciled"} and self.content is None:
            raise ValueError("reasoning content phases require a capture envelope")
        if self.phase == "completed" and self.content is not None:
            raise ValueError("completed reasoning facts cannot repeat content")
        return self


class ToolFact(TraceFactBase):
    """Describe one scoped Tool proposal, argument stream, end, or result."""

    kind: Literal["tool"] = "tool"
    phase: Literal["started", "arguments", "completed", "result"]
    tool_call_id: str = Field(min_length=1, max_length=2048)
    source_tool_call_id: str = Field(min_length=1, max_length=1024)
    tool_name: str = Field(min_length=1, max_length=1024)
    content: CapturedValue | None = None
    result_status: Literal["success", "error"] | None = None

    @model_validator(mode="after")
    def result_phase_is_explicit(self) -> ToolFact:
        """Keep Tool execution results distinct from proposal lifecycle facts."""

        if self.phase == "result" and (
            self.content is None or self.result_status is None
        ):
            raise ValueError("Tool result facts require content and result_status")
        if self.phase != "result" and self.result_status is not None:
            raise ValueError("Only Tool result facts can set result_status")
        return self


class RuntimeTaskFact(TraceFactBase):
    """Describe one scoped LangGraph runtime task phase."""

    kind: Literal["task"] = "task"
    phase: Literal["started", "completed"]
    task_id: str = Field(min_length=1, max_length=2048)
    source_task_id: str = Field(min_length=1, max_length=1024)
    task_name: str = Field(min_length=1, max_length=1024)
    triggers: tuple[str, ...] = ()
    input: CapturedValue | None = None
    result: CapturedValue | None = None
    error_type: str | None = Field(default=None, min_length=1, max_length=1024)
    interrupt_ids: tuple[str, ...] = ()


class StateRevisionFact(TraceFactBase):
    """Store changed top-level state keys without duplicating message snapshots."""

    kind: Literal["state.revision"] = "state.revision"
    revision_id: str = Field(min_length=1, max_length=2048)
    changes: CapturedValue
    removed_keys: tuple[str, ...] = ()


class InteractionFact(TraceFactBase):
    """Open or resolve one native approval, clarification, or review interaction."""

    kind: Literal["interaction"] = "interaction"
    phase: Literal["opened", "resolved"]
    interaction_id: str = Field(min_length=1, max_length=2048)
    source_interaction_id: str = Field(min_length=1, max_length=1024)
    interaction_kind: str = Field(min_length=1, max_length=1024)
    tool_call_ids: tuple[str, ...] = Field(
        default=(),
        description="Exact Native Tool calls correlated to review actions by position",
    )
    status: Literal["pending", "resolved", "cancelled"]
    payload: CapturedValue | None = None

    @model_validator(mode="after")
    def status_matches_phase(self) -> InteractionFact:
        """Enforce the pending-to-resolved interaction state machine."""

        if self.phase == "opened" and self.status != "pending":
            raise ValueError("opened interactions must be pending")
        if self.phase == "resolved" and self.status == "pending":
            raise ValueError("resolved interactions cannot remain pending")
        if any(not value or value != value.strip() for value in self.tool_call_ids):
            raise ValueError("interaction Tool call IDs must be canonical strings")
        if len(set(self.tool_call_ids)) != len(self.tool_call_ids):
            raise ValueError("interaction Tool call IDs must be unique")
        return self


class SubagentFact(TraceFactBase):
    """Open or close one validated non-root Agent graph scope."""

    kind: Literal["subagent"] = "subagent"
    phase: Literal["started", "completed"]
    subagent_id: str = Field(min_length=1, max_length=2048)
    agent_name: str | None = Field(default=None, min_length=1, max_length=1024)
    status: Literal["running", "succeeded", "failed", "cancelled"]


class PlanRevisionFact(TraceFactBase):
    """Record one public TinkerFin Plan state revision."""

    kind: Literal["plan.revision"] = "plan.revision"
    revision_id: str = Field(min_length=1, max_length=2048)
    revision: int | None = Field(default=None, ge=0)
    status: str | None = Field(default=None, min_length=1, max_length=1024)
    plan: CapturedValue


class NativeExtraFact(TraceFactBase):
    """Retain structural metadata for an allowed extra Native stream mode."""

    kind: Literal["native.extra"] = "native.extra"
    mode: Literal["updates", "checkpoints", "debug", "custom"]
    data_type: str = Field(min_length=1, max_length=1024)
    top_level_keys: tuple[str, ...] = ()


TraceSemanticFact: TypeAlias = Annotated[
    TurnFact
    | RunFact
    | MessageFact
    | ReasoningFact
    | ToolFact
    | RuntimeTaskFact
    | StateRevisionFact
    | InteractionFact
    | SubagentFact
    | PlanRevisionFact
    | NativeExtraFact,
    Field(discriminator="kind"),
]

TRACE_FACT_ADAPTER: TypeAdapter[TraceSemanticFact] = TypeAdapter(TraceSemanticFact)


class TraceEvent(TraceModel):
    """Wrap one committed semantic fact with generation and sequence identity."""

    event_id: str = Field(min_length=1, max_length=1024)
    trace_seq: int = Field(ge=1)
    generation: str = Field(min_length=1, max_length=1024)
    fact: TraceSemanticFact
    persisted_bytes: int = Field(ge=1)


__all__ = [
    "TRACE_FACT_ADAPTER",
    "InteractionFact",
    "MessageFact",
    "NativeExtraFact",
    "PlanRevisionFact",
    "ReasoningFact",
    "RunFact",
    "RuntimeTaskFact",
    "StateRevisionFact",
    "SubagentFact",
    "ToolFact",
    "TraceEvent",
    "TraceFactBase",
    "TraceSemanticFact",
    "TurnFact",
]
