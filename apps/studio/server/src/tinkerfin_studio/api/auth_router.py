"""登录、当前用户和登出路由"""

from fastapi import APIRouter

from tinkerfin_studio.api.dependencies import (
    AuthServiceDep,
    RawTokenDep,
    UserContextDep,
)
from tinkerfin_studio.api.responses import ApiResponse
from tinkerfin_studio.auth.schemas import LoginRequest, LoginResponse, UserRead

router = APIRouter(prefix="/auth", tags=["认证"])


@router.post("/login", response_model=ApiResponse[LoginResponse], summary="登录")
async def login(
    form: LoginRequest,
    auth_service: AuthServiceDep,
) -> ApiResponse[LoginResponse]:
    """校验用户名密码并返回访问令牌"""

    result = await auth_service.login(form.username, form.password)
    return ApiResponse.success(
        LoginResponse(
            access_token=result.access_token,
            expires_in=result.expires_in,
            user=UserRead.from_context(result.user),
        )
    )


@router.get("/me", response_model=ApiResponse[UserRead], summary="当前用户")
async def me(user: UserContextDep) -> ApiResponse[UserRead]:
    """返回当前登录用户"""

    return ApiResponse.success(UserRead.from_context(user))


@router.post("/logout", response_model=ApiResponse[None], summary="登出")
async def logout(
    user: UserContextDep,
    token: RawTokenDep,
    auth_service: AuthServiceDep,
) -> ApiResponse[None]:
    """撤销当前访问令牌"""

    del user
    if token is not None:
        await auth_service.logout(token)
    return ApiResponse.success()
