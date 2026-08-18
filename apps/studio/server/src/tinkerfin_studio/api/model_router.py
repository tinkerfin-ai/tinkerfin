"""前端可用模型目录路由"""

from fastapi import APIRouter

from tinkerfin_studio.api.dependencies import ModelServiceDep, UserContextDep
from tinkerfin_studio.api.responses import ApiResponse
from tinkerfin_studio.models.schemas import AgentModelCatalog

router = APIRouter(prefix="/models", tags=["模型"])


@router.get("", response_model=ApiResponse[AgentModelCatalog], summary="模型目录")
async def list_models(
    user: UserContextDep,
    service: ModelServiceDep,
) -> ApiResponse[AgentModelCatalog]:
    """返回当前允许创建新 run 的安全模型目录"""

    del user
    return ApiResponse.success(await service.list_catalog())
