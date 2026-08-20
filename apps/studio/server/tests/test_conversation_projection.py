from collections.abc import Mapping
from datetime import UTC, datetime
from typing import cast

import pytest
from ag_ui.core import BaseEvent, Event
from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tinkerfin import Identity
from tinkerfin_messaging.models import MessageEnvelope
from tinkerfin_studio.conversation.models import (
    ConversationEvent,
    ConversationInterrupt,
    ConversationMessage,
    ConversationRun,
)
from tinkerfin_studio.conversation.projection import ConversationProjector
from tinkerfin_studio.conversation.repository import ConversationRepository

_EVENT_ADAPTER = TypeAdapter(Event)


def _envelope(
    seq: int,
    event: Mapping[str, object],
    *,
    run: str = "run-1",
) -> tuple[MessageEnvelope, BaseEvent]:
    parsed = cast(BaseEvent, _EVENT_ADAPTER.validate_python(dict(event)))
    return (
        MessageEnvelope(
            channel="studio-conversation-agui",
            identity=Identity(
                threadId="users/7/threads/thread-1",
                runId=run,
            ),
            seq=seq,
            message_id=f"{run}:{seq}",
            codec="agui.event.v1",
            payload=parsed.model_dump_json(by_alias=True, exclude_none=True).encode(),
            created_at=datetime(2026, 8, 18, 1, seq, tzinfo=UTC),
        ),
        parsed,
    )


async def test_projection_builds_tool_todo_and_interrupt_snapshot(
    session: AsyncSession,
) -> None:
    """已提交事件应同时更新事实表、明细投影和 v2 快照"""

    repository = ConversationRepository(session)
    thread = await repository.create_thread(
        user_id=7,
        thread_id="thread-1",
        title="验证投影",
        model_id="main",
    )
    await repository.create_main_run(
        thread_id=thread.id,
        run_id="run-1",
        model_id="main",
        input_json={
            "messages": [{"id": "user-1", "role": "user", "content": "执行任务"}]
        },
        config_json={},
    )
    await session.commit()
    projector = ConversationProjector(session)
    events = [
        {
            "type": "RUN_STARTED",
            "threadId": "thread-1",
            "runId": "run-1",
        },
        {
            "type": "TOOL_CALL_START",
            "toolCallId": "tool-1",
            "toolCallName": "write_todos",
        },
        {"type": "TOOL_CALL_ARGS", "toolCallId": "tool-1", "delta": '{"todos":[]}'},
        {"type": "TOOL_CALL_END", "toolCallId": "tool-1"},
        {
            "type": "TOOL_CALL_RESULT",
            "messageId": "tool-result-1",
            "toolCallId": "tool-1",
            "content": "Todos updated",
            "role": "tool",
        },
        {
            "type": "STATE_SNAPSHOT",
            "snapshot": {
                "todos": [
                    {"content": "研究资料", "status": "in_progress"},
                    {"content": "写入文件", "status": "pending"},
                ]
            },
        },
        {
            "type": "RUN_FINISHED",
            "threadId": "thread-1",
            "runId": "run-1",
            "outcome": {
                "type": "interrupt",
                "interrupts": [
                    {
                        "id": "interrupt-1",
                        "reason": "tool_call",
                        "message": "确认写入",
                        "toolCallId": "tool-write",
                        "metadata": {
                            "action_request": {
                                "name": "write_file",
                                "args": {"file_path": "/result.txt"},
                            },
                            "review_config": {
                                "allowed_decisions": ["approve", "edit", "reject"]
                            },
                        },
                    }
                ],
            },
        },
    ]

    for seq, event in enumerate(events, start=1):
        envelope, parsed = _envelope(seq, event)
        await projector.project(thread_pk=thread.id, envelope=envelope, event=parsed)

    await session.commit()
    refreshed = await repository.get_thread_by_pk(thread.id)
    assert refreshed is not None
    assert refreshed.last_seq == len(events)
    assert refreshed.snapshot_seq == len(events)
    assert refreshed.status == "waiting_approval"
    assert refreshed.has_pending_interrupt is True
    assert refreshed.tool_call_count == 1
    snapshot = refreshed.snapshot_json
    assert snapshot is not None
    assert snapshot["todos"] == [
        {"id": "todo-0", "content": "研究资料", "status": "running"},
        {"id": "todo-1", "content": "写入文件", "status": "pending"},
    ]
    approval = snapshot["approval"]
    messages = snapshot["messages"]
    assert isinstance(approval, dict)
    assert isinstance(messages, list)
    assert approval["items"][0]["toolName"] == "write_file"
    assert messages[0]["id"] == "user-1"
    assert await repository.count_events(thread.id) == len(events)


