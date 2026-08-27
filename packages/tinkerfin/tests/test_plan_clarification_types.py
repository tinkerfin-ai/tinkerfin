"""Dynamic Plan clarification question, response, and extension contracts."""

from __future__ import annotations

from typing import Literal, cast

import pytest
from jsonschema.exceptions import SchemaError
from pydantic import Field, JsonValue, ValidationError, create_model

from tinkerfin import TinkerFin
from tinkerfin.plan import (
    BuiltInClarificationForm,
    ClarificationForm,
    ClarificationModel,
    ClarificationOption,
    ClarificationQuestionBase,
    ClarificationResponseBase,
    ClarificationType,
    DefaultClarificationForm,
    PlanClarificationResponseError,
    PlanModeConfigurationError,
    clarification_type,
)
from tinkerfin.plan._clarification import (
    build_response_schema,
    create_clarification_binding,
    pending_contract_digest,
    validate_and_normalize_response,
)


def test_default_form_exposes_all_builtin_discriminated_question_types() -> None:
    schema = DefaultClarificationForm.model_json_schema(by_alias=True)
    questions = schema["properties"]["questions"]

    assert "schemaVersion" not in schema["properties"]
    assert set(questions["items"]["discriminator"]["mapping"]) == {
        "single_choice",
        "multiple_choice",
        "text",
        "date",
    }


def test_mixed_form_builds_an_exact_response_schema_and_trusted_values() -> None:
    form = DefaultClarificationForm.model_validate(
        {
            "questions": [
                {
                    "id": "target",
                    "answerType": "single_choice",
                    "prompt": "Primary target?",
                    "required": True,
                    "options": [
                        {"id": "web", "label": "Web"},
                        {"id": "mobile", "label": "Mobile"},
                    ],
                },
                {
                    "id": "coverage",
                    "answerType": "multiple_choice",
                    "prompt": "Coverage?",
                    "required": True,
                    "options": [
                        {"id": "chrome", "label": "Chrome"},
                        {"id": "safari", "label": "Safari"},
                    ],
                    "minSelections": 2,
                    "maxSelections": 3,
                },
                {
                    "id": "notes",
                    "answerType": "text",
                    "prompt": "Constraints?",
                    "required": False,
                },
                {
                    "id": "deadline",
                    "answerType": "date",
                    "prompt": "Deadline?",
                    "required": True,
                },
            ]
        }
    )
    binding = create_clarification_binding(DefaultClarificationForm)
    schema = build_response_schema(binding, form)

    properties = cast(dict[str, object], schema["properties"])
    answers = cast(dict[str, object], properties["answers"])
    assert answers["additionalProperties"] is False
    assert answers["required"] == ["target", "coverage", "notes", "deadline"]
    normalized = validate_and_normalize_response(
        binding,
        form,
        schema,
        {
            "type": "respond",
            "answers": {
                "target": {
                    "status": "answered",
                    "answerType": "single_choice",
                    "optionId": "web",
                },
                "coverage": {
                    "status": "answered",
                    "answerType": "multiple_choice",
                    "optionIds": ["safari"],
                    "customAnswer": "Firefox",
                },
                "notes": {"status": "skipped"},
                "deadline": {
                    "status": "answered",
                    "answerType": "date",
                    "date": "2026-09-15",
                },
            },
        },
    )

    assert [answer.value for answer in normalized] == [
        {"option": {"id": "web", "label": "Web"}},
        {
            "options": [{"id": "safari", "label": "Safari"}],
            "customAnswer": "Firefox",
        },
        None,
        {"date": "2026-09-15"},
    ]
    assert normalized[2].skipped is True


