"""Messaging 已提交事件的幂等 MySQL 投影"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import cast

from ag_ui.core import BaseEvent
from ag_ui.core.types import ResumeEntry
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tinkerfin_agui_adapter import SubagentProvenance
from tinkerfin_messaging import RunStatus
from tinkerfin_messaging.models import MessageEnvelope
from tinkerfin_studio.conversation.models import (
    ConversationEvent,
    ConversationInterrupt,
    ConversationRun,
    ConversationThread,
)
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.conversation.snapshot import (
    empty_snapshot,
    reduce_snapshot,
    synchronize_pending_interrupts,
)


class ConversationProjector:
    """按 Redis 权威序号维护会话事实和查询投影"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def project(
        self,
        *,
        thread_pk: int,
        envelope: MessageEnvelope,
        event: BaseEvent,
    ) -> None:
        """幂等写入一个已提交事件及其全部业务投影"""

        thread = await self._session.scalar(
            select(ConversationThread)
            .where(ConversationThread.id == thread_pk)
            .with_for_update()
        )
        if thread is None:
            raise LookupError(f"会话不存在: {thread_pk}")
        event_text = envelope.payload.decode("utf-8")
        existing = await self._session.scalar(
            select(ConversationEvent).where(
                ConversationEvent.conversation_thread_id == thread_pk,
                ConversationEvent.seq == envelope.seq,
            )
        )
        if existing is not None:
            if (
                existing.event_text != event_text
                or existing.event_id != envelope.message_id
            ):
                raise RuntimeError(f"序号 {envelope.seq} 的既有事件内容不一致")
            return
        expected = thread.last_seq + 1
        if envelope.seq != expected:
            raise RuntimeError(
                f"会话事件序号不连续: expected={expected}, actual={envelope.seq}"
            )

        event_json = cast(
            dict[str, object],
            event.model_dump(mode="json", by_alias=True, exclude_none=True),
        )
        event_type = str(event_json["type"])
        main_run = await self._session.scalar(
            select(ConversationRun).where(
                ConversationRun.conversation_thread_id == thread_pk,
                ConversationRun.run_id == envelope.identity.run_id,
            )
        )
        created_at = envelope.created_at.astimezone(UTC).replace(tzinfo=None)
        await self._project_discovered_subagent_runs(
            thread_pk=thread_pk,
            main_run=main_run,
            event=event_json,
            created_at=created_at,
        )
        event_run_id = self._event_run_id(event_json, envelope.identity.run_id)
        run = (
            main_run
            if main_run is not None and main_run.run_id == event_run_id
            else await self._session.scalar(
                select(ConversationRun).where(
                    ConversationRun.conversation_thread_id == thread_pk,
                    ConversationRun.run_id == event_run_id,
                )
            )
        )
        if run is None and event_type == "RUN_STARTED":
            raise RuntimeError(f"RUN_STARTED 缺少已注册 run: {event_run_id}")
        run_input = (
            main_run.input_json
            if main_run is not None and event_run_id == envelope.identity.run_id
            else None
        )
        await self._settle_abandoned_resume(
            thread_pk=thread_pk,
            main_run=main_run,
            event=event_json,
            run_input=run_input,
        )
        resume_settled = await self._resume_is_settled(
            thread_pk=thread_pk,
            run_id=envelope.identity.run_id,
            run_input=run_input,
        )
        snapshot = reduce_snapshot(
            thread.snapshot_json,
            seq=envelope.seq,
            event=event,
            run_id=event_run_id,
            created_at=envelope.created_at,
            run_input=run_input,
            resume_settled=resume_settled,
        )
        self._session.add(
            ConversationEvent(
                conversation_thread_id=thread_pk,
                run_id=envelope.identity.run_id,
                seq=envelope.seq,
                event_id=envelope.message_id,
                event_type=event_type,
                schema_version=3,
                protocol_version="ag-ui-protocol@0.1.19",
                event_json=event_json,
                event_text=event_text,
                created_at=created_at,
            )
        )

        await self._project_run(run, event_json, created_at)
        await self._project_open_subagents_on_main_error(
            thread_pk=thread_pk,
            main_run=main_run,
            event_run_id=event_run_id,
            event=event_json,
            created_at=created_at,
        )
        await self._project_related_subagent_run(
            thread_pk=thread_pk,
            event=event_json,
            created_at=created_at,
        )
        await self._project_interrupts(
            thread_pk,
            event_run_id,
            event_json,
            created_at,
            run_input,
        )
        thread.last_seq = envelope.seq
        thread.snapshot_seq = envelope.seq
        thread.snapshot_json = snapshot
        thread.last_run_id = envelope.identity.run_id
        thread.status = self._thread_status(snapshot)
        pending_interrupts = snapshot.get("interrupts")
        thread.has_pending_interrupt = bool(
            isinstance(pending_interrupts, list) and pending_interrupts
        )
        thread.message_count = sum(
            1
            for message in cast(list[dict[str, object]], snapshot["messages"])
            if message.get("role") in {"user", "assistant"}
        )
        if event_type == "TOOL_CALL_START":
            thread.tool_call_count += 1
        if event_type == "RUN_STARTED" and main_run is not None:
            thread.last_model = main_run.model_id
        thread.updated_at = created_at
        await self._session.flush()

    async def settle_messaging_terminal(
        self,
        *,
        thread_pk: int,
        identity_run_id: str,
        durable_status: RunStatus,
    ) -> None:
        """在没有 AG-UI terminal 时收敛 Messaging 已确认的业务终态"""

        if durable_status not in {"completed", "cancelled", "failed", "owner_lost"}:
            raise ValueError("只有 durable 终态可以收敛业务运行")
        thread = await self._session.scalar(
            select(ConversationThread)
            .where(ConversationThread.id == thread_pk)
            .with_for_update()
        )
        if thread is None:
            raise LookupError(f"会话不存在: {thread_pk}")
        run = await self._session.scalar(
            select(ConversationRun)
            .where(
                ConversationRun.conversation_thread_id == thread_pk,
                ConversationRun.run_id == identity_run_id,
            )
            .with_for_update()
        )
        if run is None or run.status not in {"preparing", "running"}:
            return

        now = datetime.now(UTC).replace(tzinfo=None)
        if durable_status == "cancelled":
            run_status = "cancelled"
            code = "messaging_cancelled_without_agui_terminal"
        else:
            run_status = "error"
            code = {
                "completed": "messaging_terminal_without_agui_terminal",
                "failed": "messaging_producer_failed",
                "owner_lost": "messaging_owner_lost",
            }[durable_status]
        run.status = run_status
        run.outcome_json = {
            "type": run_status,
            "code": code,
            "durableStatus": durable_status,
        }
        run.finished_at = now
        run.updated_at = now

        repository = ConversationRepository(self._session)
        pending = await repository.list_pending_interrupts_for_update(
            thread_pk=thread_pk
        )
        # checkpoint 前失败应让用户再次审批；已经结算的审批不能因运行失败重新出现
        for interrupt in pending:
            if interrupt.resolved_run_id == identity_run_id:
                interrupt.resolved_run_id = None
                interrupt.updated_at = now
        snapshot = (
            deepcopy(thread.snapshot_json)
            if thread.snapshot_json is not None
            else empty_snapshot()
        )
        synchronize_pending_interrupts(
            snapshot,
            [dict(interrupt.request_json) for interrupt in pending],
        )
        runs = snapshot.get("runs")
        if isinstance(runs, dict):
            snapshot_run = runs.get(identity_run_id)
            if isinstance(snapshot_run, dict):
                snapshot_run["status"] = run_status
                snapshot_run["completedAt"] = now.isoformat()
        if snapshot.get("activeRunId") == identity_run_id:
            snapshot["activeRunId"] = None
        # 不伪造模型错误消息，只把历史页和刷新后的控制状态收敛为可理解的终态
        has_pending = bool(pending)
        snapshot["runStatus"] = (
            "waiting_approval"
            if has_pending
            else ("idle" if run_status == "cancelled" else "error")
        )
        thread.snapshot_json = snapshot
        thread.has_pending_interrupt = has_pending
        thread.status = (
            "waiting_approval"
            if has_pending
            else ("idle" if run_status == "cancelled" else "error")
        )
        thread.updated_at = max(thread.updated_at, now)
        await self._session.flush()

    @staticmethod
    def _event_run_id(event: dict[str, object], fallback: str) -> str:
        run_id = event.get("runId")
        if isinstance(run_id, str) and run_id:
            return run_id
        raw_event = event.get("rawEvent")
        raw_run_id = raw_event.get("runId") if isinstance(raw_event, dict) else None
        return raw_run_id if isinstance(raw_run_id, str) and raw_run_id else fallback

    @staticmethod
    def _thread_status(snapshot: dict[str, object]) -> str:
        statuses = {
            "streaming": "running",
            "waiting_approval": "waiting_approval",
            "error": "error",
        }
        return statuses.get(str(snapshot.get("runStatus")), "idle")

    async def _project_discovered_subagent_runs(
        self,
        *,
        thread_pk: int,
        main_run: ConversationRun | None,
        event: dict[str, object],
        created_at: datetime,
    ) -> None:
        """从已校验的 RAW task 描述建立服务端子 run 投影"""

        if event.get("type") != "RAW" or event.get("source") != "langgraph.tasks":
            return
        raw_event = event.get("rawEvent")
        wrapped = event.get("event")
        if (
            not isinstance(raw_event, dict)
            or raw_event.get("type") != "tasks"
            or raw_event.get("phase") != "start"
            or not isinstance(wrapped, dict)
        ):
            return
        provenance = wrapped.get("provenance")
        descriptors = (
            provenance.get("subagents") if isinstance(provenance, dict) else None
        )
        if not isinstance(descriptors, list):
            return
        if main_run is None and descriptors:
            raise RuntimeError("RAW task 子 Agent 缺少主 run")
        for descriptor in descriptors:
            if not isinstance(descriptor, dict):
                raise TypeError("RAW task subagents 项必须是对象")
            provenance = SubagentProvenance.model_validate(descriptor)
            if main_run is None or provenance.request_run_id != main_run.run_id:
                raise RuntimeError("RAW task 子 Agent 的当前主 run 不一致")
            input_json: dict[str, object] = {
                "description": provenance.description,
                "subagent_type": provenance.agent_name,
                "parent_tool_call_id": provenance.parent_tool_call_id,
                "namespace": list(provenance.namespace),
            }
            existing = await self._session.scalar(
                select(ConversationRun).where(
                    ConversationRun.conversation_thread_id == thread_pk,
                    ConversationRun.run_id == provenance.subagent_invocation_id,
                )
            )
            if existing is not None:
                if (
                    existing.agent_type != "subagent"
                    or existing.agent_name != provenance.agent_name
                    or existing.graph_task_id != provenance.graph_task_id
                    or existing.input_json != input_json
                ):
                    raise RuntimeError(
                        f"子 Agent run 身份冲突: {provenance.subagent_invocation_id}"
                    )
                existing.last_main_run_id = provenance.request_run_id
                existing.status = "running"
                existing.outcome_json = None
                existing.finished_at = None
                existing.updated_at = created_at
                continue
            self._session.add(
                ConversationRun(
                    conversation_thread_id=thread_pk,
                    run_id=provenance.subagent_invocation_id,
                    parent_run_id=None,
                    origin_main_run_id=provenance.request_run_id,
                    last_main_run_id=provenance.request_run_id,
                    agent_type="subagent",
                    agent_name=provenance.agent_name,
                    graph_task_id=provenance.graph_task_id,
                    model_id=main_run.model_id,
                    status="running",
                    input_json=input_json,
                    config_json=None,
                    outcome_json=None,
                    started_at=created_at,
                    finished_at=None,
                    created_at=created_at,
                    updated_at=created_at,
                )
            )
        await self._session.flush()

    async def _project_related_subagent_run(
        self,
        *,
        thread_pk: int,
        event: dict[str, object],
        created_at: datetime,
    ) -> None:
        """用父 task 的 ToolMessage 终结关联子 run"""

        if event.get("type") != "TOOL_CALL_RESULT":
            return
        raw_event = event.get("rawEvent")
        if not isinstance(raw_event, dict):
            return
        related_run_id = raw_event.get("relatedSubagentInvocationId")
        result_status = raw_event.get("toolResultStatus")
        if not isinstance(related_run_id, str) or result_status not in {
            "success",
            "error",
        }:
            return
        run = await self._session.scalar(
            select(ConversationRun).where(
                ConversationRun.conversation_thread_id == thread_pk,
                ConversationRun.run_id == related_run_id,
            )
        )
        if run is None or run.agent_type != "subagent":
            raise RuntimeError(f"父 task 引用了未知子 run: {related_run_id}")
        run.status = "success" if result_status == "success" else "error"
        run.outcome_json = {"type": run.status}
        run.finished_at = created_at
        run.updated_at = created_at

    async def _project_open_subagents_on_main_error(
        self,
        *,
        thread_pk: int,
        main_run: ConversationRun | None,
        event_run_id: str,
        event: dict[str, object],
        created_at: datetime,
    ) -> None:
        """主 run 异常终止时关闭仍未返回父 task 结果的子 run"""

        if (
            main_run is None
            or event.get("type") != "RUN_ERROR"
            or event_run_id != main_run.run_id
        ):
            return
        raw_event = event.get("rawEvent")
        if (
            isinstance(raw_event, dict)
            and raw_event.get("initializationFailed") is True
        ):
            return
        status = (
            "cancelled"
            if event.get("code") in {"cancelled", "resume_cancelled"}
            else "error"
        )
        subagents = await self._session.scalars(
            select(ConversationRun).where(
                ConversationRun.conversation_thread_id == thread_pk,
                ConversationRun.agent_type == "subagent",
                ConversationRun.status == "running",
                ConversationRun.last_main_run_id == main_run.run_id,
            )
        )
        for subagent in subagents:
            subagent.status = status
            subagent.outcome_json = {
                "type": status,
                "parentRunId": main_run.run_id,
                "code": event.get("code"),
            }
            subagent.finished_at = created_at
            subagent.updated_at = created_at

    async def _project_run(
        self,
        run: ConversationRun | None,
        event: dict[str, object],
        created_at: datetime,
    ) -> None:
        if run is None:
            return
        event_type = event.get("type")
        if event_type == "RUN_STARTED":
            run.status = "running"
        elif event_type == "RUN_FINISHED":
            outcome = event.get("outcome")
            outcome_type = (
                outcome.get("type", "success")
                if isinstance(outcome, dict)
                else "success"
            )
            run.status = str(outcome_type)
            run.outcome_json = (
                outcome if isinstance(outcome, dict) else {"type": "success"}
            )
            run.finished_at = created_at
        elif event_type == "RUN_ERROR":
            run.status = (
                "cancelled"
                if event.get("code") in {"cancelled", "resume_cancelled"}
                else "error"
            )
            run.outcome_json = event
            run.finished_at = created_at
        run.updated_at = created_at

    async def _project_interrupts(
        self,
        thread_pk: int,
        run_id: str,
        event: dict[str, object],
        created_at: datetime,
        run_input: dict[str, object] | None,
    ) -> None:
        event_type = event.get("type")
        resume = run_input.get("resume") if run_input is not None else None
        resume_entries = resume if isinstance(resume, list) else []
        raw_event = event.get("rawEvent")
        initialization_failed = (
            isinstance(raw_event, dict)
            and raw_event.get("initializationFailed") is True
        )
        if event_type == "RUN_ERROR" and (
            event.get("code") == "runtime_initialization_error" or initialization_failed
        ):
            for entry in resume_entries:
                if not isinstance(entry, dict):
                    continue
                interrupt_id = entry.get("interruptId")
                if not isinstance(interrupt_id, str):
                    continue
                entity = await self._session.scalar(
                    select(ConversationInterrupt).where(
                        ConversationInterrupt.conversation_thread_id == thread_pk,
                        ConversationInterrupt.interrupt_id == interrupt_id,
                    )
                )
                if (
                    entity is None
                    or entity.resolved_run_id != run_id
                    or entity.status != "pending"
                ):
                    continue
                entity.status = "pending"
                entity.resolved_run_id = None
                entity.resume_json = None
                entity.resolved_at = None
                entity.updated_at = created_at
            return
        if event_type != "RUN_FINISHED":
            return
        outcome = event.get("outcome")
        interrupts = outcome.get("interrupts") if isinstance(outcome, dict) else None
        if not isinstance(interrupts, list):
            return
        for value in interrupts:
            if not isinstance(value, dict) or not isinstance(value.get("id"), str):
                continue
            interrupt_id = value["id"]
            entity = await self._session.scalar(
                select(ConversationInterrupt).where(
                    ConversationInterrupt.conversation_thread_id == thread_pk,
                    ConversationInterrupt.interrupt_id == interrupt_id,
                )
            )
            if entity is None:
                entity = ConversationInterrupt(
                    conversation_thread_id=thread_pk,
                    run_id=run_id,
                    interrupt_id=interrupt_id,
                    created_at=created_at,
                )
                self._session.add(entity)
            entity.status = "pending"
            entity.reason = str(value.get("reason", "tool_call"))
            message = value.get("message")
            entity.message = message if isinstance(message, str) else None
            entity.request_json = value
            entity.updated_at = created_at

    async def _resume_is_settled(
        self,
        *,
        thread_pk: int,
        run_id: str,
        run_input: dict[str, object] | None,
    ) -> bool:
        """只在框架 settlement 已落库时允许快照移除 pending 审批"""

        resume = run_input.get("resume") if run_input is not None else None
        if not isinstance(resume, list) or not resume:
            return False
        interrupt_ids = {
            entry.get("interruptId")
            for entry in resume
            if isinstance(entry, dict) and isinstance(entry.get("interruptId"), str)
        }
        if len(interrupt_ids) != len(resume):
            raise RuntimeError("已注册 resume 缺少完整 interruptId")
        entities = await self._session.scalars(
            select(ConversationInterrupt).where(
                ConversationInterrupt.conversation_thread_id == thread_pk,
                ConversationInterrupt.interrupt_id.in_(interrupt_ids),
            )
        )
        resolved = list(entities)
        return len(resolved) == len(interrupt_ids) and all(
            entity.resolved_run_id == run_id
            and entity.status in {"resolved", "cancelled"}
            for entity in resolved
        )

    async def _settle_abandoned_resume(
        self,
        *,
        thread_pk: int,
        main_run: ConversationRun | None,
        event: dict[str, object],
        run_input: dict[str, object] | None,
    ) -> None:
        """在框架放弃终态持久提交后结算无 Graph 的 cancelled resume"""

        if (
            main_run is None
            or event.get("type") != "RUN_ERROR"
            or event.get("code") != "resume_cancelled"
        ):
            return
        raw_entries = run_input.get("resume") if run_input is not None else None
        if not isinstance(raw_entries, list) or not raw_entries:
            raise RuntimeError("审批放弃终态缺少 resume 输入")
        entries = tuple(ResumeEntry.model_validate(entry) for entry in raw_entries)
        if any(entry.status != "cancelled" for entry in entries):
            raise RuntimeError("审批放弃终态包含非 cancelled 条目")
        await ConversationRepository(self._session).settle_claimed_interrupts(
            thread_pk=thread_pk,
            run_id=main_run.run_id,
            entries=entries,
            resolution_id=f"abandon:{main_run.run_id}",
        )
