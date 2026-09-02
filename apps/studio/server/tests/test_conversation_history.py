from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import cast

import pytest
from starlette.types import Message as AsgiMessage
from starlette.types import Scope

from tinkerfin_contracts import (
    ModelCallObservation,
    NativeInterruptRecord,
    NativeMessageObservation,
    NativeMessageRecord,
    NativeStateObservation,
    NativeToolCallChunk,
    ObservationBoundary,
    RunClosedObservation,
    RunIdentity,
    RunInputObservation,
    RunObservationSession,
    RunSourceContext,
    RunStartedObservation,
    RunTerminalObservation,
    RunTerminalOutcome,
)
from tinkerfin_studio.api.conversation_router import follow_trace, get_history
from tinkerfin_studio.api.errors import BusinessException, ConversationErrorCode
from tinkerfin_studio.conversation.history import ConversationHistoryService
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.conversation.schemas import (
    ConversationHistoryDetail,
    ConversationTraceErrorEvent,
)
from tinkerfin_studio.conversation.todo_groups import (
    TodoGroupProjector,
    TodoGroupQueryExecutor,
)
from tinkerfin_tracing import (
    TraceCompleteness,
    TraceEntryKind,
    TraceFilter,
    Tracer,
    TraceStatus,
    TraceThread,
)


def _service(
    repository: ConversationRepository,
    *,
    tracer: Tracer,
    todo_group_query: TodoGroupQueryExecutor | None = None,
) -> ConversationHistoryService:
    return ConversationHistoryService(
        repository,
        user_id=1,
        tracer=tracer,
        todo_group_query=todo_group_query or TodoGroupQueryExecutor(),
    )


class _CapturingTodoGroupQuery(TodoGroupQueryExecutor):
    projector: TodoGroupProjector | None = None

    async def project(self, trace: TraceThread) -> TodoGroupProjector:
        self.projector = await super().project(trace)
        return self.projector


