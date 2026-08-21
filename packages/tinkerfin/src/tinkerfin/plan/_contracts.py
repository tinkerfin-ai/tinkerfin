"""Internal structured-output and resume contracts for Plan Mode."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    model_validator,
)
from pydantic.alias_generators import to_camel

from .models import (
    ClarificationQuestion,
    NonBlankText,
    PlanRoute,
    PlanStep,
    RequirementAnswer,
)


class _ContractModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )


class GateDecision(_ContractModel):
    """Structured conservative route selected before any Plan work begins."""

    route: PlanRoute
    goal: NonBlankText
    questions: tuple[ClarificationQuestion, ...] = ()

    @model_validator(mode="after")
    def questions_match_route(self) -> GateDecision:
        """Require questions only for the clarification route."""

        if self.route is PlanRoute.CLARIFY and not self.questions:
            raise ValueError("clarify route requires at least one question")
        if self.route is not PlanRoute.CLARIFY and self.questions:
            raise ValueError("only clarify route may contain questions")
        if len({question.id for question in self.questions}) != len(self.questions):
            raise ValueError("clarification question IDs must be unique")
        return self


class PlanContent(_ContractModel):
    """Plan content before the parent workflow assigns a revision."""

    goal: NonBlankText
    assumptions: tuple[NonBlankText, ...] = ()
    steps: tuple[PlanStep, ...] = Field(min_length=1)
    acceptance_criteria: tuple[NonBlankText, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def step_ids_are_unique(self) -> PlanContent:
        """Require unique step IDs before constructing a public draft."""

        ids = tuple(step.id for step in self.steps)
        if len(ids) != len(set(ids)):
            raise ValueError("Plan step IDs must be unique")
        return self


class PlannerOutcome(_ContractModel):
    """Structured result from the read-only Planner agent."""

    type: Literal["clarify", "draft"]
    questions: tuple[ClarificationQuestion, ...] = ()
    draft: PlanContent | None = None

    @model_validator(mode="after")
    def payload_matches_type(self) -> PlannerOutcome:
        """Require exactly one Planner outcome payload."""

        if self.type == "clarify":
            if not self.questions or self.draft is not None:
                raise ValueError("clarify outcome requires questions and no draft")
            if len({question.id for question in self.questions}) != len(self.questions):
                raise ValueError("clarification question IDs must be unique")
        elif self.questions or self.draft is None:
            raise ValueError("draft outcome requires a draft and no questions")
        return self


class ClarificationResponse(_ContractModel):
    """Complete user response to one clarification interrupt."""

    type: Literal["respond"]
    answers: tuple[RequirementAnswer, ...] = Field(min_length=1)


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

CLARIFICATION_RESPONSE = TypeAdapter(ClarificationResponse)
PLAN_REVIEW_RESPONSE = TypeAdapter(PlanReviewResponse)


__all__ = [
    "CLARIFICATION_RESPONSE",
    "PLAN_REVIEW_RESPONSE",
    "ApprovePlan",
    "ClarificationResponse",
    "EditPlan",
    "GateDecision",
    "PlanContent",
    "PlannerOutcome",
    "RejectPlan",
    "RespondToPlan",
]
