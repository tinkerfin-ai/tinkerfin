"""AG-UI chat 请求的 Studio 协议边界"""

from __future__ import annotations

from typing import Literal

from ag_ui.core.types import (
    Context,
    ResumeEntry,
    Tool,
)
from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

THREAD_ID_PATTERN = r"^[^:]+\z"
OPTIONAL_THREAD_ID_PATTERN = r"^[^:]*\z"


class ConversationForwardedProps(BaseModel):
    """前端传给一次 run 的扩展属性"""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    model: str = Field(min_length=1, max_length=64, description="数据库模型稳定 ID")
    mode: Literal["default", "plan"] = Field(
        default="default", description="前端 Agent 模式，仅透传不参与业务分支"
    )


class ChatUserMessage(BaseModel):
    """尚未分配服务端消息 ID 的用户输入"""

    model_config = ConfigDict(extra="forbid")

    role: Literal["user"] = Field(default="user", description="本次输入固定为用户消息")
    content: str = Field(min_length=1, description="用户提交的文本内容")


class ChatRequest(BaseModel):
    """Studio 锁定的 AG-UI run 输入"""

    model_config = ConfigDict(populate_by_name=True)

    thread_id: str = Field(
        alias="threadId",
        default="",
        max_length=128,
        pattern=OPTIONAL_THREAD_ID_PATTERN,
        description="新会话可为空；冒号会破坏 checkpoint key，禁止使用",
    )
    run_id: str = Field(
        alias="runId", min_length=1, max_length=128, description="当前主 run ID"
    )
    parent_run_id: str | None = Field(
        default=None,
        alias="parentRunId",
        min_length=1,
        max_length=128,
        description="标准 AG-UI 父 run ID",
    )
    state: JsonValue = Field(description="客户端状态快照，仅持久化和透传")
    messages: list[ChatUserMessage] = Field(description="不含客户端 ID 的本次用户输入")
    tools: list[Tool] = Field(description="客户端工具定义，仅持久化和透传")
    context: list[Context] = Field(description="AG-UI 上下文，仅持久化和透传")
    forwarded_props: ConversationForwardedProps = Field(
        alias="forwardedProps", description="模型与前端扩展属性"
    )
    resume: list[ResumeEntry] | None = Field(default=None, description="HITL 恢复条目")

    @field_validator("thread_id", "run_id", "parent_run_id")
    @classmethod
    def identifiers_are_canonical(cls, value: str | None) -> str | None:
        """拒绝后续技术边界不会接受的首尾空白身份"""

        if value is not None and value != value.strip():
            raise ValueError("身份字段不得包含首尾空白")
        return value

    def normalized(
        self,
        *,
        thread_id: str,
        message_ids: tuple[str, ...],
    ) -> dict[str, JsonValue]:
        """返回绑定服务端会话与消息 ID 的完整 AG-UI 输入"""

        if len(message_ids) != len(self.messages) or any(
            not value for value in message_ids
        ):
            raise ValueError("message_ids 必须完整覆盖本次用户输入")

        payload = self.model_dump(mode="json", by_alias=True)
        payload["threadId"] = thread_id
        payload["messages"] = [
            {
                "id": message_id,
                **message.model_dump(mode="json", by_alias=True),
            }
            for message_id, message in zip(message_ids, self.messages, strict=True)
        ]
        return payload
