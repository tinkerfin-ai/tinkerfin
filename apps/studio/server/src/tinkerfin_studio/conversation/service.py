"""会话 chat 与取消服务"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TypeVar

from ag_ui.core import BaseEvent
from langchain.agents.middleware.types import InputAgentState
from langgraph.types import Command
from sqlalchemy.ext.asyncio import AsyncSession

from tinkerfin_agui_adapter.lifecycle import AgUiLifecycleEventFactory
from tinkerfin_messaging.errors import (
    MessagingError,
    MessagingErrorCode,
)
from tinkerfin_messaging.models import MessageEnvelope
from tinkerfin_messaging.protocols import ProfiledMessageSource
from tinkerfin_studio.agent.factory import ConversationAgentFactory
from tinkerfin_studio.api.errors import (
    BusinessException,
    ConversationErrorCode,
    SystemException,
)
from tinkerfin_studio.auth.types import UserContext
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.conversation.request import ChatRequest
from tinkerfin_studio.conversation.run_preparation import (
    PreparedRunRequest,
    ResumeChatIntent,
    StartChatIntent,
    bind_start_graph_input,
    classify_intent,
    conversation_identity,
    enrich_main_event,
    finite_agui_events,
    prepare_run_request,
)
from tinkerfin_studio.conversation.run_registration import (
    ConversationRunPreparer,
    PreparedExecution,
)
from tinkerfin_studio.conversation.schemas import (
    CancelRunResponse,
)
from tinkerfin_studio.models.repository import AgentModelRepository
from tinkerfin_studio.models.schemas import AgentModelConfig
from tinkerfin_studio.models.service import AgentModelService
from tinkerfin_studio.resources import ApplicationResources

_TaskResult = TypeVar("_TaskResult")

_MESSAGING_ERRORS: dict[
    MessagingErrorCode,
    tuple[ConversationErrorCode, bool],
] = {
    MessagingErrorCode.ERROR: (ConversationErrorCode.MESSAGING_FAILURE, False),
    MessagingErrorCode.NOT_STARTED: (
        ConversationErrorCode.MESSAGING_FAILURE,
        False,
    ),
    MessagingErrorCode.SETTLEMENT_TIMEOUT: (
        ConversationErrorCode.MESSAGING_UNAVAILABLE,
        False,
    ),
    MessagingErrorCode.CLOSED: (ConversationErrorCode.MESSAGING_FAILURE, False),
    MessagingErrorCode.INVALID_CURSOR: (
        ConversationErrorCode.INVALID_LAST_EVENT_ID,
        True,
    ),
    MessagingErrorCode.CODEC_MISMATCH: (
        ConversationErrorCode.MESSAGING_FAILURE,
        False,
    ),
    MessagingErrorCode.SOURCE_PROFILE_MISMATCH: (
        ConversationErrorCode.MESSAGING_FAILURE,
        False,
    ),
    MessagingErrorCode.MESSAGE_ID_CONFLICT: (
        ConversationErrorCode.RUN_IDENTITY_CONFLICT,
        True,
    ),
    MessagingErrorCode.RUN_ALREADY_ACTIVE: (
        ConversationErrorCode.RUN_CONFLICT,
        True,
    ),
    MessagingErrorCode.RUN_NOT_FOUND: (
        ConversationErrorCode.RUN_NOT_FOUND,
        True,
    ),
    MessagingErrorCode.RUN_PRODUCER_FAILED: (
        ConversationErrorCode.MESSAGING_FAILURE,
        False,
    ),
    MessagingErrorCode.CANCELLATION_UNSUPPORTED: (
        ConversationErrorCode.RUN_CANCEL_UNSUPPORTED,
        True,
    ),
    MessagingErrorCode.RECOVERY_UNSUPPORTED: (
        ConversationErrorCode.MESSAGING_FAILURE,
        False,
    ),
    MessagingErrorCode.SSE_RENDERING_UNSUPPORTED: (
        ConversationErrorCode.MESSAGING_FAILURE,
        False,
    ),
    MessagingErrorCode.BACKEND_OWNERSHIP_LOST: (
        ConversationErrorCode.MESSAGING_FAILURE,
        False,
    ),
    MessagingErrorCode.STREAM_DELETED: (
        ConversationErrorCode.RUN_NOT_FOUND,
        True,
    ),
    MessagingErrorCode.STREAM_DELETE_CONFLICT: (
        ConversationErrorCode.DELETE_CONFLICT,
        True,
    ),
    MessagingErrorCode.BACKEND_UNAVAILABLE: (
        ConversationErrorCode.MESSAGING_UNAVAILABLE,
        False,
    ),
    MessagingErrorCode.BACKEND_TIMEOUT: (
        ConversationErrorCode.MESSAGING_UNAVAILABLE,
        False,
    ),
    MessagingErrorCode.BACKEND_PROTOCOL_ERROR: (
        ConversationErrorCode.MESSAGING_FAILURE,
        False,
    ),
    MessagingErrorCode.UNEXPECTED_BACKEND_FAILURE: (
        ConversationErrorCode.MESSAGING_FAILURE,
        False,
    ),
}


async def _settle_owned_task(task: asyncio.Task[_TaskResult]) -> _TaskResult:
    """在已有主异常后忽略重复取消，直到受保护任务完成"""

    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


def conversation_stream_key(user_id: int, thread_id: str) -> str:
    """生成用户隔离的 Messaging、checkpoint 与协调键"""

    return conversation_identity(user_id, thread_id, "scope").thread_id


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


@dataclass(frozen=True, slots=True)
class PreparedChat:
    """完成 Messaging 预握手后的 HTTP SSE 内容"""

    body: AsyncIterator[bytes]
    thread_id: str


class ConversationChatService:
    """准备请求级 Agent 事件源并启动或附着 durable AG-UI run"""

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
        intent = classify_intent(request)
        model = await AgentModelService(AgentModelRepository(self._session)).resolve(
            request.forwarded_props.model
        )
        run_preparer, prepared, execution = await self._prepare_execution(
            request,
            intent=intent,
            model=model,
        )
        try:
            events = self._create_events(
                intent=intent,
                execution=execution,
                prepared=prepared,
                model=model,
            )
        except BaseException:
            await self._cleanup_execution(
                run_preparer,
                prepared=prepared,
                execution=execution,
            )
            raise
        body = await self._start_delivery(
            events,
            after=after,
            prepared=prepared,
            execution=execution,
            run_preparer=run_preparer,
        )
        return PreparedChat(body=body, thread_id=execution.thread.thread_id)

    async def _prepare_execution(
        self,
        request: ChatRequest,
        *,
        intent: StartChatIntent | ResumeChatIntent,
        model: AgentModelConfig,
    ) -> tuple[ConversationRunPreparer, PreparedRunRequest, PreparedExecution]:
        """完成 thread 解析、权威快照和短事务 run 注册"""

        run_preparer = ConversationRunPreparer(
            self._session,
            user_id=self._user.user_id,
            projector=self._resources.conversation_projector,
        )
        thread = await run_preparer.resolve_thread(
            request,
            intent=intent,
            model_id=model.model_id,
        )
        prepared = prepare_run_request(
            request,
            user_id=self._user.user_id,
            thread_id=thread.thread_id,
        )
        try:
            execution = await run_preparer.register(
                request,
                intent=intent,
                prepared=prepared,
                model=model,
                thread=thread,
            )
        except MessagingError as error:
            raise self._messaging_error(error) from error
        return run_preparer, prepared, execution

    @staticmethod
    async def _cleanup_execution(
        run_preparer: ConversationRunPreparer,
        *,
        prepared: PreparedRunRequest,
        execution: PreparedExecution,
    ) -> None:
        """以受保护任务清理尚未启动的 run 与 interrupt claim"""

        task = asyncio.create_task(
            run_preparer.cleanup_unstarted(
                thread_pk=execution.thread.id,
                identity_run_id=prepared.identity.run_id,
                registered=execution.registered,
            ),
            name=f"studio-conversation-cleanup:{prepared.identity.run_id}",
        )
        await _settle_owned_task(task)

    def _create_events(
        self,
        *,
        intent: StartChatIntent | ResumeChatIntent,
        execution: PreparedExecution,
        prepared: PreparedRunRequest,
        model: AgentModelConfig,
    ) -> ProfiledMessageSource[BaseEvent, BaseEvent]:
        """创建普通、恢复或审批放弃使用的统一 profile source"""

        graph_input: InputAgentState | Command | None
        resume_binding = None
        if isinstance(intent, StartChatIntent):
            graph_input = bind_start_graph_input(intent, prepared)
        else:
            if execution.resume is None:
                raise RuntimeError("恢复请求缺少 PreparedResume")
            graph_input = execution.resume.graph_input
            resume_binding = execution.resume.binding
        if graph_input is None:
            lifecycle = AgUiLifecycleEventFactory()
            events = (
                lifecycle.started(identity=prepared.identity),
                lifecycle.failed(
                    identity=prepared.identity,
                    message="审批已取消",
                    code="resume_cancelled",
                ),
            )
            return finite_agui_events(
                tuple(
                    enrich_main_event(
                        event,
                        prepared=prepared,
                        title=execution.thread.title,
                    )
                    for event in events
                ),
                identity=prepared.identity,
            )
        factory = ConversationAgentFactory(
            persistence=self._resources.agent_persistence,
            sandbox_manager=self._resources.sandbox_manager,
            tinkerfin=self._resources.tinkerfin,
            tavily_api_key=(
                None
                if self._resources.settings.tavily_api_key is None
                else self._resources.settings.tavily_api_key.get_secret_value()
            ),
        )
        return factory.create_agui_events(
            user_id=self._user.user_id,
            model_config=model,
            graph_input=graph_input,
            prepared=prepared,
            resume=resume_binding,
            title=execution.thread.title,
        )

    async def _start_delivery(
        self,
        events: ProfiledMessageSource[BaseEvent, BaseEvent],
        *,
        after: int | None,
        prepared: PreparedRunRequest,
        execution: PreparedExecution,
        run_preparer: ConversationRunPreparer,
    ) -> AsyncIterator[bytes]:
        """执行无数据库锁的 Messaging 预握手，并保护失败清理"""

        async def wake_projection(envelope: MessageEnvelope) -> None:
            self._resources.conversation_projector.ensure(
                thread_pk=execution.thread.id,
                identity=envelope.identity,
            )

        cleanup_task: asyncio.Task[None] | None = None

        async def cleanup() -> None:
            nonlocal cleanup_task
            if cleanup_task is None:
                cleanup_task = asyncio.create_task(
                    run_preparer.cleanup_unstarted(
                        thread_pk=execution.thread.id,
                        identity_run_id=prepared.identity.run_id,
                        registered=execution.registered,
                    ),
                    name=f"studio-conversation-cleanup:{prepared.identity.run_id}",
                )
            await _settle_owned_task(cleanup_task)

        preflight = asyncio.create_task(
            self._resources.conversation_channel.sse(
                events,
                after=after,
                on_committed=wake_projection,
            ),
            name=f"studio-conversation-preflight:{prepared.identity.run_id}",
        )
        try:
            body = await asyncio.shield(preflight)
        except asyncio.CancelledError as cancellation:
            try:
                body = await _settle_owned_task(preflight)
            except Exception as preflight_error:  # noqa: BLE001 - 预握手失败必须释放未启动 run
                await cleanup()
                cancellation.add_note(
                    "Messaging 预握手在 HTTP 取消后失败: "
                    f"{type(preflight_error).__name__}"
                )
            else:
                close = getattr(body, "aclose", None)
                if close is not None:
                    try:

                        async def close_body() -> None:
                            await close()

                        close_task = asyncio.create_task(close_body())
                        await _settle_owned_task(close_task)
                    except Exception as close_error:  # noqa: BLE001 - 调用方取消优先于订阅关闭失败
                        cancellation.add_note(
                            f"SSE 订阅关闭失败: {type(close_error).__name__}"
                        )
            raise cancellation
        except MessagingError as error:
            await cleanup()
            raise self._messaging_error(error) from error
        except (TypeError, ValueError):
            await cleanup()
            raise
        return body

    async def cancel(self, *, thread_id: str, run_id: str) -> CancelRunResponse:
        """验证用户归属后请求并等待 durable run 取消"""

        thread = await self._repository.get_thread(
            user_id=self._user.user_id,
            thread_id=thread_id,
        )
        if thread is None:
            raise BusinessException(ConversationErrorCode.NOT_FOUND)
        thread_pk = thread.id
        if await self._repository.get_run(thread_pk=thread_pk, run_id=run_id) is None:
            raise BusinessException(ConversationErrorCode.RUN_NOT_FOUND)
        # 提交隐式只读事务可释放连接，并保留活跃流仍会读取的 thread 事实
        await self._repository.commit()
        try:
            identity = conversation_identity(
                self._user.user_id,
                thread_id,
                run_id,
            )
            cancelled = await self._resources.conversation_channel.cancel(
                identity=identity,
            )
        except MessagingError as error:
            raise self._messaging_error(error, operation="cancel") from error
        await self._resources.conversation_projector.reconcile(
            thread_pk=thread_pk,
            identity=identity,
        )
        return CancelRunResponse(cancelled=cancelled)

    @staticmethod
    def _messaging_error(
        error: MessagingError,
        *,
        operation: str = "chat",
    ) -> BusinessException | SystemException:
        error_code, business = _MESSAGING_ERRORS[error.code]
        if (
            operation == "cancel"
            and error.code is MessagingErrorCode.RUN_PRODUCER_FAILED
        ):
            error_code = ConversationErrorCode.RUN_CANCEL_FAILED
        exception_type = BusinessException if business else SystemException
        return exception_type(error_code)


__all__ = [
    "ConversationChatService",
    "PreparedChat",
    "conversation_stream_key",
    "parse_last_event_id",
]
