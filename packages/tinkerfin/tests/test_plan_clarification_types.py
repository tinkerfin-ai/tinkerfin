"""Dynamic Plan clarification question, response, and extension contracts."""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar, Literal, cast

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
    DateTimeQuestion,
    DefaultClarificationForm,
    PlanClarificationResponseError,
    PlanModeConfigurationError,
    TimeQuestion,
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
        "time",
        "datetime",
    }


class _ConfigurableTimeZoneForm(DefaultClarificationForm):
    default_time_zone: ClassVar[str] = "UTC"


def _question_schema(
    schema: dict[str, object],
    *,
    answer_type: str,
) -> dict[str, object]:
    questions = cast(dict[str, object], schema["properties"])["questions"]
    items = cast(dict[str, object], cast(dict[str, object], questions)["items"])
    discriminator = cast(dict[str, object], items["discriminator"])
    mapping = cast(dict[str, str], discriminator["mapping"])
    reference = mapping[answer_type]
    definition = reference.removeprefix("#/$defs/")
    return cast(dict[str, object], cast(dict[str, object], schema["$defs"])[definition])


def test_form_default_time_zone_is_in_schema_and_fingerprint() -> None:
    utc_binding = create_clarification_binding(_ConfigurableTimeZoneForm)
    _ConfigurableTimeZoneForm.default_time_zone = "Asia/Shanghai"
    try:
        shanghai_binding = create_clarification_binding(_ConfigurableTimeZoneForm)
    finally:
        _ConfigurableTimeZoneForm.default_time_zone = "UTC"

    assert utc_binding.fingerprint != shanghai_binding.fingerprint
    schema = cast(
        dict[str, object],
        shanghai_binding.form_schema.model_json_schema(by_alias=True),
    )
    for answer_type in ("time", "datetime"):
        question_schema = _question_schema(schema, answer_type=answer_type)
        properties = cast(dict[str, object], question_schema["properties"])
        assert cast(dict[str, object], properties["timeZone"])["default"] == (
            "Asia/Shanghai"
        )
        assert "timeZone" not in cast(list[str], question_schema["required"])

    form = cast(
        _ConfigurableTimeZoneForm,
        shanghai_binding.form_schema.model_validate(
            {
                "questions": [
                    {
                        "id": "local-time",
                        "answerType": "time",
                        "prompt": "Local time?",
                        "required": True,
                    },
                    {
                        "id": "explicit-zone",
                        "answerType": "datetime",
                        "prompt": "UTC instant?",
                        "required": True,
                        "timeZone": "UTC",
                    },
                ]
            }
        ),
    )

    local_time, explicit_zone = form.questions
    assert isinstance(local_time, TimeQuestion)
    assert isinstance(explicit_zone, DateTimeQuestion)
    assert local_time.time_zone == "Asia/Shanghai"
    assert explicit_zone.time_zone == "UTC"


def test_invalid_form_default_time_zone_fails_at_definition_creation() -> None:
    class InvalidTimeZoneForm(DefaultClarificationForm):
        default_time_zone: ClassVar[str] = "Mars/Olympus_Mons"

    with pytest.raises(PlanModeConfigurationError, match="default_time_zone"):
        TinkerFin().plan(clarification_schema=InvalidTimeZoneForm)


def test_datetime_question_normalizes_one_unique_zoned_instant() -> None:
    binding = create_clarification_binding(DefaultClarificationForm)
    form = binding.form_schema.model_validate(
        {
            "questions": [
                {
                    "id": "deployment-at",
                    "answerType": "datetime",
                    "prompt": "When should deployment begin?",
                    "required": True,
                    "timeZone": "Asia/Shanghai",
                    "minimum": "2026-08-30T09:00:00",
                    "maximum": "2026-09-30T18:00:00",
                }
            ]
        }
    )
    schema = build_response_schema(binding, form)

    answers = validate_and_normalize_response(
        binding,
        form,
        schema,
        {
            "type": "respond",
            "answers": {
                "deployment-at": {
                    "status": "answered",
                    "answerType": "datetime",
                    "dateTime": "2026-08-30T09:30:00",
                }
            },
        },
    )

    assert answers[0].answer_type == "datetime"
    assert answers[0].value == {
        "localDateTime": "2026-08-30T09:30",
        "timeZone": "Asia/Shanghai",
        "instant": "2026-08-30T01:30:00Z",
    }
    properties = cast(dict[str, JsonValue], schema["properties"])
    answers_schema = cast(dict[str, JsonValue], properties["answers"])
    answer_properties = cast(dict[str, JsonValue], answers_schema["properties"])
    response_schema = cast(dict[str, JsonValue], answer_properties["deployment-at"])
    response_properties = cast(dict[str, JsonValue], response_schema["properties"])
    date_time_schema = cast(dict[str, JsonValue], response_properties["dateTime"])
    assert cast(str, date_time_schema["pattern"]).endswith(r"(?::00)?$")


