"""认证和用户 HTTP 边界模型"""

from pydantic import BaseModel, ConfigDict, Field

from tinkerfin_studio.auth.types import UserContext


class UserRead(BaseModel):
    """可通过接口返回的用户信息"""

    model_config = ConfigDict(from_attributes=True)

    user_id: int = Field(ge=1, description="用户 ID")
    username: str = Field(description="登录用户名")
    display_name: str = Field(description="展示名称")
    roles: list[str] = Field(default_factory=list, description="用户角色列表")
    disabled: bool = Field(description="是否禁止登录")

    @classmethod
    def from_context(cls, context: UserContext) -> "UserRead":
        """从可信用户上下文构造响应"""

        return cls.model_validate(context)


class LoginRequest(BaseModel):
    """登录请求"""

    username: str = Field(min_length=1, max_length=64, description="登录用户名")
    password: str = Field(min_length=1, max_length=1024, description="明文登录密码")


class LoginResponse(BaseModel):
    """登录成功响应"""

    access_token: str = Field(description="访问令牌")
    token_type: str = Field(default="Bearer", description="令牌类型")
    expires_in: int = Field(ge=1, description="剩余有效秒数")
    user: UserRead = Field(description="当前登录用户")
