"""会话历史、命令和取消接口模型"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

PendingInteractionKind = Literal[
    "tool_approval",
    "plan_clarification",
    "plan_review",
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
    last_seq: int = Field(alias="lastSeq", ge=0)
    message_count: int = Field(alias="messageCount", ge=0)
    tool_call_count: int = Field(alias="toolCallCount", ge=0)
    has_pending_interrupt: bool = Field(alias="hasPendingInterrupt")
    pending_interaction_kind: PendingInteractionKind | None = Field(
        alias="pendingInteractionKind",
        description="待处理交互的业务类型；无待处理 interrupt 时为 null",
    )
    pinned: bool
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(
        alias="updatedAt",
        description="用于历史排序的最近会话活动时间，元信息修改不改变该值",
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


class ConversationEventEnvelope(BaseModel):
    """历史读取接口返回的已提交事件"""

    model_config = ConfigDict(populate_by_name=True)

    seq: int = Field(ge=1)
    event_id: str = Field(alias="eventId")
    event_type: str = Field(alias="eventType")
    run_id: str = Field(alias="runId")
    event: JsonValue
    created_at: datetime = Field(alias="createdAt")


class ConversationHistoryDetail(BaseModel):
    """单个会话的当前快照和尾部事件"""

    model_config = ConfigDict(populate_by_name=True)

    id: int
    thread_id: str = Field(alias="threadId")
    title: str
    status: str
    last_run_id: str | None = Field(default=None, alias="lastRunId")
    last_model: str | None = Field(default=None, alias="lastModel")
    last_seq: int = Field(alias="lastSeq", ge=0)
    snapshot_seq: int = Field(alias="snapshotSeq", ge=0)
    message_count: int = Field(alias="messageCount", ge=0)
    tool_call_count: int = Field(alias="toolCallCount", ge=0)
    has_pending_interrupt: bool = Field(alias="hasPendingInterrupt")
    pending_interaction_kind: PendingInteractionKind | None = Field(
        alias="pendingInteractionKind",
        description="待处理交互的业务类型；无待处理 interrupt 时为 null",
    )
    pinned: bool
    snapshot: dict[str, JsonValue] | None
    events: list[ConversationEventEnvelope]
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(
        alias="updatedAt",
        description="用于历史排序的最近会话活动时间，元信息修改不改变该值",
    )


class ConversationThreadUpdate(BaseModel):
    """重命名或置顶请求"""

    title: str | None = Field(default=None, min_length=1, max_length=255)
    pinned: bool | None = None


class CancelRunResponse(BaseModel):
    """分布式取消结果"""

    cancelled: bool = Field(description="本请求是否发起并完成了取消")