@pytest.mark.parametrize(
    "answer",
    [
        {
            "status": "answered",
            "answerType": "multiple_choice",
            "optionIds": ["chrome", "chrome"],
        },
        {
            "status": "answered",
            "answerType": "multiple_choice",
            "optionIds": ["unknown"],
            "customAnswer": "Firefox",
        },
    ],
)
def test_multiple_choice_rejects_duplicate_unknown_and_underfilled_answers(
    answer: dict[str, object],
) -> None:
    form = DefaultClarificationForm.model_validate(
        {
            "questions": [
                {
                    "id": "coverage",
                    "answerType": "multiple_choice",
                    "prompt": "Coverage?",
                    "required": True,
                    "options": [
                        {"id": "chrome", "label": "Chrome"},
                        {"id": "safari", "label": "Safari"},
                    ],
                    "minSelections": 2,
                }
            ]
        }
    )
    binding = create_clarification_binding(DefaultClarificationForm)
    schema = build_response_schema(binding, form)

    with pytest.raises(PlanClarificationResponseError):
        validate_and_normalize_response(
            binding,
            form,
            schema,
            {"type": "respond", "answers": {"coverage": answer}},
        )


@pytest.mark.parametrize("value", ["2026-02-29", "2026-13-01", "2026-01-01T00:00:00Z"])
def test_date_response_rejects_invalid_calendar_values(value: str) -> None:
    form = DefaultClarificationForm.model_validate(
        {
            "questions": [
                {
                    "id": "deadline",
                    "answerType": "date",
                    "prompt": "Deadline?",
                    "required": True,
                }
            ]
        }
    )
    binding = create_clarification_binding(DefaultClarificationForm)
    schema = build_response_schema(binding, form)

    with pytest.raises(PlanClarificationResponseError):
        validate_and_normalize_response(
            binding,
            form,
            schema,
            {
                "type": "respond",
                "answers": {
                    "deadline": {
                        "status": "answered",
                        "answerType": "date",
                        "date": value,
                    }
                },
            },
        )


class _QuestionAttributes(ClarificationModel):
    category: str


class _OptionAttributes(ClarificationModel):
    recommended: bool


class _NumericAttributes(ClarificationModel):
    score: float


_Option = ClarificationOption[_OptionAttributes]


class _BrandedForm(BuiltInClarificationForm[_QuestionAttributes, _Option]):
    title: str


def test_shared_metadata_form_requires_no_per_question_subclasses() -> None:
    schema = _BrandedForm.model_json_schema(by_alias=True)

    assert set(
        schema["properties"]["questions"]["items"]["discriminator"]["mapping"]
    ) == {
        "single_choice",
        "multiple_choice",
        "text",
        "date",
    }
    TinkerFin().plan(clarification_schema=_BrandedForm)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_clarification_metadata_requires_finite_json(value: float) -> None:
    with pytest.raises(ValidationError, match="finite number"):
        _NumericAttributes(score=value)


class _RatingQuestion(ClarificationQuestionBase):
    answer_type: Literal["acme:rating.v1"] = "acme:rating.v1"
    maximum: int = Field(ge=1)


class _RatingResponse(ClarificationResponseBase):
    answer_type: Literal["acme:rating.v1"] = "acme:rating.v1"
    rating: int


class _DetailedRatingResponse(ClarificationResponseBase):
    answer_type: Literal["acme:rating.v1"] = "acme:rating.v1"
    rating: int
    reason: str | None = None


class _RatingOnlyForm(ClarificationForm[_RatingQuestion]):
    pass


def _rating_schema(question: _RatingQuestion) -> dict[str, JsonValue]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["status", "answerType", "rating"],
        "properties": {
            "status": {"const": "answered"},
            "answerType": {"const": "acme:rating.v1"},
            "rating": {"type": "integer", "minimum": 1, "maximum": question.maximum},
        },
    }


def _referenced_rating_schema(question: _RatingQuestion) -> dict[str, JsonValue]:
    return {
        "$defs": {
            "Bound": {
                "type": "integer",
                "minimum": 1,
                "maximum": question.maximum,
            }
        },
        "type": "object",
        "additionalProperties": False,
        "required": ["status", "answerType", "rating"],
        "properties": {
            "status": {"const": "answered"},
            "answerType": {"const": "acme:rating.v1"},
            "rating": {"$ref": "#/$defs/Bound"},
        },
    }


