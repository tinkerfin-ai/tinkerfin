import asyncio
from datetime import UTC, datetime, timedelta
from typing import cast

from ag_ui.core import (
    BaseEvent,
    RunFinishedEvent,
    RunFinishedSuccessOutcome,
    RunStartedEvent,
)

from tinkerfin import Identity
from tinkerfin_messaging.agui import AgUiCodec
from tinkerfin_messaging.backend import MemoryBackend
from tinkerfin_messaging.errors import RunNotFound, RunProducerFailed
from tinkerfin_messaging.messaging import MessageChannel, Messaging
from tinkerfin_studio.conversation.coordinator import (
    ConversationProjectionCoordinator,
)
from tinkerfin_studio.conversation.models import ConversationInterrupt
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.conversation.snapshot import empty_snapshot
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


async def test_recover_preparing_removes_only_a_missing_unstarted_run(
    database: Database,
) -> None:
    """恢复只删除 Redis 不存在的空 run，并释放它尚未消费的 claim"""

    async with database.session() as session:
        repository = ConversationRepository(session)
        thread = await repository.create_thread(
            user_id=7,
            thread_id="thread-stale-preparing",
            title="准备恢复",
            model_id="main",
        )
        previous = await repository.create_main_run(
            thread_id=thread.id,
            run_id="run-previous",
            model_id="main",
            input_json={},
            config_json={},
        )
        previous.status = "success"
        stale = await repository.create_main_run(
            thread_id=thread.id,
            run_id="run-stale",
            model_id="main",
            input_json={},
            config_json={},
        )
        stale.updated_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=1)
        now = datetime.now(UTC).replace(tzinfo=None)
        session.add(
            ConversationInterrupt(
                conversation_thread_id=thread.id,
                run_id="run-interrupted",
                resolved_run_id=stale.run_id,
                interrupt_id="interrupt-stale",
                status="pending",
                reason="tool_call",
                message="确认",
                request_json={"id": "interrupt-stale", "reason": "tool_call"},
                resume_json=None,
                created_at=now,
                resolved_at=None,
                updated_at=now,
            )
        )
        await repository.commit()
        thread_pk = thread.id
        stale_pk = stale.id

    class MissingChannel:
        async def get_run_status(self, *, identity: Identity):
            raise RunNotFound(identity=identity)

    coordinator = ConversationProjectionCoordinator(
        database=database,
        channel=cast("MessageChannel[BaseEvent, BaseEvent]", MissingChannel()),
    )
    deleted_threads = await coordinator.recover_preparing(thread_pk=thread_pk)

    async with database.session() as session:
        repository = ConversationRepository(session)
        assert await repository.get_thread_by_pk(thread_pk) is not None
        assert await session.get(type(stale), stale_pk) is None
        interrupt = await repository.list_pending_interrupts_for_update(
            thread_pk=thread_pk
        )
        assert len(interrupt) == 1
        assert interrupt[0].resolved_run_id is None
    assert deleted_threads == frozenset()


async def test_recover_preparing_keeps_a_durable_running_owner(
    database: Database,
) -> None:
    """已存在 durable owner 时不得按数据库年龄误删准备中 run"""

    async with database.session() as session:
        repository = ConversationRepository(session)
        thread = await repository.create_thread(
            user_id=7,
            thread_id="thread-active-preparing",
            title="活跃准备",
            model_id="main",
        )
        run = await repository.create_main_run(
            thread_id=thread.id,
            run_id="run-active",
            model_id="main",
            input_json={},
            config_json={},
        )
        run.updated_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=1)
        await repository.commit()
        thread_pk = thread.id
        run_pk = run.id

    class RunningChannel:
        async def get_run_status(self, *, identity: Identity):
            del identity
            return "running"

    coordinator = ConversationProjectionCoordinator(
        database=database,
        channel=cast("MessageChannel[BaseEvent, BaseEvent]", RunningChannel()),
    )
    assert await coordinator.recover_preparing(thread_pk=thread_pk) == frozenset()

    async with database.session() as session:
        assert await session.get(type(run), run_pk) is not None


