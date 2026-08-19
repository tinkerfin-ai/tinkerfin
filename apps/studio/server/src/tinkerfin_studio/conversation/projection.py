"""Messaging 已提交事件的幂等 MySQL 投影"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

from ag_ui.core import BaseEvent
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tinkerfin_messaging.models import MessageEnvelope
from tinkerfin_studio.conversation.models import (
    ConversationEvent,
    ConversationInterrupt,
    ConversationMessage,
    ConversationRun,
    ConversationThread,
)
from tinkerfin_studio.conversation.snapshot import reduce_snapshot


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
                ConversationRun.run_id == envelope.run,
            )
        )
        created_at = envelope.created_at.astimezone(UTC).replace(tzinfo=None)
        await self._project_discovered_subagent_runs(
            thread_pk=thread_pk,
            main_run=main_run,
            event=event_json,
            created_at=created_at,
        )
        event_run_id = self._event_run_id(event_json, envelope.run)
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
            run = self._new_subagent_run(
                thread_pk=thread_pk,
                event_run_id=event_run_id,
                main_run=main_run,
                event=event_json,
                created_at=created_at,
            )
            self._session.add(run)
        run_input = (
            main_run.input_json
            if main_run is not None and event_run_id == envelope.run
            else None
        )
        snapshot = reduce_snapshot(
            thread.snapshot_json,
            seq=envelope.seq,
            event=event,
            run_id=event_run_id,
            created_at=envelope.created_at,
            run_input=run_input,
        )
        self._session.add(
            ConversationEvent(
                conversation_thread_id=thread_pk,
                run_id=envelope.run,
                seq=envelope.seq,
                event_id=envelope.message_id,
                event_type=event_type,
                schema_version=2,
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
        await self._project_messages(
            thread_pk, event_run_id, snapshot, envelope.seq, created_at
        )

        thread.last_seq = envelope.seq
        thread.snapshot_seq = envelope.seq
        thread.snapshot_json = snapshot
        thread.last_run_id = envelope.run
        thread.status = self._thread_status(snapshot)
        thread.has_pending_interrupt = snapshot.get("approval") is not None
        thread.message_count = sum(
            1
            for message in cast(list[dict[str, object]], snapshot["messages"])
            if message.get("role") in {"user", "assistant"}
        )
        if event_type == "TOOL_CALL_START":
            thread.tool_call_count += 1
        thread.updated_at = created_at
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
    def _new_subagent_run(
        *,
        thread_pk: int,
        event_run_id: str,
        main_run: ConversationRun | None,
        event: dict[str, object],
        created_at: datetime,
    ) -> ConversationRun:
        raw_event = event.get("rawEvent")
        source = raw_event.get("source") if isinstance(raw_event, dict) else None
        parent_agent_run_id = (
            raw_event.get("parentAgentRunId") if isinstance(raw_event, dict) else None
        )
        return ConversationRun(
            conversation_thread_id=thread_pk,
            run_id=event_run_id,
            parent_run_id=(
                event.get("parentRunId")
                if isinstance(event.get("parentRunId"), str)
                else None
            ),
            parent_agent_run_id=(
                parent_agent_run_id
                if isinstance(parent_agent_run_id, str)
                else (None if main_run is None else main_run.run_id)
            ),
            agent_type=(
                str(source.get("agentType", "subagent"))
                if isinstance(source, dict)
                else "subagent"
            ),
            agent_name=(
                str(source.get("agentName"))
                if isinstance(source, dict) and source.get("agentName") is not None
                else None
            ),
            graph_task_id=(
                str(source.get("graphTaskId"))
                if isinstance(source, dict) and source.get("graphTaskId") is not None
                else None
            ),
            model_id=None if main_run is None else main_run.model_id,
            status="running",
            input_json=None,
            config_json=None,
            outcome_json=None,
            started_at=created_at,
            finished_at=None,
            created_at=created_at,
            updated_at=created_at,
        )

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
            run_id = descriptor.get("runId")
            parent_run_id = descriptor.get("parentAgentRunId")
            graph_task_id = descriptor.get("graphTaskId")
            agent_name = descriptor.get("agentName")
            parent_tool_call_id = descriptor.get("parentToolCallId")
            description = descriptor.get("description")
            namespace = descriptor.get("namespace")
            if (
                not all(
                    isinstance(value, str) and value
                    for value in (
                        run_id,
                        parent_run_id,
                        graph_task_id,
                        agent_name,
                        parent_tool_call_id,
                        description,
                    )
                )
                or not isinstance(namespace, list)
                or not all(isinstance(value, str) and value for value in namespace)
            ):
                raise ValueError("RAW task 子 Agent 描述不完整")
            if main_run is None or parent_run_id != main_run.run_id:
                raise RuntimeError("RAW task 子 Agent 的父 run 不一致")
            input_json: dict[str, object] = {
                "description": description,
                "subagent_type": agent_name,
                "parent_tool_call_id": parent_tool_call_id,
                "namespace": namespace,
            }
            existing = await self._session.scalar(
                select(ConversationRun).where(
                    ConversationRun.conversation_thread_id == thread_pk,
                    ConversationRun.run_id == run_id,
                )
            )
            if existing is not None:
                if (
                    existing.agent_type != "subagent"
                    or existing.agent_name != agent_name
                    or existing.graph_task_id != graph_task_id
                    or existing.input_json != input_json
                ):
                    raise RuntimeError(f"子 Agent run 身份冲突: {run_id}")
                continue
            self._session.add(
                ConversationRun(
                    conversation_thread_id=thread_pk,
                    run_id=run_id,
                    parent_run_id=None,
                    parent_agent_run_id=parent_run_id,
                    agent_type="subagent",
                    agent_name=agent_name,
                    graph_task_id=graph_task_id,
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
        related_run_id = raw_event.get("relatedRunId")
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
        status = (
            "cancelled"
            if event.get("code") in {"cancelled", "resume_cancelled"}
            else "error"
        )
        # resume 会更换主请求 run ID，子 run 保留首次父级，因此按会话关闭当前全部运行项
        subagents = await self._session.scalars(
            select(ConversationRun).where(
                ConversationRun.conversation_thread_id == thread_pk,
                ConversationRun.agent_type == "subagent",
                ConversationRun.status == "running",
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
                if entity is None or entity.resolved_run_id != run_id:
                    continue
                entity.status = "pending"
                entity.resolved_run_id = None
                entity.resume_json = None
                entity.resolved_at = None
                entity.updated_at = created_at
            return
        if event_type == "RUN_STARTED" and run_input is not None:
            if initialization_failed:
                return
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
                if entity is None:
                    continue
                if entity.resolved_run_id != run_id:
                    continue
                entity.status = (
                    "cancelled" if entry.get("status") == "cancelled" else "resolved"
                )
                entity.resume_json = entry
                entity.resolved_at = created_at
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

    async def _project_messages(
        self,
        thread_pk: int,
        run_id: str,
        snapshot: dict[str, object],
        seq: int,
        created_at: datetime,
    ) -> None:
        messages = cast(list[dict[str, object]], snapshot["messages"])
        for value in messages:
            message_id = value.get("id")
            if not isinstance(message_id, str):
                continue
            meta = value.get("meta")
            message_run_id = (
                meta.get("runId")
                if isinstance(meta, dict) and isinstance(meta.get("runId"), str)
                else run_id
            )
            entity = await self._session.scalar(
                select(ConversationMessage).where(
                    ConversationMessage.conversation_thread_id == thread_pk,
                    ConversationMessage.message_id == message_id,
                )
            )
            if entity is None:
                entity = ConversationMessage(
                    conversation_thread_id=thread_pk,
                    run_id=message_run_id,
                    message_id=message_id,
                    first_seq=seq,
                    created_at=created_at,
                )
                self._session.add(entity)
            entity.role = str(value.get("role", "assistant"))
            entity.content = str(value.get("content", ""))
            entity.meta_json = meta if isinstance(meta, dict) else None
            entity.status = (
                str(meta.get("status", "completed"))
                if isinstance(meta, dict)
                else "completed"
            )
            entity.last_seq = seq
            entity.updated_at = created_at