async def test_projection_is_idempotent_and_rejects_same_seq_with_other_payload(
    session: AsyncSession,
) -> None:
    """重复交付只能重放同一事实，不得覆盖已提交序号"""

    repository = ConversationRepository(session)
    thread = await repository.create_thread(
        user_id=7,
        thread_id="thread-idempotent",
        title="幂等",
        model_id="main",
    )
    await repository.create_main_run(
        thread_id=thread.id,
        run_id="run-1",
        model_id="main",
        input_json={"messages": []},
        config_json={},
    )
    await session.commit()
    projector = ConversationProjector(session)
    envelope, event = _envelope(
        1,
        {"type": "RUN_STARTED", "threadId": "thread-idempotent", "runId": "run-1"},
    )

    await projector.project(thread_pk=thread.id, envelope=envelope, event=event)
    await projector.project(thread_pk=thread.id, envelope=envelope, event=event)
    await session.commit()

    changed, changed_event = _envelope(
        1,
        {"type": "RUN_ERROR", "message": "changed", "code": "changed"},
    )
    with pytest.raises(RuntimeError, match="序号.*内容不一致"):
        await projector.project(
            thread_pk=thread.id,
            envelope=changed,
            event=changed_event,
        )

    assert await repository.count_events(thread.id) == 1
    stored = await session.get(ConversationEvent, 1)
    assert stored is not None
    assert stored.event_type == "RUN_STARTED"


async def test_resume_projection_cannot_take_another_run_claim(
    session: AsyncSession,
) -> None:
    """迟到的 RUN_STARTED 不得覆盖其他 resume run 的审批认领"""

    repository = ConversationRepository(session)
    thread = await repository.create_thread(
        user_id=7,
        thread_id="thread-resume-claim",
        title="恢复认领",
        model_id="main",
    )
    await repository.create_main_run(
        thread_id=thread.id,
        run_id="run-late",
        model_id="main",
        input_json={
            "messages": [],
            "resume": [
                {
                    "interruptId": "interrupt-claimed",
                    "status": "resolved",
                    "payload": {"type": "approve"},
                }
            ],
        },
        config_json={},
    )
    created_at = datetime(2026, 8, 18, 1, 0)
    session.add(
        ConversationInterrupt(
            conversation_thread_id=thread.id,
            run_id="run-interrupted",
            resolved_run_id="run-owner",
            interrupt_id="interrupt-claimed",
            status="pending",
            reason="tool_call",
            message="确认写入",
            request_json={"id": "interrupt-claimed", "reason": "tool_call"},
            resume_json=None,
            created_at=created_at,
            resolved_at=None,
            updated_at=created_at,
        )
    )
    await session.commit()
    projector = ConversationProjector(session)
    envelope, event = _envelope(
        1,
        {
            "type": "RUN_STARTED",
            "threadId": thread.thread_id,
            "runId": "run-late",
        },
        run="run-late",
    )

    await projector.project(thread_pk=thread.id, envelope=envelope, event=event)
    await session.commit()

    stored = await session.scalar(
        select(ConversationInterrupt).where(
            ConversationInterrupt.conversation_thread_id == thread.id,
            ConversationInterrupt.interrupt_id == "interrupt-claimed",
        )
    )
    assert stored is not None
    assert stored.status == "pending"
    assert stored.resolved_run_id == "run-owner"
    assert stored.resume_json is None
    assert stored.resolved_at is None