async def _get_detail(
    service: ConversationHistoryService,
    thread_id: str,
    *,
    history_cursor: str | None = None,
    limit: int = 100,
    include_task_trace: bool = True,
) -> ConversationHistoryDetail:
    return await service.get_detail(
        thread_id,
        history_cursor=history_cursor,
        limit=limit,
        include_task_trace=include_task_trace,
    )


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
    *,
    outcome: RunTerminalOutcome = "succeeded",
) -> None:
    now = datetime.now(UTC)
    await session.observe(
        RunTerminalObservation(
            identity=context.identity,
            outcome=outcome,
            observed_at=now,
            monotonic_ns=90,
        )
    )
    await session.observe(
        RunClosedObservation(
            identity=context.identity,
            outcome=outcome,
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

    detail = await _get_detail(_service(repository, tracer=tracer), thread.thread_id)
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


async def test_trace_entry_query_returns_the_final_model_request(session) -> None:
    tracer = Tracer()
    repository = ConversationRepository(session)
    thread = await _register(
        repository,
        user_id=1,
        thread_id="thread-entry-history",
        run_id="run-entry-history",
    )
    context, trace_session = await _open_trace(
        tracer,
        thread_id=thread.thread_id,
        run_id="run-entry-history",
    )
    now = datetime.now(UTC)
    await trace_session.observe(
        ModelCallObservation(
            identity=context.identity,
            phase="started",
            call_id="model-entry",
            provider="openai",
            model="gpt-test",
            messages=(
                NativeMessageRecord(message_type="system", content="final-system"),
                NativeMessageRecord(message_type="human", content="final-user"),
            ),
            invocation={"model": "gpt-test"},
            options={"temperature": 0.2},
            observed_at=now,
            monotonic_ns=3,
        )
    )
    await trace_session.observe(
        ModelCallObservation(
            identity=context.identity,
            phase="completed",
            call_id="model-entry",
            usage={"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
            observed_at=now,
            monotonic_ns=4,
        )
    )
    await _finish_trace(context, trace_session)

    page = await _service(repository, tracer=tracer).query_trace_entries(
        thread.thread_id,
        where=TraceFilter(kinds={TraceEntryKind.PROVIDER}),
        cursor=None,
        limit=100,
    )

    assert len(page.items) == 1
    assert len(page.turns) == 1
    assert page.turns[0].ordinal == 1
    assert page.turns[0].user_message is not None
    assert page.turns[0].user_message.content == "request run-entry-history"
    assert page.items[0].turn_id == page.turns[0].id
    assert page.items[0].request == {
        "messages": [
            {
                "messageType": "system",
                "id": None,
                "name": None,
                "content": "final-system",
                "toolCalls": [],
                "toolCallChunks": [],
                "toolCallId": None,
                "toolStatus": None,
                "responseMetadata": {},
                "usageMetadata": None,
            },
            {
                "messageType": "human",
                "id": None,
                "name": None,
                "content": "final-user",
                "toolCalls": [],
                "toolCallChunks": [],
                "toolCallId": None,
                "toolStatus": None,
                "responseMetadata": {},
                "usageMetadata": None,
            },
        ],
        "invocation": {"model": "gpt-test"},
        "options": {"temperature": 0.2},
    }
    assert page.items[0].usage == {
        "input_tokens": 2,
        "output_tokens": 1,
        "total_tokens": 3,
    }
    with pytest.raises(BusinessException) as invalid_cursor:
        await _service(repository, tracer=tracer).query_trace_entries(
            thread.thread_id,
            where=TraceFilter(),
            cursor="not-a-trace-entry-cursor",
            limit=100,
        )
    assert invalid_cursor.value.error_code is ConversationErrorCode.INVALID_CURSOR


async def test_trace_entry_follow_sends_snapshot_update_and_closes(session) -> None:
    tracer = Tracer()
    repository = ConversationRepository(session)
    thread = await _register(
        repository,
        user_id=1,
        thread_id="thread-entry-follow",
        run_id="run-entry-follow",
    )
    context, trace_session = await _open_trace(
        tracer,
        thread_id=thread.thread_id,
        run_id="run-entry-follow",
    )
    events = await _service(repository, tracer=tracer).follow_trace_entries(
        thread.thread_id,
        where=TraceFilter(kinds={TraceEntryKind.PROVIDER}),
        limit=100,
    )

    snapshot = await anext(events)
    assert snapshot.type == "snapshot"
    assert snapshot.snapshot.items == ()
    assert snapshot.snapshot.turns == ()
    assert session.in_transaction() is False
    pending = asyncio.create_task(anext(events))
    await trace_session.observe(
        ModelCallObservation(
            identity=context.identity,
            phase="started",
            call_id="model-entry-follow",
            messages=(NativeMessageRecord(message_type="human", content="follow"),),
            observed_at=datetime.now(UTC),
            monotonic_ns=3,
        )
    )

    update = await asyncio.wait_for(pending, timeout=2)
    assert update.type == "update"
    assert [(item.kind, item.status) for item in update.update.upserts] == [
        (TraceEntryKind.PROVIDER, "running")
    ]
    assert len(update.update.turn_upserts) == 1
    assert update.update.turn_upserts[0].user_message is not None
    assert (
        update.update.turn_upserts[0].user_message.content == "request run-entry-follow"
    )
    await events.aclose()
    await _finish_trace(context, trace_session)


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
    service = _service(repository, tracer=tracer)
    first = await _get_detail(service, thread.thread_id, limit=1)
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
    latest = await _get_detail(service, thread.thread_id, limit=1)

    assert latest.history_cursor is not None
    fixed = await _get_detail(
        service,
        thread.thread_id,
        history_cursor=latest.history_cursor,
        limit=1,
        include_task_trace=False,
    )
    assert fixed.as_of_seq == latest.as_of_seq
    assert fixed.head_run_id == "run-second"
    assert len(fixed.messages) > len(latest.messages)
    assert latest.task_trace is not None
    assert fixed.task_trace is None


async def test_history_keeps_pending_interactions_outside_the_visible_turn(
    session,
) -> None:
    tracer = Tracer()
    repository = ConversationRepository(session)
    thread = await _register(
        repository,
        user_id=1,
        thread_id="thread-pending-history",
        run_id="run-pending-first",
    )
    first_context, first_session = await _open_trace(
        tracer,
        thread_id=thread.thread_id,
        run_id="run-pending-first",
    )
    await first_session.observe(
        NativeStateObservation(
            identity=first_context.identity,
            namespace=(),
            state={},
            interrupts=(
                NativeInterruptRecord(
                    id="pending-outside-window",
                    value={"kind": "input_required", "message": "Wait"},
                ),
            ),
            observed_at=datetime.now(UTC),
            monotonic_ns=3,
        )
    )
    await _finish_trace(first_context, first_session, outcome="interrupted")
    await _register(
        repository,
        user_id=1,
        thread_id=thread.thread_id,
        run_id="run-latest-turn",
    )
    latest_context, latest_session = await _open_trace(
        tracer,
        thread_id=thread.thread_id,
        run_id="run-latest-turn",
    )
    await _finish_trace(latest_context, latest_session)

    detail = await _get_detail(
        _service(repository, tracer=tracer),
        thread.thread_id,
        limit=1,
    )

    assert detail.status.execution == "waiting"
    assert [
        item.source_id for item in detail.interactions if item.status == "pending"
    ] == ["pending-outside-window"]


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
    service = _service(repository, tracer=Tracer())

    with pytest.raises(BusinessException) as captured:
        await service.get_detail("thread-private")

    assert captured.value.error_code is ConversationErrorCode.NOT_FOUND
    with pytest.raises(BusinessException) as entry_error:
        await service.query_trace_entries(
            "thread-private",
            where=TraceFilter(),
            cursor=None,
            limit=100,
        )
    assert entry_error.value.error_code is ConversationErrorCode.NOT_FOUND


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
    events = await _service(repository, tracer=tracer).follow_trace(thread.thread_id)
    assert session.in_transaction() is False

    snapshot = await anext(events)
    assert snapshot.type == "snapshot"
    assert snapshot.snapshot.status.execution == "running"
    assert snapshot.snapshot.task_trace is not None
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
    assert update.task_trace is None
    await events.aclose()
    await _finish_trace(context, trace_session)


async def test_trace_follow_closes_projector_when_disconnected_after_snapshot(
    session,
) -> None:
    tracer = Tracer()
    repository = ConversationRepository(session)
    thread = await _register(
        repository,
        user_id=1,
        thread_id="thread-follow-snapshot-close",
        run_id="run-follow-snapshot-close",
    )
    context, trace_session = await _open_trace(
        tracer,
        thread_id=thread.thread_id,
        run_id="run-follow-snapshot-close",
    )
    query = _CapturingTodoGroupQuery()
    events = await _service(
        repository,
        tracer=tracer,
        todo_group_query=query,
    ).follow_trace(thread.thread_id)

    assert (await anext(events)).type == "snapshot"
    await events.aclose()

    assert query.projector is not None
    with pytest.raises(RuntimeError, match="已关闭"):
        query.projector.snapshot(
            status=TraceStatus(
                execution="running", head_run_id=context.identity.run_id
            ),
            completeness=TraceCompleteness(),
        )
    await _finish_trace(context, trace_session)


async def test_trace_follow_replaces_task_trace_only_after_authoritative_state(
    session,
) -> None:
    tracer = Tracer()
    repository = ConversationRepository(session)
    thread = await _register(
        repository,
        user_id=1,
        thread_id="thread-follow-todos",
        run_id="run-follow-todos",
    )
    context, trace_session = await _open_trace(
        tracer,
        thread_id=thread.thread_id,
        run_id="run-follow-todos",
    )
    events = await _service(repository, tracer=tracer).follow_trace(thread.thread_id)
    initial = await anext(events)
    assert initial.type == "snapshot"
    assert initial.snapshot.task_trace is not None
    assert initial.snapshot.task_trace.todo_groups == ()

    await trace_session.observe(
        NativeMessageObservation(
            identity=context.identity,
            namespace=(),
            message=NativeMessageRecord(
                message_type="assistant_chunk",
                id="assistant-todos",
                content="",
                tool_call_chunks=(
                    NativeToolCallChunk(
                        index=0,
                        id="call-write-todos",
                        name="write_todos",
                        arguments=(
                            '{"todos":[{"content":"实现投影","status":"in_progress"}]}'
                        ),
                    ),
                ),
            ),
            observed_at=datetime.now(UTC),
            monotonic_ns=3,
        )
    )
    await trace_session.observe(
        NativeMessageObservation(
            identity=context.identity,
            namespace=(),
            message=NativeMessageRecord(
                message_type="tool",
                id="tool-message-todos",
                name="write_todos",
                content="Updated todo list",
                tool_call_id="call-write-todos",
                tool_status="success",
            ),
            observed_at=datetime.now(UTC),
            monotonic_ns=4,
        )
    )
    await trace_session.observe(
        NativeStateObservation(
            identity=context.identity,
            namespace=(),
            state={"todos": [{"content": "实现投影", "status": "in_progress"}]},
            interrupts=(),
            observed_at=datetime.now(UTC),
            monotonic_ns=5,
        )
    )
    await trace_session.force(ObservationBoundary.TERMINAL)

    replacement = None
    for _index in range(5):
        update = await asyncio.wait_for(anext(events), timeout=2)
        assert update.type == "update"
        if update.task_trace is not None:
            replacement = update.task_trace
            break
    assert replacement is not None
    assert len(replacement.todo_groups) == 1
    assert replacement.todo_groups[0].todos[0].content == "实现投影"
    assert replacement.todo_groups[0].todos[0].status == "running"

    await trace_session.observe(
        NativeStateObservation(
            identity=context.identity,
            namespace=(),
            state={"todos": [{"content": "实现投影", "status": "completed"}]},
            interrupts=(),
            observed_at=datetime.now(UTC),
            monotonic_ns=6,
        )
    )
    await trace_session.force(ObservationBoundary.TERMINAL)
    completed = None
    for _index in range(5):
        update = await asyncio.wait_for(anext(events), timeout=2)
        assert update.type == "update"
        if update.task_trace is not None:
            completed = update.task_trace
            break
    assert completed is not None
    assert completed.todo_groups[0].todos[0].status == "completed"

    await events.aclose()
    await _finish_trace(context, trace_session)


async def test_detached_follow_can_skip_task_trace_without_losing_base_updates(
    session,
) -> None:
    tracer = Tracer()
    repository = ConversationRepository(session)
    thread = await _register(
        repository,
        user_id=1,
        thread_id="thread-follow-without-todos",
        run_id="run-follow-without-todos",
    )
    context, trace_session = await _open_trace(
        tracer,
        thread_id=thread.thread_id,
        run_id="run-follow-without-todos",
    )
    events = await _service(repository, tracer=tracer).follow_trace(
        thread.thread_id,
        include_task_trace=False,
    )

    initial = await anext(events)
    assert initial.type == "snapshot"
    assert initial.snapshot.task_trace is None
    pending = asyncio.create_task(anext(events))
    await trace_session.observe(
        NativeMessageObservation(
            identity=context.identity,
            namespace=(),
            message=NativeMessageRecord(
                message_type="assistant",
                id="assistant-without-todos",
                content="后台恢复",
            ),
            observed_at=datetime.now(UTC),
            monotonic_ns=3,
        )
    )
    update = await asyncio.wait_for(pending, timeout=2)

    assert update.type == "update"
    assert update.update.messages.upserts[0].content == "后台恢复"
    assert update.task_trace is None
    await events.aclose()
    await _finish_trace(context, trace_session)


async def test_history_route_returns_one_validated_json_body_with_task_trace(
    session,
) -> None:
    tracer = Tracer()
    repository = ConversationRepository(session)
    thread = await _register(
        repository,
        user_id=1,
        thread_id="thread-history-response",
        run_id="run-history-response",
    )
    context, trace_session = await _open_trace(
        tracer,
        thread_id=thread.thread_id,
        run_id="run-history-response",
    )
    await _finish_trace(context, trace_session)
    response = await get_history(
        thread.thread_id,
        _service(repository, tracer=tracer),
        history_cursor=None,
        limit=100,
        include_task_trace=True,
    )
    sent: list[AsgiMessage] = []

    async def receive() -> AsgiMessage:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: AsgiMessage) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": f"/api/conversation/{thread.thread_id}/history",
        "raw_path": b"/api/conversation/thread/history",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 80),
    }
    await response(
        scope,
        receive,
        send,
    )

    payload = json.loads(bytes(response.body))
    assert payload["code"] == 0
    assert payload["data"]["taskTrace"] == {
        "status": "ready",
        "todoGroups": [],
    }
    assert [message["type"] for message in sent] == [
        "http.response.start",
        "http.response.body",
    ]


class _TraceRouteService:
    async def follow_trace(
        self,
        _thread_id: str,
        *,
        include_task_trace: bool,
    ):
        assert include_task_trace is True

        async def events():
            yield ConversationTraceErrorEvent()

        return events()


async def test_trace_route_serializes_one_complete_sse_frame() -> None:
    response = await follow_trace(
        "thread-trace-response",
        cast(ConversationHistoryService, _TraceRouteService()),
        include_task_trace=True,
    )

    chunks: list[bytes] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.encode() if isinstance(chunk, str) else bytes(chunk))

    assert b"".join(chunks) == (
        b'event: trace\ndata: {"type":"error","code":"trace_unavailable"}\n\n'
    )
