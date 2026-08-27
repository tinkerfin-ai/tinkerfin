"""Internal structured-output and resume contracts for Plan Mode."""

from __future__ import annotations

from dataclasses import dataclass
from types import UnionType
from typing import Annotated, Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    create_model,
    model_validator,
)
from pydantic.alias_generators import to_camel

from ._clarification import ClarificationSchemaBinding
from ._content import PlanContentBinding
from .clarification import ClarificationFormBase
from .models import NonBlankText, PlanContentModel, PlanReviewAction


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
    draft: PlanContentModel | None = None

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


class PlanClarificationPayload(_ContractModel):
    """Public Plan clarification payload projected through runtime metadata."""

    form: dict[str, JsonValue]


class PlanClarificationMetadata(_ContractModel):
    """Public metadata for one Planner clarification interrupt."""

    origin: Literal["plan"] = "plan"
    clarification: PlanClarificationPayload


class ApprovePlan(_ContractModel):
    type: Literal["approve"]
    base_revision: int = Field(ge=1, strict=True)


class EditPlanBase(_ContractModel):
    type: Literal["edit"]
    base_revision: int = Field(ge=1, strict=True)


class RespondToPlan(_ContractModel):
    type: Literal["respond"]
    base_revision: int = Field(ge=1, strict=True)
    message: NonBlankText


class RejectPlan(_ContractModel):
    type: Literal["reject"]
    base_revision: int = Field(ge=1, strict=True)
    message: NonBlankText | None = None


class PlanReviewPayloadBase(_ContractModel):
    """Review payload specialized with one concrete draft type."""


class PlanReviewMetadataBase(_ContractModel):
    """Public metadata wrapper specialized with one concrete review payload."""

    origin: Literal["plan"] = "plan"


@dataclass(frozen=True, slots=True)
class PlanContractBinding:
    """Dynamic Planner and review contracts frozen for one Definition."""

    planner_response_type: type[PlannerOutcomeBase]
    review_response: TypeAdapter[object]
    review_payload_type: type[_ContractModel]
    review_metadata_type: type[_ContractModel]


def _review_model_union(
    review_types: tuple[type[BaseModel], ...],
) -> type[BaseModel] | UnionType:
    """Combine configured review models into one runtime type expression."""

    if len(review_types) == 1:
        return review_types[0]
    review_union = review_types[0] | review_types[1]
    for review_type in review_types[2:]:
        review_union = review_union | review_type
    return review_union


def create_plan_contract_binding(
    clarification: ClarificationSchemaBinding,
    content: PlanContentBinding,
    *,
    allowed_review_actions: tuple[PlanReviewAction, ...],
) -> PlanContractBinding:
    """Bind one clarification form and one Plan content schema atomically."""

    planner_type = create_model(
        "PlannerOutcome",
        __base__=PlannerOutcomeBase,
        clarification=(clarification.form_schema | None, None),
        draft=(content.schema | None, None),
    )
    edit_type = create_model(
        "EditPlan",
        __base__=EditPlanBase,
        content=(content.schema, ...),
    )
    review_types = tuple(
        {
            PlanReviewAction.APPROVE: ApprovePlan,
            PlanReviewAction.EDIT: edit_type,
            PlanReviewAction.RESPOND: RespondToPlan,
            PlanReviewAction.REJECT: RejectPlan,
        }[action]
        for action in allowed_review_actions
    )
    review_union = _review_model_union(review_types)
    review_annotation = (
        Annotated[
            review_union,
            Field(discriminator="type"),
        ]  # pyright: ignore[reportInvalidTypeForm]
        if len(review_types) > 1
        else review_union
    )
    review_response = cast(
        TypeAdapter[object],
        TypeAdapter(review_annotation),
    )
    review_payload_type = create_model(
        "PlanReviewPayload",
        __base__=PlanReviewPayloadBase,
        draft=(content.draft_type, ...),
    )
    review_metadata_type = create_model(
        "PlanReviewMetadata",
        __base__=PlanReviewMetadataBase,
        review=(review_payload_type, ...),
    )
    return PlanContractBinding(
        planner_response_type=planner_type,
        review_response=review_response,
        review_payload_type=review_payload_type,
        review_metadata_type=review_metadata_type,
    )


__all__ = [
    "ApprovePlan",
    "EditPlanBase",
    "PlanClarificationMetadata",
    "PlanClarificationPayload",
    "PlanContractBinding",
    "PlanReviewMetadataBase",
    "PlanReviewPayloadBase",
    "PlannerOutcomeBase",
    "RejectPlan",
    "RespondToPlan",
    "create_plan_contract_binding",
]
