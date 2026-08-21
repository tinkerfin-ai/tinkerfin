"""会话请求意图、权威快照、恢复状态与主事件增强"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast
from uuid import NAMESPACE_URL, uuid5

from ag_ui.core import (
    BaseEvent,
    RunAgentInput,
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
)
from ag_ui.core import (
    Interrupt as AgUiInterrupt,
)
from ag_ui.core.types import ResumeEntry
from langchain.agents.middleware.types import InputAgentState
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError, model_validator

from tinkerfin import AgentMode, AgUiResumeBinding, Identity
from tinkerfin_agui_adapter.resume import ResumeMapper, ResumeMappingError
from tinkerfin_messaging import (
    FiniteMessageSource,
    MessageSourceBinding,
    ProfiledDeferredMessageSource,
)
from tinkerfin_studio.api.errors import BusinessException, ConversationErrorCode
from tinkerfin_studio.conversation.models import ConversationInterrupt
from tinkerfin_studio.conversation.request import ChatRequest

_DEFAULT_TITLE = "新会话"


def conversation_identity(user_id: int, thread_id: str, run_id: str) -> Identity:
    """创建用户隔离的 Graph、Messaging 与 checkpoint 运行身份"""

    return Identity(
        threadId=f"users/{user_id}/threads/{thread_id}",
        runId=run_id,
    )


@dataclass(frozen=True, slots=True)
class StartChatIntent:
    """一次普通用户输入及其 Graph 映射"""

    message_index: int
    graph_message: HumanMessage
    title: str


@dataclass(frozen=True, slots=True)
class ResumeChatIntent:
    """一次完整覆盖当前审批批次的恢复请求"""

    entries: tuple[ResumeEntry, ...]


ChatIntent = StartChatIntent | ResumeChatIntent


@dataclass(frozen=True, slots=True)
class PreparedRunRequest:
    """数据库、Messaging、Graph 与主开始事件共用的权威请求事实"""

    protocol_input: RunAgentInput
    input_json: dict[str, JsonValue]
    identity: Identity
    graph_config: RunnableConfig
    message_ids: tuple[str, ...]
    mode: AgentMode


@dataclass(frozen=True, slots=True)
class PreparedResume:
    """互斥且经过校验的恢复执行结果"""

    graph_input: Command | None
    binding: AgUiResumeBinding | None
    persisted_config: dict[str, JsonValue]
    claimed_interrupt_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class RegisteredRun:
    """已持久化或已确认附着的主 run"""

    run_id: int
    created: bool
    claimed_interrupt_ids: frozenset[str]


class _PersistedResumeConfig(BaseModel):
    """数据库中与恢复执行相关的精确配置片段"""

    model_config = ConfigDict(extra="allow", strict=True)

    resume_abandoned: bool = False
    resume_data: dict[str, JsonValue] | None = None
    prior_tool_call_ids: list[str] | None = None

    @model_validator(mode="after")
    def resume_modes_are_exclusive(self) -> _PersistedResumeConfig:
        if self.resume_abandoned and self.resume_data is not None:
            raise ValueError("abandoned 与 command 恢复状态不能同时存在")
        if self.resume_data is not None and self.prior_tool_call_ids is None:
            raise ValueError("command 恢复状态缺少 prior_tool_call_ids")
        return self


def classify_intent(request: ChatRequest) -> ChatIntent:
    """只执行一次普通运行与恢复运行分类"""

    if request.resume is not None:
        if not request.thread_id.strip():
            raise BusinessException(ConversationErrorCode.RESUME_THREAD_ID_REQUIRED)
        if not request.resume:
            raise BusinessException(ConversationErrorCode.RESUME_REQUIRED)
        return ResumeChatIntent(entries=tuple(request.resume))

    for index in range(len(request.messages) - 1, -1, -1):
        message = request.messages[index]
        if message.get("role") != "user":
            continue
        content = message.get("content")
        mapped = _langchain_user_content(content)
        if mapped is None:
            continue
        return StartChatIntent(
            message_index=index,
            graph_message=HumanMessage(content=mapped),
            title=_title_from_content(content),
        )
    raise BusinessException(ConversationErrorCode.USER_MESSAGE_REQUIRED)


def prepare_run_request(
    request: ChatRequest,
    *,
    user_id: int,
    thread_id: str,
) -> PreparedRunRequest:
    """分配服务端消息 ID 并构造一次权威标准请求快照"""

    message_ids = tuple(
        "message-"
        + str(
            uuid5(
                NAMESPACE_URL,
                f"tinkerfin-studio:{user_id}:{thread_id}:{request.run_id}:{index}",
            )
        )
        for index in range(len(request.messages))
    )
    protocol_input = request.normalized(
        thread_id=thread_id,
        message_ids=message_ids,
    )
    input_json = cast(
        dict[str, JsonValue],
        protocol_input.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=False,
        ),
    )
    identity = conversation_identity(user_id, thread_id, request.run_id)
    graph_config: RunnableConfig = {
        "configurable": {
            "thread_id": identity.thread_id,
            "forwarded_props": request.forwarded_props.model_dump(
                mode="json",
                by_alias=True,
            ),
        }
    }
    return PreparedRunRequest(
        protocol_input=protocol_input,
        input_json=input_json,
        identity=identity,
        graph_config=graph_config,
        message_ids=message_ids,
        mode=request.forwarded_props.mode,
    )


def bind_start_graph_input(
    intent: StartChatIntent,
    prepared: PreparedRunRequest,
) -> InputAgentState:
    """为本次选中的用户消息绑定服务端权威消息 ID"""

    message = intent.graph_message.model_copy(
        update={"id": prepared.message_ids[intent.message_index]}
    )
    return InputAgentState(messages=[message])


def prepare_resume(
    request: ChatRequest,
    *,
    identity: Identity,
    existing_config: dict[str, object] | None,
    interrupts: Sequence[ConversationInterrupt],
) -> PreparedResume:
    """解析持久恢复状态或从可信 interrupt 生成新的恢复命令"""

    claimed_ids = frozenset(entry.interrupt_id for entry in request.resume or ())
    if existing_config is not None:
        try:
            persisted = _PersistedResumeConfig.model_validate(existing_config)
        except ValidationError as error:
            raise BusinessException(
                ConversationErrorCode.RESUME_REQUIRED,
                message="服务端保存的审批状态无法恢复",
            ) from error
        if persisted.resume_abandoned:
            return PreparedResume(None, None, {"resume_abandoned": True}, claimed_ids)
        if persisted.resume_data is not None:
            command = Command(resume=persisted.resume_data)
            try:
                binding = AgUiResumeBinding(
                    identity=identity,
                    command=command,
                    prior_tool_call_ids=frozenset(persisted.prior_tool_call_ids or ()),
                )
            except (TypeError, ValueError) as error:
                raise BusinessException(
                    ConversationErrorCode.RESUME_REQUIRED,
                    message="服务端保存的审批状态无法恢复",
                ) from error
            return PreparedResume(
                binding.command,
                binding,
                {
                    "resume_data": persisted.resume_data,
                    "prior_tool_call_ids": list(persisted.prior_tool_call_ids or ()),
                },
                claimed_ids,
            )

    try:
        translation = ResumeMapper().map_agui(
            entries=request.resume or (),
            interrupts=tuple(
                AgUiInterrupt.model_validate(entity.request_json)
                for entity in interrupts
            ),
        )
    except (ResumeMappingError, ValidationError) as error:
        message = (
            error.message
            if isinstance(error, ResumeMappingError)
            else "服务端保存的审批状态无法恢复"
        )
        raise BusinessException(
            ConversationErrorCode.RESUME_REQUIRED,
            message=message,
        ) from error
    if translation.mode == "custom":
        raise BusinessException(ConversationErrorCode.MIXED_RESUME_UNSUPPORTED)
    if translation.mode == "abandon":
        return PreparedResume(None, None, {"resume_abandoned": True}, claimed_ids)
    binding = AgUiResumeBinding.from_translation(
        identity=identity,
        translation=translation,
    )
    return PreparedResume(
        binding.command,
        binding,
        {
            "resume_data": translation.root,
            "prior_tool_call_ids": list(translation.prior_tool_call_ids),
        },
        claimed_ids,
    )


def enrich_main_event(
    event: BaseEvent,
    *,
    prepared: PreparedRunRequest,
    title: str,
) -> BaseEvent:
    """统一补充主运行公开 thread、canonical input、标题和取消文案"""

    run_id = prepared.identity.run_id
    public_thread_id = prepared.protocol_input.thread_id
    if isinstance(event, RunStartedEvent) and event.run_id == run_id:
        raw_event = dict(event.raw_event) if isinstance(event.raw_event, dict) else None
        if raw_event is not None:
            raw_event.update({"threadId": public_thread_id, "runId": run_id})
        return event.model_copy(
            update={
                "thread_id": public_thread_id,
                "input": prepared.protocol_input,
                "title": title,
                "raw_event": raw_event,
            }
        )
    if isinstance(event, RunFinishedEvent) and event.run_id == run_id:
        return event.model_copy(update={"thread_id": public_thread_id})
    if isinstance(event, RunErrorEvent):
        raw_event = dict(event.raw_event) if isinstance(event.raw_event, dict) else {}
        if raw_event.get("runId") != run_id:
            return event
        raw_event.update({"threadId": public_thread_id, "runId": run_id})
        update: dict[str, object] = {"raw_event": raw_event}
        if event.code == "cancelled":
            update["message"] = "聊天生成已取消"
        return event.model_copy(update=update)
    return event


def finite_agui_events(
    events: Sequence[BaseEvent],
    *,
    identity: Identity,
) -> ProfiledDeferredMessageSource[BaseEvent, BaseEvent]:
    """把有限主生命周期发布为无需重复身份参数的 AG-UI profile source"""

    async def open_events() -> MessageSourceBinding[BaseEvent]:
        return MessageSourceBinding(source=FiniteMessageSource.from_events(events))

    return ProfiledDeferredMessageSource(
        open_events,
        identity=identity,
        codec_profile="agui.event.v1",
        source_type=BaseEvent,
        replay_type=BaseEvent,
        cancellable=False,
    )


def _title_from_content(content: JsonValue | None) -> str:
    if isinstance(content, str) and content.strip():
        return content.strip()[:60]
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()[:60]
    return _DEFAULT_TITLE


def _langchain_user_content(
    content: JsonValue | None,
) -> str | list[str | dict[str, object]] | None:
    if isinstance(content, str):
        return content if content.strip() else None
    if not isinstance(content, list):
        return None
    mapped: list[str | dict[str, object]] = []
    for raw_block in content:
        if not isinstance(raw_block, dict):
            continue
        block_type = raw_block.get("type")
        if block_type == "text":
            text = raw_block.get("text")
            if isinstance(text, str) and text:
                mapped.append({"type": "text", "text": text})
            continue
        if block_type in {"image", "audio", "video", "document"}:
            source = raw_block.get("source")
            if not isinstance(source, dict):
                continue
            mapped_type = "file" if block_type == "document" else block_type
            block: dict[str, object] = {"type": mapped_type}
            if source.get("type") == "url":
                block["url"] = source.get("value")
            elif source.get("type") == "data":
                block["base64"] = source.get("value")
            mime_type = source.get("mimeType")
            if isinstance(mime_type, str):
                block["mime_type"] = mime_type
            metadata = raw_block.get("metadata")
            if isinstance(metadata, dict):
                block["extras"] = metadata
            mapped.append(block)
            continue
        if block_type == "binary":
            block = {"type": "file", "mime_type": raw_block.get("mimeType")}
            if isinstance(raw_block.get("id"), str):
                block["file_id"] = raw_block["id"]
            if isinstance(raw_block.get("url"), str):
                block["url"] = raw_block["url"]
            if isinstance(raw_block.get("data"), str):
                block["base64"] = raw_block["data"]
            if isinstance(raw_block.get("filename"), str):
                block["extras"] = {"filename": raw_block["filename"]}
            mapped.append(block)
    return mapped or None


__all__ = [
    "ChatIntent",
    "PreparedResume",
    "PreparedRunRequest",
    "RegisteredRun",
    "ResumeChatIntent",
    "StartChatIntent",
    "bind_start_graph_input",
    "classify_intent",
    "conversation_identity",
    "enrich_main_event",
    "finite_agui_events",
    "prepare_resume",
    "prepare_run_request",
]
