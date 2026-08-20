"""跨存储会话删除失败后的幂等重试契约"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tinkerfin import Identity
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.conversation.service import ConversationCommandService
from tinkerfin_studio.resources import ApplicationResources


@pytest.mark.parametrize("failure_stage", ["messaging", "checkpoint", "database"])
async def test_delete_retries_each_cross_store_failure_safely(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    repository = ConversationRepository(session)
    thread = await repository.create_thread(
        user_id=7,
        thread_id=f"thread-delete-{failure_stage}",
        title="删除重试",
        model_id="main",
    )
    await repository.commit()
    thread_pk = thread.id
    thread_id = thread.thread_id
    calls = {"messaging": 0, "checkpoint": 0, "database": 0}

    class Projector:
        async def reconcile(self, *, thread_pk: int, identity: Identity) -> int:
            del thread_pk, identity
            assert not session.in_transaction()
            return 0

    class Channel:
        async def delete_stream(self, *, identity: Identity) -> None:
            del identity
            assert not session.in_transaction()
            calls["messaging"] += 1
            if failure_stage == "messaging" and calls["messaging"] == 1:
                raise RuntimeError("messaging delete failed")

    class Checkpointer:
        async def adelete_thread(self, thread_id: str) -> None:
            del thread_id
            assert not session.in_transaction()
            calls["checkpoint"] += 1
            if failure_stage == "checkpoint" and calls["checkpoint"] == 1:
                raise RuntimeError("checkpoint delete failed")

    original_delete = repository.delete_thread_cascade

    async def delete_database(target_thread_pk: int) -> None:
        calls["database"] += 1
        if failure_stage == "database" and calls["database"] == 1:
            raise RuntimeError("database delete failed")
        await original_delete(target_thread_pk)

    monkeypatch.setattr(repository, "delete_thread_cascade", delete_database)
    resources = cast(
        ApplicationResources,
        SimpleNamespace(
            conversation_projector=Projector(),
            conversation_channel=Channel(),
            agent_persistence=SimpleNamespace(checkpointer=Checkpointer()),
        ),
    )
    service = ConversationCommandService(
        repository,
        user_id=7,
        resources=resources,
    )

    with pytest.raises(RuntimeError, match="delete failed"):
        await service.delete(thread_id=thread_id)

    retained = await repository.get_thread_by_pk(thread_pk)
    assert retained is not None
    assert retained.status == "deleting"

    await service.delete(thread_id=thread_id)

    assert await repository.get_thread_by_pk(thread_pk) is None
    assert calls["messaging"] >= 1
    assert calls["checkpoint"] >= 1
    assert calls["database"] >= 1
