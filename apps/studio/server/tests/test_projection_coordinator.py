from ag_ui.core import RunFinishedEvent, RunFinishedSuccessOutcome, RunStartedEvent

from tinkerfin import Identity
from tinkerfin_messaging.agui import AgUiCodec
from tinkerfin_messaging.backend import MemoryBackend
from tinkerfin_messaging.messaging import Messaging
from tinkerfin_studio.conversation.coordinator import (
    ConversationProjectionCoordinator,
)
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.infrastructure.database import Database


async def test_reconcile_projects_the_committed_redis_tail(database: Database) -> None:
    """同步追赶应只消费 Messaging 已提交事件并保持相同序号"""

    async with database.session() as session:
        repository = ConversationRepository(session)
        thread = await repository.create_thread(
            user_id=7,
            thread_id="thread-reconcile",
            title="追赶",
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
        thread_pk = thread.id

    backend = MemoryBackend()
    codec = AgUiCodec()
    identity = Identity(
        threadId="users/7/threads/thread-reconcile",
        runId="run-1",
    )
    prepared = await backend.prepare(
        channel="studio-conversation-agui",
        identity=identity,
        codec=codec.codec_id,
        after=0,
        cancellable=False,
        recoverable=False,
    )
    started = RunStartedEvent(thread_id="thread-reconcile", run_id="run-1")
    finished = RunFinishedEvent(
        thread_id="thread-reconcile",
        run_id="run-1",
        outcome=RunFinishedSuccessOutcome(),
    )
    for index, event in enumerate((started, finished), start=1):
        await backend.append(
            prepared.handle,
            message_id=f"run-1:{index}",
            codec=codec.codec_id,
            payload=codec.encode(event),
        )
    assert await backend.begin_settlement(prepared.handle) is False
    await backend.finish(prepared.handle, status="completed")
    async with Messaging(backend=backend) as messaging:
        coordinator = ConversationProjectionCoordinator(
            database=database,
            channel=messaging.channel(
                name="studio-conversation-agui",
                codec=codec,
            ),
        )
        projected = await coordinator.reconcile(
            thread_pk=thread_pk,
            identity=identity,
        )
        await coordinator.aclose()

    async with database.session() as session:
        refreshed = await ConversationRepository(session).get_thread_by_pk(thread_pk)
        assert refreshed is not None
        assert refreshed.last_seq == 2
        assert refreshed.status == "idle"
        assert await ConversationRepository(session).count_events(thread_pk) == 2
    assert projected == 2
