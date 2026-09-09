"""用户查询路由"""

from typing import Annotated

from fastapi import APIRouter, Path

from tinkerfin_studio.api.dependencies import AuthServiceDep, UserContextDep
from tinkerfin_studio.api.errors import BusinessException, GlobalErrorCode
from tinkerfin_studio.api.responses import ApiResponse
from tinkerfin_studio.auth.schemas import UserRead, UserUpdate

router = APIRouter(prefix="/user", tags=["用户"])


@router.patch("/me", response_model=ApiResponse[UserRead], summary="修改当前用户信息")
async def update_current_user(
    payload: UserUpdate,
    current_user: UserContextDep,
    auth_service: AuthServiceDep,
) -> ApiResponse[UserRead]:
    """修改当前登录用户明确提交的资料字段"""

    fields = frozenset(payload.model_fields_set)
    if not fields:
        return ApiResponse.success(UserRead.from_context(current_user))
    user = await auth_service.update_user(
        current_user.user_id,
        display_name=payload.display_name,
        avatar_url=payload.avatar_url,
        fields=fields,
    )
    if user is None:
        raise BusinessException(GlobalErrorCode.UNAUTHORIZED)
    return ApiResponse.success(UserRead.from_context(user))


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
