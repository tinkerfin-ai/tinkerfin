"""Redis Messaging 到 MySQL 的后台投影协调"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from ag_ui.core import BaseEvent
from ag_ui.core.types import ResumeEntry

from tinkerfin import AgUiResumeCheckpoint, Identity
from tinkerfin_messaging import (
    DecodedMessage,
    MessageChannel,
    RunNotFound,
    RunProducerFailed,
    RunStatus,
    StreamDeleted,
)
from tinkerfin_studio.conversation.projection import ConversationProjector
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.infrastructure.database import Database

logger = logging.getLogger(__name__)
_PREPARING_RECOVERY_GRACE = timedelta(seconds=30)
_FINAL_RUN_STATUSES = frozenset({"completed", "cancelled", "failed", "owner_lost"})


class ConversationProjectionCoordinator:
    """拥有当前 Worker 的会话投影任务并支持请求前同步追赶"""

    def __init__(
        self,
        *,
        database: Database,
        channel: MessageChannel[BaseEvent, BaseEvent],
    ) -> None:
        self._database = database
        self._channel = channel
        self._tasks: dict[Identity, asyncio.Task[None]] = {}
        self._closed = False

    def ensure(
        self,
        *,
        thread_pk: int,
        identity: Identity,
    ) -> None:
        """保证当前 Worker 至多有一个该 run 的跟随任务"""

        if self._closed:
            raise RuntimeError("会话投影协调器已经关闭")
        existing = self._tasks.get(identity)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            self._follow(thread_pk=thread_pk, identity=identity),
            name=f"studio-conversation-projector:{identity.run_id}",
        )
        self._tasks[identity] = task
        task.add_done_callback(lambda completed: self._finished(identity, completed))

    async def reconcile(self, *, thread_pk: int, identity: Identity) -> int:
        """把 Redis 当前已提交尾部完整投影到 MySQL"""

        while True:
            after = await self._last_seq(thread_pk)
            latest = await self._channel.latest_seq(identity=identity)
            if after >= latest:
                return after
            messages = await self._channel.read(
                identity=identity,
                after=after,
                limit=min(1000, latest - after),
            )
            if not messages:
                raise RuntimeError(
                    f"Redis 事件尾部无法补齐: after={after}, latest={latest}"
                )
            for message in messages:
                await self._project(thread_pk, message)

    async def reconcile_and_settle(
        self,
        *,
        thread_pk: int,
        identity: Identity,
    ) -> RunStatus:
        """追赶已提交事件，并收敛 Messaging 已确认但没有协议事件的终态"""

        await self.reconcile(thread_pk=thread_pk, identity=identity)
        status = await self._channel.get_run_status(identity=identity)
        if status in _FINAL_RUN_STATUSES:
            await self._settle_messaging_terminal(
                thread_pk=thread_pk,
                identity=identity,
                status=status,
            )
        return status

    async def settle_resume(
        self,
        *,
        thread_pk: int,
        entries: tuple[ResumeEntry, ...],
        checkpoint: AgUiResumeCheckpoint,
    ) -> None:
        """根据框架 checkpoint 证据幂等完成业务审批结算"""

        if not entries:
            raise ValueError("checkpoint 回调缺少公开 resume 条目")
        async with self._database.session() as session:
            try:
                repository = ConversationRepository(session)
                thread = await repository.get_thread_by_pk(thread_pk)
                if thread is None:
                    raise LookupError(f"恢复会话不存在: {thread_pk}")
                if thread.thread_id != checkpoint.identity.thread_id:
                    raise ValueError("checkpoint 与会话 threadId 不一致")
                await repository.settle_claimed_interrupts(
                    thread_pk=thread_pk,
                    run_id=checkpoint.identity.run_id,
                    entries=entries,
                    resolution_id=checkpoint.marker_id,
                )
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

    async def recover_preparing(
        self, *, thread_pk: int | None = None
    ) -> frozenset[int]:
        """清理超过宽限且 Messaging 中不存在的未启动 run"""

        cutoff = datetime.now(UTC).replace(tzinfo=None) - _PREPARING_RECOVERY_GRACE
        async with self._database.session() as session:
            runs = await ConversationRepository(session).list_stale_preparing_runs(
                older_than=cutoff,
                thread_pk=thread_pk,
            )
            candidates = tuple(
                (run.id, run.conversation_thread_id, run.run_id) for run in runs
            )
            await session.commit()
        deleted_threads: set[int] = set()
        for run_pk, candidate_thread_pk, run_id in candidates:
            identity = await self._run_identity(candidate_thread_pk, run_id)
            if identity is None:
                continue
            try:
                await self._channel.get_run_status(identity=identity)
            except (RunNotFound, StreamDeleted):
                # 只有没有任何 durable 执行事实的空注册才可删除，有 owner 的慢启动必须保留
                async with self._database.session() as session:
                    try:
                        result = await ConversationRepository(
                            session
                        ).delete_unstarted_run(
                            thread_pk=candidate_thread_pk,
                            run_pk=run_pk,
                            run_id=run_id,
                            delete_empty_thread=True,
                        )
                        await session.commit()
                    except BaseException:
                        await session.rollback()
                        raise
                if result.thread_deleted:
                    deleted_threads.add(candidate_thread_pk)
        return frozenset(deleted_threads)

    async def aclose(self) -> None:
        """取消阻塞 follow，并等待所有本地投影任务退出"""

        if self._closed:
            return
        self._closed = True
        tasks = tuple(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _follow(self, *, thread_pk: int, identity: Identity) -> None:
        after = await self._last_seq(thread_pk)
        subscription = await self._channel.follow(
            identity=identity,
            after=after,
        )
        try:
            try:
                async for message in subscription:
                    await self._project(thread_pk, message)
            except RunProducerFailed:
                status = await self._channel.get_run_status(identity=identity)
                await self._settle_messaging_terminal(
                    thread_pk=thread_pk,
                    identity=identity,
                    status=status,
                )
            else:
                status = await self._channel.get_run_status(identity=identity)
                if status in _FINAL_RUN_STATUSES:
                    await self._settle_messaging_terminal(
                        thread_pk=thread_pk,
                        identity=identity,
                        status=status,
                    )
        finally:
            await subscription.aclose()

    async def _last_seq(self, thread_pk: int) -> int:
        async with self._database.session() as session:
            thread = await ConversationRepository(session).get_thread_by_pk(thread_pk)
            if thread is None:
                raise LookupError(f"会话不存在: {thread_pk}")
            return thread.last_seq

    async def _run_identity(self, thread_pk: int, run_id: str) -> Identity | None:
        async with self._database.session() as session:
            thread = await ConversationRepository(session).get_thread_by_pk(thread_pk)
            if thread is None:
                return None
            return Identity(threadId=thread.thread_id, runId=run_id)

    async def _project(
        self,
        thread_pk: int,
        message: DecodedMessage[BaseEvent],
    ) -> None:
        async with self._database.session() as session:
            try:
                await ConversationProjector(session).project(
                    thread_pk=thread_pk,
                    envelope=message.envelope,
                    event=message.data,
                )
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

    async def _settle_messaging_terminal(
        self,
        *,
        thread_pk: int,
        identity: Identity,
        status: RunStatus,
    ) -> None:
        if status not in _FINAL_RUN_STATUSES:
            return
        async with self._database.session() as session:
            try:
                await ConversationProjector(session).settle_messaging_terminal(
                    thread_pk=thread_pk,
                    identity_run_id=identity.run_id,
                    durable_status=status,
                )
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

    def _finished(
        self,
        identity: Identity,
        task: asyncio.Task[None],
    ) -> None:
        if self._tasks.get(identity) is task:
            self._tasks.pop(identity, None)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "会话后台投影失败: thread_id=%s run_id=%s",
                identity.thread_id,
                identity.run_id,
                exc_info=error,
            )
