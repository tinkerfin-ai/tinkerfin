"""Agent 模型配置数据访问"""

from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tinkerfin_studio.models.entity import AgentModel
from tinkerfin_studio.models.schemas import AgentModelWrite


class AgentModelRepository:
    """模型目录查询与事务写入"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_enabled(self) -> list[AgentModel]:
        """按业务排序读取启用模型"""

        result = await self._session.scalars(
            select(AgentModel)
            .where(AgentModel.enabled.is_(True))
            .order_by(AgentModel.sort_order, AgentModel.id)
        )
        return list(result)

    async def get(self, model_id: str) -> AgentModel | None:
        """按稳定 ID 读取模型"""

        return await self._session.scalar(
            select(AgentModel).where(AgentModel.model_id == model_id)
        )

    async def clear_default(self) -> None:
        """在当前事务中清除既有默认标记"""

        await self._session.execute(
            update(AgentModel)
            .where(AgentModel.is_default.is_(True))
            .values(is_default=False, updated_at=datetime.now(UTC).replace(tzinfo=None))
        )

    async def upsert(self, value: AgentModelWrite) -> AgentModel:
        """新增或完整更新一条模型配置"""

        entity = await self.get(value.model_id)
        now = datetime.now(UTC).replace(tzinfo=None)
        if entity is None:
            entity = AgentModel(model_id=value.model_id, created_at=now, updated_at=now)
            self._session.add(entity)
        entity.display_name = value.display_name
        entity.provider = value.provider
        entity.model_name = value.model_name
        entity.base_url = str(value.base_url)
        entity.api_key = value.api_key.get_secret_value()
        entity.reasoning_enabled = value.reasoning_enabled
        entity.enabled = value.enabled
        entity.is_default = value.is_default
        entity.sort_order = value.sort_order
        entity.updated_at = now
        await self._session.flush()
        return entity

    async def commit(self) -> None:
        """提交当前模型目录事务"""

        await self._session.commit()

    async def rollback(self) -> None:
        """回滚当前模型目录事务"""

        await self._session.rollback()