@pytest.mark.parametrize(
    ("error_code", "run_status"),
    (("runtime_initialization_error", "error"), ("cancelled", "cancelled")),
)
async def test_initialization_error_releases_resume_claim_without_consuming_it(
    session: AsyncSession,
    error_code: str,
    run_status: str,
) -> None:
    """Graph 尚未创建时的错误终止不得消费 checkpoint 中的待审批项"""

    repository = ConversationRepository(session)
    thread = await repository.create_thread(
        user_id=7,
        thread_id="thread-init-failure",
        title="初始化失败",
        model_id="main",
    )
    run = await repository.create_main_run(
        thread_id=thread.id,
        run_id="run-init-failure",
        model_id="main",
        input_json={
            "messages": [],
            "resume": [
                {
                    "interruptId": "interrupt-init-failure",
                    "status": "resolved",
                    "payload": {"type": "approve"},
                }
            ],
        },
        config_json={},
    )
    created_at = datetime(2026, 8, 18, 1, 0)
    session.add(
        ConversationInterrupt(
            conversation_thread_id=thread.id,
            run_id="run-interrupted",
            resolved_run_id=run.run_id,
            interrupt_id="interrupt-init-failure",
            status="pending",
            reason="tool_call",
            message="确认写入",
            request_json={"id": "interrupt-init-failure", "reason": "tool_call"},
            resume_json=None,
            created_at=created_at,
            resolved_at=None,
            updated_at=created_at,
        )
    )
    await session.commit()
    projector = ConversationProjector(session)
    events = (
        {
            "type": "RUN_STARTED",
            "threadId": thread.thread_id,
            "runId": run.run_id,
            "rawEvent": {
                "runId": run.run_id,
                "initializationFailed": True,
            },
        },
        {
            "type": "RUN_ERROR",
            "message": "Agent run failed",
            "code": error_code,
            "rawEvent": {
                "runId": run.run_id,
                "initializationFailed": True,
            },
        },
    )

    for seq, value in enumerate(events, start=1):
        envelope, event = _envelope(seq, value, run=run.run_id)
        await projector.project(thread_pk=thread.id, envelope=envelope, event=event)
    await session.commit()

    stored = await session.scalar(
        select(ConversationInterrupt).where(
            ConversationInterrupt.conversation_thread_id == thread.id,
            ConversationInterrupt.interrupt_id == "interrupt-init-failure",
        )
    )
    await session.refresh(run)
    assert stored is not None
    assert stored.status == "pending"
    assert stored.resolved_run_id is None
    assert stored.resume_json is None
    assert stored.resolved_at is None
    assert run.status == run_status


async def test_subagent_terminal_does_not_finish_the_main_run(
    session: AsyncSession,
) -> None:
    """子 Agent 生命周期必须写入独立 run，不能抢占主 run 终态"""

    repository = ConversationRepository(session)
    thread = await repository.create_thread(
        user_id=7,
        thread_id="thread-subagent",
        title="子 Agent",
        model_id="main",
    )
    main_run = await repository.create_main_run(
        thread_id=thread.id,
        run_id="run-1",
        model_id="main",
        input_json={"messages": []},
        config_json={},
    )
    await session.commit()
    projector = ConversationProjector(session)
    raw_event = {
        "streamMode": "tasks",
        "source": {
            "agentType": "subagent",
            "agentName": "researcher",
            "namespace": ["tools:task-1"],
            "graphTaskId": "task-1",
        },
        "runId": "sub-run-1",
        "parentAgentRunId": "run-1",
    }
    events = (
        {
            "type": "RUN_STARTED",
            "threadId": "thread-subagent",
            "runId": "sub-run-1",
            "rawEvent": raw_event,
        },
        {
            "type": "RUN_FINISHED",
            "threadId": "thread-subagent",
            "runId": "sub-run-1",
            "outcome": {"type": "success"},
            "rawEvent": raw_event,
        },
    )

    for seq, value in enumerate(events, start=1):
        envelope, event = _envelope(seq, value)
        await projector.project(thread_pk=thread.id, envelope=envelope, event=event)
    await session.commit()

    await session.refresh(main_run)
    sub_run = await repository.get_run(thread_pk=thread.id, run_id="sub-run-1")
    assert main_run.status == "running"
    assert sub_run is not None
    assert sub_run.agent_type == "subagent"
    assert sub_run.agent_name == "researcher"
    assert sub_run.status == "success"


