"""模型管理、目录与运行时边界模型"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_validator,
)

from tinkerfin_studio.agent.runtime_profiles import RuntimeProfileId

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

    model_config = ConfigDict(frozen=True)

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
    reasoning_enabled: bool = Field(
        default=False, description="是否启用 provider reasoning 参数"
    )
    runtime_profile: RuntimeProfileId = Field(
        description="创建与恢复 Run 使用的已安装 Runtime Profile",
    )
    enabled: bool = Field(default=True, description="是否允许创建新 run")
    is_default: bool = Field(default=False, description="是否设为唯一默认模型")
    sort_order: int = Field(default=0, description="模型目录升序排序值")

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
    reasoning_enabled: bool = Field(
        alias="reasoningEnabled", description="是否启用 reasoning"
    )
    runtime_profile: RuntimeProfileId = Field(
        alias="runtimeProfile", description="模型绑定的 Runtime Profile"
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
    """创建请求级 Agent graph 使用的完整模型连接"""

    model_config = ConfigDict(frozen=True)

    model_id: str
    display_name: str
    provider: Literal["deepseek", "openai"]
    model_name: str
    base_url: str
    api_key: SecretStr
    reasoning_enabled: bool
    runtime_profile: RuntimeProfileId
    updated_at: str
