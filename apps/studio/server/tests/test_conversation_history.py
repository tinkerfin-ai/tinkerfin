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
