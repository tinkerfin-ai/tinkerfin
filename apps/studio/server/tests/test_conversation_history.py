import base64
import json
from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tinkerfin import Identity
from tinkerfin_agui_adapter import ScopedIdCodec
from tinkerfin_studio.api.errors import (
    BusinessException,
    ConversationErrorCode,
    SystemException,
)
from tinkerfin_studio.conversation.coordinator import (
    ConversationProjectionCoordinator,
)
from tinkerfin_studio.conversation.history import ConversationHistoryService
from tinkerfin_studio.conversation.models import ConversationInterrupt
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.conversation.snapshot import empty_snapshot
from tinkerfin_studio.infrastructure.database import Database


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


def test_history_cursor_rejects_a_different_search_query() -> None:
    """搜索游标不得被其他查询词复用"""

    cursor = base64.urlsafe_b64encode(
        json.dumps(
            {
                "pinned": False,
                "updatedAt": "2026-08-18T10:00:00",
                "id": 1,
                "query": "目标",
            },
            separators=(",", ":"),
        ).encode()
    ).decode()

    with pytest.raises(BusinessException):
        ConversationHistoryService._decode_cursor(cursor, query="其他")


def test_history_group_config_exposes_server_day_ranges() -> None:
    """前端时间分组只消费服务端公开的有序范围"""

    assert ConversationHistoryService.group_config().day_ranges == [7, 30]


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


async def test_list_threads_searches_a_literal_title_substring_for_current_user(
    session: AsyncSession,
) -> None:
    """标题模糊查询必须限制用户并把 SQL 通配符作为普通字符"""

    repository = ConversationRepository(session)
    values = (
        (7, "thread-percent", "进度 100% 完成"),
        (7, "thread-target", "服务端目标会话"),
        (7, "thread-other", "其他会话"),
        (8, "thread-other-user", "服务端目标会话"),
    )
    for user_id, thread_id, title in values:
        await repository.create_thread(
            user_id=user_id,
            thread_id=thread_id,
            title=title,
            model_id="main",
        )
    await repository.commit()

    literal_percent = await repository.list_threads(
        user_id=7,
        page_size=10,
        cursor=None,
        query="%",
    )
    target = await repository.list_threads(
        user_id=7,
        page_size=10,
        cursor=None,
        query="目标",
    )

    assert [thread.thread_id for thread in literal_percent] == ["thread-percent"]
    assert [thread.thread_id for thread in target] == ["thread-target"]


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


