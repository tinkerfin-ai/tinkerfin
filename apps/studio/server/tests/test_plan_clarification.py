"""Studio Plan 澄清表单契约测试"""

import pytest
from pydantic import ValidationError

from tinkerfin import TinkerFin
from tinkerfin_studio.agent.plan_clarification import StudioPlanClarificationForm


def _form_payload(
    *,
    recommendations: tuple[bool, ...],
    required: bool = True,
) -> dict[str, object]:
    return {
        "title": "确认分析方向",
        "description": "请先选择最符合目标的分析方向",
        "questions": [
            {
                "id": "topic",
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


def test_studio_plan_form_exposes_required_model_fields() -> None:
    schema = StudioPlanClarificationForm.model_json_schema(by_alias=True)

    assert set(schema["required"]) == {"questions", "title", "description"}
    assert schema["properties"]["schemaVersion"]["const"] == 2
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
    question_schema = schema["$defs"]["StudioPlanClarificationQuestion"]
    assert "required" in question_schema["required"]

    TinkerFin().plan(clarification_schema=StudioPlanClarificationForm)


def test_studio_plan_form_accepts_only_the_first_option_as_recommended() -> None:
    form = StudioPlanClarificationForm.model_validate(
        _form_payload(recommendations=(True, False, False))
    )

    assert form.questions[0].options[0].attributes.recommended is True
    assert all(
        not option.attributes.recommended for option in form.questions[0].options[1:]
    )


@pytest.mark.parametrize(
    ("recommendations", "message"),
    [
        ((False, False), "第一个选项必须是推荐项"),
        ((False, True), "第一个选项必须是推荐项"),
        ((True, True), "除第一个选项外不得标记其他推荐项"),
    ],
)
def test_studio_plan_form_rejects_invalid_recommendation_order(
    recommendations: tuple[bool, ...],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        StudioPlanClarificationForm.model_validate(
            _form_payload(recommendations=recommendations)
        )


def test_studio_plan_form_allows_a_free_text_only_question() -> None:
    form = StudioPlanClarificationForm.model_validate(
        _form_payload(recommendations=(), required=False)
    )

    assert form.questions[0].options == ()
    assert form.questions[0].allow_free_text is True
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
    payload = _form_payload(recommendations=(True, False))
    payload[field] = value

    with pytest.raises(ValidationError):
        StudioPlanClarificationForm.model_validate(payload)
