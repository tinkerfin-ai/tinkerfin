import base64
import json
from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tinkerfin import Identity
from tinkerfin_studio.api.errors import BusinessException
from tinkerfin_studio.conversation.coordinator import (
    ConversationProjectionCoordinator,
)
from tinkerfin_studio.conversation.models import ConversationInterrupt
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.conversation.service import ConversationHistoryService


@pytest.mark.parametrize(
    "payload",
    [
        {"pinned": "false", "updatedAt": "2026-08-18T10:00:00", "id": 1},
        {"pinned": False, "updatedAt": "2026-08-18T10:00:00+08:00", "id": 1},
        {"pinned": False, "updatedAt": "2026-08-18T10:00:00", "id": "1"},
        {"pinned": False, "updatedAt": "2026-08-18T10:00:00", "id": 1, "extra": True},
    ],
)
def test_history_cursor_rejects_noncanonical_payloads(
    payload: dict[str, object],
) -> None:
    """游标字段不得通过宽松类型转换或额外字段进入查询"""

    cursor = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode()

    with pytest.raises(BusinessException):
        ConversationHistoryService._decode_cursor(cursor)


async def test_list_threads_pages_across_pinned_and_recent_groups(
    session: AsyncSession,
) -> None:
    """布尔置顶排序的游标必须稳定跨越置顶与普通会话"""

    repository = ConversationRepository(session)
    values = (
        ("thread-pinned-new", True, datetime(2026, 8, 18, 12, 0)),
        ("thread-pinned-old", True, datetime(2026, 8, 18, 11, 0)),
        ("thread-recent-new", False, datetime(2026, 8, 18, 13, 0)),
        ("thread-recent-old", False, datetime(2026, 8, 18, 10, 0)),
    )
    for thread_id, pinned, updated_at in values:
        thread = await repository.create_thread(
            user_id=7,
            thread_id=thread_id,
            title=thread_id,
            model_id="main",
        )
        thread.pinned = pinned
        thread.updated_at = updated_at
    await repository.commit()

    first_page_with_lookahead = await repository.list_threads(
        user_id=7,
        page_size=2,
        cursor=None,
    )
    first_page = first_page_with_lookahead[:2]
    assert [thread.thread_id for thread in first_page] == [
        "thread-pinned-new",
        "thread-pinned-old",
    ]

    page_tail = first_page[-1]
    second_page = await repository.list_threads(
        user_id=7,
        page_size=2,
        cursor=(page_tail.pinned, page_tail.updated_at, page_tail.id),
    )

    assert [thread.thread_id for thread in second_page] == [
        "thread-recent-new",
        "thread-recent-old",
    ]


async def test_get_detail_releases_read_transaction_before_projection(
    session: AsyncSession,
) -> None:
    """历史追赶属于外部 I/O，不得继承归属查询的隐式事务"""

    repository = ConversationRepository(session)
    thread = await repository.create_thread(
        user_id=7,
        thread_id="thread-history-transaction",
        title="事务边界",
        model_id="main",
    )
    await repository.commit()
    transaction_states: list[bool] = []

    class TransactionCheckingProjector(ConversationProjectionCoordinator):
        def __init__(self) -> None:
            pass

        async def reconcile(self, *, thread_pk: int, identity: Identity) -> int:
            assert thread_pk == thread.id
            del identity
            transaction_states.append(session.in_transaction())
            return 0

    detail = await ConversationHistoryService(
        repository,
        user_id=7,
        projector=TransactionCheckingProjector(),
    ).get_detail(thread.thread_id)

    assert detail.thread_id == thread.thread_id
    assert transaction_states == [False]


async def test_get_detail_repairs_existing_tool_approval_from_pending_facts(
    session: AsyncSession,
) -> None:
    """既有错误 snapshot 必须用 pending interrupt 明细原地修复"""

    repository = ConversationRepository(session)
    thread = await repository.create_thread(
        user_id=7,
        thread_id="thread-history-repair",
        title="历史审批修复",
        model_id="main",
    )
    original_args = {
        "file_path": "/history-result.txt",
        "content": "HISTORY_APPROVAL_OK",
    }
    request_json = {
        "id": "history-interrupt#0",
        "reason": "tool_call",
        "message": "确认历史写入",
        "toolCallId": "history-tool-call",
        "metadata": {
            "deepagents": {
                "toolName": "write_file",
                "originalArgs": original_args,
                "allowedDecisions": ["approve", "edit", "reject"],
            }
        },
    }
    thread.status = "waiting_approval"
    thread.last_seq = 7
    thread.snapshot_seq = 7
    thread.has_pending_interrupt = True
    thread.snapshot_json = {
        "snapshotSeq": 7,
        "snapshotVersion": 2,
        "messages": [],
        "todos": [],
        "mode": "default",
        "approval": {
            "items": [
                {
                    "id": "history-interrupt#0",
                    "interruptId": "history-interrupt#0",
                    "toolCallId": "history-tool-call",
                    "toolName": "tool",
                    "params": "{}",
                    "input": "{}",
                    "description": "确认历史写入",
                    "originalArgs": {},
                    "allowedDecisions": [],
                }
            ],
            "activeIndex": 0,
            "submitted": False,
        },
        "runStatus": "waiting_approval",
        "activeRunId": None,
        "serverState": {},
        "runs": {},
        "activities": [],
        "interrupts": [request_json],
    }
    created_at = datetime(2026, 8, 18, 1, 0)
    session.add(
        ConversationInterrupt(
            conversation_thread_id=thread.id,
            run_id="run-interrupted",
            resolved_run_id=None,
            interrupt_id="history-interrupt#0",
            status="pending",
            reason="tool_call",
            message="确认历史写入",
            request_json=request_json,
            resume_json=None,
            created_at=created_at,
            resolved_at=None,
            updated_at=created_at,
        )
    )
    await repository.commit()

    class NoopProjector(ConversationProjectionCoordinator):
        def __init__(self) -> None:
            pass

        async def reconcile(self, *, thread_pk: int, identity: Identity) -> int:
            assert thread_pk == thread.id
            del identity
            return 0

    detail = await ConversationHistoryService(
        repository,
        user_id=7,
        projector=NoopProjector(),
    ).get_detail(thread.thread_id)

    assert detail.status == "waiting_approval"
    assert detail.has_pending_interrupt is True
    assert detail.snapshot is not None
    approval = detail.snapshot["approval"]
    assert isinstance(approval, dict)
    items = approval.get("items")
    assert isinstance(items, list)
    item = items[0]
    assert isinstance(item, dict)
    assert item["toolName"] == "write_file"
    assert item["originalArgs"] == original_args
    assert item["allowedDecisions"] == ["approve", "edit", "reject"]
    params = item["params"]
    assert isinstance(params, str)
    assert json.loads(params) == original_args
    await session.refresh(thread)
    assert thread.snapshot_json == detail.snapshot