def test_datetime_question_rejects_dst_gaps_folds_offsets_and_hidden_seconds() -> None:
    binding = create_clarification_binding(DefaultClarificationForm)
    form = binding.form_schema.model_validate(
        {
            "questions": [
                {
                    "id": "deployment-at",
                    "answerType": "datetime",
                    "prompt": "When should deployment begin?",
                    "required": True,
                    "timeZone": "America/New_York",
                }
            ]
        }
    )
    schema = build_response_schema(binding, form)

    for local_datetime in (
        "2026-03-08T02:30:00",
        "2026-11-01T01:30:00",
        "2026-08-30T09:30:01",
        "2026-08-30T09:30:00-04:00",
    ):
        with pytest.raises(PlanClarificationResponseError):
            validate_and_normalize_response(
                binding,
                form,
                schema,
                {
                    "type": "respond",
                    "answers": {
                        "deployment-at": {
                            "status": "answered",
                            "answerType": "datetime",
                            "dateTime": local_datetime,
                        }
                    },
                },
            )

    with pytest.raises(ValidationError, match="minute precision"):
        DateTimeQuestion(
            id="bounded",
            prompt="Choose",
            required=True,
            time_zone="UTC",
            minimum=datetime(2026, 8, 30, 9, 0, 1),
        )


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
        "time",
        "datetime",
    }
    TinkerFin().plan(clarification_schema=_BrandedForm)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_clarification_metadata_requires_finite_json(value: float) -> None:
    with pytest.raises(ValidationError, match="finite number"):
        _NumericAttributes(score=value)


class _RatingQuestion(ClarificationQuestionBase):
    answer_type: Literal["acme:rating"] = "acme:rating"
    maximum: int = Field(ge=1)


class _RatingResponse(ClarificationResponseBase):
    answer_type: Literal["acme:rating"] = "acme:rating"
    rating: int


class _DetailedRatingResponse(ClarificationResponseBase):
    answer_type: Literal["acme:rating"] = "acme:rating"
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
            "answerType": {"const": "acme:rating"},
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
            "answerType": {"const": "acme:rating"},
            "rating": {"$ref": "#/$defs/Bound"},
        },
    }


def test_custom_type_is_one_registration_unit_and_extends_the_default_form() -> None:
    rating = clarification_type(
        type_id="acme:rating",
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
                    "answerType": "acme:rating",
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
                    "answerType": "acme:rating",
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


@pytest.mark.parametrize(
    "type_id",
    [
        "rating",
        "acme:rating.v0",
        "acme:rating.v00",
        "acme:rating.v01",
        "acme:rating.v1",
        "acme:rating.v10",
    ],
)
def test_custom_type_requires_unversioned_namespaced_discriminators(
    type_id: str,
) -> None:
    with pytest.raises(PlanModeConfigurationError, match="unversioned namespaced"):
        clarification_type(
            type_id=type_id,
            description="Rating",
            question_model=_RatingQuestion,
            response_model=_RatingResponse,
            normalize=lambda _question, _response: {"rating": 1},
        )


def test_public_descriptor_constructor_enforces_the_factory_contract() -> None:
    with pytest.raises(PlanModeConfigurationError, match="does not match"):
        ClarificationType(
            type_id="acme:different",
            description="Use for one bounded integer rating.",
            question_model=_RatingQuestion,
            response_model=_RatingResponse,
            bind_response_schema=None,
            validate=None,
            normalize=lambda _question, response: {"rating": response.rating},
        )
    with pytest.raises(PlanModeConfigurationError, match="must not be blank"):
        ClarificationType(
            type_id="acme:rating",
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
        answer_type=(Literal["acme:broken"], "acme:broken"),
        rating=(int, ...),
    )

    with pytest.raises(PlanModeConfigurationError, match="status"):
        clarification_type(
            type_id="acme:broken",
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
        type_id="acme:rating",
        description="Use for one bounded integer rating.",
        question_model=_RatingQuestion,
        response_model=_RatingResponse,
        normalize=lambda _question, response: {"rating": response.rating},
    )

    binding = create_clarification_binding(
        _RatingOnlyForm,
        custom_types=(rating,),
    )

    assert set(binding.types) == {"acme:rating"}


def test_custom_response_definitions_are_scoped_per_question() -> None:
    rating = clarification_type(
        type_id="acme:rating",
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
                    "answerType": "acme:rating",
                    "prompt": "Strict rating?",
                    "required": True,
                    "maximum": 5,
                },
                {
                    "id": "wide",
                    "answerType": "acme:rating",
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
                        "answerType": "acme:rating",
                        "rating": 7,
                    },
                    "wide": {
                        "status": "answered",
                        "answerType": "acme:rating",
                        "rating": 7,
                    },
                },
            },
        )


def test_custom_schema_and_normalized_values_require_finite_json() -> None:
    invalid_schema = clarification_type(
        type_id="acme:rating",
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
                    "answerType": "acme:rating",
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
        type_id="acme:rating",
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
                        "answerType": "acme:rating",
                        "rating": 4,
                    }
                },
            },
        )


def test_binding_fingerprint_covers_the_custom_response_contract() -> None:
    basic = clarification_type(
        type_id="acme:rating",
        description="Use for one bounded integer rating.",
        question_model=_RatingQuestion,
        response_model=_RatingResponse,
        normalize=lambda _question, response: {"rating": response.rating},
    )
    detailed = clarification_type(
        type_id="acme:rating",
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
