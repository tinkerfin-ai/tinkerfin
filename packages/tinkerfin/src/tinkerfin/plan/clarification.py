"""Public strongly typed contracts for Plan clarification forms."""

from __future__ import annotations

from typing import Annotated, Generic, Literal, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)
from pydantic.alias_generators import to_camel

ClarificationId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    ),
]
ClarificationText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]


class ClarificationModel(BaseModel):
    """Base for public, immutable clarification boundary models."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )


class ClarificationOptionBase(ClarificationModel):
    """Core fields shared by every model-generated single-select option."""

    id: ClarificationId = Field(description="Stable option ID within one question")
    label: ClarificationText = Field(description="Concise option label shown to users")
    description: ClarificationText | None = Field(
        default=None,
        description="Optional user-visible consequence or tradeoff",
    )


OptionAttributesT = TypeVar("OptionAttributesT", bound=ClarificationModel)


class ClarificationOption(
    ClarificationOptionBase,
    Generic[OptionAttributesT],
):
    """Option with one host-defined, strongly typed attributes model."""

    attributes: OptionAttributesT | None = Field(
        default=None,
        description="Optional non-authoritative host metadata exposed to users",
    )


class ClarificationQuestionBase(ClarificationModel):
    """Core fields shared by every clarification question."""

    id: ClarificationId = Field(description="Stable question ID within one form")
    prompt: ClarificationText = Field(description="Question shown to the user")
    required: bool = Field(
        description=(
            "Whether planning must stop until the user answers this question; "
            "optional questions may be explicitly skipped"
        )
    )
    options: tuple[ClarificationOptionBase, ...] = Field(
        default=(),
        description="Model-generated single-select options",
    )
    allow_free_text: bool = Field(
        default=True,
        description="Whether the user may answer without selecting an option",
    )

    @model_validator(mode="after")
    def options_are_usable(self) -> ClarificationQuestionBase:
        """Require unique options and a usable answer path."""

        option_ids = tuple(option.id for option in self.options)
        if len(option_ids) != len(set(option_ids)):
            raise ValueError("Clarification option IDs must be unique")
        if not self.allow_free_text and not self.options:
            raise ValueError(
                "Clarification questions without free text require options"
            )
        return self


QuestionAttributesT = TypeVar("QuestionAttributesT", bound=ClarificationModel)


class ClarificationQuestion(
    ClarificationQuestionBase,
    Generic[QuestionAttributesT, OptionAttributesT],
):
    """Question with independent host-defined question and option metadata."""

    # Pydantic freezes this tuple field, so narrowing its item type is covariant.
    options: tuple[ClarificationOption[OptionAttributesT], ...] = Field(  # pyright: ignore[reportIncompatibleVariableOverride]
        default=(),
        description="Model-generated single-select options",
    )
    attributes: QuestionAttributesT | None = Field(
        default=None,
        description="Optional non-authoritative host metadata exposed to users",
    )


class ClarificationFormBase(ClarificationModel):
    """Core form accepted by the Plan clarification workflow."""

    schema_version: Literal[2] = Field(
        default=2,
        description="Clarification form serialization schema version",
    )
    questions: tuple[ClarificationQuestionBase, ...] = Field(
        min_length=1,
        description="Questions that must each be answered or explicitly skipped",
    )

    @model_validator(mode="after")
    def questions_are_usable(self) -> ClarificationFormBase:
        """Require a non-empty form with unambiguous question addressing."""

        question_ids = tuple(question.id for question in self.questions)
        if not question_ids:
            raise ValueError("Clarification forms must contain at least one question")
        if len(question_ids) != len(set(question_ids)):
            raise ValueError("Clarification question IDs must be unique")
        return self


QuestionT = TypeVar("QuestionT", bound=ClarificationQuestionBase)


class ClarificationForm(ClarificationFormBase, Generic[QuestionT]):
    """Form specialized with one concrete question type or discriminated union."""

    # Pydantic freezes this tuple field, so narrowing its item type is covariant.
    questions: tuple[QuestionT, ...] = Field(  # pyright: ignore[reportIncompatibleVariableOverride]
        min_length=1,
        description="Questions that must each be answered or explicitly skipped",
    )


class DefaultClarificationOption(ClarificationOption[ClarificationModel]):
    """Default option that rejects every host-specific attributes field."""


class DefaultClarificationQuestion(
    ClarificationQuestion[ClarificationModel, ClarificationModel]
):
    """Default question used when the host does not configure a custom form."""

    # The frozen tuple narrows only the default option item type.
    options: tuple[DefaultClarificationOption, ...] = ()  # pyright: ignore[reportIncompatibleVariableOverride]


class DefaultClarificationForm(ClarificationForm[DefaultClarificationQuestion]):
    """Concrete default form requiring no host-defined business models."""


__all__ = [
    "ClarificationForm",
    "ClarificationFormBase",
    "ClarificationModel",
    "ClarificationOption",
    "ClarificationOptionBase",
    "ClarificationQuestion",
    "ClarificationQuestionBase",
    "DefaultClarificationForm",
]
