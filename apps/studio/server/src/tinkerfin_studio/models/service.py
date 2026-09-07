"""Agent 模型目录业务服务"""

import asyncio
from typing import Literal

import anyio
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
    AgentModelSave,
    AgentModelSettings,
    AgentModelWrite,
)
from tinkerfin_studio.models.transport import validate_model_url


class AgentModelService:
    """维护默认模型并向运行时提供完整连接配置"""

    def __init__(self, repository: AgentModelRepository) -> None:
        self._repository = repository

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
                        "imageSupport": model.image_support,
                        "isDefault": model.is_default,
                    }
                )
                for model in models
            ]
        except ValidationError as error:
            raise SystemException(ModelErrorCode.CATALOG_UNAVAILABLE) from error
        default = next((item.model_id for item in items if item.is_default), None)
        return AgentModelCatalog(items=items, defaultModelId=default)

    async def resolve(
        self, model_id: str, *, purpose: Literal["chat", "image"] = "chat"
    ) -> AgentModelConfig:
        """校验所选模型，返回当前请求需要的连接配置"""

        model = await self._repository.get(model_id)
        if model is None:
            raise BusinessException(ModelErrorCode.NOT_FOUND)
        if not model.enabled:
            raise BusinessException(ModelErrorCode.DISABLED)
        if model.purpose != purpose:
            raise BusinessException(ModelErrorCode.PURPOSE_MISMATCH)
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
                    "image_support": model.image_support,
                    "purpose": model.purpose,
                    "generation_options": model.generation_options,
                }
            )
        except ValidationError as error:
            raise SystemException(ModelErrorCode.CATALOG_UNAVAILABLE) from error

    async def settings(self) -> list[AgentModelSettings]:
        """读取当前用户设置，密钥只返回是否已配置"""
        rows = await self._repository.list_settings()
        return [
            AgentModelSettings.model_validate(
                {
                    "model_id": row.model_id,
                    "display_name": row.display_name,
                    "purpose": row.purpose,
                    "provider": row.provider,
                    "model_name": row.model_name,
                    "base_url": row.base_url,
                    "has_key": bool(row.api_key),
                    "generation_options": row.generation_options,
                    "image_support": row.image_support,
                    "reasoning_enabled": row.reasoning_enabled,
                    "enabled": row.enabled,
                    "is_default": row.is_default,
                    "sort_order": row.sort_order,
                }
            )
            for row in rows
        ]

    async def _configuration_key(self, value: AgentModelSave) -> SecretStr:
        key = value.api_key
        if not key.get_secret_value().strip():
            existing = await self._repository.get(value.model_id)
            if existing is None or not existing.api_key:
                raise BusinessException(ModelErrorCode.KEY_REQUIRED)
            if existing.base_url.rstrip("/") != value.base_url.rstrip("/"):
                raise BusinessException(ModelErrorCode.KEY_ENDPOINT_CHANGED)
            key = SecretStr(existing.api_key)
        return key

    async def resolve_draft(self, value: AgentModelSave) -> AgentModelConfig:
        """读取本人草稿所需密钥，不写入配置；调用方在网络请求前关闭会话"""
        try:
            validate_model_url(value.base_url)
        except ValueError as error:
            raise BusinessException(ModelErrorCode.INVALID_CONFIGURATION) from error
        return AgentModelConfig.model_validate(
            {**value.model_dump(), "api_key": await self._configuration_key(value)}
        )

    async def save_settings(self, value: AgentModelSave) -> None:
        """保存本人配置；本方法拥有用户加锁到提交或回滚的完整事务

        密钥留空只复用本人同 ID、同服务地址的密钥。运行和审批未结束时
        不允许修改配置；取消时先回滚，再向调用方传播取消。

        Args:
            value: 设置页提交的模型配置

        Raises:
            BusinessException: 配置不合法、缺少密钥或模型仍在使用中
        """
        try:
            validate_model_url(value.base_url)
        except ValueError as error:
            raise BusinessException(
                ModelErrorCode.INVALID_CONFIGURATION, message=str(error)
            ) from error
        if {"model", "prompt", "n", "api_key", "authorization"}.intersection(
            value.generation_options
        ):
            raise BusinessException(
                ModelErrorCode.INVALID_CONFIGURATION,
                message="附加参数不能覆盖模型、提示词、数量或认证字段",
            )
        try:
            await self._repository.lock_owner()
            if await self._repository.in_use(value.model_id):
                raise BusinessException(ModelErrorCode.IN_USE)
            key = await self._configuration_key(value)
            write = AgentModelWrite.model_validate(
                {**value.model_dump(), "api_key": key}
            )
            if write.is_default:
                await self._repository.clear_default(write.purpose)
            await self._repository.upsert(write)
            await self._repository.commit()
        except BaseException:
            with anyio.CancelScope(shield=True):
                await self._repository.rollback()
            raise

    async def set_default(self, model_id: str) -> None:
        """启用本人模型并设为同用途默认项，仅影响后续选择

        本方法拥有加锁、提交和回滚的完整事务；连接配置和进行中的运行
        保持不变。请求取消时等待回滚完成，再传播取消。

        Args:
            model_id: 当前用户已保存的模型标识

        Raises:
            BusinessException: 模型不存在或尚未配置密钥
        """
        try:
            await self._repository.lock_owner()
            model = await self._repository.get_for_update(model_id)
            if model is None:
                raise BusinessException(ModelErrorCode.NOT_FOUND)
            if not model.api_key.strip():
                raise BusinessException(
                    ModelErrorCode.KEY_REQUIRED,
                    message="模型需要填写 API 密钥后才能设为默认",
                )
            await self._repository.set_default(model_id, purpose=model.purpose)
            await self._repository.commit()
        except BaseException:
            # 回滚任务由本次操作收回，重复的原生取消也不能提前释放事务所有权
            rollback = asyncio.create_task(self._repository.rollback())
            cancelled = False
            with anyio.CancelScope(shield=True):
                while not rollback.done():
                    try:
                        await asyncio.shield(rollback)
                    except asyncio.CancelledError:
                        cancelled = True
            rollback.result()
            if cancelled:
                raise asyncio.CancelledError() from None
            raise

    async def delete_settings(self, model_id: str) -> None:
        """删除本人模型配置，事务失败或取消时回滚，会话历史保持不变"""
        try:
            await self._repository.lock_owner()
            if await self._repository.in_use(model_id):
                raise BusinessException(ModelErrorCode.IN_USE)
            await self._repository.delete(model_id)
            await self._repository.commit()
        except BaseException:
            with anyio.CancelScope(shield=True):
                await self._repository.rollback()
            raise

    async def resolve_image_model(self) -> AgentModelConfig | None:
        """返回本人启用的默认生图服务，未设置默认项时不调用其他服务"""
        rows = await self._repository.list_settings()
        selected = next(
            (
                row
                for row in rows
                if row.purpose == "image" and row.enabled and row.is_default
            ),
            None,
        )
        if selected is None:
            return None
        return await self.resolve(selected.model_id, purpose="image")
