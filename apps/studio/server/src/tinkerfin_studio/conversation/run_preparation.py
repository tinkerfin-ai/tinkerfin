"""会话请求意图、权威快照、恢复状态与主事件增强"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import NAMESPACE_URL, uuid5

from ag_ui.core import (
    BaseEvent,
    RunErrorEvent,
    RunStartedEvent,
)
from ag_ui.core import Interrupt as AgUiInterrupt
from ag_ui.core.types import ResumeEntry
from langchain.agents.middleware.types import InputAgentState
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from pydantic import JsonValue, ValidationError

from tinkerfin import AgentMode, AgUiResumeBinding, AgUiResumeBindingError, Identity
from tinkerfin_studio.api.errors import BusinessException, ConversationErrorCode
from tinkerfin_studio.conversation.models import ConversationInterrupt
from tinkerfin_studio.conversation.request import ChatRequest

_DEFAULT_TITLE = "新会话"


def conversation_identity(thread_id: str, run_id: str) -> Identity:
    """创建公开生命周期与持久执行共用的会话身份"""

    return Identity(threadId=thread_id, runId=run_id)


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

    input_json: dict[str, JsonValue]
    identity: Identity
    parent_run_id: str | None
    graph_config: RunnableConfig
    message_ids: tuple[str, ...]
    mode: AgentMode


@dataclass(frozen=True, slots=True)
class RegisteredRun:
    """已持久化或已确认附着的主 run"""

    run_id: int
    created: bool
    claimed_interrupt_ids: frozenset[str]


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
    input_json = request.normalized_json(
        thread_id=thread_id,
        message_ids=message_ids,
    )
    identity = conversation_identity(thread_id, request.run_id)
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
        input_json=input_json,
        identity=identity,
        parent_run_id=request.parent_run_id,
        graph_config=graph_config,
        message_ids=message_ids,
        mode=request.forwarded_props.agent_mode,
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
    interrupts: Sequence[ConversationInterrupt],
) -> AgUiResumeBinding:
    """从业务层保存的公开审批事实构造框架恢复 Binding"""

    try:
        return AgUiResumeBinding.from_agui(
            entries=request.resume or (),
            interrupts=tuple(
                AgUiInterrupt.model_validate(entity.request_json)
                for entity in interrupts
            ),
        )
    except (AgUiResumeBindingError, ValidationError) as error:
        message = (
            error.message
            if isinstance(error, AgUiResumeBindingError)
            else "服务端保存的审批状态无法恢复"
        )
        raise BusinessException(
            ConversationErrorCode.RESUME_REQUIRED,
            message=message,
        ) from error


def decorate_main_event(
    event: BaseEvent,
    *,
    prepared: PreparedRunRequest,
    title: str,
) -> BaseEvent:
    """只补充 Studio 产品标题和取消文案，不重写框架协议字段"""

    run_id = prepared.identity.run_id
    if isinstance(event, RunStartedEvent) and event.run_id == run_id:
        return event.model_copy(update={"title": title})
    if isinstance(event, RunErrorEvent):
        raw_event = event.raw_event
        if not isinstance(raw_event, dict):
            return event
        if raw_event.get("runId") != run_id:
            return event
        if event.code == "cancelled":
            return event.model_copy(update={"message": "聊天生成已取消"})
    return event


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
    "PreparedRunRequest",
    "RegisteredRun",
    "ResumeChatIntent",
    "StartChatIntent",
    "bind_start_graph_input",
    "classify_intent",
    "conversation_identity",
    "decorate_main_event",
    "prepare_resume",
    "prepare_run_request",
]
