"""会话事件与查询投影 ORM 实体"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import Mapped, mapped_column

from tinkerfin_studio.infrastructure.database import Base

_PRIMARY_KEY = BigInteger().with_variant(Integer, "sqlite")
_EVENT_TEXT = LONGTEXT().with_variant(Text, "sqlite")


class ConversationThread(Base):
    """用户可见会话及其最新可信快照"""

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
        {"comment": "用户会话元信息与最新可信前端快照"},
    )

    id: Mapped[int] = mapped_column(
        _PRIMARY_KEY, primary_key=True, autoincrement=True, comment="会话主键"
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="所属用户 ID，由应用层保证存在"
    )
    thread_id: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="外部 AG-UI threadId"
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False, comment="会话标题")
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="idle",
        comment="会话状态：idle/running/waiting_approval/error/deleting",
    )
    last_run_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="最近主 run ID"
    )
    last_model: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="最近主 run 使用的稳定模型 ID"
    )
    last_seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, comment="Messaging 会话流最新已投影序号"
    )
    snapshot_seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, comment="当前快照覆盖到的序号"
    )
    message_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="user 与 assistant 消息数量"
    )
    tool_call_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="Tool 调用开始事件累计数"
    )
    has_pending_interrupt: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, comment="是否存在待处理审批"
    )
    pinned: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, comment="是否置顶"
    )
    snapshot_json: Mapped[dict[str, object] | None] = mapped_column(
        JSON, nullable=True, comment="按 snapshot_seq 生成的完整前端恢复快照"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, comment="创建时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, comment="更新时间"
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(), nullable=True, comment="软删除时间"
    )


class ConversationRun(Base):
    """主 Agent 与子 Agent run 投影"""

    __tablename__ = "conversation_runs"
    __table_args__ = (
        UniqueConstraint(
            "conversation_thread_id", "run_id", name="uq_conversation_runs_thread_run"
        ),
        Index(
            "ix_conversation_runs_thread_started",
            "conversation_thread_id",
            "started_at",
            "id",
        ),
        Index(
            "ix_conversation_runs_thread_status",
            "conversation_thread_id",
            "status",
            "updated_at",
        ),
        Index(
            "ix_conversation_runs_thread_last_main_status",
            "conversation_thread_id",
            "last_main_run_id",
            "status",
        ),
        {"comment": "主 Agent 与子 Agent 运行投影"},
    )

    id: Mapped[int] = mapped_column(
        _PRIMARY_KEY, primary_key=True, autoincrement=True, comment="run 投影主键"
    )
    conversation_thread_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="所属会话主键，由应用层保证存在"
    )
    run_id: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="主请求 runId 或 subagentInvocationId"
    )
    parent_run_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="标准 AG-UI parentRunId"
    )
    origin_main_run_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="首次发现子执行的主请求 run ID"
    )
    last_main_run_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="最近承载子执行事件的主请求 run ID"
    )
    agent_type: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="运行主体：main 或 subagent"
    )
    agent_name: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="运行主体名称"
    )
    graph_task_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="子图 runtime task ID"
    )
    model_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="主 run 使用的稳定模型 ID"
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="preparing",
        comment="run 状态：preparing/running/success/interrupt/error/cancelled",
    )
    input_json: Mapped[dict[str, object] | None] = mapped_column(
        JSON, nullable=True, comment="主 run 的完整请求输入"
    )
    config_json: Mapped[dict[str, object] | None] = mapped_column(
        JSON, nullable=True, comment="checkpoint、模型与 resume 配置投影"
    )
    outcome_json: Mapped[dict[str, object] | None] = mapped_column(
        JSON, nullable=True, comment="最终 AG-UI outcome 或 error"
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, comment="开始时间"
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(), nullable=True, comment="结束时间"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, comment="创建时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, comment="更新时间"
    )


class ConversationEvent(Base):
    """Redis Messaging 已提交事件的 MySQL 事实副本"""

    __tablename__ = "conversation_events"
    __table_args__ = (
        UniqueConstraint(
            "conversation_thread_id", "seq", name="uq_conversation_events_thread_seq"
        ),
        UniqueConstraint(
            "conversation_thread_id",
            "event_id",
            name="uq_conversation_events_thread_event",
        ),
        Index(
            "ix_conversation_events_thread_run_seq",
            "conversation_thread_id",
            "run_id",
            "seq",
        ),
        {"comment": "Redis Messaging 已提交 AG-UI 事件的 MySQL 事实副本"},
    )

    id: Mapped[int] = mapped_column(
        _PRIMARY_KEY, primary_key=True, autoincrement=True, comment="事件主键"
    )
    conversation_thread_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="所属会话主键，由应用层保证存在"
    )
    run_id: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="Messaging producer run ID"
    )
    seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="与 SSE id 一致的严格递增序号"
    )
    event_id: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="Messaging 幂等 message ID"
    )
    event_type: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="AG-UI 事件类型"
    )
    protocol_version: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="ag-ui-protocol@0.1.19",
        comment="AG-UI 协议版本",
    )
    event_json: Mapped[dict[str, object]] = mapped_column(
        JSON, nullable=False, comment="AG-UI 事件 JSON"
    )
    event_text: Mapped[str] = mapped_column(
        _EVENT_TEXT, nullable=False, comment="与 SSE data 完全一致的 UTF-8 JSON"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, comment="Redis 首次提交时间"
    )


class ConversationInterrupt(Base):
    """前端可恢复的 HITL interrupt 投影"""

    __tablename__ = "conversation_interrupts"
    __table_args__ = (
        UniqueConstraint(
            "conversation_thread_id",
            "interrupt_id",
            name="uq_conversation_interrupts_thread_interrupt",
        ),
        Index(
            "ix_conversation_interrupts_thread_status",
            "conversation_thread_id",
            "status",
            "updated_at",
        ),
        {"comment": "前端可恢复的 HITL interrupt 投影"},
    )

    id: Mapped[int] = mapped_column(
        _PRIMARY_KEY, primary_key=True, autoincrement=True, comment="interrupt 投影主键"
    )
    conversation_thread_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="所属会话主键，由应用层保证存在"
    )
    run_id: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="产生 interrupt 的主 run ID"
    )
    resolved_run_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="认领或处理该 interrupt 的 resume run ID"
    )
    interrupt_id: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="AG-UI 公共 interrupt ID"
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="pending",
        comment="pending/resolved/cancelled",
    )
    reason: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="AG-UI interrupt reason"
    )
    message: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="前端审批提示"
    )
    request_json: Mapped[dict[str, object]] = mapped_column(
        JSON, nullable=False, comment="完整公开 interrupt 请求"
    )
    resume_json: Mapped[dict[str, object] | None] = mapped_column(
        JSON, nullable=True, comment="对应 resume 条目"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, comment="创建时间"
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(), nullable=True, comment="解决时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, comment="更新时间"
    )
