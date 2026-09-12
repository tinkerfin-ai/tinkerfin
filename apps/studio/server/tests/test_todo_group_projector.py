"""会话任务轨迹边界与查询期投影测试"""

from __future__ import annotations

import json
import tracemalloc
from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest
from pydantic import JsonValue, ValidationError

from tinkerfin_contracts import RunIdentity
from tinkerfin_studio.conversation.todo_groups import (
    TaskTraceSnapshot,
    TodoGroup,
    TodoGroupProjector,
    TodoTraceItem,
)
from tinkerfin_tracing import (
    CapturedValue,
    MessageFact,
    NativeExtraFact,
    RunFact,
    StateRevisionFact,
    ToolFact,
    TraceCompleteness,
    TraceEvent,
    TraceSemanticFact,
    TraceStatus,
    TurnFact,
)

_IDENTITY = RunIdentity(
    namespace="test",
    thread_id="thread:first-success",
    run_id="run-1",
)
_OCCURRED_AT = datetime(2026, 8, 30, 12, tzinfo=UTC)


def _snapshot_for(
    projector: TodoGroupProjector,
    execution: Literal[
        "running",
        "waiting",
        "succeeded",
        "failed",
        "cancelled",
        "abandoned",
        "unknown",
    ] = "running",
) -> TaskTraceSnapshot:
    return projector.snapshot(
        status=TraceStatus(
            execution=execution,
            head_run_id="run-1",
        ),
        completeness=TraceCompleteness(
            missing_prefix=False,
            missing_tail=False,
            payload_omitted=False,
        ),
    )


def _capture(value: JsonValue) -> CapturedValue:
    return CapturedValue(
        disposition="inline",
        safe_size_bytes=len(
            json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ),
        value=value,
    )


def _event(trace_seq: int, fact: TraceSemanticFact) -> TraceEvent:
    return TraceEvent(
        event_id=f"event:first-success:{trace_seq}",
        trace_seq=trace_seq,
        generation="generation:first-success",
        fact=fact,
        persisted_bytes=1,
    )


def _confirmed_todo_events() -> tuple[TraceEvent, ...]:
    def occurred(seconds: int) -> datetime:
        return _OCCURRED_AT + timedelta(seconds=seconds)

    return (
        _event(
            1,
            RunFact(
                source_observation_id="observation:run-started",
                identity=_IDENTITY,
                occurred_at=occurred(0),
                monotonic_ns=1,
                phase="started",
                input_kind="ordinary",
            ),
        ),
        _event(
            2,
            TurnFact(
                source_observation_id="observation:turn-started",
                identity=_IDENTITY,
                occurred_at=occurred(1),
                monotonic_ns=2,
                turn_id="turn:run-1",
                user_message_id="source-user:run-1",
            ),
        ),
        _event(
            3,
            MessageFact(
                source_observation_id="observation:user-message",
                identity=_IDENTITY,
                occurred_at=occurred(2),
                monotonic_ns=3,
                phase="reconciled",
                message_id="message:run-1",
                source_message_id="source-user:run-1",
                role="user",
                content=_capture("实现任务轨迹"),
            ),
        ),
        _event(
            4,
            ToolFact(
                source_observation_id="observation:tool-started",
                identity=_IDENTITY,
                occurred_at=occurred(3),
                monotonic_ns=4,
                phase="started",
                tool_call_id="tool:root:call-1",
                source_tool_call_id="call-1",
                tool_name="write_todos",
            ),
        ),
        _event(
            5,
            ToolFact(
                source_observation_id="observation:tool-result",
                identity=_IDENTITY,
                occurred_at=occurred(5),
                monotonic_ns=5,
                phase="result",
                tool_call_id="tool:root:call-1",
                source_tool_call_id="call-1",
                tool_name="write_todos",
                content=_capture("ok"),
                result_status="success",
            ),
        ),
        _event(
            6,
            StateRevisionFact(
                source_observation_id="observation:state",
                identity=_IDENTITY,
                occurred_at=occurred(7),
                monotonic_ns=6,
                revision_id="revision:run-1",
                changes=_capture(
                    {
                        "todos": [
                            {
                                "id": "todo-1",
                                "content": "实现 Server",
                                "status": "in_progress",
                            },
                            {
                                "id": "todo-2",
                                "content": "实现 Web",
                                "status": "pending",
                            },
                        ]
                    }
                ),
            ),
        ),
    )