@pytest.mark.parametrize(
    ("before_status", "after_status", "after_run_status", "has_pending"),
    (
        ("idle", "running", "streaming", False),
        ("running", "waiting_approval", "waiting_approval", True),
        ("waiting_approval", "idle", "idle", False),
    ),
)
async def test_get_detail_reloads_thread_after_independent_projection(
    database: Database,
    before_status: str,
    after_status: str,
    after_run_status: str,
    has_pending: bool,
) -> None:
    """独立投影提交后详情必须返回同一份最新 thread 与 snapshot 事实"""

    old_interrupt_id = "interrupt-before"
    async with database.session() as session:
        repository = ConversationRepository(session)
        thread = await repository.create_thread(
            user_id=7,
            thread_id=f"thread-reload-{before_status}-{after_status}",
            title="投影后重载",
            model_id="main",
        )
        before_snapshot = empty_snapshot()
        before_snapshot["snapshotSeq"] = 7
        before_snapshot["runStatus"] = (
            "streaming" if before_status == "running" else before_status
        )
        before_snapshot["activeRunId"] = (
            "run-before" if before_status == "running" else None
        )
        if before_status == "waiting_approval":
            before_interrupt = {
                "id": old_interrupt_id,
                "reason": "plan_review",
                "message": "确认旧 Plan",
            }
            before_snapshot["interrupts"] = [before_interrupt]
            created_at = datetime(2026, 8, 22, 1, 0)
            session.add(
                ConversationInterrupt(
                    conversation_thread_id=thread.id,
                    run_id="run-before",
                    resolved_run_id=None,
                    interrupt_id=old_interrupt_id,
                    status="pending",
                    reason="plan_review",
                    message="确认旧 Plan",
                    request_json=before_interrupt,
                    resume_json=None,
                    created_at=created_at,
                    resolved_at=None,
                    updated_at=created_at,
                )
            )
        thread.status = before_status
        thread.last_run_id = "run-before"
        thread.last_seq = 7
        thread.snapshot_seq = 7
        thread.has_pending_interrupt = before_status == "waiting_approval"
        thread.snapshot_json = before_snapshot
        await repository.commit()
        thread_pk = thread.id

        class IndependentProjector(ConversationProjectionCoordinator):
            def __init__(self) -> None:
                pass

            async def reconcile(
                self,
                *,
                thread_pk: int,
                identity: Identity,
            ) -> int:
                del identity
                async with database.session() as projected_session:
                    projected_repository = ConversationRepository(projected_session)
                    projected = await projected_repository.get_thread_by_pk(thread_pk)
                    assert projected is not None
                    existing = await projected_session.scalar(
                        select(ConversationInterrupt).where(
                            ConversationInterrupt.conversation_thread_id == thread_pk,
                            ConversationInterrupt.interrupt_id == old_interrupt_id,
                        )
                    )
                    if existing is not None:
                        existing.status = "resolved"
                        existing.resolved_at = datetime(2026, 8, 22, 1, 1)
                        existing.updated_at = datetime(2026, 8, 22, 1, 1)

                    after_snapshot = empty_snapshot()
                    after_snapshot["snapshotSeq"] = 8
                    after_snapshot["runStatus"] = after_run_status
                    after_snapshot["activeRunId"] = (
                        "run-after" if after_status == "running" else None
                    )
                    if has_pending:
                        pending_interrupt = {
                            "id": "interrupt-after",
                            "reason": "plan_review",
                            "message": "确认新 Plan",
                        }
                        after_snapshot["interrupts"] = [pending_interrupt]
                        created_at = datetime(2026, 8, 22, 1, 1)
                        projected_session.add(
                            ConversationInterrupt(
                                conversation_thread_id=thread_pk,
                                run_id="run-after",
                                resolved_run_id=None,
                                interrupt_id="interrupt-after",
                                status="pending",
                                reason="plan_review",
                                message="确认新 Plan",
                                request_json=pending_interrupt,
                                resume_json=None,
                                created_at=created_at,
                                resolved_at=None,
                                updated_at=created_at,
                            )
                        )
                    projected.status = after_status
                    projected.last_run_id = "run-after"
                    projected.last_seq = 8
                    projected.snapshot_seq = 8
                    projected.has_pending_interrupt = has_pending
                    projected.snapshot_json = after_snapshot
                    await projected_session.commit()
                return 8

        detail = await ConversationHistoryService(
            repository,
            user_id=7,
            projector=IndependentProjector(),
        ).get_detail(thread.thread_id)

        assert detail.last_seq == 8
        assert detail.snapshot_seq == 8
        assert detail.snapshot is not None
        assert detail.snapshot["snapshotSeq"] == 8
        assert detail.status == after_status
        assert detail.has_pending_interrupt is has_pending
        assert detail.last_run_id == "run-after"

        async with database.session() as verification_session:
            stored = await ConversationRepository(
                verification_session
            ).get_thread_by_pk(thread_pk)
            assert stored is not None
            assert stored.last_seq == 8
            assert stored.snapshot_seq == 8
            assert stored.snapshot_json == detail.snapshot
            assert stored.snapshot_json is not None
            assert stored.snapshot_json["snapshotSeq"] == stored.snapshot_seq


async def test_stale_history_reader_cannot_overwrite_new_projection(
    database: Database,
) -> None:
    """旧 Session 的历史修复不得覆盖并发 projector 已提交的新快照"""

    async with database.session() as stale_session:
        stale_repository = ConversationRepository(stale_session)
        thread = await stale_repository.create_thread(
            user_id=7,
            thread_id="thread-history-repair-race",
            title="历史修复竞态",
            model_id="main",
        )
        old_snapshot = empty_snapshot()
        old_snapshot.update(
            {
                "snapshotSeq": 7,
                "messages": [{"id": "message-old", "role": "assistant"}],
                "todos": [{"id": "todo-old", "status": "pending"}],
                "approval": {"items": [{"id": "interrupt-old"}]},
                "interrupts": [{"id": "interrupt-old"}],
                "activeRunId": "run-old",
                "runStatus": "waiting_approval",
            }
        )
        thread.last_seq = 7
        thread.snapshot_seq = 7
        thread.snapshot_json = old_snapshot
        thread.status = "waiting_approval"
        thread.has_pending_interrupt = True
        await stale_repository.commit()

        stale_thread = await stale_repository.get_thread_by_pk(thread.id)
        assert stale_thread is not None
        expected_snapshot = empty_snapshot()
        expected_snapshot.update(
            {
                "snapshotSeq": 8,
                "messages": [{"id": "message-new", "role": "assistant"}],
                "todos": [{"id": "todo-new", "status": "running"}],
                "approval": None,
                "interrupts": [],
                "activeRunId": "run-new",
                "runStatus": "streaming",
            }
        )

        async with database.session() as projected_session:
            projected_repository = ConversationRepository(projected_session)
            projected = await projected_repository.get_thread_by_pk(thread.id)
            assert projected is not None
            projected.last_seq = 8
            projected.snapshot_seq = 8
            projected.snapshot_json = expected_snapshot
            projected.status = "running"
            projected.has_pending_interrupt = False
            await projected_repository.commit()

        class NoopProjector(ConversationProjectionCoordinator):
            def __init__(self) -> None:
                pass

            async def reconcile(self, *, thread_pk: int, identity: Identity) -> int:
                del identity
                assert thread_pk == stale_thread.id
                return 8

        detail = await ConversationHistoryService(
            stale_repository,
            user_id=7,
            projector=NoopProjector(),
        ).get_detail(stale_thread.thread_id)
        assert detail.snapshot == expected_snapshot

    async with database.session() as verification_session:
        stored = await ConversationRepository(verification_session).get_thread_by_pk(
            thread.id
        )
        assert stored is not None
        assert stored.snapshot_seq == 8
        assert stored.snapshot_json is not None
        assert stored.snapshot_json["snapshotSeq"] == stored.snapshot_seq
        assert stored.snapshot_json == expected_snapshot
        assert stored.status == "running"
        assert stored.has_pending_interrupt is False


