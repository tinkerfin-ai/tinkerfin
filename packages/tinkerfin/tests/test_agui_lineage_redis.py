"""Real RedisSaver contracts for AG-UI lineage and resume indexing."""

from __future__ import annotations

from typing import Any, TypedDict, cast
from uuid import uuid4

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import CheckpointTuple
from langgraph.checkpoint.redis.aio import AsyncRedisSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from tinkerfin import AgUiResumeBinding, AgUiResumeCheckpoint, RunIdentity, TinkerFin
from tinkerfin._agui_lineage_state import (
    LINEAGE_STATE_KEY,
    RESUME_MARKER_STATE_KEY,
    LineageMarker,
    parse_lineage_marker,
)


class _RedisResumeState(TypedDict, total=False):
    _tinkerfin_lineage: dict[str, object]
    _tinkerfin_resume: dict[str, object]
    result: str


def _lineage_marker(checkpoint: CheckpointTuple) -> LineageMarker:
    """Read the committed or source-input lineage using LangGraph semantics."""

    channel_values = checkpoint.checkpoint.get("channel_values", {})
    committed = channel_values.get(LINEAGE_STATE_KEY)
    pending = [
        value
        for _task_id, channel, value in checkpoint.pending_writes or ()
        if channel == LINEAGE_STATE_KEY
    ]
    value = (
        pending[-1]
        if checkpoint.metadata.get("source") == "input" and pending
        else committed
    )
    assert value is not None
    return parse_lineage_marker(value)


async def _cleanup_saver(
    saver: AsyncRedisSaver,
    client: Redis,
    *,
    thread_ids: tuple[str, ...],
    checkpoint_prefix: str,
    write_prefix: str,
) -> None:
    """Delete test threads, auxiliary keys, and the two disposable indexes."""

    try:
        for thread_id in thread_ids:
            await saver.adelete_thread(thread_id)
    finally:
        auxiliary_keys: list[bytes] = []
        for thread_id in thread_ids:
            auxiliary_keys.extend(
                [
                    key
                    async for key in client.scan_iter(
                        match=f"write_keys_zset:{thread_id}:*",
                        count=100,
                    )
                ]
            )
            auxiliary_keys.extend(
                [
                    key
                    async for key in client.scan_iter(
                        match=f"{checkpoint_prefix}_latest:{thread_id}:*",
                        count=100,
                    )
                ]
            )
        if auxiliary_keys:
            await client.unlink(*auxiliary_keys)
        for index_name in (checkpoint_prefix, write_prefix):
            try:
                await client.execute_command("FT.DROPINDEX", index_name, "DD")
            except ResponseError:
                pass
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.redis_e2e
async def test_real_redis_saver_indexes_run_id_and_resumes_once(
    monkeypatch: pytest.MonkeyPatch,
    redis_checkpoint_url: str,
) -> None:
    token = uuid4().hex
    thread_id = f"tinkerfin:agui:{token}:thread:with:colons"
    checkpoint_prefix = f"tinkerfin:test:agui-lineage:{token}:checkpoint"
    write_prefix = f"tinkerfin:test:agui-lineage:{token}:write"
    client = Redis.from_url(
        redis_checkpoint_url,
        decode_responses=False,
        socket_connect_timeout=5,
        socket_timeout=5,
    )
    saver = AsyncRedisSaver(
        redis_client=client,
        checkpoint_prefix=checkpoint_prefix,
        checkpoint_write_prefix=write_prefix,
    )
    graphs: list[Any] = []
    executions: list[object] = []

    def build(*_args: object, **_kwargs: object):
        async def reviewed(state: _RedisResumeState) -> dict[str, object]:
            del state
            answer = interrupt({"question": "continue?"})
            executions.append(answer)
            return {"result": "done"}

        builder = StateGraph(_RedisResumeState)
        builder.add_node("reviewed", reviewed)
        builder.add_edge(START, "reviewed")
        builder.add_edge("reviewed", END)
        graph = builder.compile(checkpointer=saver)
        graphs.append(graph)
        return graph

    monkeypatch.setattr(
        "tinkerfin.runtime_profile._deepagents_graph.create_deep_agent",
        build,
    )
    try:
        await saver.asetup()
        definition = TinkerFin().create_deep_agent(model="provider:model", tools=[])
        parent = cast(Any, definition).new_agui(
            identity=RunIdentity(threadId=thread_id, runId="run-parent"),
        )
        parent_events = [event async for event in parent.astream({})]
        assert parent_events[-1].type.value == "RUN_FINISHED"
        snapshot = await graphs[-1].aget_state(
            {"configurable": {"thread_id": thread_id}}
        )
        assert len(snapshot.interrupts) == 1

        binding = AgUiResumeBinding(
            mode="resume",
            resume_data={"answer": "continue"},
            native_interrupt_ids=(snapshot.interrupts[0].id,),
        )
        checkpoints: list[AgUiResumeCheckpoint] = []

        async def fail_after_staging(value: AgUiResumeCheckpoint) -> None:
            checkpoints.append(value)
            raise RuntimeError("host settlement unavailable")

        failed = cast(Any, definition).new_agui(
            identity=RunIdentity(threadId=thread_id, runId="run-resume"),
            resume=binding,
            on_resume_checkpointed=fail_after_staging,
        )
        failed_stream = failed.astream()
        failed_events = [event async for event in failed_stream]

        assert failed_events[-1].type.value == "RUN_ERROR"
        assert isinstance(failed_stream.error, RuntimeError)
        assert executions == []
        assert len(checkpoints) == 1
        staged = await saver.aget_tuple(
            {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
        )
        assert staged is not None
        assert any(
            channel == RESUME_MARKER_STATE_KEY
            for _task_id, channel, _value in staged.pending_writes or ()
        )

        async def checkpointed(value: AgUiResumeCheckpoint) -> None:
            checkpoints.append(value)

        resumed = cast(Any, definition).new_agui(
            identity=RunIdentity(threadId=thread_id, runId="run-resume"),
            resume=binding,
            on_resume_checkpointed=checkpointed,
        )
        resumed_stream = resumed.astream()
        resumed_events = [event async for event in resumed_stream]

        assert resumed_events[-1].type.value == "RUN_FINISHED"
        assert resumed_stream.error is None
        assert executions == [{"answer": "continue"}]
        assert len(checkpoints) == 2
        assert checkpoints[0] == checkpoints[1]
        indexed = [
            checkpoint
            async for checkpoint in saver.alist(
                {
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": "",
                        "run_id": "run-resume",
                    }
                }
            )
        ]
        assert indexed
        indexed_run_ids = tuple(
            _lineage_marker(checkpoint).run_id for checkpoint in indexed
        )
        assert set(indexed_run_ids) == {"run-resume"}, indexed_run_ids
    finally:
        await _cleanup_saver(
            saver,
            client,
            thread_ids=(thread_id,),
            checkpoint_prefix=checkpoint_prefix,
            write_prefix=write_prefix,
        )


