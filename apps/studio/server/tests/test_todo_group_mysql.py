"""会话任务轨迹在可销毁 MySQL 上的代表性查询验证"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import JsonValue
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool

from tinkerfin_contracts import RunIdentity
from tinkerfin_studio.api.responses import ApiResponse
from tinkerfin_studio.conversation.todo_groups import TodoGroupQueryExecutor
from tinkerfin_tracing import (
    CapturedValue,
    MessageFact,
    NativeExtraFact,
    RunFact,
    SqlAlchemyTraceStore,
    StateRevisionFact,
    ToolFact,
    Tracer,
    TraceThreadKey,
    TurnFact,
)

pytestmark = pytest.mark.studio_mysql_integration

_TRACE_EVENTS = 12_000
_PROJECTION_NAME = "tinkerfin-studio.todo-groups"


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


async def _create_database(engine: AsyncEngine, database_name: str) -> None:
    if not database_name.startswith("tinkerfin_todo_trace_"):
        raise ValueError("临时数据库名称不符合任务轨迹测试边界")
    async with engine.begin() as connection:
        await connection.exec_driver_sql(
            f"CREATE DATABASE `{database_name}` CHARACTER SET utf8mb4"
        )


async def _drop_database(engine: AsyncEngine, database_name: str) -> None:
    if not database_name.startswith("tinkerfin_todo_trace_"):
        raise ValueError("临时数据库名称不符合任务轨迹测试边界")
    async with engine.begin() as connection:
        await connection.exec_driver_sql(f"DROP DATABASE IF EXISTS `{database_name}`")


async def _append_trace(
    store: SqlAlchemyTraceStore,
    *,
    thread_id: str,
    run_id: str,
) -> TraceThreadKey:
    identity = RunIdentity(threadId=thread_id, runId=run_id)
    writer = await store.open_writer(identity)
    occurred_at = datetime.now(UTC)
    todos: list[JsonValue] = [
        {"id": "todo-1", "content": "完成代表性容量验证", "status": "completed"}
    ]
    base = (
        RunFact(
            source_observation_id=f"{run_id}:started",
            identity=identity,
            occurred_at=occurred_at,
            monotonic_ns=1,
            phase="started",
            input_kind="ordinary",
        ),
        TurnFact(
            source_observation_id=f"{run_id}:turn",
            identity=identity,
            occurred_at=occurred_at,
            monotonic_ns=2,
            turn_id=f"turn:{run_id}",
            user_message_id=f"source-user:{run_id}",
        ),
        MessageFact(
            source_observation_id=f"{run_id}:user",
            identity=identity,
            occurred_at=occurred_at,
            monotonic_ns=3,
            phase="reconciled",
            message_id=f"message:{run_id}",
            source_message_id=f"source-user:{run_id}",
            role="user",
            content=_capture("验证长 Trace 的任务轨迹查询"),
        ),
        ToolFact(
            source_observation_id=f"{run_id}:tool-start",
            identity=identity,
            occurred_at=occurred_at,
            monotonic_ns=4,
            phase="started",
            tool_call_id=f"tool:{run_id}",
            source_tool_call_id=f"source-tool:{run_id}",
            tool_name="write_todos",
        ),
        ToolFact(
            source_observation_id=f"{run_id}:tool-result",
            identity=identity,
            occurred_at=occurred_at,
            monotonic_ns=5,
            phase="result",
            tool_call_id=f"tool:{run_id}",
            source_tool_call_id=f"source-tool:{run_id}",
            tool_name="write_todos",
            content=_capture("ok"),
            result_status="success",
        ),
        StateRevisionFact(
            source_observation_id=f"{run_id}:state",
            identity=identity,
            occurred_at=occurred_at,
            monotonic_ns=6,
            revision_id=f"revision:{run_id}",
            changes=_capture({"todos": todos}),
        ),
    )
    try:
        await writer.append(base)
        extra_events = _TRACE_EVENTS - len(base) - 2
        emitted = 0
        while emitted < extra_events:
            batch_size = min(500, extra_events - emitted)
            await writer.append(
                tuple(
                    NativeExtraFact(
                        source_observation_id=(f"{run_id}:extra:{emitted + index}"),
                        identity=identity,
                        occurred_at=occurred_at,
                        monotonic_ns=7 + emitted + index,
                        mode="custom",
                        data_type="capacity",
                    )
                    for index in range(batch_size)
                )
            )
            emitted += batch_size
        await writer.append(
            (
                RunFact(
                    source_observation_id=f"{run_id}:terminal",
                    identity=identity,
                    occurred_at=occurred_at,
                    monotonic_ns=_TRACE_EVENTS - 1,
                    phase="terminal",
                    outcome="succeeded",
                ),
                RunFact(
                    source_observation_id=f"{run_id}:closed",
                    identity=identity,
                    occurred_at=occurred_at,
                    monotonic_ns=_TRACE_EVENTS,
                    phase="closed",
                    outcome="succeeded",
                ),
            ),
            mandatory=True,
        )
    finally:
        await writer.aclose()
    snapshot = await store.snapshot(thread_id)
    assert snapshot.as_of_seq == _TRACE_EVENTS
    return snapshot.key


async def test_12k_trace_projects_and_encodes_without_checkpoint(
    mysql_admin_url: str,
) -> None:
    admin_url = make_url(mysql_admin_url)
    database_name = f"tinkerfin_todo_trace_{uuid4().hex}"
    admin_engine = create_async_engine(admin_url)
    engine: AsyncEngine | None = None
    store: SqlAlchemyTraceStore | None = None
    key: TraceThreadKey | None = None
    try:
        await _create_database(admin_engine, database_name)
        engine = create_async_engine(
            admin_url.set(database=database_name),
            pool_size=4,
            max_overflow=0,
            pool_pre_ping=True,
        )
        store = SqlAlchemyTraceStore(
            engine,
            namespace=f"todo-trace-{uuid4().hex}",
        )
        key = await _append_trace(
            store,
            thread_id="todo-trace-12k",
            run_id="run-12k",
        )
        tracer = Tracer(store=store)
        trace = await tracer.get("todo-trace-12k", head_run_id="run-12k")
        executor = TodoGroupQueryExecutor(capacity=2)

        started = time.perf_counter()
        projectors = await asyncio.gather(
            executor.project(trace),
            executor.project(trace),
        )
        query_seconds = time.perf_counter() - started
        try:
            snapshots = [
                projector.snapshot(
                    status=trace.status,
                    completeness=trace.completeness,
                )
                for projector in projectors
            ]
            assert snapshots[0] == snapshots[1]
            assert snapshots[0].status == "ready"
            assert len(snapshots[0].todo_groups) == 1
            assert snapshots[0].todo_groups[0].todos[0].status == "completed"

            body = ApiResponse.success(snapshots[0]).model_dump_json(
                by_alias=True,
                exclude_none=False,
            )
            assert json.loads(body)["data"]["todoGroups"][0]["id"] == (
                "todo-group:run-12k"
            )
        finally:
            for projector in projectors:
                projector.close()

        checkpoint = await store.load_projection_checkpoint(
            key,
            projection_name=_PROJECTION_NAME,
            run_id=None,
            as_of_seq=_TRACE_EVENTS,
        )
        pool = engine.pool
        assert isinstance(pool, AsyncAdaptedQueuePool)
        assert checkpoint is None
        assert executor.borrowed_tokens == 0
        assert pool.checkedout() == 0
        assert query_seconds <= 10
        print(
            json.dumps(
                {
                    "events": _TRACE_EVENTS,
                    "concurrent_queries": 2,
                    "query_seconds": query_seconds,
                    "response_bytes": len(body),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    finally:
        if store is not None and key is not None:
            await store.delete(key)
        if engine is not None:
            await engine.dispose()
        await _drop_database(admin_engine, database_name)
        await admin_engine.dispose()
