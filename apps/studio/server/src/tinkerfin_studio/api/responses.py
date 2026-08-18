"""统一 API 响应包络"""

from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    """统一业务响应结构"""

    code: int = Field(default=0, description="业务状态码，0 表示成功")
    message: str = Field(default="success", description="可安全展示的业务消息")
    data: T | None = Field(default=None, description="业务数据")

    @classmethod
    def success(cls, data: T | None = None) -> "ApiResponse[T]":
        """构造成功响应"""

        return cls(data=data)
