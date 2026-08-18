"""会话历史、命令、chat 与取消业务服务"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import cast
from uuid import NAMESPACE_URL, uuid5

from ag_ui.core import BaseEvent, RunAgentInput, RunErrorEvent, RunStartedEvent
from langchain.agents.middleware.types import InputAgentState
from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, StateSnapshot
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    field_validator,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tinkerfin.agui_native import AgUiNativeStreamConfig
from tinkerfin_agui_adapter.lifecycle import AgUiLifecycleEventFactory
from tinkerfin_agui_adapter.models import AgentRuntimeInterrupt
from tinkerfin_agui_adapter.resume import ResumeMapper, ResumeMappingError
from tinkerfin_messaging.errors import (
    CancellationUnsupported,
    InvalidCursor,
    MessagingError,
    RunAlreadyActive,
    RunIdentityConflict,
    RunNotFound,
    RunProducerFailed,
    StreamDeleteConflict,
)
from tinkerfin_messaging.models import MessageEnvelope
from tinkerfin_messaging.protocols import MessageSource
from tinkerfin_messaging.sources import FiniteMessageSource, map_source
from tinkerfin_studio.agent.factory import ConversationAgentFactory
from tinkerfin_studio.api.errors import (
    BusinessException,
    ConversationErrorCode,
    SystemException,
)
from tinkerfin_studio.auth.types import UserContext
from tinkerfin_studio.conversation.coordinator import (
    ConversationProjectionCoordinator,
)
from tinkerfin_studio.conversation.models import ConversationEvent, ConversationThread
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.conversation.request import ChatRequest
from tinkerfin_studio.conversation.schemas import (
    CancelRunResponse,
    ConversationEventEnvelope,
    ConversationHistoryDetail,
    ConversationHistoryListItem,
    ConversationHistoryListResponse,
)
from tinkerfin_studio.conversation.subagent_events import SubagentRunEventEnricher
from tinkerfin_studio.models.repository import AgentModelRepository
from tinkerfin_studio.models.service import AgentModelService
from tinkerfin_studio.resources import ApplicationResources

_HISTORY_PAGE_SIZE_MAX = 100
_EVENT_LIMIT_MAX = 1000


class _HistoryCursorPayload(BaseModel):
    """历史 keyset 游标的严格边界数据"""

    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)

    pinned: bool = Field(description="游标行是否置顶")
    updated_at: datetime = Field(
        alias="updatedAt", description="游标行的无时区数据库更新时间"
    )
    row_id: int = Field(alias="id", gt=0, description="游标行数据库主键")

    @field_validator("updated_at")
    @classmethod
    def updated_at_is_naive(cls, value: datetime) -> datetime:
        """拒绝不能与数据库无时区字段直接比较的时间"""

        if value.utcoffset() is not None:
            raise ValueError("updatedAt 必须是不含时区的 ISO 时间")
        return value


def conversation_stream_key(user_id: int, thread_id: str) -> str:
    """生成用户隔离的 Messaging、checkpoint 与协调键"""

    return f"users/{user_id}/threads/{thread_id}"


def parse_last_event_id(value: str | None) -> int | None:
    """解析规范十进制 SSE 重连游标"""

    if value is None:
        return None
    if not value.isascii() or not value.isdecimal():
        raise BusinessException(ConversationErrorCode.INVALID_LAST_EVENT_ID)
    parsed = int(value)
    if str(parsed) != value:
        raise BusinessException(ConversationErrorCode.INVALID_LAST_EVENT_ID)
    return parsed


def _history_item(thread: ConversationThread) -> ConversationHistoryListItem:
    return ConversationHistoryListItem(
        id=thread.id,
        threadId=thread.thread_id,
        title=thread.title,
        status=thread.status,
        lastRunId=thread.last_run_id,
        lastModel=thread.last_model,
        lastSeq=thread.last_seq,
        messageCount=thread.message_count,
        toolCallCount=thread.tool_call_count,
        hasPendingInterrupt=thread.has_pending_interrupt,
        pinned=thread.pinned,
        createdAt=thread.created_at,
        updatedAt=thread.updated_at,
    )


def _event_envelope(event: ConversationEvent) -> ConversationEventEnvelope:
    return ConversationEventEnvelope(
        seq=event.seq,
        eventId=event.event_id,
        eventType=event.event_type,
        runId=event.run_id,
        event=cast(JsonValue, event.event_json),
        createdAt=event.created_at,
    )


class ConversationHistoryService:
    """查询前先追赶 Redis 事实日志的会话历史服务"""

    def __init__(
        self,
        repository: ConversationRepository,
        *,
        user_id: int,
        projector: ConversationProjectionCoordinator,
    ) -> None:
        self._repository = repository
        self._user_id = user_id
        self._projector = projector

    async def list_history(
        self,
        *,
        page_size: int,
        cursor: str | None,
    ) -> ConversationHistoryListResponse:
        """按置顶和更新时间稳定分页"""

        resolved = self._decode_cursor(cursor)
        threads = await self._repository.list_threads(
            user_id=self._user_id,
            page_size=min(max(page_size, 1), _HISTORY_PAGE_SIZE_MAX),
            cursor=resolved,
        )
        has_more = len(threads) > page_size
        page = threads[:page_size]
        next_cursor = None
        if has_more and page:
            last = page[-1]
            next_cursor = self._encode_cursor(last)
        return ConversationHistoryListResponse(
            items=[_history_item(thread) for thread in page],
            nextCursor=next_cursor,
        )

    async def get_detail(self, thread_id: str) -> ConversationHistoryDetail:
        """追赶事件后返回可信快照和尾部"""

        thread = await self._require_thread(thread_id)
        await self._reconcile(thread)
        refreshed = await self._require_thread(thread_id)
        events = await self._repository.list_events(
            thread_pk=refreshed.id,
            after_seq=refreshed.snapshot_seq,
            limit=_EVENT_LIMIT_MAX,
        )
        return ConversationHistoryDetail(
            id=refreshed.id,
            threadId=refreshed.thread_id,
            title=refreshed.title,
            status=refreshed.status,
            lastRunId=refreshed.last_run_id,
            lastModel=refreshed.last_model,
            lastSeq=refreshed.last_seq,
            snapshotSeq=refreshed.snapshot_seq,
            snapshotVersion=refreshed.snapshot_version,
            messageCount=refreshed.message_count,
            toolCallCount=refreshed.tool_call_count,
            hasPendingInterrupt=refreshed.has_pending_interrupt,
            pinned=refreshed.pinned,
            snapshot=cast(dict[str, JsonValue] | None, refreshed.snapshot_json),
            events=[_event_envelope(event) for event in events],
            createdAt=refreshed.created_at,
            updatedAt=refreshed.updated_at,
        )

    async def list_events(
        self,
        *,
        thread_id: str,
        after_seq: int | None,
        limit: int,
    ) -> list[ConversationEventEnvelope]:
        """追赶后返回指定序号之后的连续事件"""

        thread = await self._require_thread(thread_id)
        await self._reconcile(thread)
        events = await self._repository.list_events(
            thread_pk=thread.id,
            after_seq=after_seq or 0,
            limit=min(max(limit, 1), _EVENT_LIMIT_MAX),
        )
        return [_event_envelope(event) for event in events]

    async def _reconcile(self, thread: ConversationThread) -> None:
        try:
            await self._projector.reconcile(
                thread_pk=thread.id,
                stream=conversation_stream_key(self._user_id, thread.thread_id),
            )
        except RunNotFound:
            return
        except MessagingError as error:
            raise SystemException(
                ConversationErrorCode.EVENT_PROJECTION_UNAVAILABLE
            ) from error

    async def _require_thread(self, thread_id: str) -> ConversationThread:
        thread = await self._repository.get_thread(
            user_id=self._user_id,
            thread_id=thread_id,
        )
        if thread is None:
            raise BusinessException(ConversationErrorCode.NOT_FOUND)
        return thread

    @staticmethod
    def _encode_cursor(thread: ConversationThread) -> str:
        payload = json.dumps(
            {
                "pinned": thread.pinned,
                "updatedAt": thread.updated_at.isoformat(),
                "id": thread.id,
            },
            separators=(",", ":"),
        )
        return base64.urlsafe_b64encode(payload.encode()).decode()

    @staticmethod
    def _decode_cursor(value: str | None) -> tuple[bool, datetime, int] | None:
        if value is None:
            return None
        try:
            decoded = base64.b64decode(
                value.encode(),
                altchars=b"-_",
                validate=True,
            )
            payload = _HistoryCursorPayload.model_validate_json(decoded, strict=True)
            return payload.pinned, payload.updated_at, payload.row_id
        except (ValueError, TypeError, ValidationError):
            raise BusinessException(ConversationErrorCode.INVALID_CURSOR) from None


class ConversationCommandService:
    """会话元信息更新与跨存储删除服务"""

    def __init__(
        self,
        repository: ConversationRepository,
        *,
        user_id: int,
        resources: ApplicationResources,
    ) -> None:
        self._repository = repository
        self._user_id = user_id
        self._resources = resources

    async def update(
        self,
        *,
        thread_id: str,
        title: str | None,
        pinned: bool | None,
    ) -> ConversationHistoryListItem:
        """更新明确提交的会话元信息"""

        thread = await self._require_thread(thread_id)
        await self._repository.update_thread_meta(
            thread_pk=thread.id,
            title=title,
            pinned=pinned,
        )
        await self._repository.commit()
        refreshed = await self._require_thread(thread_id)
        return _history_item(refreshed)

    async def delete(self, *, thread_id: str) -> None:
        """删除已终止会话的 Messaging、checkpoint 和 MySQL 数据"""

        thread = await self._require_thread(thread_id)
        stream = conversation_stream_key(self._user_id, thread_id)
        await self._resources.conversation_projector.reconcile(
            thread_pk=thread.id,
            stream=stream,
        )
        locked = await self._repository.lock_thread(thread.id)
        if locked is None:
            raise BusinessException(ConversationErrorCode.NOT_FOUND)
        if locked.status == "running" or await self._repository.has_running_run(
            thread.id
        ):
            raise BusinessException(ConversationErrorCode.DELETE_CONFLICT)
        try:
            await self._resources.conversation_channel.delete_stream(stream=stream)
            await self._resources.agent_persistence.checkpointer.adelete_thread(stream)
            await self._repository.delete_thread_cascade(thread.id)
            await self._repository.commit()
        except StreamDeleteConflict as error:
            await self._repository.rollback()
            raise BusinessException(ConversationErrorCode.DELETE_CONFLICT) from error
        except BaseException:
            await self._repository.rollback()
            raise

    async def _require_thread(self, thread_id: str) -> ConversationThread:
        thread = await self._repository.get_thread(
            user_id=self._user_id,
            thread_id=thread_id,
        )
        if thread is None:
            raise BusinessException(ConversationErrorCode.NOT_FOUND)
        return thread


@dataclass(frozen=True, slots=True)
class PreparedChat:
    """完成 Messaging 预握手后的 HTTP SSE 内容"""

    body: AsyncIterator[bytes]
    thread_id: str


class ConversationChatService:
    """创建请求级 graph 并启动或附着 durable AG-UI run"""

    def __init__(
        self,
        session: AsyncSession,
        *,
        user: UserContext,
        resources: ApplicationResources,
    ) -> None:
        self._session = session
        self._user = user
        self._resources = resources
        self._repository = ConversationRepository(session)

    async def start(
        self,
        request: ChatRequest,
        *,
        last_event_id: str | None,
    ) -> PreparedChat:
        """完成业务校验、Graph 创建和 Messaging 预握手"""

        after = parse_last_event_id(last_event_id)
        latest_user_message = self._validate_mode(request)
        model = await AgentModelService(AgentModelRepository(self._session)).resolve(
            request.forwarded_props.model
        )
        thread = await self._resolve_thread(
            request,
            None if latest_user_message is None else latest_user_message[1],
        )
        message_ids = tuple(
            f"message-{uuid5(NAMESPACE_URL, f'tinkerfin-studio:{self._user.user_id}:{thread.thread_id}:{request.run_id}:{index}')}"
            for index in range(len(request.messages))
        )
        normalized = request.normalized(
            thread_id=thread.thread_id,
            message_ids=message_ids,
        )
        run_input = RunAgentInput.model_validate(normalized)
        stream = conversation_stream_key(self._user.user_id, thread.thread_id)
        config: RunnableConfig = {
            "configurable": {
                "thread_id": stream,
                "forwarded_props": request.forwarded_props.model_dump(
                    mode="json",
                    by_alias=True,
                ),
            }
        }
        factory = ConversationAgentFactory(
            persistence=self._resources.agent_persistence,
            sandbox_manager=self._resources.sandbox_manager,
            tavily_api_key=(
                None
                if self._resources.settings.tavily_api_key is None
                else self._resources.settings.tavily_api_key.get_secret_value()
            ),
        )
        graph = await factory.create(
            user_id=self._user.user_id,
            model_config=model,
        )

        existing = await self._repository.get_run(
            thread_pk=thread.id,
            run_id=request.run_id,
        )
        if existing is not None and existing.input_json != normalized:
            raise BusinessException(ConversationErrorCode.RUN_IDENTITY_CONFLICT)
        claimed_interrupt_ids: frozenset[str] = frozenset()
        if request.resume is not None and existing is None:
            try:
                await self._resources.conversation_projector.reconcile(
                    thread_pk=thread.id,
                    stream=stream,
                )
            except MessagingError as error:
                raise self._messaging_error(error) from error
        locked_thread = await self._repository.lock_thread(thread.id)
        if locked_thread is None:
            await self._repository.rollback()
            raise BusinessException(ConversationErrorCode.NOT_FOUND)
        thread = locked_thread
        if request.resume is not None and existing is None:
            claimed_interrupt_ids = frozenset(
                entry.interrupt_id for entry in request.resume
            )
            claimed = await self._repository.claim_pending_interrupts(
                thread_pk=thread.id,
                run_id=request.run_id,
                interrupt_ids=claimed_interrupt_ids,
            )
            raced_existing = await self._repository.get_run_for_update(
                thread_pk=thread.id,
                run_id=request.run_id,
            )
            claimed_ids = {entity.interrupt_id for entity in claimed}
            if raced_existing is not None and raced_existing.input_json != normalized:
                await self._repository.rollback()
                raise BusinessException(ConversationErrorCode.RUN_IDENTITY_CONFLICT)
            if any(entity.resolved_run_id != request.run_id for entity in claimed):
                await self._repository.rollback()
                raise BusinessException(ConversationErrorCode.RESUME_ALREADY_CLAIMED)
            if claimed_ids != claimed_interrupt_ids:
                await self._repository.rollback()
                raise BusinessException(
                    ConversationErrorCode.RESUME_REQUIRED,
                    message="resume 包含不存在或尚未就绪的 interruptId",
                )
            if raced_existing is not None:
                existing = raced_existing
            elif any(entity.status != "pending" for entity in claimed):
                await self._repository.rollback()
                raise BusinessException(ConversationErrorCode.RESUME_ALREADY_CLAIMED)
        graph_input: InputAgentState | Command | None
        prior_tool_call_ids: frozenset[str] = frozenset()
        resume_config: dict[str, object] = {}
        try:
            if request.resume is None:
                message_index, message_content = cast(
                    tuple[int, str], latest_user_message
                )
                graph_input = InputAgentState(
                    messages=[
                        HumanMessage(
                            content=message_content,
                            id=message_ids[message_index],
                        )
                    ]
                )
            else:
                (
                    graph_input,
                    prior_tool_call_ids,
                    resume_config,
                ) = await self._resume_input(
                    graph,
                    request,
                    config,
                    existing_config=(
                        None if existing is None else existing.config_json
                    ),
                )
        except BaseException:
            if claimed_interrupt_ids:
                await self._repository.rollback()
            raise

        created_run = existing is None
        if existing is None:
            canonical_thread_id = thread.thread_id
            try:
                async with self._session.begin_nested():
                    existing = await self._repository.create_main_run(
                        thread_id=thread.id,
                        run_id=request.run_id,
                        parent_run_id=request.parent_run_id,
                        model_id=model.model_id,
                        input_json=cast(dict[str, object], normalized),
                        config_json={
                            "thread_id": stream,
                            "model_id": model.model_id,
                            "model_updated_at": model.updated_at,
                            **resume_config,
                        },
                    )
            except IntegrityError:
                await self._repository.rollback()
                thread = await self._repository.get_thread(
                    user_id=self._user.user_id,
                    thread_id=canonical_thread_id,
                )
                if thread is None:
                    raise
                existing = await self._repository.get_run(
                    thread_pk=thread.id,
                    run_id=request.run_id,
                )
                if existing is None:
                    raise
                locked_thread = await self._repository.lock_thread(thread.id)
                if locked_thread is None:
                    await self._repository.rollback()
                    raise BusinessException(ConversationErrorCode.NOT_FOUND)
                thread = locked_thread
                created_run = False
            if existing.input_json != normalized:
                raise BusinessException(ConversationErrorCode.RUN_IDENTITY_CONFLICT)
            if created_run:
                thread.last_model = model.model_id
                await self._repository.commit()

        async def cleanup_unstarted_run() -> None:
            if not created_run:
                await self._repository.rollback()
                return
            if existing is None:
                return
            await self._repository.release_pending_interrupt_claims(
                thread_pk=thread.id,
                run_id=request.run_id,
                interrupt_ids=claimed_interrupt_ids,
            )
            await self._repository.delete_run(existing.id)
            await self._repository.commit()

        cancel_run = None
        events: MessageSource[BaseEvent]
        try:
            if graph_input is None:
                lifecycle = AgUiLifecycleEventFactory()
                started = lifecycle.started(
                    thread_id=thread.thread_id,
                    run_id=request.run_id,
                ).model_copy(update={"input": run_input, "title": thread.title})
                events = FiniteMessageSource.from_events(
                    (
                        started,
                        lifecycle.failed(
                            run_id=request.run_id,
                            message="审批已取消",
                            code="resume_cancelled",
                        ),
                    )
                )
            else:
                invocation = AgUiNativeStreamConfig().bind(
                    graph.astream,
                    graph_input,
                    config=config,
                )
                run = self._resources.tinkerfin.run(
                    invocation,
                    principal=stream,
                )
                agent_events = run.astream_agui(
                    thread_id=thread.thread_id,
                    run_id=request.run_id,
                    parent_run_id=request.parent_run_id,
                    prior_tool_call_ids=prior_tool_call_ids,
                    expose_reasoning_events=False,
                    expose_subagent_events=True,
                )
                enrich_subagent_runs = SubagentRunEventEnricher(
                    user_id=self._user.user_id,
                    thread_id=thread.thread_id,
                    main_run_id=request.run_id,
                )

                def attach_run_metadata(event: BaseEvent) -> BaseEvent:
                    """发布服务端会话元数据与子 Agent 身份"""

                    event = enrich_subagent_runs(event)
                    if (
                        isinstance(event, RunStartedEvent)
                        and event.run_id == request.run_id
                    ):
                        return event.model_copy(
                            update={"input": run_input, "title": thread.title}
                        )
                    return event

                events = map_source(agent_events, attach_run_metadata)

                async def cancel_agent_run() -> list[BaseEvent]:
                    tail = await agent_events.abort()
                    return [self._localize_cancel(event) for event in tail]

                cancel_run = cancel_agent_run
        except BaseException:
            await cleanup_unstarted_run()
            raise

        async def wake_projection(envelope: MessageEnvelope) -> None:
            """按已提交序号唤醒独立投影消费者"""

            self._resources.conversation_projector.ensure(
                thread_pk=thread.id,
                stream=envelope.stream,
                run=envelope.run,
            )

        preflight = asyncio.create_task(
            self._resources.conversation_channel.sse(
                events,
                stream=stream,
                run=request.run_id,
                after=after,
                attach_identity={"user_id": self._user.user_id, "input": normalized},
                cancel=cancel_run,
                on_committed=wake_projection,
            ),
            name=f"studio-conversation-preflight:{request.run_id}",
        )
        try:
            body = await asyncio.shield(preflight)
        except asyncio.CancelledError as cancellation:
            try:
                body = await preflight
            except asyncio.CancelledError:
                await cleanup_unstarted_run()
            except Exception as preflight_error:  # noqa: BLE001 - 预握手失败必须释放未启动 run
                await cleanup_unstarted_run()
                cancellation.add_note(
                    "Messaging 预握手在 HTTP 取消后失败: "
                    f"{type(preflight_error).__name__}"
                )
            else:
                if not created_run:
                    await self._repository.commit()
                close = getattr(body, "aclose", None)
                if close is not None:
                    try:
                        await close()
                    except Exception as close_error:  # noqa: BLE001 - 调用方取消优先于订阅关闭失败
                        cancellation.add_note(
                            f"SSE 订阅关闭失败: {type(close_error).__name__}"
                        )
            raise cancellation
        except MessagingError as error:
            await cleanup_unstarted_run()
            raise self._messaging_error(error) from error
        except (TypeError, ValueError):
            await cleanup_unstarted_run()
            raise
        if not created_run:
            await self._repository.commit()
        return PreparedChat(body=body, thread_id=thread.thread_id)

    async def cancel(self, *, thread_id: str, run_id: str) -> CancelRunResponse:
        """验证用户归属后请求并等待 durable run 取消"""

        thread = await self._repository.get_thread(
            user_id=self._user.user_id,
            thread_id=thread_id,
        )
        if thread is None:
            raise BusinessException(ConversationErrorCode.NOT_FOUND)
        if await self._repository.get_run(thread_pk=thread.id, run_id=run_id) is None:
            raise BusinessException(ConversationErrorCode.RUN_NOT_FOUND)
        try:
            cancelled = await self._resources.conversation_channel.cancel(
                stream=conversation_stream_key(self._user.user_id, thread_id),
                run=run_id,
            )
        except RunNotFound as error:
            raise BusinessException(ConversationErrorCode.RUN_NOT_FOUND) from error
        except CancellationUnsupported as error:
            raise BusinessException(
                ConversationErrorCode.RUN_CANCEL_UNSUPPORTED
            ) from error
        except RunProducerFailed as error:
            raise SystemException(ConversationErrorCode.RUN_CANCEL_FAILED) from error
        await self._resources.conversation_projector.reconcile(
            thread_pk=thread.id,
            stream=conversation_stream_key(self._user.user_id, thread_id),
        )
        return CancelRunResponse(cancelled=cancelled)

    async def _resolve_thread(
        self,
        request: ChatRequest,
        latest_user_message: str | None,
    ) -> ConversationThread:
        thread_id = request.thread_id.strip()
        if thread_id:
            thread = await self._repository.get_thread(
                user_id=self._user.user_id,
                thread_id=thread_id,
            )
            if thread is None:
                raise BusinessException(ConversationErrorCode.NOT_FOUND)
            return thread
        if latest_user_message is None:
            raise BusinessException(ConversationErrorCode.USER_MESSAGE_REQUIRED)
        generated_thread_id = f"thread-{uuid5(NAMESPACE_URL, f'tinkerfin-studio:{self._user.user_id}:run:{request.run_id}')}"
        thread = await self._repository.get_thread(
            user_id=self._user.user_id,
            thread_id=generated_thread_id,
        )
        if thread is not None:
            return thread
        try:
            async with self._session.begin_nested():
                thread = await self._repository.create_thread(
                    user_id=self._user.user_id,
                    thread_id=generated_thread_id,
                    title=latest_user_message.strip()[:60],
                    model_id=request.forwarded_props.model,
                )
        except IntegrityError:
            await self._repository.rollback()
            thread = await self._repository.get_thread(
                user_id=self._user.user_id,
                thread_id=generated_thread_id,
            )
            if thread is None:
                raise
        await self._repository.commit()
        return thread

    @staticmethod
    def _validate_mode(request: ChatRequest) -> tuple[int, str] | None:
        if request.resume is not None:
            if not request.thread_id.strip():
                raise BusinessException(ConversationErrorCode.RESUME_THREAD_ID_REQUIRED)
            if not request.resume:
                raise BusinessException(ConversationErrorCode.RESUME_REQUIRED)
            return None
        for index in range(len(request.messages) - 1, -1, -1):
            message = request.messages[index]
            if message.role != "user":
                continue
            content = message.content
            if isinstance(content, str) and content.strip():
                return index, content
        raise BusinessException(ConversationErrorCode.USER_MESSAGE_REQUIRED)

    async def _resume_input(
        self,
        graph: CompiledStateGraph,
        request: ChatRequest,
        config: RunnableConfig,
        *,
        existing_config: dict[str, object] | None,
    ) -> tuple[Command | None, frozenset[str], dict[str, object]]:
        if (
            existing_config is not None
            and existing_config.get("resume_abandoned") is True
        ):
            return None, frozenset(), {"resume_abandoned": True}
        if existing_config is not None and isinstance(
            existing_config.get("resume_data"), dict
        ):
            prior = existing_config.get("prior_tool_call_ids", [])
            return (
                Command(resume=cast(dict[str, object], existing_config["resume_data"])),
                frozenset(value for value in prior if isinstance(value, str))
                if isinstance(prior, list)
                else frozenset(),
                {
                    "resume_data": existing_config["resume_data"],
                    "prior_tool_call_ids": prior,
                },
            )
        snapshot = await graph.aget_state(config, subgraphs=True)
        messages_by_namespace: dict[tuple[str, ...], tuple[BaseMessage, ...]] = {}

        def visit(current: StateSnapshot) -> None:
            configurable = current.config.get("configurable", {})
            raw_namespace = configurable.get("checkpoint_ns", "")
            if not isinstance(raw_namespace, str):
                raise TypeError("checkpoint namespace 必须是字符串")
            namespace = tuple(raw_namespace.split("|")) if raw_namespace else ()
            values = current.values
            raw_messages = (
                values.get("messages", ()) if isinstance(values, Mapping) else ()
            )
            messages_by_namespace[namespace] = (
                tuple(
                    message
                    for message in raw_messages
                    if isinstance(message, BaseMessage)
                )
                if isinstance(raw_messages, Sequence)
                else ()
            )
            for task in current.tasks:
                if isinstance(task.state, StateSnapshot):
                    visit(task.state)

        visit(snapshot)
        try:
            translation = ResumeMapper().map(
                entries=request.resume or (),
                interrupts=tuple(
                    AgentRuntimeInterrupt.model_validate(value)
                    for value in snapshot.interrupts
                ),
                messages_by_namespace=messages_by_namespace,
            )
        except ResumeMappingError as error:
            raise BusinessException(
                ConversationErrorCode.RESUME_REQUIRED,
                message=error.message,
            ) from error
        if translation.mode == "custom":
            raise BusinessException(ConversationErrorCode.MIXED_RESUME_UNSUPPORTED)
        if translation.mode == "abandon":
            return None, frozenset(), {"resume_abandoned": True}
        return (
            Command(resume=translation.root),
            frozenset(translation.prior_tool_call_ids),
            {
                "resume_data": translation.root,
                "prior_tool_call_ids": list(translation.prior_tool_call_ids),
            },
        )

    @staticmethod
    def _localize_cancel(event: BaseEvent) -> BaseEvent:
        if isinstance(event, RunErrorEvent) and event.code == "cancelled":
            return event.model_copy(update={"message": "聊天生成已取消"})
        return event

    @staticmethod
    def _messaging_error(error: MessagingError) -> BusinessException | SystemException:
        if isinstance(error, InvalidCursor):
            return BusinessException(ConversationErrorCode.INVALID_LAST_EVENT_ID)
        if isinstance(error, RunAlreadyActive):
            return BusinessException(ConversationErrorCode.RUN_CONFLICT)
        if isinstance(error, RunIdentityConflict):
            return BusinessException(ConversationErrorCode.RUN_IDENTITY_CONFLICT)
        if isinstance(error, RunNotFound):
            return BusinessException(ConversationErrorCode.RUN_NOT_FOUND)
        return SystemException(ConversationErrorCode.AGENT_UNAVAILABLE)
