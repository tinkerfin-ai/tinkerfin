"""Studio 的 Plan 澄清表单契约"""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from tinkerfin.plan import (
    ClarificationForm,
    ClarificationModel,
    ClarificationOptionBase,
    ClarificationQuestionBase,
)


class StudioPlanOptionAttributes(ClarificationModel):
    """Studio 澄清选项的展示属性"""

    recommended: bool = Field(description="是否为当前问题唯一推荐且位于第一位的选项")


class StudioPlanClarificationOption(ClarificationOptionBase):
    """带有 Studio 展示属性的澄清选项"""

    attributes: StudioPlanOptionAttributes = Field(
        description="Studio 用于渲染选项的展示属性"
    )


class StudioPlanClarificationQuestion(ClarificationQuestionBase):
    """保证推荐项顺序唯一的 Studio 澄清问题"""

    # 冻结元组仅收窄元素类型，运行时仍遵守框架的不可变字段契约
    options: tuple[StudioPlanClarificationOption, ...] = Field(  # pyright: ignore[reportIncompatibleVariableOverride]
        default=(),
        description="模型生成的单选项；第一项必须是唯一推荐项",
    )

    @model_validator(mode="after")
    def recommended_option_is_first(self) -> Self:
        """确保有选项的问题把唯一推荐项放在第一位"""

        if not self.options:
            return self
        if not self.options[0].attributes.recommended:
            raise ValueError("第一个选项必须是推荐项")
        if any(option.attributes.recommended for option in self.options[1:]):
            raise ValueError("除第一个选项外不得标记其他推荐项")
        return self


class StudioPlanClarificationForm(ClarificationForm[StudioPlanClarificationQuestion]):
    """由 Planner 生成并供 Studio 用户回答的澄清表单"""

    title: str = Field(
        description="根据本次澄清问题生成简洁、用户可见的表单标题",
        min_length=1,
        max_length=20,
    )
    description: str = Field(
        description="说明回答这些问题将如何影响本次计划",
        min_length=1,
        max_length=60,
    )


__all__ = ["StudioPlanClarificationForm"]
