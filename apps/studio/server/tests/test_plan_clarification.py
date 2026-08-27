"""Studio Plan 澄清表单契约测试"""

import pytest
from pydantic import ValidationError

from tinkerfin import TinkerFin
from tinkerfin_studio.agent.plan_clarification import StudioPlanClarificationForm


def _choice_form_payload(
    *,
    answer_type: str = "single_choice",
    recommendations: tuple[bool, ...] = (True, False),
    required: bool = True,
) -> dict[str, object]:
    return {
        "title": "确认分析方向",
        "description": "请先选择最符合目标的分析方向",
        "questions": [
            {
                "id": "topic",
                "answerType": answer_type,
                "prompt": "这份分析的主题是什么？",
                "required": required,
                "options": [
                    {
                        "id": f"option-{index}",
                        "label": f"选项 {index + 1}",
                        "attributes": {"recommended": recommended},
                    }
                    for index, recommended in enumerate(recommendations)
                ],
            }
        ],
    }


def test_studio_plan_form_exposes_all_builtin_question_types() -> None:
    schema = StudioPlanClarificationForm.model_json_schema(by_alias=True)

    assert set(schema["required"]) == {"questions", "title", "description"}
    assert "schemaVersion" not in schema["properties"]
    assert set(
        schema["properties"]["questions"]["items"]["discriminator"]["mapping"]
    ) == {"single_choice", "multiple_choice", "text", "date"}
    assert schema["properties"]["title"] == {
        "description": "根据本次澄清问题生成简洁、用户可见的表单标题",
        "maxLength": 20,
        "minLength": 1,
        "title": "Title",
        "type": "string",
    }
    assert schema["properties"]["description"] == {
        "description": "说明回答这些问题将如何影响本次计划",
        "maxLength": 60,
        "minLength": 1,
        "title": "Description",
        "type": "string",
    }
    option_attributes = schema["$defs"]["StudioPlanOptionAttributes"]
    assert option_attributes["required"] == ["recommended"]
    assert option_attributes["properties"]["recommended"]["type"] == "boolean"

    TinkerFin().plan(clarification_schema=StudioPlanClarificationForm)


def test_studio_single_choice_requires_only_the_first_option_as_recommended() -> None:
    form = StudioPlanClarificationForm.model_validate(
        _choice_form_payload(recommendations=(True, False, False))
    )

    question = form.questions[0]
    assert question.answer_type == "single_choice"
    assert question.options[0].attributes.recommended is True
    assert all(not option.attributes.recommended for option in question.options[1:])


@pytest.mark.parametrize(
    ("recommendations", "message"),
    [
        ((False, False), "第一个选项必须是推荐项"),
        ((False, True), "第一个选项必须是推荐项"),
        ((True, True), "只能标记第一个选项为推荐项"),
    ],
)
def test_studio_single_choice_rejects_invalid_recommendation_order(
    recommendations: tuple[bool, ...],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        StudioPlanClarificationForm.model_validate(
            _choice_form_payload(recommendations=recommendations)
        )


def test_studio_multiple_choice_allows_several_recommendations() -> None:
    form = StudioPlanClarificationForm.model_validate(
        _choice_form_payload(
            answer_type="multiple_choice",
            recommendations=(True, False, True),
        )
    )

    question = form.questions[0]
    assert question.answer_type == "multiple_choice"
    assert [option.attributes.recommended for option in question.options] == [
        True,
        False,
        True,
    ]


@pytest.mark.parametrize("answer_type", ["text", "date"])
def test_studio_plan_form_accepts_non_choice_questions(answer_type: str) -> None:
    form = StudioPlanClarificationForm.model_validate(
        {
            "title": "确认分析方向",
            "description": "请先补充本次分析所需信息",
            "questions": [
                {
                    "id": "detail",
                    "answerType": answer_type,
                    "prompt": "请补充信息",
                    "required": False,
                }
            ],
        }
    )

    assert form.questions[0].answer_type == answer_type
    assert form.questions[0].required is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", ""),
        ("title", "标" * 21),
        ("description", ""),
        ("description", "说" * 61),
    ],
)
def test_studio_plan_form_rejects_invalid_visible_copy_lengths(
    field: str,
    value: str,
) -> None:
    payload = _choice_form_payload()
    payload[field] = value

    with pytest.raises(ValidationError):
        StudioPlanClarificationForm.model_validate(payload)
