"""认证和用户 HTTP 边界模型"""

from datetime import datetime

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
)

from tinkerfin_studio.auth.types import AuthenticatedSession, UserContext


class UserRead(BaseModel):
    """可通过接口返回的用户信息"""

    model_config = ConfigDict(from_attributes=True)

    user_id: int = Field(ge=1, description="用户 ID")
    username: str = Field(description="登录用户名")
    display_name: str = Field(description="展示名称")
    avatar_url: str | None = Field(description="头像 HTTPS URL")
    roles: list[str] = Field(default_factory=list, description="用户角色列表")
    disabled: bool = Field(description="是否禁止登录")

    @classmethod
    def from_context(cls, context: UserContext) -> "UserRead":
        """从可信用户上下文构造响应"""

        return cls.model_validate(context)


class UserUpdate(BaseModel):
    """当前用户可修改的资料字段"""

    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    avatar_url: str | None = Field(
        default=None,
        max_length=2048,
        description="头像 HTTPS URL；传入 null 时清空头像",
    )

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, value: str | None) -> str:
        """拒绝把必需的展示名称显式清空"""

        if value is None:
            raise ValueError("展示名称不能为 null")
        normalized = value.strip()
        if not normalized:
            raise ValueError("展示名称不能为空")
        return normalized

    @field_validator("avatar_url")
    @classmethod
    def validate_avatar_url(cls, value: str | None) -> str | None:
        """只接受浏览器可直接加载的完整 HTTPS URL"""

        if value is None:
            return None
        normalized = value.strip()
        parsed = TypeAdapter(AnyHttpUrl).validate_python(normalized)
        if parsed.scheme != "https":
            raise ValueError("头像 URL 必须使用 HTTPS")
        return str(parsed)


class LoginRequest(BaseModel):
    """登录请求"""

    username: str = Field(min_length=1, max_length=64, description="登录用户名")
    password: str = Field(min_length=1, max_length=1024, description="明文登录密码")


class AuthSessionRead(BaseModel):
    """后端已确认且具有固定到期时间的登录会话"""

    expires_at: datetime = Field(description="访问令牌的 UTC 固定到期时间")
    user: UserRead = Field(description="当前登录用户")

    @classmethod
    def from_context(cls, context: AuthenticatedSession) -> "AuthSessionRead":
        """从可信认证会话构造响应"""

        return cls(
            expires_at=context.expires_at,
            user=UserRead.from_context(context.user),
        )


class LoginResponse(AuthSessionRead):
    """登录成功响应"""

    access_token: str = Field(description="访问令牌")
    token_type: str = Field(default="Bearer", description="令牌类型")