@pytest.mark.asyncio
@pytest.mark.redis_e2e
async def test_real_redis_saver_preserves_colon_thread_subgraph_state(
    redis_checkpoint_url: str,
) -> None:
    token = uuid4().hex
    thread_id = f"tenant:{token}:conversation:subgraph:thread"
    checkpoint_prefix = f"tinkerfin:test:subgraph:{token}:checkpoint"
    write_prefix = f"tinkerfin:test:subgraph:{token}:write"
    client = Redis.from_url(
        redis_checkpoint_url,
        decode_responses=False,
        socket_connect_timeout=5,
        socket_timeout=5,
    )
    saver = AsyncRedisSaver(
        redis_client=client,
        checkpoint_prefix=checkpoint_prefix,
        checkpoint_write_prefix=write_prefix,
    )

    async def reviewed(state: _RedisResumeState) -> dict[str, object]:
        del state
        interrupt({"question": "continue?"})
        return {"result": "done"}

    child = StateGraph(_RedisResumeState)
    child.add_node("reviewed", reviewed)
    child.add_edge(START, "reviewed")
    child.add_edge("reviewed", END)
    parent = StateGraph(_RedisResumeState)
    parent.add_node("child", child.compile())
    parent.add_edge(START, "child")
    parent.add_edge("child", END)
    graph = parent.compile(checkpointer=saver)
    config: RunnableConfig = {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": "",
            "run_id": "nested-run",
        }
    }
    try:
        await saver.asetup()
        async for _part in graph.astream(
            {},
            config,
            stream_mode=["tasks", "values"],
            version="v2",
            subgraphs=True,
        ):
            pass
        rows = [
            checkpoint
            async for checkpoint in saver.alist(
                {"configurable": {"thread_id": thread_id}}
            )
        ]
        namespaces = {
            checkpoint.config.get("configurable", {}).get("checkpoint_ns", "")
            for checkpoint in rows
        }
        assert "" in namespaces
        assert any(namespace for namespace in namespaces)
        assert any(checkpoint.pending_writes for checkpoint in rows)
        assert all(
            isinstance(checkpoint.checkpoint.get("pending_sends"), list)
            for checkpoint in rows
        )
        filtered = [
            checkpoint
            async for checkpoint in saver.alist(
                {
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": "",
                        "run_id": "nested-run",
                    }
                }
            )
        ]
        assert filtered

        await saver.adelete_thread(thread_id)
        assert [
            checkpoint
            async for checkpoint in saver.alist(
                {"configurable": {"thread_id": thread_id}}
            )
        ] == []
    finally:
        await _cleanup_saver(
            saver,
            client,
            thread_ids=(thread_id,),
            checkpoint_prefix=checkpoint_prefix,
            write_prefix=write_prefix,
        )