async def test_real_task_provenance_projects_subagent_run_and_inner_tool(
    session: AsyncSession,
) -> None:
    """真实 RAW task 关联必须建立子 run，并归属其内部 Tool"""

    repository = ConversationRepository(session)
    thread = await repository.create_thread(
        user_id=7,
        thread_id="thread-real-subagent",
        title="真实子 Agent",
        model_id="main",
    )
    main_run = await repository.create_main_run(
        thread_id=thread.id,
        run_id="run-1",
        model_id="main",
        input_json={
            "messages": [{"id": "user-real", "role": "user", "content": "检索资料"}]
        },
        config_json={},
    )
    await session.commit()
    projector = ConversationProjector(session)
    source = {
        "kind": "deep_agent_subagent",
        "agentType": "subagent",
        "agentName": "researcher",
        "namespace": ["tools:graph-task"],
        "graphTaskId": "graph-task",
        "parentToolCallId": "tool-task",
        "subagentInput": "检索 LangGraph",
    }
    events = (
        {"type": "RUN_STARTED", "threadId": thread.thread_id, "runId": "run-1"},
        {
            "type": "RAW",
            "source": "langgraph.tasks",
            "rawEvent": {"type": "tasks", "phase": "start", "ns": []},
            "event": {
                "data": {"id": "graph-task", "name": "tools"},
                "provenance": {
                    "kind": "root",
                    "namespace": [],
                    "agentType": "main",
                    "agentName": "main",
                    "subagents": [
                        {
                            "namespace": ["tools:graph-task"],
                            "graphTaskId": "graph-task",
                            "agentName": "researcher",
                            "parentToolCallId": "tool-task",
                            "description": "检索 LangGraph",
                            "runId": "subrun-server",
                            "parentAgentRunId": "run-1",
                        }
                    ],
                },
            },
        },
        {
            "type": "TOOL_CALL_START",
            "toolCallId": "tool-search",
            "toolCallName": "web_search",
            "rawEvent": {
                "streamMode": "messages",
                "runId": "subrun-server",
                "parentAgentRunId": "run-1",
                "source": source,
            },
        },
        {
            "type": "TOOL_CALL_ARGS",
            "toolCallId": "tool-search",
            "delta": '{"query":"LangGraph"}',
            "rawEvent": {
                "streamMode": "messages",
                "runId": "subrun-server",
                "parentAgentRunId": "run-1",
                "source": source,
            },
        },
        {
            "type": "TOOL_CALL_END",
            "toolCallId": "tool-search",
            "rawEvent": {
                "streamMode": "messages",
                "runId": "subrun-server",
                "parentAgentRunId": "run-1",
                "source": source,
            },
        },
        {
            "type": "TOOL_CALL_RESULT",
            "messageId": "message-search",
            "toolCallId": "tool-search",
            "content": "搜索结果",
            "role": "tool",
            "rawEvent": {
                "streamMode": "messages",
                "runId": "subrun-server",
                "parentAgentRunId": "run-1",
                "toolResultStatus": "success",
                "source": source,
            },
        },
        {
            "type": "TOOL_CALL_RESULT",
            "messageId": "message-task",
            "toolCallId": "tool-task",
            "content": "子 Agent 完成",
            "role": "tool",
            "rawEvent": {
                "streamMode": "messages",
                "runId": "run-1",
                "relatedRunId": "subrun-server",
                "toolResultStatus": "success",
                "source": {
                    "kind": "root",
                    "agentType": "main",
                    "agentName": "main",
                    "namespace": [],
                },
            },
        },
    )

    for seq, value in enumerate(events[:-1], start=1):
        envelope, event = _envelope(seq, value)
        await projector.project(thread_pk=thread.id, envelope=envelope, event=event)
    await session.flush()
    user_message = await session.scalar(
        select(ConversationMessage).where(
            ConversationMessage.conversation_thread_id == thread.id,
            ConversationMessage.message_id == "user-real",
        )
    )
    assert user_message is not None
    assert user_message.run_id == "run-1"

    envelope, event = _envelope(len(events), events[-1])
    await projector.project(thread_pk=thread.id, envelope=envelope, event=event)
    await session.commit()

    await session.refresh(main_run)
    refreshed = await repository.get_thread_by_pk(thread.id)
    sub_run = await repository.get_run(thread_pk=thread.id, run_id="subrun-server")
    assert refreshed is not None
    assert main_run.status == "running"
    assert sub_run is not None
    assert sub_run.parent_agent_run_id == "run-1"
    assert sub_run.agent_name == "researcher"
    assert sub_run.graph_task_id == "graph-task"
    assert sub_run.input_json == {
        "description": "检索 LangGraph",
        "subagent_type": "researcher",
        "parent_tool_call_id": "tool-task",
        "namespace": ["tools:graph-task"],
    }
    assert sub_run.status == "success"
    snapshot = cast(dict[str, object], refreshed.snapshot_json)
    messages = cast(list[dict[str, object]], snapshot["messages"])
    subagent = next(
        message for message in messages if message.get("role") == "subagent"
    )
    subagent_meta = cast(dict[str, object], subagent["meta"])
    assert subagent_meta["subRunId"] == "subrun-server"
    assert subagent_meta["status"] == "completed"
    assert subagent_meta["input"] == "检索 LangGraph"
    assert subagent_meta["result"] == "子 Agent 完成"
    child_tool = next(
        message
        for message in messages
        if cast(dict[str, object], message.get("meta", {})).get("toolCallId")
        == "tool-search"
    )
    child_meta = cast(dict[str, object], child_tool["meta"])
    assert child_meta["runId"] == "subrun-server"
    assert child_meta["sourceAgentName"] == "researcher"


