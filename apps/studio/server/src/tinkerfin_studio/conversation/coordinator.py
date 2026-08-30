"""Trace 权威列表摘要、Run 状态与恢复认领协调"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from ag_ui.core import BaseEvent
from ag_ui.core.types import ResumeEntry

from tinkerfin import AgUiResumeCheckpoint, RunIdentity
from tinkerfin_messaging import RunNotFound, is_active_run_status
from tinkerfin_messaging.messaging import MessageChannel
from tinkerfin_studio.infrastructure.database import Database
from tinkerfin_tracing import (
    Tracer,
    TraceRunNotFound,
    TraceThread,
    TraceThreadNotFound,
    TraceUpdate,
)

from .repository import ConversationRepository

_STALE_PREPARING_SECONDS = 30
_FOLLOW_RETRY_INITIAL_SECONDS = 0.05
_FOLLOW_RETRY_MAX_SECONDS = 2.0
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _StalePreparingRun:
    """跨外部存活检查携带的无连接 Run 身份"""

    thread_pk: int
    run_pk: int
    identity: RunIdentity
    expected_updated_at: datetime


class ConversationTraceCoordinator:
    """把 Trace 当前事实收敛为 Studio 列表摘要，不保存会话正文"""

    def __init__(
        self,
        *,
        database: Database,
        tracer: Tracer,
        conversation_channel: MessageChannel[BaseEvent, BaseEvent],
    ) -> None:
        self._database = database
        self._tracer = tracer
        self._conversation_channel = conversation_channel
        self._tasks: dict[tuple[int, str], asyncio.Task[None]] = {}
        self._closed = False

    def ensure(self, *, thread_pk: int, identity: RunIdentity) -> None:
        """为一个已注册 Run 保留唯一 Trace follow 所有者"""

        if self._closed:
            raise RuntimeError("Trace 摘要协调器已经关闭")
        key = (thread_pk, identity.run_id)
        current = self._tasks.get(key)
        if current is not None and not current.done():
            return
        task = asyncio.create_task(
            self._follow(thread_pk=thread_pk, identity=identity),
            name=f"studio-trace-summary:{identity.run_id}",
        )
        task.add_done_callback(
            lambda completed, task_key=key: self._finished(task_key, completed)
        )
        self._tasks[key] = task

    async def reconcile(self, *, thread_pk: int, identity: RunIdentity) -> int:
        """读取固定 Trace 前缀并同步列表摘要"""

        trace = await self._tracer.get(
            identity.thread_id,
            head_run_id=identity.run_id,
        )
        await self._persist_thread(thread_pk=thread_pk, trace=trace)
        return trace.as_of_seq

    async def settle_resume(
        self,
        *,
        thread_pk: int,
        entries: tuple[ResumeEntry, ...],
        checkpoint: AgUiResumeCheckpoint,
    ) -> None:
        """用框架 checkpoint marker 幂等结算不含 payload 的业务认领"""

        async with self._database.session() as session:
            repository = ConversationRepository(session)
            await repository.settle_claims(
                thread_pk=thread_pk,
                run_id=checkpoint.identity.run_id,
                entries=entries,
                resolution_id=checkpoint.marker_id,
            )
            await repository.commit()
        await self.reconcile(thread_pk=thread_pk, identity=checkpoint.identity)

    async def recover_preparing(
        self,
        *,
        thread_pk: int | None = None,
    ) -> frozenset[int]:
        """清理没有 Trace 的过期 preparing 注册并恢复已有 Trace 摘要"""

        candidates: list[_StalePreparingRun] = []
        async with self._database.session() as session:
            repository = ConversationRepository(session)
            registrations = await repository.list_stale_unstarted_runs(
                older_than_seconds=_STALE_PREPARING_SECONDS,
                thread_pk=thread_pk,
            )
            recovered_threads: set[int] = set()
            for registration in registrations:
                thread = await repository.get_thread_by_pk(
                    registration.conversation_thread_id
                )
                if thread is None:
                    continue
                candidates.append(
                    _StalePreparingRun(
                        thread_pk=thread.id,
                        run_pk=registration.id,
                        identity=RunIdentity(
                            threadId=thread.thread_id,
                            runId=registration.run_id,
                        ),
                        expected_updated_at=registration.updated_at,
                    )
                )
            # Trace 与 Redis 检查不占用 Studio 的业务连接或事务
            await repository.commit()

        recovered_threads: set[int] = set()
        deletions: list[_StalePreparingRun] = []
        for candidate in candidates:
            try:
                await self._tracer.get(
                    candidate.identity.thread_id,
                    head_run_id=candidate.identity.run_id,
                )
            except (TraceRunNotFound, TraceThreadNotFound):
                try:
                    producer_status = await self._conversation_channel.get_run_status(
                        identity=candidate.identity
                    )
                except RunNotFound:
                    producer_status = None
                # Messaging 只证明当前 owner 是否存活，终态不能替代 Trace 的 Agent 结果
                if is_active_run_status(producer_status):
                    continue
                deletions.append(candidate)
            else:
                recovered_threads.add(candidate.thread_pk)
                self.ensure(
                    thread_pk=candidate.thread_pk,
                    identity=candidate.identity,
                )

        if deletions:
            async with self._database.session() as session:
                repository = ConversationRepository(session)
                for candidate in deletions:
                    await repository.delete_unstarted_run(
                        thread_pk=candidate.thread_pk,
                        run_pk=candidate.run_pk,
                        run_id=candidate.identity.run_id,
                        delete_empty_thread=True,
                        expected_updated_at=candidate.expected_updated_at,
                        allow_starting=True,
                    )
                await repository.commit()
        return frozenset(recovered_threads)

    async def aclose(self) -> None:
        """取消并结算全部协调器自有 follow task"""

        if self._closed:
            return
        self._closed = True
        tasks = tuple(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _follow(self, *, thread_pk: int, identity: RunIdentity) -> None:
        """从最新 Trace snapshot 恢复摘要并持续跟随到确定终态"""

        delay = _FOLLOW_RETRY_INITIAL_SECONDS
        while True:
            try:
                terminal = await self._follow_once(
                    thread_pk=thread_pk,
                    identity=identity,
                )
            except (TraceRunNotFound, TraceThreadNotFound):
                # Deferred Agent 初始化期间 Run 尚未写入 Ledger 是预期等待，不属于 Trace 损坏
                terminal = False
            except Exception as error:  # noqa: BLE001 - owner retries ordinary failures
                # Trace 是唯一权威；摘要失败只能退避重建，不能永久放弃当前 Run
                logger.warning(
                    "Trace 摘要 follow 将重试: thread_pk=%s run_id=%s error_type=%s",
                    thread_pk,
                    identity.run_id,
                    type(error).__name__,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, _FOLLOW_RETRY_MAX_SECONDS)
                continue
            if terminal:
                return
            await asyncio.sleep(delay)
            delay = min(delay * 2, _FOLLOW_RETRY_MAX_SECONDS)

    async def _follow_once(
        self,
        *,
        thread_pk: int,
        identity: RunIdentity,
    ) -> bool:
        """从一个最新固定前缀恢复，并报告是否已经到达终态"""

        trace = await self._tracer.get(
            identity.thread_id,
            head_run_id=identity.run_id,
        )
        await self._persist_thread(thread_pk=thread_pk, trace=trace)
        if trace.summary.status.execution != "running":
            return True
        updates = trace.follow()
        try:
            async for update in updates:
                await self._persist_update(
                    thread_pk=thread_pk,
                    update=update,
                    generation=trace.key.generation,
                )
                if update.summary.status.execution != "running":
                    return True
        finally:
            await updates.aclose()
        return False

    async def _persist_thread(self, *, thread_pk: int, trace: TraceThread) -> None:
        summary = trace.summary
        pending = summary.pending_interactions
        await self._write_summary(
            thread_pk=thread_pk,
            run_id=trace.head_run_id,
            execution=summary.status.execution,
            message_count=summary.message_count,
            tool_call_count=summary.tool_call_count,
            has_pending_interrupt=bool(pending),
            pending_interaction_kind=_pending_kind(item.kind for item in pending),
            updated_at=_database_time(summary.last_occurred_at),
            settlement_id=f"trace:{trace.key.generation}:{trace.as_of_seq}",
        )

    async def _persist_update(
        self,
        *,
        thread_pk: int,
        update: TraceUpdate,
        generation: str,
    ) -> None:
        summary = update.summary
        pending = summary.pending_interactions
        await self._write_summary(
            thread_pk=thread_pk,
            run_id=summary.status.head_run_id,
            execution=summary.status.execution,
            message_count=summary.message_count,
            tool_call_count=summary.tool_call_count,
            has_pending_interrupt=bool(pending),
            pending_interaction_kind=_pending_kind(item.kind for item in pending),
            updated_at=_database_time(summary.last_occurred_at),
            settlement_id=f"trace:{generation}:{update.as_of_seq}",
        )

    async def _write_summary(
        self,
        *,
        thread_pk: int,
        run_id: str,
        execution: str,
        message_count: int,
        tool_call_count: int,
        has_pending_interrupt: bool,
        pending_interaction_kind: str | None,
        updated_at: datetime,
        settlement_id: str,
    ) -> None:
        status, outcome = _summary_status(execution, has_pending_interrupt)
        async with self._database.session() as session:
            repository = ConversationRepository(session)
            await repository.update_trace_summary(
                thread_pk=thread_pk,
                run_id=run_id,
                status=status,
                message_count=message_count,
                tool_call_count=tool_call_count,
                has_pending_interrupt=has_pending_interrupt,
                pending_interaction_kind=pending_interaction_kind,
                terminal_outcome=outcome,
                updated_at=updated_at,
            )
            if outcome == "abandoned":
                await repository.cancel_claims(
                    thread_pk=thread_pk,
                    run_id=run_id,
                    resolution_id=settlement_id,
                )
            await repository.commit()

    def _finished(
        self,
        key: tuple[int, str],
        task: asyncio.Task[None],
    ) -> None:
        if self._tasks.get(key) is task:
            self._tasks.pop(key, None)
        if task.cancelled():
            return
        task.exception()


def _summary_status(execution: str, pending: bool) -> tuple[str, str | None]:
    if execution == "running":
        return "running", None
    if execution == "waiting" or pending:
        return "waiting_approval", "interrupted"
    if execution == "succeeded":
        return "idle", "succeeded"
    if execution in {"cancelled", "abandoned"}:
        return "idle", execution
    return "error", "failed"


def _pending_kind(values: Iterable[str]) -> str | None:
    kinds = set(values)
    if not kinds:
        return None
    mapped = {
        "tinkerfin:plan_clarification": "plan_clarification",
        "tinkerfin:plan_review": "plan_review",
        "tool_approval": "tool_approval",
    }
    resolved = {mapped.get(kind, "input_required") for kind in kinds}
    return next(iter(resolved)) if len(resolved) == 1 else "input_required"


def _database_time(value: datetime) -> datetime:
    if value.utcoffset() is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


__all__ = ["ConversationTraceCoordinator"]
