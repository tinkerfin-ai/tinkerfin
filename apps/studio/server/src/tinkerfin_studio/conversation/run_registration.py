"""会话 thread 解析、resume 认领与主 run 事务注册"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tinkerfin_messaging import MessagingError
from tinkerfin_studio.api.errors import BusinessException, ConversationErrorCode
from tinkerfin_studio.conversation.coordinator import (
    ConversationProjectionCoordinator,
)
from tinkerfin_studio.conversation.models import (
    ConversationInterrupt,
    ConversationRun,
    ConversationThread,
)
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.conversation.request import ChatRequest
from tinkerfin_studio.conversation.run_preparation import (
    PreparedResume,
    PreparedRunRequest,
    RegisteredRun,
    ResumeChatIntent,
    StartChatIntent,
    prepare_resume,
)
from tinkerfin_studio.models.schemas import AgentModelConfig


@dataclass(frozen=True, slots=True)
class PreparedExecution:
    """外部流启动前已经提交的 thread、run 与恢复状态"""

    thread: ConversationThread
    registered: RegisteredRun
    resume: PreparedResume | None


class ConversationRunPreparer:
    """拥有 chat 启动前的短事务，不拥有 Graph 或 Messaging 生命周期"""

    def __init__(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        projector: ConversationProjectionCoordinator,
    ) -> None:
        self._session = session
        self._user_id = user_id
        self._projector = projector
        self._repository = ConversationRepository(session)

    async def resolve_thread(
        self,
        request: ChatRequest,
        *,
        intent: StartChatIntent | ResumeChatIntent,
        model_id: str,
    ) -> ConversationThread:
        """解析已有会话，或按 run 幂等创建新会话"""

        thread_id = request.thread_id.strip()
        if thread_id:
            thread = await self._repository.get_thread(
                user_id=self._user_id,
                thread_id=thread_id,
            )
            if thread is None:
                raise BusinessException(ConversationErrorCode.NOT_FOUND)
            return thread
        if not isinstance(intent, StartChatIntent):
            raise BusinessException(ConversationErrorCode.USER_MESSAGE_REQUIRED)
        # 公开 thread ID 保持现有确定性格式，内部 Identity 仍由用户 scope 隔离
        generated_thread_id = "thread-" + str(
            uuid5(
                NAMESPACE_URL,
                f"tinkerfin-studio:{self._user_id}:run:{request.run_id}",
            )
        )
        thread = await self._repository.get_thread(
            user_id=self._user_id,
            thread_id=generated_thread_id,
        )
        if thread is not None:
            return thread
        try:
            async with self._session.begin_nested():
                thread = await self._repository.create_thread(
                    user_id=self._user_id,
                    thread_id=generated_thread_id,
                    title=intent.title,
                    model_id=model_id,
                )
        except IntegrityError:
            await self._repository.rollback()
            thread = await self._repository.get_thread(
                user_id=self._user_id,
                thread_id=generated_thread_id,
            )
            if thread is None:
                raise
        await self._repository.commit()
        return thread

    async def register(
        self,
        request: ChatRequest,
        *,
        intent: StartChatIntent | ResumeChatIntent,
        prepared: PreparedRunRequest,
        model: AgentModelConfig,
        thread: ConversationThread,
    ) -> PreparedExecution:
        """原子认领 resume 并持久化或确认同身份主 run"""

        thread_pk = thread.id
        existing = await self._repository.get_run(
            thread_pk=thread_pk,
            run_id=prepared.identity.run_id,
        )
        self._require_same_input(existing, prepared)
        claimed_ids: frozenset[str] = frozenset()
        claimed_interrupts: tuple[ConversationInterrupt, ...] = ()
        try:
            if isinstance(intent, ResumeChatIntent) and existing is None:
                try:
                    # 提交隐式只读事务可释放连接，并保留已加载的请求事实
                    await self._repository.commit()
                    await self._projector.reconcile(
                        thread_pk=thread_pk,
                        identity=prepared.identity,
                    )
                except MessagingError:
                    raise

            locked = await self._repository.lock_thread(thread_pk)
            if locked is None:
                raise BusinessException(ConversationErrorCode.NOT_FOUND)
            if locked.status == "deleting":
                raise BusinessException(ConversationErrorCode.NOT_FOUND)
            thread = locked

            if isinstance(intent, ResumeChatIntent) and existing is None:
                claimed_ids, claimed_interrupts, existing = await self._claim_resume(
                    request,
                    prepared=prepared,
                    thread=thread,
                )

            resume = (
                prepare_resume(
                    request,
                    identity=prepared.identity,
                    existing_config=(
                        None if existing is None else existing.config_json
                    ),
                    interrupts=claimed_interrupts,
                )
                if isinstance(intent, ResumeChatIntent)
                else None
            )
            created = False
            if existing is None:
                existing, created = await self._create_run(
                    request,
                    prepared=prepared,
                    model=model,
                    thread=thread,
                    resume=resume,
                )
                if not created and isinstance(intent, ResumeChatIntent):
                    claimed_ids = frozenset()
                    resume = prepare_resume(
                        request,
                        identity=prepared.identity,
                        existing_config=existing.config_json,
                        interrupts=(),
                    )
            self._require_same_input(existing, prepared)
            if created:
                thread.last_model = model.model_id
            await self._repository.commit()
            return PreparedExecution(
                thread=thread,
                registered=RegisteredRun(
                    run_id=existing.id,
                    created=created,
                    claimed_interrupt_ids=claimed_ids,
                ),
                resume=resume,
            )
        except BaseException:
            await self._repository.rollback()
            raise

    async def cleanup_unstarted(
        self,
        *,
        thread_pk: int,
        identity_run_id: str,
        registered: RegisteredRun,
    ) -> None:
        """幂等释放未消费 claim，并删除尚未启动的数据库 run"""

        if not registered.created:
            await self._repository.rollback()
            return
        await self._repository.release_pending_interrupt_claims(
            thread_pk=thread_pk,
            run_id=identity_run_id,
            interrupt_ids=registered.claimed_interrupt_ids,
        )
        await self._repository.delete_run(registered.run_id)
        await self._repository.commit()

    async def _claim_resume(
        self,
        request: ChatRequest,
        *,
        prepared: PreparedRunRequest,
        thread: ConversationThread,
    ) -> tuple[
        frozenset[str],
        tuple[ConversationInterrupt, ...],
        ConversationRun | None,
    ]:
        claimed_ids = frozenset(entry.interrupt_id for entry in request.resume or ())
        pending = await self._repository.list_pending_interrupts_for_update(
            thread_pk=thread.id
        )
        if claimed_ids != frozenset(entity.interrupt_id for entity in pending):
            raise BusinessException(
                ConversationErrorCode.RESUME_REQUIRED,
                message="resume 必须完整覆盖当前全部待审批项",
            )
        claimed = await self._repository.claim_pending_interrupts(
            thread_pk=thread.id,
            run_id=prepared.identity.run_id,
            interrupt_ids=claimed_ids,
        )
        raced = await self._repository.get_run_for_update(
            thread_pk=thread.id,
            run_id=prepared.identity.run_id,
        )
        self._require_same_input(raced, prepared)
        if any(
            entity.resolved_run_id != prepared.identity.run_id for entity in claimed
        ):
            raise BusinessException(ConversationErrorCode.RESUME_ALREADY_CLAIMED)
        if {entity.interrupt_id for entity in claimed} != claimed_ids:
            raise BusinessException(
                ConversationErrorCode.RESUME_REQUIRED,
                message="resume 包含不存在或尚未就绪的 interruptId",
            )
        if raced is None and any(entity.status != "pending" for entity in claimed):
            raise BusinessException(ConversationErrorCode.RESUME_ALREADY_CLAIMED)
        return claimed_ids, tuple(claimed), raced

    async def _create_run(
        self,
        request: ChatRequest,
        *,
        prepared: PreparedRunRequest,
        model: AgentModelConfig,
        thread: ConversationThread,
        resume: PreparedResume | None,
    ) -> tuple[ConversationRun, bool]:
        public_thread_id = thread.thread_id
        try:
            async with self._session.begin_nested():
                created = await self._repository.create_main_run(
                    thread_id=thread.id,
                    run_id=prepared.identity.run_id,
                    parent_run_id=request.parent_run_id,
                    model_id=model.model_id,
                    input_json=prepared.input_json,
                    config_json={
                        "thread_id": prepared.identity.thread_id,
                        "model_id": model.model_id,
                        "model_updated_at": model.updated_at,
                        **({} if resume is None else resume.persisted_config),
                    },
                )
                return created, True
        except IntegrityError:
            await self._repository.rollback()
            refreshed = await self._repository.get_thread(
                user_id=self._user_id,
                thread_id=public_thread_id,
            )
            if refreshed is None:
                raise
            existing = await self._repository.get_run(
                thread_pk=refreshed.id,
                run_id=prepared.identity.run_id,
            )
            if existing is None:
                raise
            self._require_same_input(existing, prepared)
            return existing, False

    @staticmethod
    def _require_same_input(
        run: ConversationRun | None,
        prepared: PreparedRunRequest,
    ) -> None:
        if run is not None and run.input_json != prepared.input_json:
            raise BusinessException(ConversationErrorCode.RUN_IDENTITY_CONFLICT)


__all__ = ["ConversationRunPreparer", "PreparedExecution"]