async def test_get_detail_clears_existing_interrupts_without_pending_rows(
    session: AsyncSession,
) -> None:
    """已处理 interrupt 不得仅因旧 snapshot 继续出现在历史状态"""

    repository = ConversationRepository(session)
    thread = await repository.create_thread(
        user_id=7,
        thread_id="thread-history-clear",
        title="清理旧审批",
        model_id="main",
    )
    thread.status = "idle"
    thread.last_seq = 3
    thread.snapshot_seq = 3
    thread.has_pending_interrupt = True
    thread.snapshot_json = {
        "snapshotSeq": 3,
        "snapshotVersion": 2,
        "messages": [],
        "todos": [],
        "mode": "plan",
        "approval": None,
        "runStatus": "waiting_approval",
        "activeRunId": None,
        "serverState": {},
        "runs": {},
        "activities": [],
        "interrupts": [
            {
                "id": "resolved-plan",
                "reason": "plan_review",
                "message": "确认 Plan",
            }
        ],
    }
    await repository.commit()

    class NoopProjector(ConversationProjectionCoordinator):
        def __init__(self) -> None:
            pass

        async def reconcile(self, *, thread_pk: int, identity: Identity) -> int:
            assert thread_pk == thread.id
            del identity
            return 0

    detail = await ConversationHistoryService(
        repository,
        user_id=7,
        projector=NoopProjector(),
    ).get_detail(thread.thread_id)

    assert detail.status == "idle"
    assert detail.has_pending_interrupt is False
    assert detail.snapshot is not None
    assert detail.snapshot["runStatus"] == "idle"
    assert detail.snapshot["approval"] is None
    assert detail.snapshot["interrupts"] == []
    await session.refresh(thread)
    assert thread.has_pending_interrupt is False
    assert thread.snapshot_json == detail.snapshot


async def test_get_detail_preserves_running_snapshot_without_pending_interrupts(
    session: AsyncSession,
) -> None:
    """无待审批的活动会话刷新时必须保留续流 run 身份"""

    repository = ConversationRepository(session)
    thread = await repository.create_thread(
        user_id=7,
        thread_id="thread-history-running",
        title="活动流刷新",
        model_id="main",
    )
    thread.status = "running"
    thread.last_run_id = "run-history-running"
    thread.last_seq = 1
    thread.snapshot_seq = 1
    thread.has_pending_interrupt = False
    running_snapshot: dict[str, object] = {
        "snapshotSeq": 1,
        "snapshotVersion": 2,
        "messages": [],
        "todos": [],
        "mode": "default",
        "approval": None,
        "runStatus": "streaming",
        "activeRunId": "run-history-running",
        "serverState": {},
        "runs": {
            "run-history-running": {
                "runId": "run-history-running",
                "status": "running",
                "agentType": "main",
            }
        },
        "activities": [],
        "interrupts": [],
    }
    thread.snapshot_json = running_snapshot
    expected_snapshot = dict(running_snapshot)
    await repository.commit()

    class NoopProjector(ConversationProjectionCoordinator):
        def __init__(self) -> None:
            pass

        async def reconcile(self, *, thread_pk: int, identity: Identity) -> int:
            assert thread_pk == thread.id
            del identity
            return 1

    detail = await ConversationHistoryService(
        repository,
        user_id=7,
        projector=NoopProjector(),
    ).get_detail(thread.thread_id)

    assert detail.status == "running"
    assert detail.has_pending_interrupt is False
    assert detail.snapshot == expected_snapshot
    assert detail.snapshot is not None
    assert detail.snapshot["runStatus"] == "streaming"
    assert detail.snapshot["activeRunId"] == "run-history-running"
    await session.refresh(thread)
    assert thread.snapshot_json == expected_snapshot