async def test_recover_preparing_deletes_a_wholly_empty_generated_thread(
    database: Database,
) -> None:
    """无 run、事件或审批价值的服务端空 thread 可随孤立注册删除"""

    async with database.session() as session:
        repository = ConversationRepository(session)
        thread = await repository.create_thread(
            user_id=7,
            thread_id="thread-empty-preparing",
            title="空准备",
            model_id=None,
        )
        run = await repository.create_main_run(
            thread_id=thread.id,
            run_id="run-empty",
            model_id="main",
            input_json={},
            config_json={},
        )
        run.updated_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=1)
        await repository.commit()
        thread_pk = thread.id

    class MissingChannel:
        async def get_run_status(self, *, identity: Identity):
            raise RunNotFound(identity=identity)

    coordinator = ConversationProjectionCoordinator(
        database=database,
        channel=cast("MessageChannel[BaseEvent, BaseEvent]", MissingChannel()),
    )
    assert await coordinator.recover_preparing(thread_pk=thread_pk) == frozenset(
        {thread_pk}
    )

    async with database.session() as session:
        assert await ConversationRepository(session).get_thread_by_pk(thread_pk) is None


async def test_owner_loss_converges_business_state_without_a_fake_protocol_event(
    database: Database,
) -> None:
    """durable owner loss 只收敛业务投影，并恢复 checkpoint 前的审批"""

    identity = Identity(threadId="thread-owner-loss", runId="run-owner-loss")
    async with database.session() as session:
        repository = ConversationRepository(session)
        thread = await repository.create_thread(
            user_id=7,
            thread_id=identity.thread_id,
            title="Owner loss",
            model_id="main",
        )
        run = await repository.create_main_run(
            thread_id=thread.id,
            run_id=identity.run_id,
            model_id="main",
            input_json={"resume": [{"interruptId": "interrupt-owner-loss"}]},
            config_json={},
        )
        run.status = "running"
        now = datetime.now(UTC).replace(tzinfo=None)
        pending_request = {
            "id": "interrupt-owner-loss",
            "reason": "tinkerfin:plan_review",
            "message": "请确认 Plan",
            "responseSchema": {"type": "object"},
        }
        session.add(
            ConversationInterrupt(
                conversation_thread_id=thread.id,
                run_id="run-interrupted",
                resolved_run_id=run.run_id,
                interrupt_id="interrupt-owner-loss",
                status="pending",
                reason="tinkerfin:plan_review",
                message="请确认 Plan",
                request_json=pending_request,
                resume_json=None,
                created_at=now,
                resolved_at=None,
                updated_at=now,
            )
        )
        snapshot = empty_snapshot()
        snapshot.update(
            {
                "runStatus": "streaming",
                "activeRunId": run.run_id,
                "runs": {
                    run.run_id: {
                        "runId": run.run_id,
                        "status": "running",
                        "agentType": "main",
                    }
                },
                "interrupts": [pending_request],
            }
        )
        thread.status = "running"
        thread.last_run_id = run.run_id
        thread.snapshot_json = snapshot
        await repository.commit()
        thread_pk = thread.id
        run_pk = run.id

    class FailedSubscription:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise RunProducerFailed(
                identity=identity,
                cause=RuntimeError("owner lost"),
            )

        async def aclose(self) -> None:
            return None

    class OwnerLostChannel:
        async def follow(self, *, identity: Identity, after: int = 0):
            del identity, after
            return FailedSubscription()

        async def get_run_status(self, *, identity: Identity):
            del identity
            return "owner_lost"

    coordinator = ConversationProjectionCoordinator(
        database=database,
        channel=cast("MessageChannel[BaseEvent, BaseEvent]", OwnerLostChannel()),
    )
    coordinator.ensure(thread_pk=thread_pk, identity=identity)
    task = next(iter(coordinator._tasks.values()))
    await asyncio.wait_for(task, timeout=2)

    async with database.session() as session:
        repository = ConversationRepository(session)
        stored_thread = await repository.get_thread_by_pk(thread_pk)
        stored_run = await session.get(type(run), run_pk)
        pending = await repository.list_pending_interrupts_for_update(
            thread_pk=thread_pk
        )
        assert stored_thread is not None
        assert stored_run is not None
        assert stored_run.status == "error"
        assert stored_run.outcome_json == {
            "type": "error",
            "code": "messaging_owner_lost",
            "durableStatus": "owner_lost",
        }
        assert stored_thread.status == "waiting_approval"
        assert stored_thread.snapshot_json is not None
        assert stored_thread.snapshot_json["activeRunId"] is None
        assert stored_thread.snapshot_json["runStatus"] == "waiting_approval"
        assert len(pending) == 1
        assert pending[0].resolved_run_id is None
        assert await repository.count_events(thread_pk) == 0