async def test_tool_error_status_survives_the_persisted_snapshot(
    session: AsyncSession,
) -> None:
    """ToolMessage error 在刷新快照中仍必须是 failed"""

    repository = ConversationRepository(session)
    thread = await repository.create_thread(
        user_id=7,
        thread_id="thread-tool-error",
        title="工具失败",
        model_id="main",
    )
    await repository.create_main_run(
        thread_id=thread.id,
        run_id="run-1",
        model_id="main",
        input_json={"messages": []},
        config_json={},
    )
    await session.commit()
    projector = ConversationProjector(session)
    raw_event = {
        "streamMode": "messages",
        "runId": "run-1",
        "toolResultStatus": "error",
        "source": {
            "kind": "root",
            "agentType": "main",
            "agentName": "main",
            "namespace": [],
        },
    }
    events = (
        {"type": "RUN_STARTED", "threadId": thread.thread_id, "runId": "run-1"},
        {
            "type": "TOOL_CALL_START",
            "toolCallId": "tool-error",
            "toolCallName": "web_search",
            "rawEvent": raw_event,
        },
        {
            "type": "TOOL_CALL_RESULT",
            "messageId": "message-tool-error",
            "toolCallId": "tool-error",
            "content": "搜索失败",
            "role": "tool",
            "rawEvent": raw_event,
        },
    )
    for seq, value in enumerate(events, start=1):
        envelope, event = _envelope(seq, value)
        await projector.project(thread_pk=thread.id, envelope=envelope, event=event)
    await session.commit()

    refreshed = await repository.get_thread_by_pk(thread.id)
    assert refreshed is not None and refreshed.snapshot_json is not None
    messages = cast(list[dict[str, object]], refreshed.snapshot_json["messages"])
    tool = next(
        message
        for message in messages
        if cast(dict[str, object], message.get("meta", {})).get("toolCallId")
        == "tool-error"
    )
    meta = cast(dict[str, object], tool["meta"])
    assert meta["status"] == "failed"
    stored = await session.scalar(
        select(ConversationMessage).where(
            ConversationMessage.conversation_thread_id == thread.id,
            ConversationMessage.message_id == "tool-error",
        )
    )
    assert stored is not None
    assert stored.status == "failed"


