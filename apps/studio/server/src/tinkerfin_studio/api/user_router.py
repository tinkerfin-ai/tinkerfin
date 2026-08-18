"""用户查询路由"""

from typing import Annotated

from fastapi import APIRouter, Path

from tinkerfin_studio.api.dependencies import AuthServiceDep, UserContextDep
from tinkerfin_studio.api.responses import ApiResponse
from tinkerfin_studio.auth.schemas import UserRead

router = APIRouter(prefix="/user", tags=["用户"])


@router.get(
    "/{user_id}", response_model=ApiResponse[UserRead | None], summary="查询用户"
)
async def get_user(
    user_id: Annotated[int, Path(ge=1, description="用户 ID")],
    current_user: UserContextDep,
    auth_service: AuthServiceDep,
) -> ApiResponse[UserRead | None]:
    """按用户 ID 查询安全用户信息"""

    del current_user
    user = await auth_service.get_user(user_id)
    return ApiResponse.success(None if user is None else UserRead.from_context(user))
