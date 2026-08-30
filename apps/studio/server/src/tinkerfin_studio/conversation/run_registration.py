"""会话 thread 解析、恢复认领与主 Run 事务注册"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tinkerfin import AgUiResumeRequest
from tinkerfin_studio.api.errors import BusinessException, ConversationErrorCode
from tinkerfin_studio.conversation.models import (
    ConversationRunRegistration,
    ConversationThread,
)
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.conversation.request import ChatRequest
from tinkerfin_studio.conversation.run_preparation import (
    PreparedRunRequest,
    RegisteredRun,
    ResumeChatIntent,
    StartChatIntent,
)
from tinkerfin_studio.models.schemas import AgentModelConfig


@dataclass(frozen=True, slots=True)
class ResolvedThread:
    """返回解析后的 thread 及其是否由当前请求创建"""

    thread: ConversationThread
    created: bool


@dataclass(frozen=True, slots=True)
class PreparedExecution:
    """外部流启动前已提交的 thread、Run 注册与恢复请求"""

    thread: ConversationThread
    registered: RegisteredRun
    resume: AgUiResumeRequest | None
    thread_created: bool = False


class ConversationRunPreparer:
    """拥有 chat 启动前短事务，不读取 Agent 内部状态或审批 payload"""

    def __init__(self, session: AsyncSession, *, user_id: int) -> None:
        self._session = session
        self._user_id = user_id
        self._repository = ConversationRepository(session)

    async def resolve_thread(
        self,
        request: ChatRequest,
        *,
        intent: StartChatIntent | ResumeChatIntent,
    ) -> ResolvedThread:
        """解析已有会话，或按 Run 幂等创建新会话"""

        thread_id = request.thread_id.strip()
        if thread_id:
            thread = await self._repository.get_thread(
                user_id=self._user_id,
                thread_id=thread_id,
            )
            if thread is None:
                raise BusinessException(ConversationErrorCode.NOT_FOUND)
            return ResolvedThread(thread=thread, created=False)
        if not isinstance(intent, StartChatIntent):
            raise BusinessException(ConversationErrorCode.USER_MESSAGE_REQUIRED)
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
            return ResolvedThread(thread=thread, created=False)
        created = False
        try:
            async with self._session.begin_nested():
                thread = await self._repository.create_thread(
                    user_id=self._user_id,
                    thread_id=generated_thread_id,
                    title=intent.title,
                    model_id=None,
                )
                created = True
        except IntegrityError:
            await self._repository.rollback()
            thread = await self._repository.get_thread(
                user_id=self._user_id,
                thread_id=generated_thread_id,
            )
            if thread is None:
                raise
        await self._repository.commit()
        return ResolvedThread(thread=thread, created=created)

    async def register(
        self,
        *,
        intent: StartChatIntent | ResumeChatIntent,
        prepared: PreparedRunRequest,
        model: AgentModelConfig,
        thread: ConversationThread,
        thread_created: bool = False,
    ) -> PreparedExecution:
        """原子认领客户端 interrupt ID 并固定 Run 的 Runtime Profile"""

        existing = await self._repository.get_run(
            thread_pk=thread.id,
            run_id=prepared.identity.run_id,
        )
        self._require_same_registration(existing, prepared=prepared, model=model)
        claimed_ids: frozenset[str] = frozenset()
        try:
            locked = await self._repository.lock_thread(thread.id)
            if locked is None or locked.status == "deleting":
                raise BusinessException(ConversationErrorCode.NOT_FOUND)
            thread = locked
            source_run_id = self._continuation_source_run_id(
                intent,
                prepared=prepared,
                thread=thread,
            )
            if source_run_id is not None:
                source_run = await self._repository.get_run_for_update(
                    thread_pk=thread.id,
                    run_id=source_run_id,
                )
                if source_run is None:
                    raise BusinessException(ConversationErrorCode.RUN_NOT_FOUND)
                self._require_source_model(source_run, model=model)
            if isinstance(intent, ResumeChatIntent):
                if source_run_id is None:
                    raise BusinessException(ConversationErrorCode.RESUME_REQUIRED)
                claimed_ids = await self._claim_resume(
                    intent,
                    prepared=prepared,
                    thread=thread,
                    source_run_id=source_run_id,
                )
            elif existing is None and thread.has_pending_interrupt:
                raise BusinessException(ConversationErrorCode.PENDING_INTERRUPT)

            created = False
            if existing is None:
                existing, created = await self._create_run(
                    prepared=prepared,
                    model=model,
                    thread=thread,
                )
            self._require_same_registration(existing, prepared=prepared, model=model)
            await self._repository.commit()
            return PreparedExecution(
                thread=thread,
                registered=RegisteredRun(
                    run_id=existing.id,
                    created=created,
                    claimed_interrupt_ids=claimed_ids,
                ),
                resume=(
                    AgUiResumeRequest(entries=intent.entries)
                    if isinstance(intent, ResumeChatIntent)
                    else None
                ),
                thread_created=thread_created,
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
        thread_created: bool,
    ) -> None:
        """幂等释放未结算认领并删除尚未启动的 Run 注册"""

        if not registered.created:
            await self._repository.rollback()
            return
        await self._repository.delete_unstarted_run(
            thread_pk=thread_pk,
            run_pk=registered.run_id,
            run_id=identity_run_id,
            delete_empty_thread=thread_created,
        )
        await self._repository.commit()

    async def activate_started(
        self,
        *,
        thread_pk: int,
        identity_run_id: str,
        registered: RegisteredRun,
    ) -> None:
        """在 Messaging owner 已建立后以 CAS 激活业务 Run"""

        try:
            activated = await self._repository.activate_run_registration(
                thread_pk=thread_pk,
                run_pk=registered.run_id,
                run_id=identity_run_id,
            )
            if not activated:
                raise BusinessException(ConversationErrorCode.RUN_CONFLICT)
            await self._repository.commit()
        except BaseException:
            await self._repository.rollback()
            raise

    async def _claim_resume(
        self,
        intent: ResumeChatIntent,
        *,
        prepared: PreparedRunRequest,
        thread: ConversationThread,
        source_run_id: str,
    ) -> frozenset[str]:
        """认领客户端 ID，完整性与 Tool correlation 由框架 Checkpointer 校验"""

        claimed_ids = frozenset(entry.interrupt_id for entry in intent.entries)
        existing = await self._repository.list_claims_for_update(
            thread_pk=thread.id,
            interrupt_ids=claimed_ids,
        )
        if any(
            claim.claimed_run_id != prepared.identity.run_id
            or claim.source_run_id != source_run_id
            for claim in existing
        ):
            raise BusinessException(ConversationErrorCode.RESUME_ALREADY_CLAIMED)
        existing_ids = {claim.interrupt_id for claim in existing}
        missing = tuple(
            entry.interrupt_id
            for entry in intent.entries
            if entry.interrupt_id not in existing_ids
        )
        if missing:
            try:
                async with self._session.begin_nested():
                    await self._repository.create_interrupt_claims(
                        thread_pk=thread.id,
                        source_run_id=source_run_id,
                        claimed_run_id=prepared.identity.run_id,
                        interrupt_ids=missing,
                    )
            except IntegrityError as error:
                raise BusinessException(
                    ConversationErrorCode.RESUME_ALREADY_CLAIMED
                ) from error
        return claimed_ids

    @staticmethod
    def _continuation_source_run_id(
        intent: StartChatIntent | ResumeChatIntent,
        *,
        prepared: PreparedRunRequest,
        thread: ConversationThread,
    ) -> str | None:
        """解析 resume 或 branch 必须继承模型合同的来源 Run"""

        if isinstance(intent, ResumeChatIntent):
            return prepared.parent_run_id or thread.last_run_id
        return prepared.parent_run_id

    @staticmethod
    def _require_source_model(
        source: ConversationRunRegistration,
        *,
        model: AgentModelConfig,
    ) -> None:
        """拒绝客户端用不同模型或 Profile 继续已有 checkpoint"""

        if (
            source.model_id != model.model_id
            or source.runtime_profile != model.runtime_profile
        ):
            raise BusinessException(ConversationErrorCode.RUN_IDENTITY_CONFLICT)

    async def _create_run(
        self,
        *,
        prepared: PreparedRunRequest,
        model: AgentModelConfig,
        thread: ConversationThread,
    ) -> tuple[ConversationRunRegistration, bool]:
        """创建或读取同一幂等 Run 注册"""

        thread_id = thread.thread_id
        try:
            async with self._session.begin_nested():
                created = await self._repository.create_run_registration(
                    thread_id=thread.id,
                    run_id=prepared.identity.run_id,
                    parent_run_id=prepared.parent_run_id,
                    model_id=model.model_id,
                    runtime_profile=model.runtime_profile,
                    input_json=prepared.input_json,
                    config_json={
                        "thread_id": prepared.identity.thread_id,
                        "model_id": model.model_id,
                        "model_updated_at": model.updated_at,
                        "runtime_profile": model.runtime_profile,
                    },
                )
                thread.last_run_id = prepared.identity.run_id
                thread.last_model = model.model_id
                thread.status = "running"
                return created, True
        except IntegrityError:
            await self._repository.rollback()
            refreshed = await self._repository.get_thread(
                user_id=self._user_id,
                thread_id=thread_id,
            )
            if refreshed is None:
                raise
            existing = await self._repository.get_run(
                thread_pk=refreshed.id,
                run_id=prepared.identity.run_id,
            )
            if existing is None:
                raise
            self._require_same_registration(existing, prepared=prepared, model=model)
            return existing, False

    @staticmethod
    def _require_same_registration(
        run: ConversationRunRegistration | None,
        *,
        prepared: PreparedRunRequest,
        model: AgentModelConfig,
    ) -> None:
        """拒绝同 Run 改写输入、模型或 Runtime Profile"""

        if run is None:
            return
        if (
            run.input_json != prepared.input_json
            or run.model_id != model.model_id
            or run.runtime_profile != model.runtime_profile
        ):
            raise BusinessException(ConversationErrorCode.RUN_IDENTITY_CONFLICT)


__all__ = ["ConversationRunPreparer", "PreparedExecution", "ResolvedThread"]