async def test_subagent_identity_survives_a_new_main_resume_run(
    session: AsyncSession,
) -> None:
    """同一 namespace 跨 resume 仍必须引用原服务端子 run"""

    repository = ConversationRepository(session)
    thread = await repository.create_thread(
        user_id=7,
        thread_id="thread-subagent-resume",
        title="子 Agent resume",
        model_id="main",
    )
    await repository.create_main_run(
        thread_id=thread.id,
        run_id="run-1",
        model_id="main",
        input_json={"messages": []},
        config_json={},
    )
    await repository.create_main_run(
        thread_id=thread.id,
        run_id="run-resume",
        model_id="main",
        input_json={"messages": [], "resume": [{"interruptId": "interrupt-1"}]},
        config_json={},
    )
    await session.commit()
    projector = ConversationProjector(session)

    def raw_task_start(parent_run_id: str) -> dict[str, object]:
        return {
            "type": "RAW",
            "source": "langgraph.tasks",
            "rawEvent": {"type": "tasks", "phase": "start", "ns": []},
            "event": {
                "data": {"id": "graph-resume", "name": "tools"},
                "provenance": {
                    "kind": "root",
                    "namespace": [],
                    "agentType": "main",
                    "agentName": "main",
                    "subagents": [
                        {
                            "namespace": ["tools:graph-resume"],
                            "graphTaskId": "graph-resume",
                            "agentName": "researcher",
                            "parentToolCallId": "tool-task-resume",
                            "description": "继续研究",
                            "runId": "subrun-stable",
                            "parentAgentRunId": parent_run_id,
                        }
                    ],
                },
            },
        }

    values = (
        (
            "run-1",
            {"type": "RUN_STARTED", "threadId": thread.thread_id, "runId": "run-1"},
        ),
        ("run-1", raw_task_start("run-1")),
        (
            "run-resume",
            {
                "type": "RUN_STARTED",
                "threadId": thread.thread_id,
                "runId": "run-resume",
            },
        ),
        ("run-resume", raw_task_start("run-resume")),
        (
            "run-resume",
            {
                "type": "RUN_ERROR",
                "message": "resume 已取消",
                "code": "cancelled",
                "rawEvent": {"runId": "run-resume"},
            },
        ),
    )
    for seq, (run_id, value) in enumerate(values, start=1):
        envelope, event = _envelope(seq, value, run=run_id)
        await projector.project(thread_pk=thread.id, envelope=envelope, event=event)
    await session.commit()

    projected = list(
        await session.scalars(
            select(ConversationRun).where(
                ConversationRun.conversation_thread_id == thread.id,
                ConversationRun.agent_type == "subagent",
            )
        )
    )
    assert len(projected) == 1
    assert projected[0].run_id == "subrun-stable"
    assert projected[0].parent_agent_run_id == "run-1"
    assert projected[0].status == "cancelled"
    assert projected[0].finished_at is not None


