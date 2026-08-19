"""Redis Messaging 到 MySQL 的后台投影协调"""

from __future__ import annotations

import asyncio
import logging

from ag_ui.core import BaseEvent

from tinkerfin_messaging import DecodedMessage, MessageChannel
from tinkerfin_studio.conversation.projection import ConversationProjector
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.infrastructure.database import Database

logger = logging.getLogger(__name__)


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
        self._tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._closed = False

    def ensure(
        self,
        *,
        thread_pk: int,
        stream: str,
        run: str,
    ) -> None:
        """保证当前 Worker 至多有一个该 run 的跟随任务"""

        if self._closed:
            raise RuntimeError("会话投影协调器已经关闭")
        key = (stream, run)
        existing = self._tasks.get(key)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            self._follow(thread_pk=thread_pk, stream=stream, run=run),
            name=f"studio-conversation-projector:{run}",
        )
        self._tasks[key] = task
        task.add_done_callback(lambda completed: self._finished(key, completed))

    async def reconcile(self, *, thread_pk: int, stream: str) -> int:
        """把 Redis 当前已提交尾部完整投影到 MySQL"""

        while True:
            after = await self._last_seq(thread_pk)
            latest = await self._channel.latest_seq(stream=stream)
            if after >= latest:
                return after
            messages = await self._channel.read(
                stream=stream,
                after=after,
                limit=min(1000, latest - after),
            )
            if not messages:
                raise RuntimeError(
                    f"Redis 事件尾部无法补齐: after={after}, latest={latest}"
                )
            for message in messages:
                await self._project(thread_pk, message)

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

    async def _follow(self, *, thread_pk: int, stream: str, run: str) -> None:
        after = await self._last_seq(thread_pk)
        subscription = await self._channel.follow(
            stream=stream,
            run=run,
            after=after,
        )
        try:
            async for message in subscription:
                await self._project(thread_pk, message)
        finally:
            await subscription.aclose()

    async def _last_seq(self, thread_pk: int) -> int:
        async with self._database.session() as session:
            thread = await ConversationRepository(session).get_thread_by_pk(thread_pk)
            if thread is None:
                raise LookupError(f"会话不存在: {thread_pk}")
            return thread.last_seq

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

    def _finished(
        self,
        key: tuple[str, str],
        task: asyncio.Task[None],
    ) -> None:
        if self._tasks.get(key) is task:
            self._tasks.pop(key, None)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "会话后台投影失败: stream=%s run=%s",
                key[0],
                key[1],
                exc_info=error,
            )