def test_custom_type_is_one_registration_unit_and_extends_the_default_form() -> None:
    rating = clarification_type(
        type_id="acme:rating.v1",
        description="Use for one bounded integer rating.",
        question_model=_RatingQuestion,
        response_model=_RatingResponse,
        bind_response_schema=_rating_schema,
        normalize=lambda _question, response: {"rating": response.rating},
    )
    factory = TinkerFin().plan(clarification_types=(rating,))
    assert factory._plan_options is not None
    binding = factory._plan_options.clarification
    form = binding.form_schema.model_validate(
        {
            "questions": [
                {
                    "id": "risk",
                    "answerType": "acme:rating.v1",
                    "prompt": "Risk?",
                    "required": True,
                    "maximum": 5,
                }
            ]
        }
    )
    schema = build_response_schema(binding, form)

    answer = validate_and_normalize_response(
        binding,
        form,
        schema,
        {
            "type": "respond",
            "answers": {
                "risk": {
                    "status": "answered",
                    "answerType": "acme:rating.v1",
                    "rating": 4,
                }
            },
        },
    )[0]
    assert answer.value == {"rating": 4}
    assert pending_contract_digest(
        form.model_dump(mode="json", by_alias=True),
        schema,
    )


def test_custom_type_requires_versioned_namespaced_matching_discriminators() -> None:
    with pytest.raises(PlanModeConfigurationError, match="versioned namespaced"):
        clarification_type(
            type_id="rating",
            description="Rating",
            question_model=_RatingQuestion,
            response_model=_RatingResponse,
            normalize=lambda _question, _response: {"rating": 1},
        )


def test_public_descriptor_constructor_enforces_the_factory_contract() -> None:
    with pytest.raises(PlanModeConfigurationError, match="does not match"):
        ClarificationType(
            type_id="acme:different.v1",
            description="Use for one bounded integer rating.",
            question_model=_RatingQuestion,
            response_model=_RatingResponse,
            bind_response_schema=None,
            validate=None,
            normalize=lambda _question, response: {"rating": response.rating},
        )
    with pytest.raises(PlanModeConfigurationError, match="must not be blank"):
        ClarificationType(
            type_id="acme:rating.v1",
            description="   ",
            question_model=_RatingQuestion,
            response_model=_RatingResponse,
            bind_response_schema=None,
            validate=None,
            normalize=lambda _question, response: {"rating": response.rating},
        )


def test_custom_response_must_inherit_answer_status_unchanged() -> None:
    wrong_status = create_model(
        "WrongStatusResponse",
        __base__=ClarificationResponseBase,
        status=(Literal["wrong"], "wrong"),
        answer_type=(Literal["acme:broken.v1"], "acme:broken.v1"),
        rating=(int, ...),
    )

    with pytest.raises(PlanModeConfigurationError, match="status"):
        clarification_type(
            type_id="acme:broken.v1",
            description="Use for a broken response.",
            question_model=create_model(
                "BrokenQuestion",
                __base__=ClarificationQuestionBase,
                answer_type=(Literal["acme:broken.v1"], "acme:broken.v1"),
                maximum=(int, ...),
            ),
            response_model=wrong_status,
            normalize=lambda _question, _response: {"rating": 1},
        )


def test_custom_only_form_uses_its_registered_semantic_type() -> None:
    rating = clarification_type(
        type_id="acme:rating.v1",
        description="Use for one bounded integer rating.",
        question_model=_RatingQuestion,
        response_model=_RatingResponse,
        normalize=lambda _question, response: {"rating": response.rating},
    )

    binding = create_clarification_binding(
        _RatingOnlyForm,
        custom_types=(rating,),
    )

    assert set(binding.types) == {"acme:rating.v1"}


