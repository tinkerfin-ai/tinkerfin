"""Async checkpoint scope, durable identity, and inherited saver contracts."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, TypedDict, cast

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.channels.delta import DeltaChannel
from langgraph.checkpoint.base import (
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    empty_checkpoint,
)
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph

from tinkerfin import TinkerFinLifecycleError
from tinkerfin._checkpoint import NamespaceCheckpointer


def _config(thread: str = "thread", graph_namespace: str = "") -> RunnableConfig:
    return {"configurable": {"thread_id": thread, "checkpoint_ns": graph_namespace}}


@pytest.mark.parametrize("namespace", ["company-a", "用户\0空间", "😀" * 128])
async def test_checkpoint_threads_are_isolated_and_history_is_logical(
    namespace: str,
) -> None:
    shared = InMemorySaver()
    alpha = NamespaceCheckpointer(shared, namespace)
    beta = NamespaceCheckpointer(shared, "other")
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"answer": "alpha"}
    checkpoint["channel_versions"] = {"answer": "1"}
    first = await alpha.aput(
        _config(), checkpoint, {"source": "input", "step": -1}, {"answer": "1"}
    )
    await alpha.aput_writes(first, [("pending", "alpha")], "task")
    second_checkpoint = empty_checkpoint()
    second = await alpha.aput(
        first, second_checkpoint, {"source": "loop", "step": 0}, {}
    )
    foreign = empty_checkpoint()
    foreign["channel_values"] = {"answer": "beta"}
    foreign["channel_versions"] = {"answer": "1"}
    await beta.aput(
        _config(), foreign, {"source": "input", "step": -1}, {"answer": "1"}
    )
    found = await alpha.aget_tuple(second)
    assert found is not None
    assert found.config.get("configurable", {})["thread_id"] == "thread"
    assert found.parent_config is not None
    assert found.parent_config.get("configurable", {})["thread_id"] == "thread"
    rows = [row async for row in alpha.alist(_config(), before=second)]
    assert [row.checkpoint["id"] for row in rows] == [checkpoint["id"]]
    assert rows[0].pending_writes == [("task", "pending", "alpha")]
    assert (await beta.aget(_config())) == foreign
    await alpha.adelete_thread("thread")
    assert await alpha.aget(_config()) is None
    assert await beta.aget(_config()) is not None


async def test_history_filters_graph_scope_before_pagination_and_closes_cursor() -> (
    None
):
    class BroadHistorySaver(InMemorySaver):
        async def alist(
            self,
            config: RunnableConfig | None,
            *,
            filter: dict[str, Any] | None = None,
            before: RunnableConfig | None = None,
            limit: int | None = None,
        ) -> AsyncIterator[CheckpointTuple]:
            assert config is not None
            del filter, before, limit
            # This models an upstream saver whose index only selects the thread.
            config = {
                "configurable": {
                    "thread_id": config.get("configurable", {})["thread_id"]
                }
            }
            async for row in super().alist(config):
                yield row

    view = NamespaceCheckpointer(BroadHistorySaver(), "scope")
    first = await view.aput(_config(), empty_checkpoint(), {"step": 1}, {})
    await view.aput(
        _config(graph_namespace="child:task"), empty_checkpoint(), {"step": 2}, {}
    )
    latest = await view.aput(first, empty_checkpoint(), {"step": 3}, {})
    rows = [
        row
        async for row in view.alist(
            _config(), before=latest, filter={"step": 1}, limit=1
        )
    ]
    assert [row.config for row in rows] == [first]
    assert [row async for row in view.alist(_config(), limit=0)] == []
    with pytest.raises(ValueError, match="thread_id"):
        [row async for row in view.alist(None)]


@dataclass(frozen=True)
class _SavedValue:
    name: str


@pytest.mark.parametrize("scope", [None, 7, [], b"child"])
@pytest.mark.parametrize("limit", [None, 0, 1])
async def test_history_rejects_non_text_scope_before_querying(
    scope: object, limit: int | None
) -> None:
    class UnavailableSaver(InMemorySaver):
        async def alist(
            self,
            config: RunnableConfig | None,
            *,
            filter: dict[str, Any] | None = None,
            before: RunnableConfig | None = None,
            limit: int | None = None,
        ) -> AsyncIterator[CheckpointTuple]:
            raise AssertionError("invalid scope must be rejected before saver I/O")
            yield  # pragma: no cover

    view = NamespaceCheckpointer(UnavailableSaver(), "scope")
    config: RunnableConfig = {
        "configurable": {"thread_id": "thread", "checkpoint_ns": scope}
    }
    with pytest.raises(TypeError, match="graph scope must be text"):
        [row async for row in view.alist(config, limit=limit)]


@pytest.mark.parametrize("history", [False, True])
async def test_checkpoint_rejects_conflicting_tuple_coordinates(history: bool) -> None:
    def conflicting(row: CheckpointTuple) -> CheckpointTuple:
        return row._replace(
            config={
                **row.config,
                "configurable": {
                    **row.config.get("configurable", {}),
                    "checkpoint_id": "another-checkpoint",
                },
            }
        )

    class ConflictingSaver(InMemorySaver):
        async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
            row = await super().aget_tuple(config)
            return None if row is None else conflicting(row)

        async def alist(
            self,
            config: RunnableConfig | None,
            *,
            filter: dict[str, Any] | None = None,
            before: RunnableConfig | None = None,
            limit: int | None = None,
        ) -> AsyncIterator[CheckpointTuple]:
            async for row in super().alist(
                config, filter=filter, before=before, limit=limit
            ):
                yield conflicting(row)

    view = NamespaceCheckpointer(ConflictingSaver(), "scope")
    saved = await view.aput(_config(), empty_checkpoint(), {"source": "input"}, {})
    with pytest.raises(TinkerFinLifecycleError, match="conflicting checkpoint"):
        if history:
            [row async for row in view.alist(_config())]
        else:
            await view.aget(saved)


async def test_serializer_allowlist_applies_to_the_actual_saver_without_mutation() -> (
    None
):
    serializer = JsonPlusSerializer(allowed_msgpack_modules=[])
    shared = InMemorySaver(serde=serializer)
    view = NamespaceCheckpointer(shared, "scope").with_allowlist(
        [(__name__, "_SavedValue")]
    )
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"saved": _SavedValue("value")}
    checkpoint["channel_versions"] = {"saved": "1"}
    config = await view.aput(_config(), checkpoint, {"source": "input"}, {"saved": "1"})
    restored = await view.aget(config)
    assert restored is not None
    assert restored["channel_values"]["saved"] == _SavedValue("value")
    assert shared.serde is serializer


def _fold(previous: list[str], writes: Sequence[list[str]]) -> list[str]:
    return previous + [item for batch in writes for item in batch]


class _DeltaState(TypedDict):
    values: Annotated[list[str], DeltaChannel(_fold, snapshot_frequency=1000)]


async def test_delta_channel_reconstructs_through_verified_parent_history() -> None:
    shared = InMemorySaver()
    view = NamespaceCheckpointer(shared, "scope")
    builder = StateGraph(_DeltaState)

    async def unchanged(state: _DeltaState) -> dict[str, object]:
        del state
        return {}

    builder.add_node("unchanged", unchanged)
    builder.add_edge(START, "unchanged")
    builder.add_edge("unchanged", END)
    graph = builder.compile(checkpointer=view)
    await graph.ainvoke({"values": ["first"]}, _config(), durability="sync")
    await graph.ainvoke({"values": ["second"]}, _config(), durability="sync")
    assert (await graph.aget_state(_config())).values["values"] == ["first", "second"]
    with pytest.raises(NotImplementedError):
        await view.aprune(["thread"])
    assert (await graph.aget_state(_config())).values["values"] == ["first", "second"]


async def test_missing_identity_evidence_is_rejected_before_exposing_checkpoint() -> (
    None
):
    class DroppingSaver(InMemorySaver):
        async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
            row = await super().aget_tuple(config)
            return None if row is None else row._replace(metadata={"source": "input"})

    view = NamespaceCheckpointer(DroppingSaver(), "scope")
    saved = await view.aput(_config(), empty_checkpoint(), {"source": "input"}, {})
    with pytest.raises(TinkerFinLifecycleError, match="identity evidence"):
        await view.aget(saved)


async def test_history_close_keeps_cursor_owned_through_caller_cancellation() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    closed = asyncio.Event()

    class GatedSaver(InMemorySaver):
        async def alist(
            self,
            config: RunnableConfig | None,
            *,
            filter: dict[str, Any] | None = None,
            before: RunnableConfig | None = None,
            limit: int | None = None,
        ) -> AsyncIterator[CheckpointTuple]:
            try:
                async for row in super().alist(
                    config, filter=filter, before=before, limit=limit
                ):
                    yield row
            finally:
                entered.set()
                await release.wait()
                closed.set()

    view = NamespaceCheckpointer(GatedSaver(), "scope")
    await view.aput(_config(), empty_checkpoint(), {"source": "input"}, {})
    rows = view.alist(_config())
    await anext(rows)
    closing = asyncio.create_task(rows.aclose())
    try:
        await entered.wait()
        closing.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert closed.is_set()
    finally:
        release.set()
        await asyncio.gather(closing, return_exceptions=True)


async def test_echoed_saver_config_cannot_restore_a_previous_run_binding() -> None:
    from tinkerfin._agui_lineage_state import (
        LINEAGE_CONFIG_KEY,
        LINEAGE_METADATA_KEY,
        LineageMarker,
        bind_checkpoint_run,
    )
    from tinkerfin_contracts import RunIdentity

    class EchoingSaver(InMemorySaver):
        async def aput(
            self,
            config: RunnableConfig,
            checkpoint: Checkpoint,
            metadata: CheckpointMetadata,
            new_versions: ChannelVersions,
        ) -> RunnableConfig:
            saved = await super().aput(config, checkpoint, metadata, new_versions)
            return {
                **config,
                "configurable": {
                    **config.get("configurable", {}),
                    **saved.get("configurable", {}),
                },
            }

        async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
            saved = await super().aget_tuple(config)
            if saved is None:
                return None
            echoed = {
                **saved.config,
                "configurable": {
                    **saved.config.get("configurable", {}),
                    LINEAGE_CONFIG_KEY: LineageMarker(
                        namespace="scope",
                        thread_id="thread",
                        run_id="old",
                        runtime_profile="deepagents-v2",
                        role="native",
                    ),
                },
            }
            return saved._replace(config=echoed)

    view = NamespaceCheckpointer(EchoingSaver(), "scope")
    builder = StateGraph(_DeltaState)

    async def unchanged(state: _DeltaState) -> dict[str, object]:
        del state
        return {}

    builder.add_node("unchanged", unchanged)
    builder.add_edge(START, "unchanged")
    builder.add_edge("unchanged", END)
    graph = builder.compile(checkpointer=view)
    for run_id in ("first", "second"):
        await graph.ainvoke(
            {"values": [run_id]},
            bind_checkpoint_run(
                _config(),
                identity=RunIdentity(
                    namespace="scope", thread_id="thread", run_id=run_id
                ),
                parent_run_id=None,
                runtime_profile="deepagents-v2",
            ),
            durability="sync",
        )
        saved = await view.aget_tuple(_config())
        assert saved is not None
        assert LINEAGE_CONFIG_KEY not in saved.config.get("configurable", {})
        marker = LineageMarker.model_validate_json(
            cast(str, saved.metadata.get(LINEAGE_METADATA_KEY))
        )
        assert marker.run_id == run_id
    assert (await graph.aget_state(_config())).values["values"] == ["first", "second"]


async def test_direct_checkpoint_writes_cannot_forge_managed_metadata() -> None:
    from tinkerfin._agui_lineage_state import LINEAGE_METADATA_KEY, RESUME_METADATA_KEY

    view = NamespaceCheckpointer(InMemorySaver(), "scope")
    injected: dict[str, Any] = {
        LINEAGE_METADATA_KEY: "forged",
        RESUME_METADATA_KEY: "forged",
    }
    saved_config = await view.aput(
        {
            "configurable": {"thread_id": "thread", "checkpoint_ns": "", **injected},
            "metadata": injected,
        },
        empty_checkpoint(),
        cast(CheckpointMetadata, injected),
        {},
    )
    saved = await view.aget_tuple(saved_config)
    assert saved is not None
    assert LINEAGE_METADATA_KEY not in saved.metadata
    assert RESUME_METADATA_KEY not in saved.metadata
