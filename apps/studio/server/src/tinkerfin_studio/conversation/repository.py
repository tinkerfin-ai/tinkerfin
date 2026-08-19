"""会话事务与历史查询数据访问"""

from datetime import UTC, datetime
from typing import cast

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tinkerfin_studio.conversation.models import (
    ConversationEvent,
    ConversationInterrupt,
    ConversationMessage,
    ConversationRun,
    ConversationThread,
)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class ConversationRepository:
    """会话主表、run 和事件事实访问"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_thread(
        self,
        *,
        user_id: int,
        thread_id: str,
        title: str,
        model_id: str | None,
    ) -> ConversationThread:
        """创建一个尚无事件的新会话"""

        now = _now()
        thread = ConversationThread(
            user_id=user_id,
            thread_id=thread_id,
            title=title,
            status="idle",
            last_model=model_id,
            last_seq=0,
            snapshot_seq=0,
            snapshot_version=2,
            message_count=0,
            tool_call_count=0,
            has_pending_interrupt=False,
            pinned=False,
            snapshot_json=None,
            created_at=now,
            updated_at=now,
            deleted_at=None,
        )
        self._session.add(thread)
        await self._session.flush()
        return thread

    async def create_main_run(
        self,
        *,
        thread_id: int,
        run_id: str,
        model_id: str,
        input_json: dict[str, object],
        config_json: dict[str, object],
        parent_run_id: str | None = None,
    ) -> ConversationRun:
        """在 Graph 启动前记录完整主 run 输入"""

        now = _now()
        run = ConversationRun(
            conversation_thread_id=thread_id,
            run_id=run_id,
            parent_run_id=parent_run_id,
            parent_agent_run_id=None,
            agent_type="main",
            agent_name=None,
            graph_task_id=None,
            model_id=model_id,
            status="running",
            input_json=input_json,
            config_json=config_json,
            outcome_json=None,
            started_at=now,
            finished_at=None,
            created_at=now,
            updated_at=now,
        )
        self._session.add(run)
        await self._session.flush()
        return run

    async def get_thread_by_pk(self, thread_pk: int) -> ConversationThread | None:
        return cast(
            ConversationThread | None,
            await self._session.get(ConversationThread, thread_pk),
        )

    async def get_thread(
        self, *, user_id: int, thread_id: str
    ) -> ConversationThread | None:
        return await self._session.scalar(
            select(ConversationThread).where(
                ConversationThread.user_id == user_id,
                ConversationThread.thread_id == thread_id,
                ConversationThread.deleted_at.is_(None),
            )
        )

    async def get_run(self, *, thread_pk: int, run_id: str) -> ConversationRun | None:
        return await self._session.scalar(
            select(ConversationRun).where(
                ConversationRun.conversation_thread_id == thread_pk,
                ConversationRun.run_id == run_id,
            )
        )

    async def get_run_for_update(
        self,
        *,
        thread_pk: int,
        run_id: str,
    ) -> ConversationRun | None:
        """锁定并读取 claim 等待期间可能由同一请求创建的主 run"""

        return await self._session.scalar(
            select(ConversationRun)
            .where(
                ConversationRun.conversation_thread_id == thread_pk,
                ConversationRun.run_id == run_id,
            )
            .with_for_update()
        )

    async def claim_pending_interrupts(
        self,
        *,
        thread_pk: int,
        run_id: str,
        interrupt_ids: frozenset[str],
    ) -> list[ConversationInterrupt]:
        """以条件更新原子认领一次 resume 涉及的待处理审批"""

        if not interrupt_ids:
            return []
        now = _now()
        await self._session.execute(
            update(ConversationInterrupt)
            .where(
                ConversationInterrupt.conversation_thread_id == thread_pk,
                ConversationInterrupt.interrupt_id.in_(interrupt_ids),
                ConversationInterrupt.status == "pending",
                or_(
                    ConversationInterrupt.resolved_run_id.is_(None),
                    ConversationInterrupt.resolved_run_id == run_id,
                ),
            )
            .values(resolved_run_id=run_id, updated_at=now)
        )
        result = await self._session.scalars(
            select(ConversationInterrupt)
            .where(
                ConversationInterrupt.conversation_thread_id == thread_pk,
                ConversationInterrupt.interrupt_id.in_(interrupt_ids),
            )
            .order_by(ConversationInterrupt.id)
            .with_for_update()
        )
        return list(result)

    async def list_pending_interrupts_for_update(
        self,
        *,
        thread_pk: int,
    ) -> list[ConversationInterrupt]:
        """锁定并按投影顺序返回会话当前全部待处理审批"""

        result = await self._session.scalars(
            select(ConversationInterrupt)
            .where(
                ConversationInterrupt.conversation_thread_id == thread_pk,
                ConversationInterrupt.status == "pending",
            )
            .order_by(ConversationInterrupt.id)
            .with_for_update()
        )
        return list(result)

    async def release_pending_interrupt_claims(
        self,
        *,
        thread_pk: int,
        run_id: str,
        interrupt_ids: frozenset[str],
    ) -> None:
        """在 Messaging 预握手失败时释放尚未消费的审批认领"""

        if not interrupt_ids:
            return
        await self._session.execute(
            update(ConversationInterrupt)
            .where(
                ConversationInterrupt.conversation_thread_id == thread_pk,
                ConversationInterrupt.interrupt_id.in_(interrupt_ids),
                ConversationInterrupt.status == "pending",
                ConversationInterrupt.resolved_run_id == run_id,
            )
            .values(resolved_run_id=None, updated_at=_now())
        )

    async def count_events(self, thread_pk: int) -> int:
        return int(
            await self._session.scalar(
                select(func.count(ConversationEvent.id)).where(
                    ConversationEvent.conversation_thread_id == thread_pk
                )
            )
            or 0
        )

    async def list_threads(
        self,
        *,
        user_id: int,
        page_size: int,
        cursor: tuple[bool, datetime, int] | None,
    ) -> list[ConversationThread]:
        """按置顶、更新时间和主键执行稳定 keyset 分页"""

        statement = select(ConversationThread).where(
            ConversationThread.user_id == user_id,
            ConversationThread.deleted_at.is_(None),
        )
        if cursor is not None:
            pinned, updated_at, row_id = cursor
            same_group_tail = or_(
                and_(
                    ConversationThread.pinned.is_(pinned),
                    ConversationThread.updated_at < updated_at,
                ),
                and_(
                    ConversationThread.pinned.is_(pinned),
                    ConversationThread.updated_at == updated_at,
                    ConversationThread.id < row_id,
                ),
            )
            statement = statement.where(
                or_(ConversationThread.pinned.is_(False), same_group_tail)
                if pinned
                else same_group_tail
            )
        result = await self._session.scalars(
            statement.order_by(
                ConversationThread.pinned.desc(),
                ConversationThread.updated_at.desc(),
                ConversationThread.id.desc(),
            ).limit(page_size + 1)
        )
        return list(result)

    async def list_events(
        self,
        *,
        thread_pk: int,
        after_seq: int,
        limit: int,
    ) -> list[ConversationEvent]:
        """按严格序号读取事件尾部"""

        result = await self._session.scalars(
            select(ConversationEvent)
            .where(
                ConversationEvent.conversation_thread_id == thread_pk,
                ConversationEvent.seq > after_seq,
            )
            .order_by(ConversationEvent.seq)
            .limit(limit)
        )
        return list(result)

    async def update_thread_meta(
        self,
        *,
        thread_pk: int,
        title: str | None,
        pinned: bool | None,
    ) -> None:
        """只更新调用方明确提交的会话元信息"""

        values: dict[str, object] = {"updated_at": _now()}
        if title is not None:
            values["title"] = title
        if pinned is not None:
            values["pinned"] = pinned
        await self._session.execute(
            update(ConversationThread)
            .where(ConversationThread.id == thread_pk)
            .values(**values)
        )

    async def lock_thread(self, thread_pk: int) -> ConversationThread | None:
        """锁定会话，串行化新 run 入库与跨存储删除"""

        return await self._session.scalar(
            select(ConversationThread)
            .where(ConversationThread.id == thread_pk)
            .with_for_update()
        )

    async def has_running_run(self, thread_pk: int) -> bool:
        """锁定并判断会话是否仍有尚未投影首事件的运行"""

        run_pk = await self._session.scalar(
            select(ConversationRun.id)
            .where(
                ConversationRun.conversation_thread_id == thread_pk,
                ConversationRun.status == "running",
            )
            .limit(1)
            .with_for_update()
        )
        return run_pk is not None

    async def delete_thread_cascade(self, thread_pk: int) -> None:
        """按应用维护的关联顺序删除会话全部 MySQL 数据"""

        for entity in (
            ConversationMessage,
            ConversationInterrupt,
            ConversationEvent,
            ConversationRun,
        ):
            await self._session.execute(
                delete(entity).where(entity.conversation_thread_id == thread_pk)
            )
        await self._session.execute(
            delete(ConversationThread).where(ConversationThread.id == thread_pk)
        )

    async def delete_run(self, run_pk: int) -> None:
        """删除尚未产生事件的失败预握手 run"""

        await self._session.execute(
            delete(ConversationRun).where(ConversationRun.id == run_pk)
        )

    async def commit(self) -> None:
        await self._session.commit()

    async def rollback(self) -> None:
        await self._session.rollback()
