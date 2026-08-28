"""会话列表、Trace 历史、实时跟随和命令接口模型"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from tinkerfin_tracing import (
    TraceCompleteness,
    TraceInteraction,
    TraceMessage,
    TraceNode,
    TraceReasoning,
    TraceState,
    TraceStatus,
    TraceUpdate,
)

PendingInteractionKind = Literal[
    "tool_approval",
    "plan_clarification",
    "plan_review",
    "input_required",
]


class ConversationHistoryListItem(BaseModel):
    """历史列表中的会话摘要"""

    model_config = ConfigDict(populate_by_name=True)

    id: int = Field(ge=1)
    thread_id: str = Field(alias="threadId")
    title: str
    status: str
    last_run_id: str | None = Field(default=None, alias="lastRunId")
    last_model: str | None = Field(default=None, alias="lastModel")
    message_count: int = Field(alias="messageCount", ge=0)
    tool_call_count: int = Field(alias="toolCallCount", ge=0)
    has_pending_interrupt: bool = Field(alias="hasPendingInterrupt")
    pending_interaction_kind: PendingInteractionKind | None = Field(
        alias="pendingInteractionKind",
        description="待处理交互的产品类型；无待处理交互时为 null",
    )
    pinned: bool
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(
        alias="updatedAt",
        description="最近一次权威 Trace 活动时间",
    )


class ConversationHistoryListResponse(BaseModel):
    """历史会话游标分页响应"""

    model_config = ConfigDict(populate_by_name=True)

    items: list[ConversationHistoryListItem]
    next_cursor: str | None = Field(default=None, alias="nextCursor")


class ConversationHistoryGroupConfig(BaseModel):
    """历史会话时间分组配置"""

    model_config = ConfigDict(populate_by_name=True)

    day_ranges: list[int] = Field(
        alias="dayRanges",
        min_length=1,
        description="非今日会话使用的升序最大自然日差",
    )


class ConversationHistoryDetail(BaseModel):
    """用户归属校验后的固定前缀 Trace 会话视图"""

    model_config = ConfigDict(populate_by_name=True)

    id: int = Field(ge=1)
    thread_id: str = Field(alias="threadId")
    title: str
    last_model: str | None = Field(default=None, alias="lastModel")
    runtime_profile: str = Field(alias="runtimeProfile")
    pinned: bool
    as_of_seq: int = Field(alias="asOfSeq", ge=1)
    head_run_id: str = Field(alias="headRunId")
    available_heads: tuple[str, ...] = Field(alias="availableHeads")
    history_cursor: str | None = Field(default=None, alias="historyCursor")
    message_count: int = Field(alias="messageCount", ge=0)
    tool_call_count: int = Field(alias="toolCallCount", ge=0)
    messages: tuple[TraceMessage, ...]
    reasoning: tuple[TraceReasoning, ...]
    nodes: tuple[TraceNode, ...]
    state: TraceState
    interactions: tuple[TraceInteraction, ...]
    status: TraceStatus
    completeness: TraceCompleteness
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(
        alias="updatedAt",
        description="最近一次权威 Trace 活动时间",
    )


class ConversationTraceSnapshotEvent(BaseModel):
    """Trace SSE 建立连接后首先发送的完整权威快照"""

    type: Literal["snapshot"] = "snapshot"
    snapshot: ConversationHistoryDetail


class ConversationTraceUpdateEvent(BaseModel):
    """Trace SSE 在快照之后发送的语义增量"""

    type: Literal["update"] = "update"
    update: TraceUpdate


class ConversationTraceErrorEvent(BaseModel):
    """Trace SSE 已开始后可安全重试的终止信号"""

    type: Literal["error"] = "error"
    code: Literal["trace_unavailable"] = "trace_unavailable"


class ConversationThreadUpdate(BaseModel):
    """重命名或置顶请求"""

    title: str | None = Field(default=None, min_length=1, max_length=255)
    pinned: bool | None = None


class CancelRunResponse(BaseModel):
    """分布式取消结果"""

    cancelled: bool = Field(description="本请求是否发起并完成了取消")


__all__ = [
    "CancelRunResponse",
    "ConversationHistoryDetail",
    "ConversationHistoryGroupConfig",
    "ConversationHistoryListItem",
    "ConversationHistoryListResponse",
    "ConversationThreadUpdate",
    "ConversationTraceErrorEvent",
    "ConversationTraceSnapshotEvent",
    "ConversationTraceUpdateEvent",
    "PendingInteractionKind",
]
