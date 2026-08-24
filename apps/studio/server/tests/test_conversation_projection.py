import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import cast

import pytest
from ag_ui.core import BaseEvent, Event
from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tinkerfin import Identity
from tinkerfin_agui_adapter import (
    ScopedIdCodec,
    SubagentProvenance,
    ToolReviewContractError,
    create_subagent_provenance,
)
from tinkerfin_messaging.models import MessageEnvelope
from tinkerfin_studio.conversation.coordinator import (
    ConversationProjectionCoordinator,
)
from tinkerfin_studio.conversation.history import ConversationHistoryService
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


def _subagent_provenance(
    *,
    thread_id: str,
    request_run_id: str,
    graph_task_id: str,
    parent_tool_raw_id: str,
    agent_name: str = "researcher",
    description: str = "检索 LangGraph",
    parent_namespace: tuple[str, ...] = (),
) -> SubagentProvenance:
    return create_subagent_provenance(
        identity=Identity(
            threadId=f"users/7/threads/{thread_id}",
            runId=request_run_id,
        ),
        namespace=(*parent_namespace, f"tools:{graph_task_id}"),
        parent_namespace=parent_namespace,
        graph_task_id=graph_task_id,
        agent_name=agent_name,
        parent_tool_call_id=ScopedIdCodec().encode(
            "tool", parent_namespace, parent_tool_raw_id
        ),
        description=description,
    )


def _subagent_source(provenance: SubagentProvenance) -> dict[str, object]:
    return {
        "kind": "deep_agent_subagent",
        "agentType": "subagent",
        "agentName": provenance.agent_name,
        "namespace": list(provenance.namespace),
        "parentNamespace": list(provenance.parent_namespace),
        "graphTaskId": provenance.graph_task_id,
        "parentToolCallId": provenance.parent_tool_call_id,
        "subagentInput": provenance.description,
        "subagentInvocationId": provenance.subagent_invocation_id,
    }


