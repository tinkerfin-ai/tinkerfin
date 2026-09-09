"""模型管理、目录与运行时边界模型"""

from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SecretStr,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_validator,
)

ModelId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    ),
]


class AgentModelWrite(BaseModel):
    """受控模型配置写入使用的边界数据"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    generation_options: dict[str, JsonValue] = Field(
        default_factory=dict,
        description="生图接口附加参数，不得覆盖 model、prompt 或 n",
    )
    purpose: Literal["chat", "image"] = "chat"
    model_id: ModelId = Field(description="前后端使用的稳定模型 ID")
    display_name: str = Field(min_length=1, max_length=128, description="前端展示名称")
    provider: Literal["deepseek", "openai"] = Field(
        description="已安装的 LangChain provider"
    )
    model_name: str = Field(
        min_length=1, max_length=128, description="供应商实际模型名称"
    )
    base_url: str = Field(
        min_length=1, max_length=1024, description="模型服务 API 基础地址"
    )
    api_key: SecretStr = Field(description="写入数据库的明文 API 密钥")
    image_support: Literal["supported", "unsupported", "unknown"] = Field(
        default="unknown", description="已确认的图片输入能力，未知时禁止发送新图片"
    )
    reasoning_enabled: bool = Field(
        default=False, description="是否启用 provider reasoning 参数"
    )
    enabled: bool = Field(default=True, description="是否允许创建新 run")
    is_default: bool = Field(default=False, description="是否设为唯一默认模型")
    sort_order: int = Field(default=0, description="模型目录升序排序值")

    @field_validator("generation_options")
    @classmethod
    def validate_generation_options(
        cls, value: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        """保存和测试共享附加参数的大小、数值及受控字段约束"""
        if {key.casefold() for key in value}.intersection(
            {"model", "prompt", "n", "api_key", "authorization"}
        ):
            raise ValueError("附加参数不能覆盖模型、提示词、数量或认证字段")
        for key in ("size", "output_format"):
            option = value.get(key)
            if key in value and (not isinstance(option, str) or not option.strip()):
                raise ValueError("尺寸和输出格式必须是非空字符串")
        containers: list[tuple[dict[str, JsonValue] | list[JsonValue], int]] = [
            (value, 1)
        ]
        while containers:
            current, depth = containers.pop()
            if depth > 64:
                raise ValueError("附加参数嵌套不能超过 64 层")
            children = current.values() if isinstance(current, dict) else current
            for child in children:
                if isinstance(child, (dict, list)):
                    containers.append((child, depth + 1))
        try:
            encoded = json.dumps(
                value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode("utf-8")
        except (ValueError, UnicodeError) as error:
            raise ValueError("附加参数必须是有效 JSON，不允许非有限数值") from error
        if len(encoded) > 64 * 1024:
            raise ValueError("附加参数不能超过 64 KiB")
        return value

    @model_validator(mode="after")
    def validate_default_is_enabled(self) -> AgentModelWrite:
        if self.is_default and not self.enabled:
            raise ValueError("默认模型必须处于启用状态")
        return self

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        """校验并规范化 HTTP 模型服务地址"""

        return str(TypeAdapter(AnyHttpUrl).validate_python(value))


class AgentModelCatalogItem(BaseModel):
    """返回前端的安全模型目录项"""

    model_config = ConfigDict(populate_by_name=True)

    model_id: str = Field(alias="modelId", description="稳定模型 ID")
    display_name: str = Field(alias="displayName", description="前端展示名称")
    image_support: Literal["supported", "unsupported", "unknown"] = Field(
        default="unknown", alias="imageSupport", description="图片输入能力"
    )
    reasoning_enabled: bool = Field(
        alias="reasoningEnabled", description="是否启用 reasoning"
    )
    is_default: bool = Field(alias="isDefault", description="是否为默认模型")


class AgentModelCatalog(BaseModel):
    """前端模型目录响应"""

    model_config = ConfigDict(populate_by_name=True)

    items: list[AgentModelCatalogItem] = Field(description="启用模型列表")
    default_model_id: str | None = Field(
        default=None, alias="defaultModelId", description="默认模型 ID"
    )


class AgentModelConfig(BaseModel):
    """当前请求使用的完整模型连接配置"""

    model_config = ConfigDict(frozen=True)

    model_id: str
    display_name: str
    provider: Literal["deepseek", "openai"]
    model_name: str
    base_url: str
    api_key: SecretStr
    reasoning_enabled: bool
    generation_options: dict[str, JsonValue] = Field(
        default_factory=dict,
        description="生图接口附加参数，不得覆盖 model、prompt 或 n",
    )
    purpose: Literal["chat", "image"] = "chat"
    image_support: Literal["supported", "unsupported", "unknown"] = "unknown"


class AgentModelSettings(BaseModel):
    """当前用户可编辑的模型信息，任何响应均不包含密钥原文"""

    model_id: str
    display_name: str
    purpose: Literal["chat", "image"]
    provider: Literal["deepseek", "openai"]
    model_name: str
    base_url: str
    generation_options: dict[str, JsonValue]
    has_key: bool
    image_support: Literal["supported", "unsupported", "unknown"]
    reasoning_enabled: bool
    enabled: bool
    is_default: bool
    sort_order: int


class AgentModelSave(AgentModelWrite):
    """设置页提交的模型配置，密钥留空表示保留当前用户已有密钥"""

    api_key: SecretStr = SecretStr("")


ModelTestKind = Literal["basic", "text", "vision", "image"]
ModelTestCode = Literal[
    "model_listed",
    "models_unavailable",
    "model_not_listed",
    "text_received",
    "vision_response_received",
    "image_received",
    "empty_response",
    "invalid_response",
    "response_too_large",
    "timeout",
    "authentication_failed",
    "rate_limited",
    "service_error",
    "network_error",
    "endpoint_not_allowed",
    "invalid_configuration",
]


class ModelTestRequest(BaseModel):
    """测试当前表单草稿，不保存配置或改变模型能力标记"""

    model_config = ConfigDict(extra="forbid")
    kind: ModelTestKind
    configuration: AgentModelSave

    @model_validator(mode="after")
    def validate_test_purpose(self) -> ModelTestRequest:
        if self.kind == "image" and self.configuration.purpose != "image":
            raise ValueError("生图测试需要生图服务配置")
        if self.kind in {"text", "vision"} and self.configuration.purpose != "chat":
            raise ValueError("文字和看图测试需要聊天模型配置")
        return self


class ModelTestImage(BaseModel):
    """测试输入或生图结果的有界内联预览，不创建附件"""

    mime_type: Literal["image/png", "image/jpeg"]
    data_base64: str = Field(
        max_length=350_000, description="不超过 256 KiB 的图片字节的 Base64 编码"
    )


class ModelTestResult(BaseModel):
    """一次独立测试的结果；未确认不代表模型不可用"""

    kind: ModelTestKind
    outcome: Literal["success", "failed", "inconclusive"]
    elapsed_ms: int = Field(ge=0, description="本次测试耗时，单位毫秒")
    code: ModelTestCode
    text: str | None = Field(
        default=None, max_length=2000, description="文字或看图测试的有界模型回复"
    )
    image: ModelTestImage | None = None