def _expected_projected_snapshot() -> TaskTraceSnapshot:
    return TaskTraceSnapshot.model_validate_json(
        json.dumps(
            {
                "status": "ready",
                "todoGroups": [
                    {
                        "id": "todo-group:run-1",
                        "userMessageId": "message:run-1",
                        "userMessagePreview": "实现任务轨迹",
                        "groupToolCallId": "tool:root:call-1",
                        "createdAt": "2026-08-30T12:00:03.000Z",
                        "status": "running",
                        "todos": [
                            {
                                "id": "todo-1",
                                "content": "实现 Server",
                                "status": "running",
                            },
                            {
                                "id": "todo-2",
                                "content": "实现 Web",
                                "status": "pending",
                            },
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        )
    )


def _group() -> TodoGroup:
    return TodoGroup(
        id="todo-group:run-1",
        user_message_id="message:user-1",
        user_message_preview="实现任务轨迹",
        group_tool_call_id="tool:write-todos-1",
        created_at=datetime(2026, 8, 30, 12, tzinfo=UTC),
        status="running",
        todos=(
            TodoTraceItem(
                id="todo-1",
                content="实现 Server 投影",
                status="running",
            ),
        ),
    )


def test_task_trace_contract_uses_one_strict_camel_case_shape() -> None:
    snapshot = TaskTraceSnapshot(status="ready", todo_groups=(_group(),))

    assert snapshot.model_dump(mode="json", by_alias=True) == {
        "status": "ready",
        "todoGroups": [
            {
                "id": "todo-group:run-1",
                "userMessageId": "message:user-1",
                "userMessagePreview": "实现任务轨迹",
                "groupToolCallId": "tool:write-todos-1",
                "createdAt": "2026-08-30T12:00:00Z",
                "status": "running",
                "todos": [
                    {
                        "id": "todo-1",
                        "content": "实现 Server 投影",
                        "status": "running",
                    }
                ],
            }
        ],
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "ready", "todoGroups": [], "errorCode": "trace_incomplete"},
        {"status": "unavailable", "todoGroups": []},
        {
            "status": "unavailable",
            "todoGroups": [_group().model_dump(mode="json", by_alias=True)],
            "errorCode": "trace_incomplete",
        },
        {"status": "ready", "todoGroups": [], "version": 1},
    ],
)
def test_task_trace_contract_rejects_invalid_status_combinations(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        TaskTraceSnapshot.model_validate(payload)


def test_unavailable_task_trace_is_distinct_from_an_empty_ready_trace() -> None:
    ready = TaskTraceSnapshot(status="ready", todo_groups=())
    unavailable = TaskTraceSnapshot(
        status="unavailable",
        todo_groups=(),
        error_code="todo_state_omitted",
    )

    assert ready != unavailable
    assert ready.error_code is None
    assert unavailable.error_code == "todo_state_omitted"


def test_projector_projects_confirmed_root_todos_deterministically() -> None:
    projector = TodoGroupProjector()
    repeated = TodoGroupProjector()
    for event in _confirmed_todo_events():
        projector.consume(event)
        repeated.consume(event.model_copy(deep=True))

    actual = _snapshot_for(projector)
    repeated_actual = _snapshot_for(repeated)

    assert actual == _expected_projected_snapshot()
    assert actual.model_dump_json(by_alias=True) == repeated_actual.model_dump_json(
        by_alias=True
    )
    projector.close()
    repeated.close()


def test_projector_updates_the_existing_group_without_changing_its_identity() -> None:
    projector = TodoGroupProjector()
    for event in _confirmed_todo_events():
        projector.consume(event)
    projector.consume(
        _event(
            7,
            StateRevisionFact(
                source_observation_id="observation:state-updated",
                identity=_IDENTITY,
                occurred_at=_OCCURRED_AT + timedelta(seconds=9),
                monotonic_ns=7,
                revision_id="revision:run-1:updated",
                changes=_capture(
                    {
                        "todos": [
                            {
                                "id": "todo-1",
                                "content": "实现 Server",
                                "status": "completed",
                            },
                            {
                                "id": "todo-2",
                                "content": "实现 Web",
                                "status": "in_progress",
                            },
                        ]
                    }
                ),
            ),
        )
    )

    snapshot = _snapshot_for(projector)
    assert len(snapshot.todo_groups) == 1
    group = snapshot.todo_groups[0]
    assert group.id == "todo-group:run-1"
    assert group.created_at == _OCCURRED_AT + timedelta(seconds=3)
    assert [todo.status for todo in group.todos] == ["completed", "running"]


@pytest.mark.parametrize(
    ("outcome", "execution", "group_status", "running_todo_status"),
    [
        ("succeeded", "succeeded", "completed", "running"),
        ("failed", "failed", "failed", "failed"),
        ("cancelled", "cancelled", "cancelled", "cancelled"),
        ("abandoned", "abandoned", "cancelled", "cancelled"),
        ("interrupted", "waiting", "running", "running"),
    ],
)
def test_projector_maps_run_outcomes_without_losing_pending_todos(
    outcome: Literal["succeeded", "failed", "cancelled", "abandoned", "interrupted"],
    execution: Literal["succeeded", "failed", "cancelled", "abandoned", "waiting"],
    group_status: Literal["running", "completed", "failed", "cancelled"],
    running_todo_status: Literal["running", "completed", "failed", "cancelled"],
) -> None:
    projector = TodoGroupProjector()
    for event in _confirmed_todo_events():
        projector.consume(event)
    projector.consume(
        _event(
            7,
            RunFact(
                source_observation_id=f"observation:terminal:{outcome}",
                identity=_IDENTITY,
                occurred_at=_OCCURRED_AT + timedelta(seconds=9),
                monotonic_ns=7,
                phase="terminal",
                outcome=outcome,
            ),
        )
    )

    group = _snapshot_for(projector, execution).todo_groups[0]
    assert group.status == group_status
    assert [todo.status for todo in group.todos] == [running_todo_status, "pending"]


def test_projector_fails_closed_for_root_omission_invalid_todo_and_message_gap() -> (
    None
):
    events = _confirmed_todo_events()
    state_index = next(
        index
        for index, event in enumerate(events)
        if isinstance(event.fact, StateRevisionFact)
    )
    message_index = next(
        index
        for index, event in enumerate(events)
        if isinstance(event.fact, MessageFact) and event.fact.role == "user"
    )

    cases: list[tuple[int, CapturedValue, str]] = [
        (
            state_index,
            CapturedValue(
                disposition="omitted",
                safe_size_bytes=0,
                reason="capture_limit",
            ),
            "todo_state_omitted",
        ),
        (
            state_index,
            _capture(
                {
                    "todos": [
                        {"id": "todo-invalid", "content": "非法", "status": "failed"}
                    ]
                }
            ),
            "todo_state_invalid",
        ),
        (
            message_index,
            CapturedValue(
                disposition="omitted",
                safe_size_bytes=0,
                reason="capture_limit",
            ),
            "trace_incomplete",
        ),
    ]

    for index, replacement, expected_code in cases:
        projector = TodoGroupProjector()
        for event_index, event in enumerate(events):
            if event_index != index:
                projector.consume(event)
                continue
            if isinstance(event.fact, StateRevisionFact):
                fact = event.fact.model_copy(update={"changes": replacement})
            else:
                assert isinstance(event.fact, MessageFact)
                fact = event.fact.model_copy(update={"content": replacement})
            projector.consume(event.model_copy(update={"fact": fact}))

        result = _snapshot_for(projector)
        assert result.status == "unavailable"
        assert result.todo_groups == ()
        assert result.error_code == expected_code


def test_projector_rejects_reordered_and_conflicting_replays() -> None:
    events = _confirmed_todo_events()
    reordered = TodoGroupProjector()
    reordered.consume(events[1])
    reordered.consume(events[0])
    assert _snapshot_for(reordered).error_code == "trace_incomplete"

    conflicting = TodoGroupProjector()
    first = events[0]
    conflicting.consume(first)
    conflicting.consume(first.model_copy(update={"event_id": "event:conflict"}))
    assert _snapshot_for(conflicting).error_code == "trace_incomplete"


def test_projector_ignores_omitted_subgraph_state() -> None:
    projector = TodoGroupProjector()
    for event in _confirmed_todo_events():
        projector.consume(event)
    projector.consume(
        _event(
            7,
            StateRevisionFact(
                source_observation_id="observation:subgraph-state",
                identity=_IDENTITY,
                graph_namespace=("tools:subagent",),
                occurred_at=_OCCURRED_AT + timedelta(seconds=8),
                monotonic_ns=7,
                revision_id="revision:subgraph",
                changes=CapturedValue(
                    disposition="omitted",
                    safe_size_bytes=0,
                    reason="capture_limit",
                ),
            ),
        )
    )

    assert _snapshot_for(projector) == _expected_projected_snapshot()


def test_projector_streams_one_hundred_thousand_events_without_retaining_input() -> (
    None
):
    events = _confirmed_todo_events()
    projector = TodoGroupProjector()
    for event in events:
        projector.consume(event)
    last = events[-1]
    occurred_at = datetime(2026, 8, 30, 13, tzinfo=UTC)

    tracemalloc.start()
    for trace_seq in range(last.trace_seq + 1, 100_001):
        projector.consume(
            TraceEvent(
                event_id=f"event:smoke:{trace_seq}",
                trace_seq=trace_seq,
                generation=last.generation,
                fact=NativeExtraFact(
                    source_observation_id=f"observation:smoke:{trace_seq}",
                    identity=RunIdentity(
                        namespace="test",
                        thread_id="thread:first-success",
                        run_id="run-1",
                    ),
                    occurred_at=occurred_at,
                    monotonic_ns=trace_seq,
                    mode="custom",
                    data_type="smoke",
                ),
                persisted_bytes=1,
            )
        )
    _current_bytes, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert projector.last_trace_seq == 100_000
    assert _snapshot_for(projector) == _expected_projected_snapshot()
    assert peak_bytes <= 64 * 1024 * 1024
    projector.close()
    with pytest.raises(RuntimeError, match="已关闭"):
        projector.consume(last)
