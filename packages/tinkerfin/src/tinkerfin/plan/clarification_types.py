"""Public extension descriptors for custom Plan clarification semantics."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Generic, TypeVar, cast

from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter

from .clarification import (
    ClarificationModel,
    ClarificationOption,
    ClarificationOptionBase,
    ClarificationQuestionBase,
    ClarificationResponseBase,
    DateQuestion,
    DateResponse,
    MultipleChoiceQuestion,
    MultipleChoiceResponse,
    SingleChoiceQuestion,
    SingleChoiceResponse,
    TextQuestion,
    TextResponse,
)
from .errors import PlanModeConfigurationError

QuestionT = TypeVar("QuestionT", bound=ClarificationQuestionBase)
ResponseT = TypeVar("ResponseT", bound=ClarificationResponseBase)

BindResponseSchema = Callable[[QuestionT], dict[str, JsonValue]]
ValidateResponse = Callable[[QuestionT, ResponseT], None]
NormalizeResponse = Callable[[QuestionT, ResponseT], Mapping[str, JsonValue]]

_CUSTOM_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9.-]*:[a-z][a-z0-9._-]*\.v[1-9][0-9]*$")
_BUILTIN_TYPE_IDS = frozenset({"single_choice", "multiple_choice", "text", "date"})
_JSON_OBJECT = TypeAdapter(
    dict[str, JsonValue],
    config=ConfigDict(allow_inf_nan=False),
)


@dataclass(frozen=True, slots=True)
class ClarificationType(Generic[QuestionT, ResponseT]):
    """One immutable custom question, response, validation, and normalization unit.

    A descriptor has no resource lifecycle. Its callbacks must be synchronous,
    deterministic, and free of external I/O because a checkpoint replay can invoke
    them more than once for the same response.

    Attributes:
        type_id: Versioned namespaced discriminator shared by both boundary models;
            its version must change whenever callback semantics change.
        description: Model-facing guidance for choosing this semantic answer type.
        question_model: Structured question model emitted by the Planner.
        response_model: Answered payload model accepted from a client.
        bind_response_schema: Optional per-question exact JSON Schema builder.
        validate: Optional question-dependent pre-resume validator.
        normalize: Trusted canonical JSON object builder.
    """

    type_id: str
    description: str
    question_model: type[QuestionT]
    response_model: type[ResponseT]
    bind_response_schema: BindResponseSchema[QuestionT] | None
    validate: ValidateResponse[QuestionT, ResponseT] | None
    normalize: NormalizeResponse[QuestionT, ResponseT]

    def __post_init__(self) -> None:
        """Reject an invalid descriptor at its public construction boundary."""

        _validate_descriptor(self, allow_builtin=True)


def _require_concrete_model(model: object, *, name: str, base: type[BaseModel]) -> None:
    if not isinstance(model, type) or not issubclass(model, base):
        raise TypeError(f"{name} must be a {base.__name__} subclass")
    if getattr(model, "__parameters__", ()):
        raise PlanModeConfigurationError(f"{name} must not contain unbound generics")


def _require_inherited_core_fields(
    model: type[BaseModel],
    *,
    name: str,
    base: type[BaseModel],
    fields: frozenset[str],
) -> None:
    """Prevent a descriptor model from redefining framework-owned fields."""

    for parent in model.__mro__:
        if parent is base:
            return
        annotations = vars(parent).get("__annotations__", {})
        overridden = fields.intersection(annotations)
        if overridden:
            names = ", ".join(repr(field) for field in sorted(overridden))
            raise PlanModeConfigurationError(
                f"{name} must inherit framework core fields unchanged: {names}"
            )
    raise PlanModeConfigurationError(f"{name} must inherit from {base.__name__}")


def _discriminator_const(model: type[BaseModel], *, name: str) -> str:
    try:
        schema = _JSON_OBJECT.validate_python(model.model_json_schema(by_alias=True))
    except Exception as error:
        raise PlanModeConfigurationError(
            f"{name} could not produce a JSON Schema",
            cause=error,
        ) from error
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise PlanModeConfigurationError(f"{name} must expose object properties")
    answer_type = properties.get("answerType")
    if not isinstance(answer_type, dict) or not isinstance(
        answer_type.get("const"), str
    ):
        raise PlanModeConfigurationError(
            f"{name}.answer_type must be one string Literal"
        )
    return cast(str, answer_type["const"])


def _validate_descriptor(
    descriptor: ClarificationType[QuestionT, ResponseT],
    *,
    allow_builtin: bool,
) -> None:
    type_id = descriptor.type_id
    if not isinstance(type_id, str) or (
        type_id not in _BUILTIN_TYPE_IDS and not _CUSTOM_TYPE_PATTERN.fullmatch(type_id)
    ):
        raise PlanModeConfigurationError(
            "custom clarification type_id must be a versioned namespaced ID"
        )
    if not allow_builtin and type_id in _BUILTIN_TYPE_IDS:
        raise PlanModeConfigurationError(
            "custom clarification type_id must be a versioned namespaced ID"
        )
    description = descriptor.description
    if not isinstance(description, str) or not description.strip():
        raise PlanModeConfigurationError(
            "custom clarification description must not be blank"
        )
    object.__setattr__(descriptor, "description", description.strip())
    _require_concrete_model(
        descriptor.question_model,
        name="question_model",
        base=ClarificationQuestionBase,
    )
    _require_inherited_core_fields(
        descriptor.question_model,
        name="question_model",
        base=ClarificationQuestionBase,
        fields=frozenset({"id", "prompt", "required"}),
    )
    _require_concrete_model(
        descriptor.response_model,
        name="response_model",
        base=ClarificationResponseBase,
    )
    _require_inherited_core_fields(
        descriptor.response_model,
        name="response_model",
        base=ClarificationResponseBase,
        fields=frozenset({"status"}),
    )
    if (
        _discriminator_const(descriptor.question_model, name="question_model")
        != type_id
    ):
        raise PlanModeConfigurationError(
            "question_model answer_type does not match custom type_id"
        )
    if (
        _discriminator_const(descriptor.response_model, name="response_model")
        != type_id
    ):
        raise PlanModeConfigurationError(
            "response_model answer_type does not match custom type_id"
        )
    if not callable(descriptor.normalize):
        raise TypeError("normalize must be callable")
    if descriptor.bind_response_schema is not None and not callable(
        descriptor.bind_response_schema
    ):
        raise TypeError("bind_response_schema must be callable or None")
    if descriptor.validate is not None and not callable(descriptor.validate):
        raise TypeError("validate must be callable or None")


def clarification_type(
    *,
    type_id: str,
    description: str,
    question_model: type[QuestionT],
    response_model: type[ResponseT],
    normalize: NormalizeResponse[QuestionT, ResponseT],
    bind_response_schema: BindResponseSchema[QuestionT] | None = None,
    validate: ValidateResponse[QuestionT, ResponseT] | None = None,
) -> ClarificationType[QuestionT, ResponseT]:
    """Create one validated custom clarification type descriptor.

    Args:
        type_id: Versioned namespaced discriminator such as ``acme:rating.v1``;
            increment its version whenever Schema, validation, or normalization
            semantics change.
        description: Guidance telling the Planner when to choose the type.
        question_model: Concrete model for Planner-generated questions.
        response_model: Concrete model for answered client payloads.
        normalize: Pure canonical JSON object builder.
        bind_response_schema: Optional exact Schema builder for one question instance.
        validate: Optional pure validator for constraints that JSON Schema cannot express.

    Returns:
        A frozen descriptor ready for ``TinkerFin.plan``.

    Raises:
        TypeError: A model or callback has the wrong runtime type.
        PlanModeConfigurationError: IDs, discriminators, or descriptions are invalid.
    """

    descriptor = ClarificationType(
        type_id=type_id,
        description=description,
        question_model=question_model,
        response_model=response_model,
        bind_response_schema=bind_response_schema,
        validate=validate,
        normalize=normalize,
    )
    _validate_descriptor(descriptor, allow_builtin=False)
    return descriptor


def _single_value(
    question: SingleChoiceQuestion[
        ClarificationModel,
        ClarificationOptionBase,
    ],
    response: SingleChoiceResponse,
) -> Mapping[str, JsonValue]:
    if response.option_id is not None:
        option = next(
            item for item in question.options if item.id == response.option_id
        )
        return {"option": {"id": option.id, "label": option.label}}
    return {"customAnswer": cast(str, response.custom_answer)}


def _multiple_value(
    question: MultipleChoiceQuestion[
        ClarificationModel,
        ClarificationOptionBase,
    ],
    response: MultipleChoiceResponse,
) -> Mapping[str, JsonValue]:
    selected = set(response.option_ids)
    value: dict[str, JsonValue] = {
        "options": [
            {"id": option.id, "label": option.label}
            for option in question.options
            if option.id in selected
        ]
    }
    if response.custom_answer is not None:
        value["customAnswer"] = response.custom_answer
    return value


def _text_value(
    _question: TextQuestion[ClarificationModel],
    response: TextResponse,
) -> Mapping[str, JsonValue]:
    return {"answer": response.answer}


def _date_value(
    _question: DateQuestion[ClarificationModel],
    response: DateResponse,
) -> Mapping[str, JsonValue]:
    return {"date": response.date.isoformat()}


def _builtin_type(
    *,
    type_id: str,
    description: str,
    question_model: type[ClarificationQuestionBase],
    response_model: type[ClarificationResponseBase],
    normalize: NormalizeResponse[ClarificationQuestionBase, ClarificationResponseBase],
) -> ClarificationType[ClarificationQuestionBase, ClarificationResponseBase]:
    return ClarificationType(
        type_id=type_id,
        description=description,
        question_model=question_model,
        response_model=response_model,
        bind_response_schema=None,
        validate=None,
        normalize=normalize,
    )


BUILTIN_CLARIFICATION_TYPES = (
    _builtin_type(
        type_id="single_choice",
        description="Use when exactly one listed choice or one custom alternative applies.",
        question_model=SingleChoiceQuestion[
            ClarificationModel,
            ClarificationOption[ClarificationModel],
        ],
        response_model=SingleChoiceResponse,
        normalize=cast(
            NormalizeResponse[ClarificationQuestionBase, ClarificationResponseBase],
            _single_value,
        ),
    ),
    _builtin_type(
        type_id="multiple_choice",
        description="Use when several listed choices can apply at the same time.",
        question_model=MultipleChoiceQuestion[
            ClarificationModel,
            ClarificationOption[ClarificationModel],
        ],
        response_model=MultipleChoiceResponse,
        normalize=cast(
            NormalizeResponse[ClarificationQuestionBase, ClarificationResponseBase],
            _multiple_value,
        ),
    ),
    _builtin_type(
        type_id="text",
        description="Use for an open-ended non-blank textual answer.",
        question_model=TextQuestion[ClarificationModel],
        response_model=TextResponse,
        normalize=cast(
            NormalizeResponse[ClarificationQuestionBase, ClarificationResponseBase],
            _text_value,
        ),
    ),
    _builtin_type(
        type_id="date",
        description="Use for one calendar date without a time or time zone.",
        question_model=DateQuestion[ClarificationModel],
        response_model=DateResponse,
        normalize=cast(
            NormalizeResponse[ClarificationQuestionBase, ClarificationResponseBase],
            _date_value,
        ),
    ),
)


__all__ = ["ClarificationType", "clarification_type"]
