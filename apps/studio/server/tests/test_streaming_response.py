import asyncio
import gc
import warnings
from collections.abc import AsyncGenerator, AsyncIterator
from datetime import UTC, datetime
from typing import cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel
from sqlalchemy.exc import SAWarning
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool
from starlette.responses import StreamingResponse
from starlette.types import Message, Scope

from tinkerfin import SseBody
from tinkerfin_contracts import RunIdentity
from tinkerfin_studio.api.dependencies import SessionDep, get_session
from tinkerfin_studio.api.responses import trace_sse_response
from tinkerfin_tracing import RunFact, SqlAlchemyTraceStore, Tracer, TurnFact


class _TraceEvent(BaseModel):
    event_id: str


def _http_scope() -> Scope:
    return cast(
        Scope,
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/events",
            "raw_path": b"/events",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("test", 80),
        },
    )


async def test_framework_sse_body_survives_repeated_response_cancellation() -> None:
    """原生响应重复取消时必须等待框架流完成上游清理"""

    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    closed = asyncio.Event()

    async def content() -> AsyncGenerator[bytes, None]:
        try:
            started.set()
            await asyncio.Future()
            yield b"unreachable"
        finally:
            cleanup_started.set()
            await release_cleanup.wait()
            closed.set()

    iterator = content()
    body = SseBody(source_factory=lambda: iterator, close=iterator.aclose)

    async def receive() -> Message:
        await asyncio.Future()
        raise AssertionError("不可达")

    async def send(message: Message) -> None:
        del message

    response_task = asyncio.create_task(
        StreamingResponse(body, media_type="text/event-stream")(
            _http_scope(), receive, send
        )
    )
    await started.wait()
    response_task.cancel("首次取消")
    await cleanup_started.wait()
    response_task.cancel("重复取消")
    await asyncio.sleep(0)
    try:
        assert not response_task.done()
    finally:
        release_cleanup.set()

    with pytest.raises(asyncio.CancelledError):
        await response_task
    assert closed.is_set()


async def test_trace_response_encodes_models_and_owns_source_cleanup() -> None:
    """业务路由只提供事件流，响应门面负责编码与关闭"""

    closed = asyncio.Event()

    async def content() -> AsyncGenerator[BaseModel, None]:
        try:
            yield _TraceEvent(event_id="event-1")
        finally:
            closed.set()

    application = FastAPI()

    @application.get("/trace", response_class=StreamingResponse)
    async def trace() -> StreamingResponse:
        return trace_sse_response(content())

    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.get("/trace")

    assert response.status_code == 200
    assert response.text == 'event: trace\ndata: {"event_id":"event-1"}\n\n'
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    assert closed.is_set()


async def test_session_dependency_closes_before_stream_content_starts() -> None:
    """请求会话必须在原生长流开始前完成归还"""

    released = asyncio.Event()

    async def session_override() -> AsyncIterator[AsyncSession]:
        try:
            yield cast(AsyncSession, object())
        finally:
            released.set()

    application = FastAPI()
    application.dependency_overrides[get_session] = session_override

    @application.get("/events", response_class=StreamingResponse)
    async def events(session: SessionDep) -> StreamingResponse:
        del session

        async def content() -> AsyncIterator[bytes]:
            assert released.is_set()
            yield b"data: ready\n\n"

        return StreamingResponse(content(), media_type="text/event-stream")

    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.get("/events")

    assert response.status_code == 200
    assert response.content == b"data: ready\n\n"
    assert released.is_set()


@pytest.mark.docker_integration
async def test_native_trace_response_settles_wrapped_mysql_follow_before_return(
    mysql_sandbox_url: str,
) -> None:
    """原生响应断连后必须等待 Trace follower 归还 MySQL 连接"""

    engine = create_async_engine(
        mysql_sandbox_url,
        pool_size=1,
        max_overflow=0,
    )
    blocker_engine = create_async_engine(
        mysql_sandbox_url,
        pool_size=1,
        max_overflow=0,
    )
    identity = RunIdentity(threadId="trace-response-thread", runId="trace-response-run")
    store = SqlAlchemyTraceStore(engine, namespace="native-trace-response")
    await store.setup()
    writer = await store.open_writer(identity)
    now = datetime.now(UTC)
    await writer.append(
        (
            RunFact(
                source_observation_id="run-started",
                identity=identity,
                occurred_at=now,
                monotonic_ns=1,
                phase="started",
                input_kind="ordinary",
            ),
            TurnFact(
                source_observation_id="turn-started",
                identity=identity,
                occurred_at=now,
                monotonic_ns=2,
                turn_id="trace-response-turn",
            ),
        )
    )
    trace = await Tracer(store=store).get(
        identity.thread_id,
        head_run_id=identity.run_id,
    )
    updates = trace.follow()
    first_frame_sent = asyncio.Event()

    async def content() -> AsyncGenerator[BaseModel, None]:
        try:
            yield _TraceEvent(event_id="snapshot")
            async for _update in updates:
                yield _TraceEvent(event_id="update")
        finally:
            await updates.aclose()

    response = trace_sse_response(content())

    async def receive() -> Message:
        await first_frame_sent.wait()
        await asyncio.sleep(0.05)
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body" and message.get("more_body"):
            first_frame_sent.set()

    blocker = await blocker_engine.connect()
    try:
        await blocker.exec_driver_sql("LOCK TABLES tinkerfin_trace_events WRITE")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            await response(_http_scope(), receive, send)
            pool = cast(AsyncAdaptedQueuePool, engine.sync_engine.pool)
            assert pool.checkedout() == 0
            gc.collect()
            await asyncio.sleep(0)
        assert not [item for item in caught if issubclass(item.category, SAWarning)]
    finally:
        await blocker.exec_driver_sql("UNLOCK TABLES")
        await blocker.close()
        await writer.aclose()
        await blocker_engine.dispose()
        await engine.dispose()
