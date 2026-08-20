"""会话元信息更新与跨存储幂等删除"""

from __future__ import annotations

from tinkerfin_messaging.errors import StreamDeleteConflict
from tinkerfin_studio.api.errors import BusinessException, ConversationErrorCode
from tinkerfin_studio.conversation.history import _history_item
from tinkerfin_studio.conversation.models import ConversationThread
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.conversation.run_preparation import conversation_identity
from tinkerfin_studio.conversation.schemas import ConversationHistoryListItem
from tinkerfin_studio.resources import ApplicationResources


class ConversationCommandService:
    """更新会话元信息，并按可重试步骤删除跨存储数据"""

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
        return _history_item(await self._require_thread(thread_id))

    async def delete(self, *, thread_id: str) -> None:
        """不持有数据库锁执行 Messaging 与 checkpoint 删除"""

        thread = await self._require_thread(thread_id)
        thread_pk = thread.id
        identity = conversation_identity(
            self._user_id,
            thread_id,
            thread.last_run_id or "delete",
        )
        # 归属查询只产生只读事务，外部跨存储操作开始前先提交释放
        await self._repository.commit()
        await self._resources.conversation_projector.reconcile(
            thread_pk=thread_pk,
            identity=identity,
        )
        previous_status = await self._mark_deleting(thread_pk)
        try:
            await self._resources.conversation_channel.delete_stream(identity=identity)
            await self._resources.agent_persistence.checkpointer.adelete_thread(
                identity.thread_id
            )
            locked = await self._repository.lock_thread(thread_pk)
            if locked is None:
                return
            if await self._repository.has_running_run(thread_pk):
                raise RuntimeError("会话删除标记期间出现了新的 running run")
            await self._repository.delete_thread_cascade(thread_pk)
            await self._repository.commit()
        except StreamDeleteConflict as error:
            await self._restore_delete_status(thread_pk, previous_status)
            raise BusinessException(ConversationErrorCode.DELETE_CONFLICT) from error
        except BaseException:
            await self._repository.rollback()
            raise

    async def _mark_deleting(self, thread_pk: int) -> str:
        """短事务检查运行状态并阻止新的 run 注册"""

        locked = await self._repository.lock_thread(thread_pk)
        if locked is None:
            await self._repository.rollback()
            raise BusinessException(ConversationErrorCode.NOT_FOUND)
        previous_status = locked.status
        if previous_status == "running" or await self._repository.has_running_run(
            thread_pk
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
        """确认未开始删除时恢复原状态"""

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
