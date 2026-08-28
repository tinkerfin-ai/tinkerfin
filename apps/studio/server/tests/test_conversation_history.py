from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from tinkerfin_contracts import (
    NativeMessageObservation,
    NativeMessageRecord,
    RunClosedObservation,
    RunIdentity,
    RunInputObservation,
    RunObservationSession,
    RunSourceContext,
    RunStartedObservation,
    RunTerminalObservation,
)
from tinkerfin_studio.api.errors import BusinessException, ConversationErrorCode
from tinkerfin_studio.conversation.history import ConversationHistoryService
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_tracing import Tracer


def _context(thread_id: str, run_id: str) -> RunSourceContext:
    return RunSourceContext(
        identity=RunIdentity(threadId=thread_id, runId=run_id),
        runtime_profile="deepagents-v2",
        input_kind="ordinary",
        input={
            "messages": [
                {
                    "id": f"user-{run_id}",
                    "role": "user",
                    "content": f"request {run_id}",
                }
            ]
        },
        config={},
    )


async def _open_trace(
    tracer: Tracer,
    *,
    thread_id: str,
    run_id: str,
) -> tuple[RunSourceContext, RunObservationSession]:
    context = _context(thread_id, run_id)
    session = await tracer.open_run(context)
    now = datetime.now(UTC)
    await session.observe(
        RunStartedObservation(
            identity=context.identity,
            observed_at=now,
            monotonic_ns=1,
        )
    )
    await session.observe(
        RunInputObservation(
            identity=context.identity,
            source=context,
            observed_at=now,
            monotonic_ns=2,
        )
    )
    return context, session


async def _finish_trace(
    context: RunSourceContext,
    session: RunObservationSession,
) -> None:
    now = datetime.now(UTC)
    await session.observe(
        RunTerminalObservation(
            identity=context.identity,
            outcome="succeeded",
            observed_at=now,
            monotonic_ns=90,
        )
    )
    await session.observe(
        RunClosedObservation(
            identity=context.identity,
            outcome="succeeded",
            observed_at=now,
            monotonic_ns=91,
        )
    )
    await session.aclose()


async def _register(
    repository: ConversationRepository,
    *,
    user_id: int,
    thread_id: str,
    run_id: str,
):
    thread = await repository.get_thread(user_id=user_id, thread_id=thread_id)
    if thread is None:
        thread = await repository.create_thread(
            user_id=user_id,
            thread_id=thread_id,
            title="Trace 会话",
            model_id="model-main",
        )
    await repository.create_run_registration(
        thread_id=thread.id,
        run_id=run_id,
        parent_run_id=thread.last_run_id,
        model_id="model-main",
        runtime_profile="deepagents-v2",
        input_json={"runId": run_id},
        config_json={"runtimeProfile": "deepagents-v2"},
    )
    thread.last_run_id = run_id
    thread.last_model = "model-main"
    await repository.commit()
    return thread


async def test_history_reads_fixed_trace_view_without_agui_event_tail(
    session,
) -> None:
    tracer = Tracer()
    repository = ConversationRepository(session)
    thread = await _register(
        repository,
        user_id=1,
        thread_id="thread-history",
        run_id="run-history",
    )
    context, trace_session = await _open_trace(
        tracer,
        thread_id=thread.thread_id,
        run_id="run-history",
    )
    await trace_session.observe(
        NativeMessageObservation(
            identity=context.identity,
            namespace=(),
            message=NativeMessageRecord(
                message_type="assistant",
                id="assistant-history",
                content="answer",
            ),
            observed_at=datetime.now(UTC),
            monotonic_ns=3,
        )
    )
    await _finish_trace(context, trace_session)

    detail = await ConversationHistoryService(
        repository,
        user_id=1,
        tracer=tracer,
    ).get_detail(thread.thread_id)
    payload = detail.model_dump(mode="json", by_alias=True)

    assert detail.head_run_id == "run-history"
    assert detail.status.execution == "succeeded"
    assert [(item.role, item.content) for item in detail.messages] == [
        ("user", "request run-history"),
        ("assistant", "answer"),
    ]
    assert detail.message_count == 2
    assert "snapshot" not in payload
    assert "events" not in payload


async def test_history_cursor_keeps_original_as_of_after_new_turn(session) -> None:
    tracer = Tracer()
    repository = ConversationRepository(session)
    thread = await _register(
        repository,
        user_id=1,
        thread_id="thread-cursor",
        run_id="run-first",
    )
    first_context, first_session = await _open_trace(
        tracer,
        thread_id=thread.thread_id,
        run_id="run-first",
    )
    await _finish_trace(first_context, first_session)
    service = ConversationHistoryService(repository, user_id=1, tracer=tracer)
    first = await service.get_detail(thread.thread_id, limit=1)
    assert first.history_cursor is None

    await _register(
        repository,
        user_id=1,
        thread_id=thread.thread_id,
        run_id="run-second",
    )
    second_context, second_session = await _open_trace(
        tracer,
        thread_id=thread.thread_id,
        run_id="run-second",
    )
    await _finish_trace(second_context, second_session)
    latest = await service.get_detail(thread.thread_id, limit=1)

    assert latest.history_cursor is not None
    fixed = await service.get_detail(
        thread.thread_id,
        history_cursor=latest.history_cursor,
        limit=1,
    )
    assert fixed.as_of_seq == latest.as_of_seq
    assert fixed.head_run_id == "run-second"
    assert len(fixed.messages) > len(latest.messages)


async def test_history_rejects_another_users_thread_before_trace_lookup(
    session,
) -> None:
    repository = ConversationRepository(session)
    await _register(
        repository,
        user_id=2,
        thread_id="thread-private",
        run_id="run-private",
    )
    service = ConversationHistoryService(repository, user_id=1, tracer=Tracer())

    with pytest.raises(BusinessException) as captured:
        await service.get_detail("thread-private")

    assert captured.value.error_code is ConversationErrorCode.NOT_FOUND


async def test_trace_follow_sends_snapshot_then_semantic_update_and_closes(
    session,
) -> None:
    tracer = Tracer()
    repository = ConversationRepository(session)
    thread = await _register(
        repository,
        user_id=1,
        thread_id="thread-follow",
        run_id="run-follow",
    )
    context, trace_session = await _open_trace(
        tracer,
        thread_id=thread.thread_id,
        run_id="run-follow",
    )
    events = await ConversationHistoryService(
        repository,
        user_id=1,
        tracer=tracer,
    ).follow_trace(thread.thread_id)
    assert session.in_transaction() is False

    snapshot = await anext(events)
    assert snapshot.type == "snapshot"
    assert snapshot.snapshot.status.execution == "running"
    pending = asyncio.create_task(anext(events))
    await trace_session.observe(
        NativeMessageObservation(
            identity=context.identity,
            namespace=(),
            message=NativeMessageRecord(
                message_type="assistant",
                id="assistant-follow",
                content="delta",
            ),
            observed_at=datetime.now(UTC),
            monotonic_ns=3,
        )
    )

    update = await asyncio.wait_for(pending, timeout=2)
    assert update.type == "update"
    assert update.update.messages.upserts[0].content == "delta"
    await events.aclose()
    await _finish_trace(context, trace_session)
