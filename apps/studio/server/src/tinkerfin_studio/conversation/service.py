"""会话 chat 与取消服务"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass

from ag_ui.core import BaseEvent
from langchain.agents.middleware.types import InputAgentState
from sqlalchemy.ext.asyncio import AsyncSession

from tinkerfin import AgUiResumeCheckpoint, join_task
from tinkerfin_messaging.errors import (
    MessagingError,
    MessagingErrorCode,
    RunNotFound,
    RunProducerFailed,
    StreamDeleted,
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
    MessagingErrorCode.QUOTA_EXCEEDED: (
        ConversationErrorCode.MESSAGING_QUOTA_EXCEEDED,
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
        resolved_thread = await run_preparer.resolve_thread(
            request,
            intent=intent,
        )
        thread = resolved_thread.thread
        # 释放 thread 查询产生的只读事务，恢复检查不得占用请求连接或数据库锁
        await self._repository.commit()
        deleted_threads = (
            await self._resources.conversation_projector.recover_preparing(
                thread_pk=thread.id,
            )
        )
        if thread.id in deleted_threads:
            if request.thread_id:
                raise BusinessException(ConversationErrorCode.NOT_FOUND)
            resolved_thread = await run_preparer.resolve_thread(
                request,
                intent=intent,
            )
            thread = resolved_thread.thread
        if thread.last_run_id and thread.last_run_id != request.run_id:
            # 用户发送下一条消息前先追上上一 run 的最终审批，避免投影延迟绕过卡片
            previous_identity = conversation_identity(
                thread.thread_id,
                thread.last_run_id,
            )
            try:
                previous_status = (
                    await self._resources.conversation_projector.reconcile_and_settle(
                        thread_pk=thread.id,
                        identity=previous_identity,
                    )
                )
            except (RunNotFound, StreamDeleted):
                previous_status = None
            if previous_status in {"running", "cancel_requested"}:
                raise BusinessException(ConversationErrorCode.RUN_CONFLICT)
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
                thread_created=resolved_thread.created,
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
                thread_created=execution.thread_created,
            ),
            name=f"studio-conversation-cleanup:{prepared.identity.run_id}",
        )
        await join_task(task)

    def _create_events(
        self,
        *,
        intent: StartChatIntent | ResumeChatIntent,
        execution: PreparedExecution,
        prepared: PreparedRunRequest,
        model: AgentModelConfig,
    ) -> ProfiledMessageSource[BaseEvent, BaseEvent]:
        """创建普通、恢复或审批放弃使用的统一 profile source"""

        graph_input: InputAgentState | None
        resume_binding = None
        if isinstance(intent, StartChatIntent):
            graph_input = bind_start_graph_input(intent, prepared)
        else:
            if execution.resume is None:
                raise RuntimeError("恢复请求缺少 AgUiResumeBinding")
            graph_input = None
            resume_binding = execution.resume
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

        async def record_resume_checkpoint(checkpoint: AgUiResumeCheckpoint) -> None:
            if not isinstance(intent, ResumeChatIntent):
                raise TypeError("普通运行不应收到 resume checkpoint")
            await self._resources.conversation_projector.settle_resume(
                thread_pk=execution.thread.id,
                entries=intent.entries,
                checkpoint=checkpoint,
            )

        return factory.create_agui_events(
            user_id=self._user.user_id,
            model_config=model,
            graph_input=graph_input,
            prepared=prepared,
            resume=resume_binding,
            on_resume_checkpointed=(
                record_resume_checkpoint if resume_binding is not None else None
            ),
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
                        thread_created=execution.thread_created,
                    ),
                    name=f"studio-conversation-cleanup:{prepared.identity.run_id}",
                )
            await join_task(cleanup_task)

        async def settle_interrupted_preflight() -> BaseException | None:
            """完成预握手的业务清理或关闭，并返回次要失败"""

            try:
                interrupted_body = await preflight
            except BaseException as preflight_error:  # noqa: BLE001 - owned task 终态
                try:
                    await cleanup()
                except BaseException as cleanup_error:  # noqa: BLE001 - 保留两项失败
                    preflight_error.add_note(
                        "会话预握手清理同时失败: "
                        f"{type(cleanup_error).__name__}: {cleanup_error}"
                    )
                return preflight_error
            close = getattr(interrupted_body, "aclose", None)
            if close is None:
                return None
            try:
                await close()
            except BaseException as close_error:  # noqa: BLE001 - 返回给原取消附注
                return close_error
            return None

        preflight = asyncio.create_task(
            self._resources.conversation_channel.sse(
                events,
                after=after,
                on_committed=wake_projection,
            ),
            name=f"studio-conversation-preflight:{prepared.identity.run_id}",
        )
        current = asyncio.current_task()
        try:
            body = await asyncio.shield(preflight)
        except asyncio.CancelledError as cancellation:
            caller_cancelled = current is not None and current.cancelling() > 0
            settlement = asyncio.create_task(
                settle_interrupted_preflight(),
                name=f"studio-conversation-preflight-settlement:{prepared.identity.run_id}",
            )
            try:
                settlement_error = await join_task(settlement)
            except asyncio.CancelledError as repeated_cancellation:
                # join_task 已保证 settlement 完成；这里保留首次取消作为请求主因
                settlement_error = settlement.result()
                cancellation.add_note(
                    f"等待会话预握手结算期间再次收到调用方取消: {repeated_cancellation}"
                )
            if settlement_error is not None:
                stage = "HTTP 取消后" if caller_cancelled else "owned task 取消后"
                cancellation.add_note(
                    f"Messaging 预握手在{stage}结算失败: "
                    f"{type(settlement_error).__name__}: {settlement_error}"
                )
            raise cancellation
        except BaseException as error:
            try:
                await cleanup()
            except asyncio.CancelledError as cleanup_cancellation:
                cleanup_cancellation.add_note(
                    f"会话预握手同时失败: {type(error).__name__}: {error}"
                )
                raise
            except BaseException as cleanup_error:  # noqa: BLE001 - 主失败必须保留
                error.add_note(
                    "会话预握手清理同时失败: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
            if isinstance(error, MessagingError):
                raise self._messaging_error(error) from error
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
            identity = conversation_identity(thread_id, run_id)
            cancelled = await self._resources.conversation_channel.cancel(
                identity=identity,
            )
        except RunProducerFailed:
            # run 已经失败时“停止”是幂等确认，用户应回到可重试状态而不是看到 500
            await self._resources.conversation_projector.reconcile_and_settle(
                thread_pk=thread_pk,
                identity=identity,
            )
            return CancelRunResponse(cancelled=False)
        except MessagingError as error:
            raise self._messaging_error(error, operation="cancel") from error
        await self._resources.conversation_projector.reconcile_and_settle(
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
    "parse_last_event_id",
]