@pytest.mark.parametrize(
    ("code", "expected_run_status", "expected_thread_status"),
    [
        ("runtime_error", "error", "error"),
        ("cancelled", "cancelled", "idle"),
    ],
)
async def test_main_error_terminates_discovered_subagent_runs(
    session: AsyncSession,
    code: str,
    expected_run_status: str,
    expected_thread_status: str,
) -> None:
    """主 run 异常或取消不得遗留 running 子 run"""

    repository = ConversationRepository(session)
    thread = await repository.create_thread(
        user_id=7,
        thread_id=f"thread-main-{code}",
        title="主 run 终止",
        model_id="main",
    )
    main_run = await repository.create_main_run(
        thread_id=thread.id,
        run_id="run-1",
        model_id="main",
        input_json={"messages": []},
        config_json={},
    )
    await session.commit()
    projector = ConversationProjector(session)
    source = {
        "kind": "deep_agent_subagent",
        "agentType": "subagent",
        "agentName": "researcher",
        "namespace": ["tools:graph-error"],
        "graphTaskId": "graph-error",
        "parentToolCallId": "tool-task-error",
        "subagentInput": "执行研究",
    }
    events = (
        {"type": "RUN_STARTED", "threadId": thread.thread_id, "runId": "run-1"},
        {
            "type": "RAW",
            "source": "langgraph.tasks",
            "rawEvent": {"type": "tasks", "phase": "start", "ns": []},
            "event": {
                "data": {"id": "graph-error", "name": "tools"},
                "provenance": {
                    "kind": "root",
                    "namespace": [],
                    "agentType": "main",
                    "agentName": "main",
                    "subagents": [
                        {
                            "namespace": ["tools:graph-error"],
                            "graphTaskId": "graph-error",
                            "agentName": "researcher",
                            "parentToolCallId": "tool-task-error",
                            "description": "执行研究",
                            "runId": "subrun-error",
                            "parentAgentRunId": "run-1",
                        }
                    ],
                },
            },
        },
        {
            "type": "TOOL_CALL_START",
            "toolCallId": "tool-child-running",
            "toolCallName": "web_search",
            "rawEvent": {
                "streamMode": "messages",
                "runId": "subrun-error",
                "parentAgentRunId": "run-1",
                "source": source,
            },
        },
        {
            "type": "RUN_ERROR",
            "message": "主 run 已终止",
            "code": code,
            "rawEvent": {"runId": "run-1"},
        },
    )
    for seq, value in enumerate(events, start=1):
        envelope, event = _envelope(seq, value)
        await projector.project(thread_pk=thread.id, envelope=envelope, event=event)
    await session.commit()

    await session.refresh(main_run)
    refreshed = await repository.get_thread_by_pk(thread.id)
    sub_run = await repository.get_run(thread_pk=thread.id, run_id="subrun-error")
    assert refreshed is not None and refreshed.snapshot_json is not None
    assert main_run.status == expected_run_status
    assert sub_run is not None
    assert sub_run.status == expected_run_status
    assert sub_run.finished_at is not None
    assert refreshed.status == expected_thread_status
    runs = cast(dict[str, dict[str, object]], refreshed.snapshot_json["runs"])
    assert runs["subrun-error"]["status"] == expected_run_status
    messages = cast(list[dict[str, object]], refreshed.snapshot_json["messages"])
    subagent = next(message for message in messages if message["role"] == "subagent")
    child_tool = next(
        message
        for message in messages
        if cast(dict[str, object], message.get("meta", {})).get("toolCallId")
        == "tool-child-running"
    )
    assert cast(dict[str, object], subagent["meta"])["status"] == "failed"
    assert cast(dict[str, object], child_tool["meta"])["status"] == "failed"


async def test_subagent_error_is_preserved_in_history_snapshot(
    session: AsyncSession,
) -> None:
    """子 Agent 失败必须在 run 与消息快照中同时终止"""

    repository = ConversationRepository(session)
    thread = await repository.create_thread(
        user_id=7,
        thread_id="thread-subagent-error",
        title="子 Agent 失败",
        model_id="main",
    )
    main_run = await repository.create_main_run(
        thread_id=thread.id,
        run_id="run-main",
        model_id="main",
        input_json={"messages": []},
        config_json={},
    )
    await session.commit()
    projector = ConversationProjector(session)
    raw_event = {
        "streamMode": "tasks",
        "source": {
            "agentType": "subagent",
            "agentName": "researcher",
            "namespace": ["tools:task-error"],
            "graphTaskId": "task-error",
        },
        "runId": "sub-run-error",
        "parentAgentRunId": "run-main",
    }
    events = (
        {
            "type": "RUN_STARTED",
            "threadId": "thread-subagent-error",
            "runId": "sub-run-error",
            "rawEvent": raw_event,
        },
        {
            "type": "TEXT_MESSAGE_START",
            "messageId": "subagent-message",
            "role": "assistant",
            "rawEvent": raw_event,
        },
        {
            "type": "RUN_ERROR",
            "message": "子 Agent 运行失败",
            "code": "runtime_error",
            "rawEvent": raw_event,
        },
    )
    for seq, value in enumerate(events, start=1):
        envelope, event = _envelope(seq, value)
        await projector.project(thread_pk=thread.id, envelope=envelope, event=event)
    await session.commit()

    await session.refresh(main_run)
    await session.refresh(thread)
    sub_run = await repository.get_run(
        thread_pk=thread.id,
        run_id="sub-run-error",
    )
    assert main_run.status == "running"
    assert sub_run is not None
    assert sub_run.status == "error"
    snapshot = thread.snapshot_json
    assert snapshot is not None
    runs = snapshot["runs"]
    assert isinstance(runs, dict)
    assert runs["sub-run-error"]["status"] == "error"
    messages = snapshot["messages"]
    assert isinstance(messages, list)
    subagent_message = next(
        message
        for message in messages
        if isinstance(message, dict) and message.get("id") == "subagent-message"
    )
    meta = subagent_message["meta"]
    assert isinstance(meta, dict)
    assert meta["status"] == "failed"