async def test_owner_loss_after_checkpoint_keeps_resolved_approval_consumed(
    database: Database,
) -> None:
    """checkpoint 后的 owner loss 不得重新开放已经 durable 结算的审批"""

    identity = Identity(threadId="thread-owner-checkpoint", runId="run-resumed")
    async with database.session() as session:
        repository = ConversationRepository(session)
        thread = await repository.create_thread(
            user_id=7,
            thread_id=identity.thread_id,
            title="Checkpoint owner loss",
            model_id="main",
        )
        run = await repository.create_main_run(
            thread_id=thread.id,
            run_id=identity.run_id,
            model_id="main",
            input_json={"resume": [{"interruptId": "interrupt-resolved"}]},
            config_json={"resume_resolution_id": "marker-1"},
        )
        run.status = "running"
        now = datetime.now(UTC).replace(tzinfo=None)
        request_json = {
            "id": "interrupt-resolved",
            "reason": "tinkerfin:plan_review",
            "message": "请确认 Plan",
            "responseSchema": {"type": "object"},
        }
        session.add(
            ConversationInterrupt(
                conversation_thread_id=thread.id,
                run_id="run-interrupted",
                resolved_run_id=run.run_id,
                interrupt_id="interrupt-resolved",
                status="resolved",
                reason="tinkerfin:plan_review",
                message="请确认 Plan",
                request_json=request_json,
                resume_json={
                    "interruptId": "interrupt-resolved",
                    "status": "resolved",
                    "payload": {"type": "approve"},
                },
                created_at=now,
                resolved_at=now,
                updated_at=now,
            )
        )
        snapshot = empty_snapshot()
        snapshot.update(
            {
                "runStatus": "streaming",
                "activeRunId": run.run_id,
                "interrupts": [request_json],
            }
        )
        thread.status = "running"
        thread.last_run_id = run.run_id
        thread.snapshot_json = snapshot
        await repository.commit()
        thread_pk = thread.id

    class OwnerLostChannel:
        async def latest_seq(self, *, identity: Identity):
            del identity
            return 0

        async def get_run_status(self, *, identity: Identity):
            del identity
            return "owner_lost"

    coordinator = ConversationProjectionCoordinator(
        database=database,
        channel=cast("MessageChannel[BaseEvent, BaseEvent]", OwnerLostChannel()),
    )
    assert (
        await coordinator.reconcile_and_settle(
            thread_pk=thread_pk,
            identity=identity,
        )
        == "owner_lost"
    )

    async with database.session() as session:
        repository = ConversationRepository(session)
        stored_thread = await repository.get_thread_by_pk(thread_pk)
        resolved = await repository.list_interrupts_for_update(
            thread_pk=thread_pk,
            interrupt_ids=frozenset({"interrupt-resolved"}),
        )
        assert stored_thread is not None
        assert stored_thread.status == "error"
        assert stored_thread.has_pending_interrupt is False
        assert stored_thread.snapshot_json is not None
        assert stored_thread.snapshot_json["interrupts"] == []
        assert stored_thread.snapshot_json["runStatus"] == "error"
        assert len(resolved) == 1
        assert resolved[0].status == "resolved"
        assert resolved[0].resolved_run_id == identity.run_id
