"""Agent 模型配置 ORM 实体"""

from datetime import UTC, datetime

from pydantic import JsonValue
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from tinkerfin_studio.infrastructure.database import Base


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class AgentModel(Base):
    """数据库管理的可选模型及其明文连接信息"""

    __tablename__ = "agent_models"
    __table_args__ = (
        UniqueConstraint("user_id", "model_id", name="uq_agent_models_owner_model"),
        Index(
            "ix_agent_models_enabled_order", "user_id", "enabled", "sort_order", "id"
        ),
        Index("ix_agent_models_default", "user_id", "purpose", "is_default", "enabled"),
        {"comment": "可由前端选择的 Agent 模型与连接配置"},
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True, comment="模型配置主键"
    )
    generation_options: Mapped[dict[str, JsonValue]] = mapped_column(
        JSON, nullable=False, default=dict, comment="生图接口附加参数，不包含认证信息"
    )
    user_id: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="模型及密钥所属用户 ID"
    )
    purpose: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="chat",
        server_default="chat",
        comment="chat 对话模型或 image 生图服务",
    )
    model_id: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="前后端使用的稳定模型 ID"
    )
    display_name: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="前端展示名称"
    )
    provider: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="已安装的 LangChain provider：deepseek 或 openai",
    )
    model_name: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="供应商实际模型名称"
    )
    base_url: Mapped[str] = mapped_column(
        String(1024), nullable=False, comment="模型服务 API 基础地址"
    )
    api_key: Mapped[str] = mapped_column(
        Text, nullable=False, comment="模型服务明文 API 密钥，禁止通过接口或日志暴露"
    )
    image_support: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="unknown",
        server_default="unknown",
        comment="图片输入能力：supported、unsupported 或 unknown",
    )
    reasoning_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="是否启用已验证的 provider reasoning 参数",
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, comment="是否允许创建新 run"
    )
    is_default: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="是否为前端默认模型，由应用事务保证唯一",
    )
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="模型目录升序排序值"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, default=_utcnow, comment="创建时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
        comment="更新时间",
    )