async def test_cancelled_main_run_returns_thread_to_idle(
    session: AsyncSession,
) -> None:
    """用户取消应保留 cancelled run 证据，但允许会话继续输入"""

    repository = ConversationRepository(session)
    thread = await repository.create_thread(
        user_id=7,
        thread_id="thread-cancelled",
        title="取消",
        model_id="main",
    )
    run = await repository.create_main_run(
        thread_id=thread.id,
        run_id="run-1",
        model_id="main",
        input_json={"messages": []},
        config_json={},
    )
    await session.commit()
    projector = ConversationProjector(session)
    for seq, value in enumerate(
        (
            {"type": "RUN_STARTED", "threadId": "thread-cancelled", "runId": "run-1"},
            {
                "type": "TOOL_CALL_START",
                "rawEvent": {
                    "streamMode": "messages",
                    "source": {
                        "agentType": "main",
                        "agentName": "main",
                        "namespace": [],
                    },
                    "runId": "run-1",
                },
                "toolCallId": "tool-running",
                "toolCallName": "web_search",
            },
            {
                "type": "TOOL_CALL_END",
                "toolCallId": "tool-running",
            },
            {
                "type": "RUN_ERROR",
                "rawEvent": {"runId": "run-1"},
                "message": "聊天生成已取消",
                "code": "cancelled",
            },
        ),
        start=1,
    ):
        envelope, event = _envelope(seq, value)
        await projector.project(thread_pk=thread.id, envelope=envelope, event=event)
    await session.commit()

    await session.refresh(thread)
    await session.refresh(run)
    snapshot = thread.snapshot_json
    assert snapshot is not None
    runs = snapshot["runs"]
    assert isinstance(runs, dict)
    assert thread.status == "idle"
    assert run.status == "cancelled"
    assert runs["run-1"]["status"] == "cancelled"
    messages = snapshot["messages"]
    assert isinstance(messages, list)
    tool = next(
        message
        for message in messages
        if isinstance(message, dict) and message.get("id") == "tool-running"
    )
    tool_meta = tool["meta"]
    assert isinstance(tool_meta, dict)
    assert tool_meta["status"] == "failed"
    assert tool_meta["result"] == "聊天生成已取消"
    terminal_message = messages[-1]
    assert isinstance(terminal_message, dict)
    assert terminal_message["role"] == "error"
    assert terminal_message["content"] == "聊天生成已取消"


async def test_state_delta_updates_server_state_and_todo_projection(
    session: AsyncSession,
) -> None:
    """RFC 6902 state delta 中的 todos 必须进入历史快照"""

    repository = ConversationRepository(session)
    thread = await repository.create_thread(
        user_id=7,
        thread_id="thread-delta",
        title="Delta",
        model_id="main",
    )
    await repository.create_main_run(
        thread_id=thread.id,
        run_id="run-1",
        model_id="main",
        input_json={"messages": []},
        config_json={},
    )
    await session.commit()
    projector = ConversationProjector(session)
    for seq, value in enumerate(
        (
            {"type": "RUN_STARTED", "threadId": "thread-delta", "runId": "run-1"},
            {
                "type": "STATE_DELTA",
                "delta": [
                    {
                        "op": "add",
                        "path": "/todos",
                        "value": [
                            {
                                "content": "验证 todo 实时输出",
                                "status": "in_progress",
                            }
                        ],
                    }
                ],
            },
        ),
        start=1,
    ):
        envelope, event = _envelope(seq, value)
        await projector.project(thread_pk=thread.id, envelope=envelope, event=event)
    await session.commit()

    await session.refresh(thread)
    snapshot = thread.snapshot_json
    assert snapshot is not None
    assert snapshot["serverState"] == {
        "todos": [{"content": "验证 todo 实时输出", "status": "in_progress"}]
    }
    assert snapshot["todos"] == [
        {
            "id": "todo-0",
            "content": "验证 todo 实时输出",
            "status": "running",
        }
    ]
