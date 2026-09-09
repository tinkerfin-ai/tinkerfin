"""会话元信息更新与跨存储可重试删除"""

from __future__ import annotations

from tinkerfin import RunIdentity
from tinkerfin_messaging.errors import (
    MessagingError,
    RunNotFound,
    StreamDeleteConflict,
    StreamDeleted,
    StreamExpired,
)
from tinkerfin_studio.api.errors import (
    BusinessException,
    ConversationErrorCode,
    SystemException,
)
from tinkerfin_studio.conversation.history import _history_item
from tinkerfin_studio.conversation.models import ConversationThread
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.conversation.run_preparation import conversation_identity
from tinkerfin_studio.conversation.schemas import ConversationHistoryListItem
from tinkerfin_studio.resources import ApplicationResources
from tinkerfin_tracing import TraceRunConflict, TraceThreadNotFound, TracingError


class ConversationCommandService:
    """更新会话元信息，并按固定次序删除四个存储域"""

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
            thread,
            title=title,
            pinned=pinned,
        )
        await self._repository.commit()
        return _history_item(await self._require_thread(thread_id))

    async def delete(self, *, thread_id: str) -> None:
        """先删权威 Trace，再删恢复、传输与 Studio 业务记录"""

        thread = await self._require_thread(thread_id)
        if thread.last_run_id is None:
            raise SystemException(ConversationErrorCode.TRACE_UNAVAILABLE)
        thread_pk = thread.id
        identity = conversation_identity(thread.thread_id, thread.last_run_id)
        retrying = thread.status == "deleting"
        await self._repository.commit()
        previous_status = await self._mark_deleting(thread_pk)
        destruction_started = retrying
        try:
            await self._require_inactive_transport(identity)
            trace = None
            try:
                trace = await self._resources.tracer.get(
                    identity.thread_id,
                    head_run_id=identity.run_id,
                )
            except TraceThreadNotFound:
                if not retrying:
                    raise
            if trace is not None:
                await trace.delete()
            destruction_started = True
            await self._resources.agent_persistence.checkpointer.adelete_thread(
                identity.thread_id
            )
            await self._resources.conversation_channel.delete_stream(identity=identity)
            locked = await self._repository.lock_thread(thread_pk)
            if locked is None:
                return
            if await self._repository.has_running_run(thread_pk):
                raise RuntimeError("会话删除标记期间出现了新的 running Run")
            await self._repository.delete_thread_cascade(thread_pk)
            await self._repository.commit()
        except (TraceRunConflict, StreamDeleteConflict) as error:
            if not destruction_started:
                await self._restore_delete_status(thread_pk, previous_status)
            raise BusinessException(ConversationErrorCode.DELETE_CONFLICT) from error
        except TracingError as error:
            if not destruction_started:
                await self._restore_delete_status(thread_pk, previous_status)
            raise SystemException(ConversationErrorCode.TRACE_UNAVAILABLE) from error
        except MessagingError as error:
            if not destruction_started:
                await self._restore_delete_status(thread_pk, previous_status)
            raise SystemException(
                ConversationErrorCode.MESSAGING_UNAVAILABLE
            ) from error
        except BaseException:
            await self._repository.rollback()
            if not destruction_started:
                await self._restore_delete_status(thread_pk, previous_status)
            raise

    async def _require_inactive_transport(self, identity: RunIdentity) -> None:
        """删除任何持久数据前确认 Messaging producer 已经结算"""

        try:
            status = await self._resources.conversation_channel.get_run_status(
                identity=identity
            )
        except (RunNotFound, StreamDeleted, StreamExpired):
            return
        if status in {"running", "cancel_requested"}:
            raise BusinessException(ConversationErrorCode.DELETE_CONFLICT)

    async def _mark_deleting(self, thread_pk: int) -> str:
        """短事务检查运行状态并阻止新的 Run 注册"""

        locked = await self._repository.lock_thread(thread_pk)
        if locked is None:
            await self._repository.rollback()
            raise BusinessException(ConversationErrorCode.NOT_FOUND)
        previous_status = locked.status
        if previous_status != "deleting" and (
            previous_status == "running"
            or await self._repository.has_running_run(thread_pk)
        ):
            await self._repository.rollback()
            raise BusinessException(ConversationErrorCode.DELETE_CONFLICT)
        locked.status = "deleting"
        await self._repository.commit()
        return previous_status

    async def _restore_delete_status(
        self,
        thread_pk: int,
        previous_status: str,
    ) -> None:
        """确认未开始跨存储删除时恢复原状态"""

        await self._repository.rollback()
        locked = await self._repository.lock_thread(thread_pk)
        if locked is not None and locked.status == "deleting":
            locked.status = previous_status
        await self._repository.commit()

    async def _require_thread(self, thread_id: str) -> ConversationThread:
        thread = await self._repository.get_thread(
            user_id=self._user_id,
            thread_id=thread_id,
        )
        if thread is None:
            raise BusinessException(ConversationErrorCode.NOT_FOUND)
        return thread


__all__ = ["ConversationCommandService"]
