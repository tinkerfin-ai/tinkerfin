"""Internal structured-output and resume contracts for Plan Mode."""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    model_validator,
)
from pydantic.alias_generators import to_camel

from .clarification import ClarificationFormBase
from .models import NonBlankText, PlanContent, PlanStepId


class _ContractModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )


class PlannerOutcomeBase(_ContractModel):
    """Structured result from the single read-only Planner agent."""

    type: Literal["clarify", "draft", "accept_edit"]
    clarification: ClarificationFormBase | None = None
    draft: PlanContent | None = None

    @model_validator(mode="after")
    def payload_matches_type(self) -> PlannerOutcomeBase:
        """Require exactly the payload owned by the selected Planner outcome."""

        if self.type == "clarify":
            if self.clarification is None or self.draft is not None:
                raise ValueError("clarify outcome requires a form and no draft")
        elif self.type == "draft":
            if self.clarification is not None or self.draft is None:
                raise ValueError(
                    "draft outcome requires a draft and no clarification form"
                )
        elif self.clarification is not None or self.draft is not None:
            raise ValueError("accept_edit outcome cannot contain a form or draft")
        return self


class ClarificationOptionAnswer(_ContractModel):
    """One untrusted client answer selecting a checkpointed option by ID."""

    question_id: PlanStepId
    option_id: PlanStepId


class ClarificationFreeTextAnswer(_ContractModel):
    """One untrusted client answer containing only non-blank free text."""

    question_id: PlanStepId
    answer: NonBlankText


ClarificationAnswer: TypeAlias = ClarificationOptionAnswer | ClarificationFreeTextAnswer


class ClarificationResponse(_ContractModel):
    """Complete user response to one clarification interrupt."""

    type: Literal["respond"]
    answers: tuple[ClarificationAnswer, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def question_ids_are_unique(self) -> ClarificationResponse:
        """Reject ambiguous duplicate answers before checkpoint comparison."""

        question_ids = tuple(answer.question_id for answer in self.answers)
        if len(question_ids) != len(set(question_ids)):
            raise ValueError("clarification response question IDs must be unique")
        return self


class PlanClarificationPayload(_ContractModel):
    """Public Plan clarification payload projected through runtime metadata."""

    contract: Literal["tinkerfin.plan-clarification.v1"] = Field(
        default="tinkerfin.plan-clarification.v1",
        alias="schema",
    )
    form: dict[str, JsonValue]


class PlanClarificationMetadata(_ContractModel):
    """Versioned public metadata for one Planner clarification interrupt."""

    origin: Literal["plan"] = "plan"
    clarification: PlanClarificationPayload


class ApprovePlan(_ContractModel):
    type: Literal["approve"]
    base_revision: int = Field(ge=1, strict=True)


class EditPlan(_ContractModel):
    type: Literal["edit"]
    base_revision: int = Field(ge=1, strict=True)
    draft: PlanContent


class RespondToPlan(_ContractModel):
    type: Literal["respond"]
    base_revision: int = Field(ge=1, strict=True)
    message: NonBlankText


class RejectPlan(_ContractModel):
    type: Literal["reject"]
    base_revision: int = Field(ge=1, strict=True)
    message: NonBlankText | None = None


PlanReviewResponse = Annotated[
    ApprovePlan | EditPlan | RespondToPlan | RejectPlan,
    Field(discriminator="type"),
]

CLARIFICATION_RESPONSE: TypeAdapter[ClarificationResponse] = TypeAdapter(
    ClarificationResponse
)
PLAN_REVIEW_RESPONSE: TypeAdapter[PlanReviewResponse] = TypeAdapter(PlanReviewResponse)


__all__ = [
    "CLARIFICATION_RESPONSE",
    "PLAN_REVIEW_RESPONSE",
    "ApprovePlan",
    "ClarificationAnswer",
    "ClarificationFreeTextAnswer",
    "ClarificationOptionAnswer",
    "ClarificationResponse",
    "EditPlan",
    "PlanClarificationMetadata",
    "PlanClarificationPayload",
    "PlannerOutcomeBase",
    "RejectPlan",
    "RespondToPlan",
]