async def test_projection_builds_tool_todo_and_interrupt_snapshot(
    session: AsyncSession,
) -> None:
    """已提交事件应同时更新事实表、明细投影和 v3 快照"""

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
                        "toolCallId": ScopedIdCodec().encode("tool", (), "tool-write"),
                        "metadata": {
                            "langgraphValue": {
                                "action_requests": [
                                    {
                                        "name": "write_file",
                                        "args": {"file_path": "/result.txt"},
                                    }
                                ],
                                "review_configs": [
                                    {
                                        "action_name": "write_file",
                                        "allowed_decisions": [
                                            "approve",
                                            "edit",
                                            "reject",
                                        ],
                                    }
                                ],
                            },
                            "deepagents": {
                                "schema": "tinkerfin.deepagents.tool-review.v1",
                                "nativeInterruptId": "interrupt-1",
                                "actionIndex": 0,
                                "toolName": "write_file",
                                "originalArgs": {"file_path": "/result.txt"},
                                "allowedDecisions": ["approve", "edit", "reject"],
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
    item = approval["items"][0]
    assert item["toolName"] == "write_file"
    assert item["toolCallId"] == ScopedIdCodec().encode("tool", (), "tool-write")
    assert item["originalArgs"] == {"file_path": "/result.txt"}
    assert item["allowedDecisions"] == ["approve", "edit", "reject"]
    assert json.loads(item["params"]) == {"file_path": "/result.txt"}
    assert messages[0]["id"] == "user-1"
    assert await repository.count_events(thread.id) == len(events)


async def test_projection_rejects_tool_interrupt_without_v1_metadata(
    session: AsyncSession,
) -> None:
    """Studio 不得为缺失框架契约的 Tool interrupt 制造默认审批"""

    repository = ConversationRepository(session)
    thread = await repository.create_thread(
        user_id=7,
        thread_id="thread-invalid-tool-review",
        title="无效 Tool 审批",
        model_id="main",
    )
    await repository.create_main_run(
        thread_id=thread.id,
        run_id="run-invalid-tool-review",
        model_id="main",
        input_json={"messages": []},
        config_json={},
    )
    await repository.commit()
    thread_pk = thread.id
    projector = ConversationProjector(session)
    envelope, event = _envelope(
        1,
        {
            "type": "RUN_STARTED",
            "threadId": thread.thread_id,
            "runId": "run-invalid-tool-review",
        },
        run="run-invalid-tool-review",
    )
    await projector.project(thread_pk=thread.id, envelope=envelope, event=event)
    await repository.commit()
    envelope, event = _envelope(
        2,
        {
            "type": "RUN_FINISHED",
            "threadId": thread.thread_id,
            "runId": "run-invalid-tool-review",
            "outcome": {
                "type": "interrupt",
                "interrupts": [
                    {
                        "id": "invalid-tool-review",
                        "reason": "tool_call",
                        "toolCallId": ScopedIdCodec().encode(
                            "tool", (), "invalid-tool-review"
                        ),
                        "metadata": {
                            "langgraphValue": {
                                "action_requests": [{"name": "write_file", "args": {}}],
                                "review_configs": [
                                    {
                                        "action_name": "write_file",
                                        "allowed_decisions": ["approve"],
                                    }
                                ],
                            },
                            "deepagents": {
                                "nativeInterruptId": "invalid-tool-review",
                                "actionIndex": 0,
                                "toolName": "write_file",
                                "allowedDecisions": ["approve"],
                                "originalArgs": {},
                            },
                        },
                    }
                ],
            },
        },
        run="run-invalid-tool-review",
    )

    with pytest.raises(ToolReviewContractError):
        await projector.project(thread_pk=thread.id, envelope=envelope, event=event)
    await repository.rollback()

    stored = await repository.get_thread_by_pk(thread_pk)
    assert stored is not None
    assert stored.last_seq == 1
    assert stored.snapshot_seq == 1
    assert stored.has_pending_interrupt is False
    assert await repository.count_events(thread_pk) == 1


async def test_projection_preserves_plan_mode_and_pending_plan_interrupt(
    session: AsyncSession,
) -> None:
    """Plan 暂停必须通过同一 v3 快照恢复 mode 与交互，不伪造 Tool 审批"""

    repository = ConversationRepository(session)
    thread = await repository.create_thread(
        user_id=7,
        thread_id="thread-1",
        title="Plan 投影",
        model_id="main",
    )
    await repository.create_main_run(
        thread_id=thread.id,
        run_id="run-1",
        model_id="main",
        input_json={
            "messages": [],
            "forwardedProps": {"model": "main", "command": {"plan": "on"}},
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
            "type": "RUN_FINISHED",
            "threadId": "thread-1",
            "runId": "run-1",
            "outcome": {
                "type": "interrupt",
                "interrupts": [
                    {
                        "id": "plan-interrupt-1",
                        "reason": "plan_clarification",
                        "message": "请补充部署环境",
                        "responseSchema": {"type": "object"},
                        "metadata": {
                            "runtimeInterrupt": {
                                "schema": "tinkerfin.runtime-interrupt.v1",
                                "nativeInterruptId": "plan-interrupt-1",
                                "envelope": {
                                    "schema": "tinkerfin.runtime-interrupt.v1",
                                    "kind": "plan_clarification",
                                    "message": "请补充部署环境",
                                    "responseSchema": {"type": "object"},
                                    "metadata": {
                                        "origin": "plan",
                                        "clarification": {
                                            "schema": "tinkerfin.plan-clarification.v2",
                                            "form": {
                                                "schemaVersion": 2,
                                                "questions": [
                                                    {
                                                        "id": "environment",
                                                        "prompt": "部署到哪里？",
                                                        "required": True,
                                                        "options": [
                                                            {
                                                                "id": "staging",
                                                                "label": "测试环境",
                                                                "description": None,
                                                                "attributes": None,
                                                            }
                                                        ],
                                                        "allowFreeText": False,
                                                        "attributes": None,
                                                    },
                                                    *[
                                                        {
                                                            "id": f"question-{index}",
                                                            "prompt": f"第 {index + 1} 个问题？",
                                                            "required": False,
                                                            "options": [],
                                                            "allowFreeText": True,
                                                            "attributes": None,
                                                        }
                                                        for index in range(1, 4)
                                                    ],
                                                ],
                                            },
                                        },
                                    },
                                },
                            }
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
    assert refreshed.status == "waiting_approval"
    assert refreshed.has_pending_interrupt is True
    snapshot = refreshed.snapshot_json
    assert snapshot is not None
    assert snapshot["mode"] == "plan"
    assert snapshot["approval"] is None
    assert snapshot["interrupts"] == events[-1]["outcome"]["interrupts"]
    interrupts = cast(list[dict[str, object]], snapshot["interrupts"])
    public_metadata = cast(dict[str, object], interrupts[0]["metadata"])
    runtime_interrupt = cast(dict[str, object], public_metadata["runtimeInterrupt"])
    envelope = cast(dict[str, object], runtime_interrupt["envelope"])
    metadata = cast(dict[str, object], envelope["metadata"])
    clarification = cast(dict[str, object], metadata["clarification"])
    form = cast(dict[str, object], clarification["form"])
    questions = cast(list[dict[str, object]], form["questions"])
    assert [question["id"] for question in questions] == [
        "environment",
        "question-1",
        "question-2",
        "question-3",
    ]


@pytest.mark.parametrize(
    ("resume_status", "resume_payload", "error_code", "interrupt_status"),
    (
        ("resolved", {"type": "approve"}, "cancelled", "resolved"),
        ("cancelled", None, "resume_cancelled", "cancelled"),
    ),
)
async def test_resume_started_clears_pending_snapshot_before_later_error(
    session: AsyncSession,
    resume_status: str,
    resume_payload: dict[str, str] | None,
    error_code: str,
    interrupt_status: str,
) -> None:
    """RUN_STARTED 后已处理审批不得因 resumed run 终止而重新 pending"""

    repository = ConversationRepository(session)
    thread = await repository.create_thread(
        user_id=7,
        thread_id="thread-resume-snapshot",
        title="恢复快照",
        model_id="main",
    )
    await repository.create_main_run(
        thread_id=thread.id,
        run_id="run-interrupted",
        model_id="main",
        input_json={"messages": []},
        config_json={},
    )
    await session.commit()
    projector = ConversationProjector(session)
    interrupted_events = (
        {
            "type": "RUN_STARTED",
            "threadId": thread.thread_id,
            "runId": "run-interrupted",
        },
        {
            "type": "RUN_FINISHED",
            "threadId": thread.thread_id,
            "runId": "run-interrupted",
            "outcome": {
                "type": "interrupt",
                "interrupts": [
                    {
                        "id": "interrupt-resume",
                        "reason": "tool_call",
                        "message": "确认写入",
                        "toolCallId": ScopedIdCodec().encode("tool", (), "tool-resume"),
                        "metadata": {
                            "langgraphValue": {
                                "action_requests": [
                                    {
                                        "name": "write_file",
                                        "args": {"file_path": "/resume.txt"},
                                    }
                                ],
                                "review_configs": [
                                    {
                                        "action_name": "write_file",
                                        "allowed_decisions": ["approve", "reject"],
                                    }
                                ],
                            },
                            "deepagents": {
                                "schema": "tinkerfin.deepagents.tool-review.v1",
                                "nativeInterruptId": "interrupt-resume",
                                "actionIndex": 0,
                                "toolName": "write_file",
                                "originalArgs": {"file_path": "/resume.txt"},
                                "allowedDecisions": ["approve", "reject"],
                            },
                        },
                    }
                ],
            },
        },
    )
    for seq, value in enumerate(interrupted_events, start=1):
        envelope, event = _envelope(seq, value, run="run-interrupted")
        await projector.project(thread_pk=thread.id, envelope=envelope, event=event)
    await session.commit()

    resume_entry = {
        "interruptId": "interrupt-resume",
        "status": resume_status,
        "payload": resume_payload,
    }
    await repository.create_main_run(
        thread_id=thread.id,
        run_id="run-resumed",
        model_id="main",
        input_json={"messages": [], "resume": [resume_entry]},
        config_json={},
    )
    claimed = await session.scalar(
        select(ConversationInterrupt).where(
            ConversationInterrupt.conversation_thread_id == thread.id,
            ConversationInterrupt.interrupt_id == "interrupt-resume",
        )
    )
    assert claimed is not None
    claimed.resolved_run_id = "run-resumed"
    await session.commit()

    envelope, event = _envelope(
        3,
        {
            "type": "RUN_STARTED",
            "threadId": thread.thread_id,
            "runId": "run-resumed",
        },
        run="run-resumed",
    )
    await projector.project(thread_pk=thread.id, envelope=envelope, event=event)
    await session.flush()

    started = await repository.get_thread_by_pk(thread.id)
    assert started is not None
    assert started.status == "running"
    assert started.has_pending_interrupt is False
    assert started.snapshot_json is not None
    assert started.snapshot_json["approval"] is None
    assert started.snapshot_json["interrupts"] == []

    envelope, event = _envelope(
        4,
        {
            "type": "RUN_ERROR",
            "rawEvent": {"runId": "run-resumed"},
            "message": "恢复运行已终止",
            "code": error_code,
        },
        run="run-resumed",
    )
    await projector.project(thread_pk=thread.id, envelope=envelope, event=event)
    await session.commit()

    refreshed = await repository.get_thread_by_pk(thread.id)
    assert refreshed is not None
    assert refreshed.status == "idle"
    assert refreshed.has_pending_interrupt is False
    assert refreshed.snapshot_json is not None
    assert refreshed.snapshot_json["approval"] is None
    assert refreshed.snapshot_json["interrupts"] == []
    await session.refresh(claimed)
    assert claimed.status == interrupt_status
    assert claimed.resume_json == resume_entry


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
    pending_request = {
        "id": "interrupt-init-failure",
        "reason": "tool_call",
        "message": "确认写入",
        "toolCallId": ScopedIdCodec().encode("tool", (), "tool-init-failure"),
        "metadata": {
            "langgraphValue": {
                "action_requests": [
                    {
                        "name": "write_file",
                        "args": {"file_path": "/init-failure.txt"},
                    }
                ],
                "review_configs": [
                    {
                        "action_name": "write_file",
                        "allowed_decisions": ["approve", "reject"],
                    }
                ],
            },
            "deepagents": {
                "schema": "tinkerfin.deepagents.tool-review.v1",
                "nativeInterruptId": "interrupt-init-failure",
                "actionIndex": 0,
                "toolName": "write_file",
                "originalArgs": {"file_path": "/init-failure.txt"},
                "allowedDecisions": ["approve", "reject"],
            },
        },
    }
    approval = {
        "items": [
            {
                "id": "interrupt-init-failure",
                "interruptId": "interrupt-init-failure",
                "toolCallId": ScopedIdCodec().encode("tool", (), "tool-init-failure"),
                "toolName": "write_file",
                "params": '{"file_path":"/init-failure.txt"}',
                "input": '{"file_path":"/init-failure.txt"}',
                "description": "确认写入",
                "originalArgs": {"file_path": "/init-failure.txt"},
                "allowedDecisions": ["approve", "reject"],
            }
        ],
        "activeIndex": 0,
        "submitted": False,
    }
    thread.status = "waiting_approval"
    thread.has_pending_interrupt = True
    thread.snapshot_json = {
        "snapshotSeq": 0,
        "snapshotVersion": 3,
        "messages": [],
        "todos": [{"id": "todo-pending", "status": "running"}],
        "mode": "default",
        "approval": approval,
        "runStatus": "waiting_approval",
        "activeRunId": None,
        "serverState": {"todos": [{"id": "todo-pending"}]},
        "runs": {},
        "interrupts": [pending_request],
    }
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
            request_json=pending_request,
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
    await session.refresh(thread)
    assert thread.status == "waiting_approval"
    assert thread.has_pending_interrupt is True
    assert thread.last_seq == 2
    assert thread.snapshot_seq == 2
    snapshot = thread.snapshot_json
    assert snapshot is not None
    assert snapshot["snapshotSeq"] == thread.snapshot_seq
    assert snapshot["runStatus"] == "waiting_approval"
    assert snapshot["activeRunId"] is None
    assert snapshot["approval"] == approval
    assert snapshot["interrupts"] == [pending_request]
    assert snapshot["todos"] == [{"id": "todo-pending", "status": "running"}]

    class NoopProjector(ConversationProjectionCoordinator):
        def __init__(self) -> None:
            pass

        async def reconcile(self, *, thread_pk: int, identity: Identity) -> int:
            del identity
            assert thread_pk == thread.id
            return 2

    history = ConversationHistoryService(
        repository,
        user_id=7,
        projector=NoopProjector(),
    )
    history_list = await history.list_history(page_size=10, cursor=None)
    assert len(history_list.items) == 1
    assert history_list.items[0].status == "waiting_approval"
    assert history_list.items[0].has_pending_interrupt is True
    detail = await history.get_detail(thread.thread_id)
    assert detail.status == "waiting_approval"
    assert detail.has_pending_interrupt is True
    assert detail.last_seq == 2
    assert detail.snapshot_seq == 2
    assert detail.snapshot == thread.snapshot_json


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
    provenance = _subagent_provenance(
        thread_id=thread.thread_id,
        request_run_id="run-1",
        graph_task_id="graph-task",
        parent_tool_raw_id="tool-task",
    )
    source = _subagent_source(provenance)
    child_tool_id = ScopedIdCodec().encode("tool", provenance.namespace, "tool-search")
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
                    "subagents": [provenance.model_dump(mode="json", by_alias=True)],
                },
            },
        },
        {
            "type": "TOOL_CALL_START",
            "toolCallId": child_tool_id,
            "toolCallName": "web_search",
            "rawEvent": {
                "streamMode": "messages",
                "runId": "run-1",
                "source": source,
            },
        },
        {
            "type": "TOOL_CALL_ARGS",
            "toolCallId": child_tool_id,
            "delta": '{"query":"LangGraph"}',
            "rawEvent": {
                "streamMode": "messages",
                "runId": "run-1",
                "source": source,
            },
        },
        {
            "type": "TOOL_CALL_END",
            "toolCallId": child_tool_id,
            "rawEvent": {
                "streamMode": "messages",
                "runId": "run-1",
                "source": source,
            },
        },
        {
            "type": "TOOL_CALL_RESULT",
            "messageId": "message-search",
            "toolCallId": child_tool_id,
            "content": "搜索结果",
            "role": "tool",
            "rawEvent": {
                "streamMode": "messages",
                "runId": "run-1",
                "toolResultStatus": "success",
                "source": source,
            },
        },
        {
            "type": "TOOL_CALL_RESULT",
            "messageId": "message-task",
            "toolCallId": provenance.parent_tool_call_id,
            "content": "子 Agent 完成",
            "role": "tool",
            "rawEvent": {
                "streamMode": "messages",
                "runId": "run-1",
                "relatedSubagentInvocationId": provenance.subagent_invocation_id,
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
    sub_run = await repository.get_run(
        thread_pk=thread.id,
        run_id=provenance.subagent_invocation_id,
    )
    assert refreshed is not None
    assert main_run.status == "running"
    assert sub_run is not None
    assert sub_run.origin_main_run_id == "run-1"
    assert sub_run.last_main_run_id == "run-1"
    assert sub_run.agent_name == "researcher"
    assert sub_run.graph_task_id == "graph-task"
    assert sub_run.input_json == {
        "description": "检索 LangGraph",
        "subagent_type": "researcher",
        "parent_tool_call_id": provenance.parent_tool_call_id,
        "namespace": ["tools:graph-task"],
    }
    assert sub_run.status == "success"
    snapshot = cast(dict[str, object], refreshed.snapshot_json)
    messages = cast(list[dict[str, object]], snapshot["messages"])
    subagent = next(
        message for message in messages if message.get("role") == "subagent"
    )
    subagent_meta = cast(dict[str, object], subagent["meta"])
    assert subagent_meta["subRunId"] == provenance.subagent_invocation_id
    assert subagent_meta["originMainRunId"] == "run-1"
    assert subagent_meta["lastMainRunId"] == "run-1"
    assert subagent_meta["status"] == "completed"
    assert subagent_meta["input"] == "检索 LangGraph"
    assert subagent_meta["result"] == "子 Agent 完成"
    child_tool = next(
        message
        for message in messages
        if cast(dict[str, object], message.get("meta", {})).get("toolCallId")
        == child_tool_id
    )
    child_meta = cast(dict[str, object], child_tool["meta"])
    assert child_meta["runId"] == provenance.subagent_invocation_id
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

    def raw_task_start(request_run_id: str) -> dict[str, object]:
        provenance = _subagent_provenance(
            thread_id=thread.thread_id,
            request_run_id=request_run_id,
            graph_task_id="graph-resume",
            parent_tool_raw_id="tool-task-resume",
            description="继续研究",
        )
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
                    "subagents": [provenance.model_dump(mode="json", by_alias=True)],
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
    expected = _subagent_provenance(
        thread_id=thread.thread_id,
        request_run_id="run-1",
        graph_task_id="graph-resume",
        parent_tool_raw_id="tool-task-resume",
        description="继续研究",
    )
    assert len(projected) == 1
    assert projected[0].run_id == expected.subagent_invocation_id
    assert projected[0].origin_main_run_id == "run-1"
    assert projected[0].last_main_run_id == "run-resume"
    assert projected[0].status == "cancelled"
    assert projected[0].finished_at is not None
    await session.refresh(thread)
    assert thread.snapshot_json is not None
    runs = cast(dict[str, dict[str, object]], thread.snapshot_json["runs"])
    assert runs[expected.subagent_invocation_id]["originMainRunId"] == "run-1"
    assert runs[expected.subagent_invocation_id]["lastMainRunId"] == "run-resume"
    messages = cast(list[dict[str, object]], thread.snapshot_json["messages"])
    subagent = next(message for message in messages if message["role"] == "subagent")
    meta = cast(dict[str, object], subagent["meta"])
    assert meta["originMainRunId"] == "run-1"
    assert meta["lastMainRunId"] == "run-resume"


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
    provenance = _subagent_provenance(
        thread_id=thread.thread_id,
        request_run_id="run-1",
        graph_task_id="graph-error",
        parent_tool_raw_id="tool-task-error",
        description="执行研究",
    )
    source = _subagent_source(provenance)
    child_tool_id = ScopedIdCodec().encode(
        "tool", provenance.namespace, "tool-child-running"
    )
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
                    "subagents": [provenance.model_dump(mode="json", by_alias=True)],
                },
            },
        },
        {
            "type": "TOOL_CALL_START",
            "toolCallId": child_tool_id,
            "toolCallName": "web_search",
            "rawEvent": {
                "streamMode": "messages",
                "runId": "run-1",
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
    sub_run = await repository.get_run(
        thread_pk=thread.id,
        run_id=provenance.subagent_invocation_id,
    )
    assert refreshed is not None and refreshed.snapshot_json is not None
    assert main_run.status == expected_run_status
    assert sub_run is not None
    assert sub_run.status == expected_run_status
    assert sub_run.finished_at is not None
    assert refreshed.status == expected_thread_status
    runs = cast(dict[str, dict[str, object]], refreshed.snapshot_json["runs"])
    assert runs[provenance.subagent_invocation_id]["status"] == expected_run_status
    messages = cast(list[dict[str, object]], refreshed.snapshot_json["messages"])
    subagent = next(message for message in messages if message["role"] == "subagent")
    child_tool = next(
        message
        for message in messages
        if cast(dict[str, object], message.get("meta", {})).get("toolCallId")
        == child_tool_id
    )
    assert cast(dict[str, object], subagent["meta"])["status"] == "failed"
    assert cast(dict[str, object], child_tool["meta"])["status"] == "failed"


async def test_main_error_only_terminates_subagents_owned_by_that_request(
    session: AsyncSession,
) -> None:
    """并存子执行只由最近承载其事件的主请求终结"""

    repository = ConversationRepository(session)
    thread = await repository.create_thread(
        user_id=7,
        thread_id="thread-main-owner-isolation",
        title="主请求归属隔离",
        model_id="main",
    )
    for run_id in ("run-a", "run-b"):
        await repository.create_main_run(
            thread_id=thread.id,
            run_id=run_id,
            model_id="main",
            input_json={"messages": []},
            config_json={},
        )
    await repository.commit()
    provenance_a = _subagent_provenance(
        thread_id=thread.thread_id,
        request_run_id="run-a",
        graph_task_id="graph-a",
        parent_tool_raw_id="task-a",
        description="任务 A",
    )
    provenance_b = _subagent_provenance(
        thread_id=thread.thread_id,
        request_run_id="run-b",
        graph_task_id="graph-b",
        parent_tool_raw_id="task-b",
        description="任务 B",
    )

    def descriptor_event(provenance: SubagentProvenance) -> dict[str, object]:
        return {
            "type": "RAW",
            "source": "langgraph.tasks",
            "rawEvent": {"type": "tasks", "phase": "start", "ns": []},
            "event": {
                "data": {"id": provenance.graph_task_id, "name": "tools"},
                "provenance": {
                    "kind": "root",
                    "namespace": [],
                    "agentType": "main",
                    "agentName": "main",
                    "subagents": [provenance.model_dump(mode="json", by_alias=True)],
                },
            },
        }

    values = (
        (
            "run-a",
            {"type": "RUN_STARTED", "threadId": thread.thread_id, "runId": "run-a"},
        ),
        ("run-a", descriptor_event(provenance_a)),
        (
            "run-b",
            {"type": "RUN_STARTED", "threadId": thread.thread_id, "runId": "run-b"},
        ),
        ("run-b", descriptor_event(provenance_b)),
        (
            "run-b",
            {
                "type": "RUN_ERROR",
                "message": "run-b 失败",
                "code": "runtime_error",
                "rawEvent": {"runId": "run-b"},
            },
        ),
    )
    projector = ConversationProjector(session)
    for seq, (run_id, value) in enumerate(values, start=1):
        envelope, event = _envelope(seq, value, run=run_id)
        await projector.project(thread_pk=thread.id, envelope=envelope, event=event)
    await session.commit()

    subagent_a = await repository.get_run(
        thread_pk=thread.id,
        run_id=provenance_a.subagent_invocation_id,
    )
    subagent_b = await repository.get_run(
        thread_pk=thread.id,
        run_id=provenance_b.subagent_invocation_id,
    )
    assert subagent_a is not None and subagent_a.status == "running"
    assert subagent_b is not None and subagent_b.status == "error"
    await session.refresh(thread)
    assert thread.snapshot_json is not None
    runs = cast(dict[str, dict[str, object]], thread.snapshot_json["runs"])
    assert runs[provenance_a.subagent_invocation_id]["status"] == "running"
    assert runs[provenance_b.subagent_invocation_id]["status"] == "error"


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
    provenance = _subagent_provenance(
        thread_id=thread.thread_id,
        request_run_id="run-main",
        graph_task_id="task-error",
        parent_tool_raw_id="task-error-call",
        description="执行研究",
    )
    source = _subagent_source(provenance)
    events = (
        {
            "type": "RUN_STARTED",
            "threadId": "thread-subagent-error",
            "runId": "run-main",
        },
        {
            "type": "RAW",
            "source": "langgraph.tasks",
            "rawEvent": {"type": "tasks", "phase": "start", "ns": []},
            "event": {
                "data": {"id": "task-error", "name": "tools"},
                "provenance": {
                    "kind": "root",
                    "namespace": [],
                    "agentType": "main",
                    "agentName": "main",
                    "subagents": [provenance.model_dump(mode="json", by_alias=True)],
                },
            },
        },
        {
            "type": "TEXT_MESSAGE_CONTENT",
            "messageId": "subagent-message",
            "delta": "部分结果",
            "rawEvent": {
                "streamMode": "messages",
                "runId": "run-main",
                "source": source,
            },
        },
        {
            "type": "TOOL_CALL_RESULT",
            "messageId": "task-error-result",
            "toolCallId": provenance.parent_tool_call_id,
            "content": "子 Agent 运行失败",
            "role": "tool",
            "rawEvent": {
                "streamMode": "messages",
                "runId": "run-main",
                "relatedSubagentInvocationId": provenance.subagent_invocation_id,
                "relatedNamespace": list(provenance.namespace),
                "toolResultStatus": "error",
                "source": {
                    "kind": "root",
                    "agentType": "main",
                    "agentName": "main",
                    "namespace": [],
                },
            },
        },
    )
    for seq, value in enumerate(events, start=1):
        envelope, event = _envelope(seq, value, run="run-main")
        await projector.project(thread_pk=thread.id, envelope=envelope, event=event)
    await session.commit()

    await session.refresh(main_run)
    await session.refresh(thread)
    sub_run = await repository.get_run(
        thread_pk=thread.id,
        run_id=provenance.subagent_invocation_id,
    )
    assert main_run.status == "running"
    assert sub_run is not None
    assert sub_run.status == "error"
    snapshot = thread.snapshot_json
    assert snapshot is not None
    runs = snapshot["runs"]
    assert isinstance(runs, dict)
    assert runs[provenance.subagent_invocation_id]["status"] == "error"
    messages = snapshot["messages"]
    assert isinstance(messages, list)
    subagent_message = next(
        message
        for message in messages
        if isinstance(message, dict)
        and cast(dict[str, object], message.get("meta", {})).get("subRunId")
        == provenance.subagent_invocation_id
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
                "type": "STATE_SNAPSHOT",
                "snapshot": {
                    "tinkerfin_plan": {
                        "workflowVersion": "tinkerfin.plan.v1",
                        "effectiveMode": "plan",
                    },
                    "todos": [],
                },
            },
            {
                "type": "STATE_DELTA",
                "delta": [
                    {
                        "op": "replace",
                        "path": "/tinkerfin_plan/effectiveMode",
                        "value": "default",
                    },
                    {
                        "op": "add",
                        "path": "/todos",
                        "value": [
                            {
                                "content": "验证 todo 实时输出",
                                "status": "in_progress",
                            }
                        ],
                    },
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
        "tinkerfin_plan": {
            "workflowVersion": "tinkerfin.plan.v1",
            "effectiveMode": "default",
        },
        "todos": [{"content": "验证 todo 实时输出", "status": "in_progress"}],
    }
    assert snapshot["mode"] == "default"
    assert snapshot["todos"] == [
        {
            "id": "todo-0",
            "content": "验证 todo 实时输出",
            "status": "running",
        }
    ]
