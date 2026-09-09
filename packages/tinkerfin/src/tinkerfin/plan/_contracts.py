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
    RootModel,
    TypeAdapter,
    create_model,
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


class _PlannerOutcomeMember(_ContractModel):
    """Provide the discriminator shared by the three Planner results."""

    type: Literal["clarify", "draft", "accept_edit"]


class PlannerOutcomeBase(RootModel[_PlannerOutcomeMember]):
    """Expose one discriminated result from the single read-only Planner call."""

    # Tool providers require function parameters to declare an object at the root.
    # Pydantic's discriminated RootModel otherwise emits only oneOf/discriminator even
    # though every member is an extra-forbid object with the same discriminator.
    model_config = ConfigDict(
        frozen=True,
        json_schema_extra={"type": "object"},
    )

    @property
    def type(self) -> Literal["clarify", "draft", "accept_edit"]:
        """Return the selected Planner result kind."""

        return self.root.type

    @property
    def clarification(self) -> ClarificationFormBase | None:
        """Return the clarification form only for a clarify result."""

        value = getattr(self.root, "clarification", None)
        return value if isinstance(value, ClarificationFormBase) else None

    @property
    def draft(self) -> PlanContentModel | None:
        """Return the proposed content only for a draft result."""

        value = getattr(self.root, "draft", None)
        return value if isinstance(value, PlanContentModel) else None


class PlanClarificationPayload(_ContractModel):
    """Public Plan clarification payload projected through runtime metadata."""

    form: dict[str, JsonValue]


class PlanClarificationMetadata(_ContractModel):
    """Public metadata for one Planner clarification interrupt."""

    origin: Literal["plan"] = "plan"
    clarification: PlanClarificationPayload


class ApprovePlan(_ContractModel):
    """Approve the current Plan revision without edits."""

    type: Literal["approve"]
    base_revision: int = Field(ge=1, strict=True)


class CancelPlan(_ContractModel):
    """Discard the current draft while keeping the Planning conversation active."""

    type: Literal["cancel"]
    base_revision: int = Field(ge=1, strict=True)


class EditPlanBase(_ContractModel):
    """Identify an edit decision for one current Plan revision."""

    type: Literal["edit"]
    base_revision: int = Field(ge=1, strict=True)


class RespondToPlan(_ContractModel):
    """Return user feedback for one current Plan revision."""

    type: Literal["respond"]
    base_revision: int = Field(ge=1, strict=True)
    message: NonBlankText


class RejectPlan(_ContractModel):
    """Reject one current Plan revision with optional feedback."""

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
    planner_edit_response_type: type[PlannerOutcomeBase]
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


def _planner_response_type(
    first: type[BaseModel],
    second: type[BaseModel],
) -> type[PlannerOutcomeBase]:
    """Build one state-specific two-outcome Planner Tool contract."""

    planner_annotation = Annotated[
        first | second,
        Field(discriminator="type"),
    ]  # pyright: ignore[reportInvalidTypeForm]
    return create_model(
        "PlannerOutcome",
        __base__=PlannerOutcomeBase,
        root=(planner_annotation, ...),
    )


def create_plan_contract_binding(
    clarification: ClarificationSchemaBinding,
    content: PlanContentBinding,
    *,
    allowed_review_actions: tuple[PlanReviewAction, ...],
) -> PlanContractBinding:
    """Bind one clarification form and one Plan content schema atomically."""

    clarify_type = create_model(
        "PlannerClarifyOutcome",
        __base__=_PlannerOutcomeMember,
        type=(Literal["clarify"], ...),
        clarification=(clarification.form_schema, ...),
    )
    draft_type = create_model(
        "PlannerDraftOutcome",
        __base__=_PlannerOutcomeMember,
        type=(Literal["draft"], ...),
        draft=(content.schema, ...),
    )
    accept_edit_type = create_model(
        "PlannerAcceptEditOutcome",
        __base__=_PlannerOutcomeMember,
        type=(Literal["accept_edit"], ...),
    )
    planner_type = _planner_response_type(
        clarify_type,
        draft_type,
    )
    planner_edit_type = _planner_response_type(
        clarify_type,
        accept_edit_type,
    )
    edit_type = create_model(
        "EditPlan",
        __base__=EditPlanBase,
        content=(content.schema, ...),
    )
    review_types = tuple(
        {
            PlanReviewAction.APPROVE: ApprovePlan,
            PlanReviewAction.CANCEL: CancelPlan,
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
        planner_edit_response_type=planner_edit_type,
        review_response=review_response,
        review_payload_type=review_payload_type,
        review_metadata_type=review_metadata_type,
    )


__all__ = [
    "ApprovePlan",
    "CancelPlan",
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
