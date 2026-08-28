"""会话归属、Run 注册与恢复认领 ORM 实体"""

from datetime import datetime

from pydantic import JsonValue
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from tinkerfin_studio.infrastructure.database import Base

_PRIMARY_KEY = BigInteger().with_variant(Integer, "sqlite")


class ConversationThread(Base):
    """用户归属、产品控制与 Trace 列表摘要"""

    __tablename__ = "conversation_threads"
    __table_args__ = (
        UniqueConstraint("thread_id", name="uq_conversation_threads_thread"),
        Index(
            "ix_conversation_threads_user_updated",
            "user_id",
            "deleted_at",
            "updated_at",
            "id",
        ),
        Index(
            "ix_conversation_threads_user_pinned_updated",
            "user_id",
            "deleted_at",
            "pinned",
            "updated_at",
            "id",
        ),
        Index(
            "ix_conversation_threads_user_status_updated",
            "user_id",
            "status",
            "updated_at",
            "id",
        ),
        {"comment": "用户会话归属、产品控制与 Trace 列表摘要"},
    )

    id: Mapped[int] = mapped_column(
        _PRIMARY_KEY, primary_key=True, autoincrement=True, comment="会话主键"
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="所属用户 ID，由应用层保证存在"
    )
    thread_id: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="公开 AG-UI 与 Trace 共用的 threadId"
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False, comment="会话标题")
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="idle",
        comment="列表状态：idle/running/waiting_approval/error/deleting",
    )
    last_run_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="最近主 Run ID"
    )
    last_model: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="最近主 Run 使用的稳定模型 ID"
    )
    message_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="Trace 中 user 与 assistant 消息数"
    )
    tool_call_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="Trace 中 Tool proposal 数"
    )
    has_pending_interrupt: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, comment="Trace 是否存在待处理交互"
    )
    pending_interaction_kind: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="Trace 待处理交互的产品类型"
    )
    pinned: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, comment="是否置顶"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, comment="创建时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, comment="最近 Trace 或用户会话活动时间"
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(), nullable=True, comment="软删除时间"
    )


class ConversationRunRegistration(Base):
    """主 Run 请求幂等、Profile 与业务状态注册"""

    __tablename__ = "conversation_run_registrations"
    __table_args__ = (
        UniqueConstraint(
            "conversation_thread_id",
            "run_id",
            name="uq_conversation_run_registrations_thread_run",
        ),
        Index(
            "ix_conversation_run_registrations_thread_started",
            "conversation_thread_id",
            "started_at",
            "id",
        ),
        Index(
            "ix_conversation_run_registrations_thread_status",
            "conversation_thread_id",
            "status",
            "updated_at",
        ),
        {"comment": "主 Run 请求幂等、Runtime Profile 与业务状态注册"},
    )

    id: Mapped[int] = mapped_column(
        _PRIMARY_KEY, primary_key=True, autoincrement=True, comment="Run 注册主键"
    )
    conversation_thread_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="所属会话主键，由应用层保证存在"
    )
    run_id: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="公开且幂等的主 Run ID"
    )
    parent_run_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="branch 或 resume 来源 Run ID"
    )
    model_id: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="主 Run 使用的稳定模型 ID"
    )
    runtime_profile: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="主 Run 固定使用的 Runtime Profile"
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="preparing",
        comment="preparing/starting/running/waiting/succeeded/failed/cancelled/abandoned",
    )
    input_json: Mapped[dict[str, JsonValue]] = mapped_column(
        JSON, nullable=False, comment="用于同 runId 幂等核验的标准请求"
    )
    config_json: Mapped[dict[str, JsonValue]] = mapped_column(
        JSON, nullable=False, comment="模型与 Runtime Profile 的业务配置快照"
    )
    terminal_outcome: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="Trace 终态结果"
    )
    error_code: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="客户端安全的终态错误码"
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, comment="请求注册时间"
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(), nullable=True, comment="Trace 终态时间"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, comment="创建时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, comment="最近业务状态更新时间"
    )


class ConversationInterruptClaim(Base):
    """不复制 interrupt payload 的恢复请求原子认领与结算"""

    __tablename__ = "conversation_interrupt_claims"
    __table_args__ = (
        UniqueConstraint(
            "conversation_thread_id",
            "interrupt_id",
            name="uq_conversation_interrupt_claims_thread_interrupt",
        ),
        Index(
            "ix_conversation_interrupt_claims_run_status",
            "conversation_thread_id",
            "claimed_run_id",
            "status",
        ),
        {"comment": "由框架恢复事实驱动的 interrupt 原子认领与结算"},
    )

    id: Mapped[int] = mapped_column(
        _PRIMARY_KEY, primary_key=True, autoincrement=True, comment="认领主键"
    )
    conversation_thread_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="所属会话主键，由应用层保证存在"
    )
    interrupt_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="框架从 Checkpointer 解析的公开 interrupt ID",
    )
    source_run_id: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="产生 interrupt 的来源 Run ID"
    )
    claimed_run_id: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="原子认领该 interrupt 的 resume Run ID"
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="claimed",
        comment="claimed/resolved/cancelled",
    )
    resolution_id: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="checkpoint marker 或 Trace abandonment 证据",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, comment="认领创建时间"
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(), nullable=True, comment="框架确认恢复 checkpoint 的时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, comment="最近状态更新时间"
    )


__all__ = [
    "ConversationInterruptClaim",
    "ConversationRunRegistration",
    "ConversationThread",
]