@pytest.mark.parametrize(
    ("snapshot_seq", "snapshot_json"),
    (
        (1, None),
        (1, {"snapshotVersion": 2, "snapshotSeq": 1}),
        (1, {"snapshotVersion": 3, "snapshotSeq": 0}),
    ),
)
async def test_get_detail_rejects_noncurrent_or_inconsistent_snapshot(
    database: Database,
    snapshot_seq: int,
    snapshot_json: dict[str, object] | None,
) -> None:
    """历史详情不得把非 v3 或序号矛盾的快照降级为事件回放"""

    async with database.session() as session:
        repository = ConversationRepository(session)
        thread = await repository.create_thread(
            user_id=7,
            thread_id=f"thread-invalid-snapshot-{snapshot_json}",
            title="无效快照",
            model_id="main",
        )
        thread.last_seq = snapshot_seq
        thread.snapshot_seq = snapshot_seq
        thread.snapshot_json = snapshot_json
        await repository.commit()
        thread_pk = thread.id

        class NoopProjector(ConversationProjectionCoordinator):
            def __init__(self) -> None:
                pass

            async def reconcile(self, *, thread_pk: int, identity: Identity) -> int:
                del identity
                assert thread_pk == thread.id
                return snapshot_seq

        with pytest.raises(SystemException) as captured:
            await ConversationHistoryService(
                repository,
                user_id=7,
                projector=NoopProjector(),
            ).get_detail(thread.thread_id)

        assert (
            captured.value.error_code is ConversationErrorCode.HISTORY_SCHEMA_MISMATCH
        )

    async with database.session() as verification_session:
        stored = await ConversationRepository(verification_session).get_thread_by_pk(
            thread_pk
        )
        assert stored is not None
        assert stored.snapshot_seq == snapshot_seq
        assert stored.snapshot_json == snapshot_json


async def test_get_detail_does_not_reinterpret_persisted_tool_approval(
    session: AsyncSession,
) -> None:
    """历史查询只返回当前快照契约，不从 interrupt 表重建旧数据"""

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
        "toolCallId": ScopedIdCodec().encode("tool", (), "history-tool-call"),
        "metadata": {
            "langgraphValue": {
                "action_requests": [{"name": "write_file", "args": original_args}],
                "review_configs": [
                    {
                        "action_name": "write_file",
                        "allowed_decisions": ["approve", "edit", "reject"],
                    }
                ],
            },
            "deepagents": {
                "schema": "tinkerfin.deepagents.tool-review.v1",
                "nativeInterruptId": "history-interrupt#0",
                "actionIndex": 0,
                "toolName": "write_file",
                "originalArgs": original_args,
                "allowedDecisions": ["approve", "edit", "reject"],
            },
        },
    }
    thread.status = "waiting_approval"
    thread.last_seq = 7
    thread.snapshot_seq = 7
    thread.has_pending_interrupt = True
    persisted_snapshot: dict[str, object] = {
        "snapshotSeq": 7,
        "snapshotVersion": 3,
        "messages": [],
        "todos": [],
        "mode": "default",
        "approval": {
            "items": [
                {
                    "id": "history-interrupt#0",
                    "interruptId": "history-interrupt#0",
                    "toolCallId": ScopedIdCodec().encode(
                        "tool", (), "history-tool-call"
                    ),
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
        "interrupts": [request_json],
    }
    thread.snapshot_json = persisted_snapshot
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
    assert detail.snapshot == persisted_snapshot
    await session.refresh(thread)
    assert thread.snapshot_json == persisted_snapshot


async def test_get_detail_does_not_repair_stale_interrupt_snapshot(
    session: AsyncSession,
) -> None:
    """旧快照不会触发查询时兼容写入"""

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
    persisted_snapshot: dict[str, object] = {
        "snapshotSeq": 3,
        "snapshotVersion": 3,
        "messages": [],
        "todos": [],
        "mode": "plan",
        "approval": None,
        "runStatus": "waiting_approval",
        "activeRunId": None,
        "serverState": {},
        "runs": {},
        "interrupts": [
            {
                "id": "resolved-plan",
                "reason": "plan_review",
                "message": "确认 Plan",
            }
        ],
    }
    thread.snapshot_json = persisted_snapshot
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
    assert detail.has_pending_interrupt is True
    assert detail.snapshot == persisted_snapshot
    await session.refresh(thread)
    assert thread.has_pending_interrupt is True
    assert thread.snapshot_json == persisted_snapshot


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
        "snapshotVersion": 3,
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
