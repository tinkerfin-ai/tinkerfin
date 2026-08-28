"""Agent 模型目录业务服务"""

from pydantic import SecretStr, ValidationError

from tinkerfin_studio.api.errors import (
    BusinessException,
    ModelErrorCode,
    SystemException,
)
from tinkerfin_studio.models.repository import AgentModelRepository
from tinkerfin_studio.models.schemas import (
    AgentModelCatalog,
    AgentModelCatalogItem,
    AgentModelConfig,
    AgentModelWrite,
)


class AgentModelService:
    """维护默认模型并向运行时提供完整连接配置"""

    def __init__(self, repository: AgentModelRepository) -> None:
        self._repository = repository

    async def upsert(self, value: AgentModelWrite) -> None:
        """原子写入模型，并在需要时替换默认模型"""

        try:
            if value.is_default:
                await self._repository.clear_default()
            await self._repository.upsert(value)
            await self._repository.commit()
        except BaseException:
            await self._repository.rollback()
            raise

    async def list_catalog(self) -> AgentModelCatalog:
        """返回不含连接信息和密钥的启用模型目录"""

        models = await self._repository.list_enabled()
        try:
            items = [
                AgentModelCatalogItem.model_validate(
                    {
                        "modelId": model.model_id,
                        "displayName": model.display_name,
                        "reasoningEnabled": model.reasoning_enabled,
                        "runtimeProfile": model.runtime_profile,
                        "isDefault": model.is_default,
                    }
                )
                for model in models
            ]
        except ValidationError as error:
            raise SystemException(ModelErrorCode.CATALOG_UNAVAILABLE) from error
        default = next((item.model_id for item in items if item.is_default), None)
        return AgentModelCatalog(items=items, defaultModelId=default)

    async def resolve(self, model_id: str) -> AgentModelConfig:
        """校验并返回创建请求级 Agent graph 所需的模型连接"""

        model = await self._repository.get(model_id)
        if model is None:
            raise BusinessException(ModelErrorCode.NOT_FOUND)
        if not model.enabled:
            raise BusinessException(ModelErrorCode.DISABLED)
        try:
            return AgentModelConfig.model_validate(
                {
                    "model_id": model.model_id,
                    "display_name": model.display_name,
                    "provider": model.provider,
                    "model_name": model.model_name,
                    "base_url": model.base_url,
                    "api_key": SecretStr(model.api_key),
                    "reasoning_enabled": model.reasoning_enabled,
                    "runtime_profile": model.runtime_profile,
                    "updated_at": model.updated_at.isoformat(),
                }
            )
        except ValidationError as error:
            raise SystemException(ModelErrorCode.CATALOG_UNAVAILABLE) from error
