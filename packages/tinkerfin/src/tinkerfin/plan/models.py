"""Stable Plan Mode values stored in checkpoints and exposed through AG-UI state."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    model_validator,
)
from pydantic.alias_generators import to_camel

NonBlankText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
PlanStepId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    ),
]
PlanDigest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class PlanStatus(StrEnum):
    """Observable phase of the standalone Planning workflow."""

    PLANNING = "planning"
    AWAITING_CLARIFICATION = "awaiting_clarification"
    AWAITING_REVIEW = "awaiting_review"
    APPROVED = "approved"
    CANCELLED = "cancelled"


class PlanReviewAction(StrEnum):
    """Resolved user action for the current Plan draft."""

    APPROVE = "approve"
    EDIT = "edit"
    RESPOND = "respond"
    REJECT = "reject"


class _PlanModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )


class PlanStep(_PlanModel):
    """One ordered, independently verifiable step in a user-reviewed Plan."""

    id: PlanStepId = Field(description="Stable step ID within one Plan revision")
    title: NonBlankText = Field(description="Concise step title")
    description: NonBlankText = Field(
        description="Implementation intent and boundary for this step"
    )
    verification: tuple[NonBlankText, ...] = Field(
        min_length=1,
        description="Observable checks that prove this step is complete",
    )


class PlanContent(_PlanModel):
    """Complete Plan content before the workflow assigns a revision."""

    goal: NonBlankText = Field(description="Operational goal of the Plan")
    assumptions: tuple[NonBlankText, ...] = Field(
        default=(),
        description="Assumptions that materially constrain execution",
    )
    steps: tuple[PlanStep, ...] = Field(
        min_length=1,
        description="Ordered implementation steps",
    )
    acceptance_criteria: tuple[NonBlankText, ...] = Field(
        min_length=1,
        description="Final observable conditions for accepting the result",
    )

    @model_validator(mode="after")
    def step_ids_are_unique(self) -> PlanContent:
        """Require stable, unambiguous step addressing within one Plan."""

        step_ids = tuple(step.id for step in self.steps)
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("Plan step IDs must be unique")
        return self


class PlanDraft(PlanContent):
    """Versioned Plan proposed for human review."""

    schema_version: Literal[1] = Field(
        default=1,
        description="Plan serialization schema version",
    )
    revision: int = Field(
        ge=1,
        strict=True,
        description="Monotonic draft revision",
    )


class ConfirmedPlan(PlanDraft):
    """Immutable Plan revision approved for native Deep Agent execution."""

    @classmethod
    def from_draft(cls, draft: PlanDraft) -> ConfirmedPlan:
        """Freeze a validated draft without changing its revision or content."""

        if not isinstance(draft, PlanDraft):
            raise TypeError("draft must be a PlanDraft")
        return cls.model_validate(
            draft.model_dump(mode="python", by_alias=False, exclude_none=False)
        )


class PlanHandoff(_PlanModel):
    """Deterministic boundary from an approved Plan to native execution."""

    schema_version: Literal[1] = Field(
        default=1,
        description="Handoff serialization schema version",
    )
    message_id: NonBlankText = Field(
        description="Original user message ID reused for native execution"
    )
    digest: PlanDigest = Field(
        description="SHA-256 of the approved Plan and bound message identity"
    )
    dispatched: bool = Field(
        default=False,
        description="Whether the native handoff boundary was synchronously committed",
    )


class RequirementAnswer(_PlanModel):
    """One trusted answer captured from a clarification interrupt."""

    question_id: PlanStepId = Field(description="Question ID being answered")
    answer: NonBlankText = Field(
        description="Trusted free text or checkpoint-derived option label"
    )
    option_id: PlanStepId | None = Field(
        default=None,
        description="Selected option ID, or None for a custom answer",
    )


class PendingClarification(_PlanModel):
    """Concrete form waiting for trusted user input at one interrupt."""

    form: dict[str, JsonValue] = Field(
        description="JSON-only form validated before checkpoint persistence"
    )


class ClarificationExchange(PendingClarification):
    """Resolved form and normalized answers retained as trusted Plan context."""

    answers: tuple[RequirementAnswer, ...] = Field(
        min_length=1,
        description="Answers normalized against the checkpoint form",
    )


class PlanState(_PlanModel):
    """Complete standalone Planning state projected through the AG-UI channel."""

    workflow_version: Literal["tinkerfin.plan.v3"] = "tinkerfin.plan.v3"
    status: PlanStatus = PlanStatus.PLANNING
    effective_mode: Literal["default", "plan"] = "plan"
    request_message_id: NonBlankText | None = None
    goal: NonBlankText | None = None
    pending_clarification: PendingClarification | None = None
    clarification_history: tuple[ClarificationExchange, ...] = ()
    draft: PlanDraft | None = None
    edited_draft: PlanContent | None = None
    confirmed_plan: ConfirmedPlan | None = None
    handoff: PlanHandoff | None = None
    feedback: tuple[NonBlankText, ...] = ()
    revision: int = Field(default=0, ge=0, strict=True)
    review_action: PlanReviewAction | None = None


__all__ = [
    "ClarificationExchange",
    "ConfirmedPlan",
    "PendingClarification",
    "PlanContent",
    "PlanDraft",
    "PlanHandoff",
    "PlanReviewAction",
    "PlanState",
    "PlanStatus",
    "PlanStep",
    "RequirementAnswer",
]