def test_custom_response_definitions_are_scoped_per_question() -> None:
    rating = clarification_type(
        type_id="acme:rating.v1",
        description="Use for one bounded integer rating.",
        question_model=_RatingQuestion,
        response_model=_RatingResponse,
        bind_response_schema=_referenced_rating_schema,
        normalize=lambda _question, response: {"rating": response.rating},
    )
    binding = create_clarification_binding(
        _RatingOnlyForm,
        custom_types=(rating,),
    )
    form = binding.form_schema.model_validate(
        {
            "questions": [
                {
                    "id": "strict",
                    "answerType": "acme:rating.v1",
                    "prompt": "Strict rating?",
                    "required": True,
                    "maximum": 5,
                },
                {
                    "id": "wide",
                    "answerType": "acme:rating.v1",
                    "prompt": "Wide rating?",
                    "required": True,
                    "maximum": 10,
                },
            ]
        }
    )
    schema = build_response_schema(binding, form)

    assert len(cast(dict[str, object], schema["$defs"])) == 2
    with pytest.raises(PlanClarificationResponseError):
        validate_and_normalize_response(
            binding,
            form,
            schema,
            {
                "type": "respond",
                "answers": {
                    "strict": {
                        "status": "answered",
                        "answerType": "acme:rating.v1",
                        "rating": 7,
                    },
                    "wide": {
                        "status": "answered",
                        "answerType": "acme:rating.v1",
                        "rating": 7,
                    },
                },
            },
        )


def test_custom_schema_and_normalized_values_require_finite_json() -> None:
    invalid_schema = clarification_type(
        type_id="acme:rating.v1",
        description="Use for one bounded integer rating.",
        question_model=_RatingQuestion,
        response_model=_RatingResponse,
        bind_response_schema=lambda _question: {
            "type": "object",
            "maximum": float("nan"),
        },
        normalize=lambda _question, response: {"rating": response.rating},
    )
    binding = create_clarification_binding(
        _RatingOnlyForm,
        custom_types=(invalid_schema,),
    )
    form = binding.form_schema.model_validate(
        {
            "questions": [
                {
                    "id": "risk",
                    "answerType": "acme:rating.v1",
                    "prompt": "Risk?",
                    "required": True,
                    "maximum": 5,
                }
            ]
        }
    )
    with pytest.raises((SchemaError, ValidationError), match="finite"):
        build_response_schema(binding, form)

    invalid_normalizer = clarification_type(
        type_id="acme:rating.v1",
        description="Use for one bounded integer rating.",
        question_model=_RatingQuestion,
        response_model=_RatingResponse,
        bind_response_schema=_rating_schema,
        normalize=lambda _question, _response: {"rating": float("inf")},
    )
    binding = create_clarification_binding(
        _RatingOnlyForm,
        custom_types=(invalid_normalizer,),
    )
    schema = build_response_schema(binding, form)
    with pytest.raises(PlanClarificationResponseError, match="pending form"):
        validate_and_normalize_response(
            binding,
            form,
            schema,
            {
                "type": "respond",
                "answers": {
                    "risk": {
                        "status": "answered",
                        "answerType": "acme:rating.v1",
                        "rating": 4,
                    }
                },
            },
        )


def test_binding_fingerprint_covers_the_custom_response_contract() -> None:
    basic = clarification_type(
        type_id="acme:rating.v1",
        description="Use for one bounded integer rating.",
        question_model=_RatingQuestion,
        response_model=_RatingResponse,
        normalize=lambda _question, response: {"rating": response.rating},
    )
    detailed = clarification_type(
        type_id="acme:rating.v1",
        description="Use for one bounded integer rating.",
        question_model=_RatingQuestion,
        response_model=_DetailedRatingResponse,
        normalize=lambda _question, response: {
            "rating": response.rating,
            "reason": response.reason,
        },
    )

    basic_binding = create_clarification_binding(
        DefaultClarificationForm,
        custom_types=(basic,),
    )
    detailed_binding = create_clarification_binding(
        DefaultClarificationForm,
        custom_types=(detailed,),
    )
    assert basic_binding.fingerprint != detailed_binding.fingerprint
