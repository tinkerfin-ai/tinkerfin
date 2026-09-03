"""SQLite durable Trace Store sequencing, replay, checkpoints, and borrowed Engine."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest
from pydantic import JsonValue
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from tinkerfin_contracts import RunIdentity
from tinkerfin_tracing._graph_projection import project_trace_graph_node
from tinkerfin_tracing.backend import TraceLedgerStateRequest, TraceStoreOptions
from tinkerfin_tracing.capture import CapturedValue
from tinkerfin_tracing.codec import CanonicalTracePayloadCodec, EncodedTracePayload
from tinkerfin_tracing.durable_store import InMemoryTraceStore
from tinkerfin_tracing.errors import (
    TraceProjectionCheckpointConflict,
    TraceStoreError,
    TraceStoreProtocolError,
    TraceStoreTimeout,
    TraceThreadNotFound,
)
from tinkerfin_tracing.facts import (
    CallTrackingFact,
    MessageFact,
    ModelCallFact,
    RunFact,
    RuntimeTaskFact,
    SubagentFact,
    ToolExecutionFact,
    ToolFact,
    TraceEvent,
    TraceSemanticFact,
    TurnFact,
)
from tinkerfin_tracing.graph import (
    TraceGraphFilter,
    TraceGraphNodeKind,
    TraceGraphNodeStatus,
)
from tinkerfin_tracing.limits import TraceLimits
from tinkerfin_tracing.sql_schema import TRACE_TABLE_NAMES
from tinkerfin_tracing.sql_store import (
    SqlAlchemyTraceStore,
    _complete_connection_cleanup,
    _SqlAlchemyTraceLedgerBackend,
)
from tinkerfin_tracing.store import TraceProjectionCheckpoint
from tinkerfin_tracing.tracer import Tracer
from tinkerfin_tracing.writing import TraceBatchWriter, TraceWritePolicy


class _EncryptedTraceCodec(CanonicalTracePayloadCodec):
    """Small reversible codec proving indexed details still use the codec boundary."""

    _key = 0xA5

    @classmethod
    def _transform(cls, value: bytes) -> bytes:
        return bytes(item ^ cls._key for item in value)

    def encode_fact(self, fact: TraceSemanticFact) -> EncodedTracePayload:
        canonical = CanonicalTracePayloadCodec().encode_fact(fact)
        encrypted = self._transform(canonical.data)
        return EncodedTracePayload(
            data=encrypted,
            digest=canonical.digest,
        )

    def decode_fact(self, payload: bytes) -> TraceSemanticFact:
        return CanonicalTracePayloadCodec().decode_fact(self._transform(payload))

    def encode_json(self, value: JsonValue) -> EncodedTracePayload:
        canonical = CanonicalTracePayloadCodec().encode_json(value)
        encrypted = self._transform(canonical.data)
        return EncodedTracePayload(
            data=encrypted,
            digest=canonical.digest,
        )

    def decode_json(self, payload: bytes) -> JsonValue:
        return CanonicalTracePayloadCodec().decode_json(self._transform(payload))

    @staticmethod
    def digest(payload: bytes) -> str:
        canonical = _EncryptedTraceCodec._transform(payload)
        return CanonicalTracePayloadCodec.digest(canonical)


def _identity(run_id: str = "run-sql") -> RunIdentity:
    return RunIdentity(threadId="thread-sql", runId=run_id)


def _captured(value: JsonValue) -> CapturedValue:
    encoded = CanonicalTracePayloadCodec().encode_json(value)
    return CapturedValue(
        disposition="inline",
        safe_size_bytes=len(encoded.data),
        value=value,
    )


def _fact(
    phase: Literal["started", "input", "terminal", "closed"],
    *,
    run_id: str = "run-sql",
) -> RunFact:
    captured = (
        CapturedValue(disposition="inline", safe_size_bytes=2, value={})
        if phase == "input"
        else None
    )
    return RunFact(
        source_observation_id=f"observation-{run_id}-{phase}",
        identity=_identity(run_id),
        occurred_at=datetime.now(UTC),
        monotonic_ns=1,
        phase=phase,
        input_kind="ordinary" if phase in {"started", "input"} else None,
        input=captured,
        config=captured,
        outcome="succeeded" if phase in {"terminal", "closed"} else None,
    )


def test_encrypted_codec_preserves_the_canonical_pre_transform_digest() -> None:
    canonical_codec = CanonicalTracePayloadCodec()
    encrypted_codec = _EncryptedTraceCodec()
    fact = _fact("started")

    canonical_fact = canonical_codec.encode_fact(fact)
    encrypted_fact = encrypted_codec.encode_fact(fact)
    assert encrypted_fact.data != canonical_fact.data
    assert encrypted_fact.digest == canonical_fact.digest
    assert encrypted_codec.digest(encrypted_fact.data) == canonical_fact.digest
    assert encrypted_codec.decode_fact(encrypted_fact.data) == fact

    state = {"cursor": 3, "status": "ready"}
    canonical_state = canonical_codec.encode_json(state)
    encrypted_state = encrypted_codec.encode_json(state)
    assert encrypted_state.data != canonical_state.data
    assert encrypted_state.digest == canonical_state.digest
    assert encrypted_codec.digest(encrypted_state.data) == canonical_state.digest
    assert encrypted_codec.decode_json(encrypted_state.data) == state

    tampered = encrypted_fact.data[:-1] + bytes((encrypted_fact.data[-1] ^ 1,))
    assert encrypted_codec.digest(tampered) != encrypted_fact.digest


async def _ignore_committed(_events: tuple[TraceEvent, ...]) -> None:
    return None


def _backend(store: SqlAlchemyTraceStore) -> _SqlAlchemyTraceLedgerBackend:
    backend = store.backend
    assert isinstance(backend, _SqlAlchemyTraceLedgerBackend)
    return backend


async def test_sql_batch_admission_reuses_one_canonical_encoding_per_fact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = CanonicalTracePayloadCodec.encode_fact
    encoded_observations: list[str] = []

    def count_encoding(
        codec: CanonicalTracePayloadCodec,
        fact: TraceSemanticFact,
    ) -> EncodedTracePayload:
        encoded_observations.append(fact.source_observation_id)
        return original(codec, fact)

    monkeypatch.setattr(CanonicalTracePayloadCodec, "encode_fact", count_encoding)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'encoding.db'}")
    store = SqlAlchemyTraceStore(engine, namespace="encoding-test")
    try:
        writer = TraceBatchWriter(
            await store.open_writer(_identity()),
            policy=TraceWritePolicy(max_batch_delay_seconds=0),
            on_committed=_ignore_committed,
        )
        await writer.submit((_fact("started"), _fact("input")), mandatory=False)
        await writer.force()
        await writer.aclose()
    finally:
        await engine.dispose()

    assert encoded_observations == [
        "observation-run-sql-started",
        "observation-run-sql-input",
    ]


async def test_sqlite_graph_index_filters_without_repeating_fact_payloads(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'graph.db'}")
    store = SqlAlchemyTraceStore(engine, namespace="graph-query")
    writer = await store.open_writer(_identity())
    marker = "unique-final-request-marker"
    now = datetime.now(UTC)
    try:
        await writer.append(
            (
                _fact("started"),
                _fact("input"),
                CallTrackingFact(
                    source_observation_id="observation-call-tracking",
                    identity=_identity(),
                    occurred_at=now,
                    monotonic_ns=2,
                ),
                ModelCallFact(
                    source_observation_id="observation-model-start",
                    identity=_identity(),
                    occurred_at=now,
                    monotonic_ns=3,
                    phase="started",
                    call_id="model-call",
                    system_message_positions=(),
                    output_message_ids=(),
                    provider="openai",
                    model="gpt-test",
                    request=_captured({"messages": [{"content": marker}]}),
                ),
                ModelCallFact(
                    source_observation_id="observation-model-completed",
                    identity=_identity(),
                    occurred_at=now,
                    monotonic_ns=4,
                    phase="completed",
                    call_id="model-call",
                    system_message_positions=(),
                    output_message_ids=(),
                ),
            )
        )
        await writer.append(
            (_fact("terminal"), _fact("closed")),
            mandatory=True,
        )
        snapshot = await store.snapshot(_identity().thread_id)
        page = await store.query_trace_graph(
            snapshot.key,
            run_ids=(_identity().run_id,),
            where=TraceGraphFilter(kinds={TraceGraphNodeKind.MODEL}),
            limit=10,
        )

        assert page.call_tracking_present is True
        assert len(page.nodes) == 1
        node = project_trace_graph_node(
            page.nodes[0],
            turn_id="turn:test",
            parent_id=None,
            relationship_missing=False,
        )
        assert node.request == {"messages": [{"content": marker}]}
        rebuilt = await store.rebuild_trace_graph(snapshot.key)
        assert rebuilt == 2
        async with engine.connect() as connection:
            columns = {
                row[1]
                for row in (
                    await connection.execute(
                        text("PRAGMA table_info(tinkerfin_trace_graph_nodes)")
                    )
                ).all()
            }
            payloads = (
                await connection.execute(
                    text("SELECT payload FROM tinkerfin_trace_events")
                )
            ).scalars()
            indexed = (
                await connection.execute(
                    text(
                        "SELECT namespace_hash, thread_hash, generation, "
                        "run_hash FROM tinkerfin_trace_graph_nodes LIMIT 1"
                    )
                )
            ).one()
            plan = (
                await connection.execute(
                    text(
                        "EXPLAIN QUERY PLAN SELECT node_id "
                        "FROM tinkerfin_trace_graph_nodes "
                        "WHERE namespace_hash = :namespace_hash "
                        "AND thread_hash = :thread_hash "
                        "AND generation = :generation "
                        "AND run_hash = :run_hash"
                    ),
                    {
                        "namespace_hash": indexed.namespace_hash,
                        "thread_hash": indexed.thread_hash,
                        "generation": indexed.generation,
                        "run_hash": indexed.run_hash,
                    },
                )
            ).all()
            index_names = {
                row[1]
                for row in (
                    await connection.execute(
                        text("PRAGMA index_list(tinkerfin_trace_graph_nodes)")
                    )
                ).all()
                if not str(row[1]).startswith("sqlite_autoindex")
            }
        assert not ({"payload", "request", "result"} & columns)
        assert sum(bytes(payload).count(marker.encode()) for payload in payloads) == 1
        assert any("ix_tinkerfin_trace_graph_run" in str(row) for row in plan)
        assert index_names == {"ix_tinkerfin_trace_graph_run"}
    finally:
        await writer.aclose()
        await engine.dispose()


async def test_sqlite_graph_query_merges_only_selected_run_revisions(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'graph-branches.db'}"
    )
    store = SqlAlchemyTraceStore(engine, namespace="graph-branches")
    root = await store.open_writer(_identity("graph-root"))
    branch_a = await store.open_writer(_identity("graph-branch-a"))
    branch_b = await store.open_writer(_identity("graph-branch-b"))
    tool_id = "tool:shared"
    now = datetime.now(UTC)
    try:
        await root.append(
            (
                _fact("started", run_id="graph-root"),
                ToolFact(
                    source_observation_id="graph-root-tool",
                    identity=_identity("graph-root"),
                    occurred_at=now,
                    monotonic_ns=2,
                    phase="started",
                    tool_call_id=tool_id,
                    source_tool_call_id="shared",
                    tool_name="search",
                ),
            )
        )
        await branch_a.append(
            (
                _fact("started", run_id="graph-branch-a"),
                ToolFact(
                    source_observation_id="graph-branch-a-tool",
                    identity=_identity("graph-branch-a"),
                    occurred_at=now,
                    monotonic_ns=2,
                    phase="result",
                    tool_call_id=tool_id,
                    source_tool_call_id="shared",
                    tool_name="search",
                    content=CapturedValue(
                        disposition="inline",
                        safe_size_bytes=4,
                        value="ok",
                    ),
                    result_status="success",
                ),
            )
        )
        await branch_b.append(
            (
                _fact("started", run_id="graph-branch-b"),
                ToolFact(
                    source_observation_id="graph-branch-b-tool",
                    identity=_identity("graph-branch-b"),
                    occurred_at=now,
                    monotonic_ns=2,
                    phase="abandoned",
                    tool_call_id=tool_id,
                    source_tool_call_id="shared",
                    tool_name="search",
                ),
            )
        )
        snapshot = await store.snapshot(_identity().thread_id)
        only_root = await store.query_trace_graph(
            snapshot.key,
            run_ids=("graph-root",),
            where=TraceGraphFilter(kinds={TraceGraphNodeKind.TOOL}),
            limit=10,
        )
        selected_a = await store.query_trace_graph(
            snapshot.key,
            run_ids=("graph-root", "graph-branch-a"),
            where=TraceGraphFilter(kinds={TraceGraphNodeKind.TOOL}),
            limit=10,
        )
        selected_b = await store.query_trace_graph(
            snapshot.key,
            run_ids=("graph-root", "graph-branch-b"),
            where=TraceGraphFilter(kinds={TraceGraphNodeKind.TOOL}),
            limit=10,
        )

        assert only_root.nodes[0].status.value == "waiting"
        assert selected_a.nodes[0].status.value == "succeeded"
        assert selected_b.nodes[0].status.value == "abandoned"
        assert selected_a.nodes[0].started_seq == only_root.nodes[0].started_seq
        async with engine.connect() as connection:
            physical_rows = await connection.scalar(
                text(
                    "SELECT COUNT(*) FROM tinkerfin_trace_graph_nodes "
                    "WHERE kind = 'tool'"
                )
            )
        assert physical_rows == 3
    finally:
        await root.aclose()
        await branch_a.aclose()
        await branch_b.aclose()
        await engine.dispose()


async def test_sqlite_graph_query_keeps_latest_non_null_lineage_values(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'graph-lineage-values.db'}"
    )
    memory = InMemoryTraceStore(namespace="graph-lineage-values")
    sql = SqlAlchemyTraceStore(engine, namespace="graph-lineage-values")
    stores = (memory, sql)
    origin_started_at = datetime(2026, 1, 1, 0, 0, 10, tzinfo=UTC)
    later_revision_at = origin_started_at - timedelta(seconds=5)
    subagent_id = "subagent:shared"
    try:
        for store in stores:
            root = await store.open_writer(_identity("graph-parent"))
            child = await store.open_writer(_identity("graph-child"))
            await root.append(
                (
                    _fact("started", run_id="graph-parent"),
                    SubagentFact(
                        source_observation_id="graph-parent-subagent",
                        identity=_identity("graph-parent"),
                        occurred_at=origin_started_at,
                        monotonic_ns=2,
                        phase="started",
                        subagent_id=subagent_id,
                        agent_name="KÄ研究Researcher",
                        parent_tool_call_id="call-task",
                        input=_captured({"description": "research"}),
                        status="running",
                    ),
                )
            )
            await child.append(
                (
                    _fact("started", run_id="graph-child"),
                    SubagentFact(
                        source_observation_id="graph-child-subagent",
                        identity=_identity("graph-child"),
                        occurred_at=later_revision_at,
                        monotonic_ns=3,
                        phase="completed",
                        subagent_id=subagent_id,
                        agent_name="KÄ研究Researcher",
                        status="succeeded",
                    ),
                )
            )
            await root.aclose()
            await child.aclose()

        observed = []
        for store in stores:
            snapshot = await store.snapshot(_identity().thread_id)
            page = await store.query_trace_graph(
                snapshot.key,
                run_ids=("graph-parent", "graph-child"),
                where=TraceGraphFilter(kinds={TraceGraphNodeKind.SUBAGENT}),
                limit=10,
            )
            observed.append(
                (
                    page.nodes[0].structural_parent_id,
                    page.nodes[0].link_issue,
                    page.nodes[0].started_at,
                )
            )

        assert observed[0][0] is not None
        assert observed == [observed[0], observed[0]]
        assert observed[0][2] == origin_started_at

        for store in stores:
            snapshot = await store.snapshot(_identity().thread_id)
            exact_unicode = await store.query_trace_graph(
                snapshot.key,
                run_ids=("graph-parent", "graph-child"),
                where=TraceGraphFilter(
                    kinds={TraceGraphNodeKind.SUBAGENT},
                    search="Ä",
                    started_after=origin_started_at - timedelta(seconds=1),
                ),
                limit=10,
            )
            different_unicode_case = await store.query_trace_graph(
                snapshot.key,
                run_ids=("graph-parent", "graph-child"),
                where=TraceGraphFilter(
                    kinds={TraceGraphNodeKind.SUBAGENT},
                    search="ä",
                ),
                limit=10,
            )
            exact_chinese = await store.query_trace_graph(
                snapshot.key,
                run_ids=("graph-parent", "graph-child"),
                where=TraceGraphFilter(
                    kinds={TraceGraphNodeKind.SUBAGENT},
                    search="研究",
                ),
                limit=10,
            )
            exact_kelvin = await store.query_trace_graph(
                snapshot.key,
                run_ids=("graph-parent", "graph-child"),
                where=TraceGraphFilter(
                    kinds={TraceGraphNodeKind.SUBAGENT},
                    search="K",
                ),
                limit=10,
            )
            ascii_does_not_fold_kelvin = await store.query_trace_graph(
                snapshot.key,
                run_ids=("graph-parent", "graph-child"),
                where=TraceGraphFilter(
                    kinds={TraceGraphNodeKind.SUBAGENT},
                    search="k",
                ),
                limit=10,
            )
            ascii_case_insensitive = await store.query_trace_graph(
                snapshot.key,
                run_ids=("graph-parent", "graph-child"),
                where=TraceGraphFilter(
                    kinds={TraceGraphNodeKind.SUBAGENT},
                    search="RESEARCHER",
                ),
                limit=10,
            )
            assert len(exact_unicode.nodes) == 1
            assert exact_unicode.facets.kinds[TraceGraphNodeKind.SUBAGENT] == 1
            assert different_unicode_case.nodes == ()
            assert len(exact_chinese.nodes) == 1
            assert len(exact_kelvin.nodes) == 1
            assert ascii_does_not_fold_kelvin.nodes == ()
            assert len(ascii_case_insensitive.nodes) == 1

        for store in stores:
            conflicting = await store.open_writer(_identity("graph-conflict"))
            await conflicting.append(
                (
                    _fact("started", run_id="graph-conflict"),
                    SubagentFact(
                        source_observation_id="graph-conflicting-subagent",
                        identity=_identity("graph-conflict"),
                        occurred_at=later_revision_at + timedelta(seconds=1),
                        monotonic_ns=4,
                        phase="completed",
                        subagent_id=subagent_id,
                        agent_name="KÄ研究Researcher",
                        parent_tool_call_id="another-call",
                        status="succeeded",
                    ),
                )
            )
            await conflicting.aclose()
            snapshot = await store.snapshot(_identity().thread_id)
            page = await store.query_trace_graph(
                snapshot.key,
                run_ids=("graph-parent", "graph-child", "graph-conflict"),
                where=TraceGraphFilter(kinds={TraceGraphNodeKind.SUBAGENT}),
                limit=10,
            )
            assert page.nodes[0].structural_parent_id is not None
            assert page.nodes[0].structural_parent_id.endswith(":another-call")
    finally:
        await engine.dispose()


async def test_memory_and_sql_graph_removal_isolated_to_one_sibling_lineage(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'graph-remove.db'}")
    stores = (
        InMemoryTraceStore(namespace="graph-remove"),
        SqlAlchemyTraceStore(engine, namespace="graph-remove"),
    )
    try:
        for store in stores:
            root = await store.open_writer(_identity("remove-root"))
            now = datetime.now(UTC)
            await root.append(
                (
                    _fact("started", run_id="remove-root"),
                    TurnFact(
                        source_observation_id="remove-turn",
                        identity=_identity("remove-root"),
                        occurred_at=now,
                        monotonic_ns=2,
                        turn_id="turn:remove",
                        user_message_id="human-remove",
                    ),
                )
            )
            snapshot = await store.snapshot(_identity().thread_id)
            root_page = await store.query_trace_graph(
                snapshot.key,
                run_ids=("remove-root",),
                where=TraceGraphFilter(
                    kinds={TraceGraphNodeKind.HUMAN_MESSAGE},
                    include_ancestor_nodes=False,
                ),
                limit=10,
            )
            assert len(root_page.nodes) == 1
            human_node_id = root_page.nodes[0].node_id

            removed = await store.open_writer(_identity("remove-branch"))
            await removed.append(
                (
                    _fact("started", run_id="remove-branch"),
                    MessageFact(
                        source_observation_id="remove-message",
                        identity=_identity("remove-branch"),
                        occurred_at=now,
                        monotonic_ns=3,
                        phase="removed",
                        message_id=human_node_id,
                        source_message_id="human-remove",
                        role="other",
                    ),
                )
            )
            sibling = await store.open_writer(_identity("keep-branch"))
            await sibling.append((_fact("started", run_id="keep-branch"),))
            snapshot = await store.snapshot(_identity().thread_id)

            removed_page = await store.query_trace_graph(
                snapshot.key,
                run_ids=("remove-root", "remove-branch"),
                where=TraceGraphFilter(
                    kinds={TraceGraphNodeKind.HUMAN_MESSAGE},
                    include_ancestor_nodes=False,
                ),
                limit=10,
            )
            sibling_page = await store.query_trace_graph(
                snapshot.key,
                run_ids=("remove-root", "keep-branch"),
                where=TraceGraphFilter(
                    kinds={TraceGraphNodeKind.HUMAN_MESSAGE},
                    include_ancestor_nodes=False,
                ),
                limit=10,
            )
            assert removed_page.nodes == ()
            assert tuple(node.node_id for node in sibling_page.nodes) == (
                human_node_id,
            )

            await store.rebuild_trace_graph(snapshot.key)
            rebuilt_removed = await store.query_trace_graph(
                snapshot.key,
                run_ids=("remove-root", "remove-branch"),
                where=TraceGraphFilter(
                    kinds={TraceGraphNodeKind.HUMAN_MESSAGE},
                    include_ancestor_nodes=False,
                ),
                limit=10,
            )
            assert rebuilt_removed.nodes == ()
            await root.aclose()
            await removed.aclose()
            await sibling.aclose()
    finally:
        await engine.dispose()


async def test_sqlite_graph_clears_a_tool_parent_gap_after_model_completion(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'resolved-tool-parent.db'}"
    )
    store = SqlAlchemyTraceStore(engine, namespace="resolved-tool-parent")
    writer = await store.open_writer(_identity())
    now = datetime.now(UTC)
    model_id = "model-call"
    try:
        await writer.append(
            (
                ModelCallFact(
                    source_observation_id="resolved-parent-model-start",
                    identity=_identity(),
                    occurred_at=now,
                    monotonic_ns=1,
                    phase="started",
                    call_id=model_id,
                    parent_call_id="agent-call",
                    request=_captured({"messages": []}),
                    system_message_positions=(),
                    output_message_ids=(),
                ),
                ToolFact(
                    source_observation_id="resolved-parent-tool-start",
                    identity=_identity(),
                    occurred_at=now,
                    monotonic_ns=2,
                    phase="started",
                    tool_call_id="tool-call",
                    source_tool_call_id="tool-call",
                    tool_name="read_file",
                ),
            )
        )
        await writer.append(
            (
                ModelCallFact(
                    source_observation_id="resolved-parent-model-complete",
                    identity=_identity(),
                    occurred_at=now,
                    monotonic_ns=3,
                    phase="completed",
                    call_id=model_id,
                    system_message_positions=(),
                    output_message_ids=(),
                    tool_call_ids=("tool-call",),
                ),
            )
        )
        snapshot = await store.snapshot(_identity().thread_id)
        page = await store.query_trace_graph(
            snapshot.key,
            run_ids=(_identity().run_id,),
            where=TraceGraphFilter(
                kinds={TraceGraphNodeKind.TOOL},
                include_ancestor_nodes=False,
            ),
            limit=10,
        )

        assert len(page.nodes) == 1
        assert page.nodes[0].structural_parent_id == model_id
        assert page.nodes[0].link_issue is None

        await writer.aclose()
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM tinkerfin_trace_graph_nodes"))
        assert await Tracer(store=store).rebuild_graph(_identity().thread_id) == 2
        rebuilt = await store.query_trace_graph(
            (await store.snapshot(_identity().thread_id)).key,
            run_ids=(_identity().run_id,),
            where=TraceGraphFilter(
                kinds={TraceGraphNodeKind.TOOL},
                include_ancestor_nodes=False,
            ),
            limit=10,
        )
        assert rebuilt.nodes[0].structural_parent_id == model_id
        assert rebuilt.nodes[0].link_issue is None
    finally:
        await writer.aclose()
        await engine.dispose()


async def test_sqlite_graph_query_reads_details_through_a_custom_encrypted_codec(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'encrypted-graph.db'}"
    )
    store = SqlAlchemyTraceStore(
        engine,
        namespace="encrypted-graph-query",
        codec=_EncryptedTraceCodec(),
    )
    writer = await store.open_writer(_identity())
    marker = "encrypted-final-request-marker"
    now = datetime.now(UTC)
    try:
        await writer.append(
            (
                _fact("started"),
                ModelCallFact(
                    source_observation_id="encrypted-model-start",
                    identity=_identity(),
                    occurred_at=now,
                    monotonic_ns=2,
                    phase="started",
                    call_id="encrypted-model-call",
                    system_message_positions=(),
                    output_message_ids=(),
                    request=_captured({"messages": [{"content": marker}]}),
                ),
                ModelCallFact(
                    source_observation_id="encrypted-model-completed",
                    identity=_identity(),
                    occurred_at=now,
                    monotonic_ns=3,
                    phase="completed",
                    call_id="encrypted-model-call",
                    system_message_positions=(),
                    output_message_ids=(),
                ),
            )
        )
        snapshot = await store.snapshot(_identity().thread_id)
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM tinkerfin_trace_graph_nodes"))
        assert await Tracer(store=store).rebuild_graph(_identity().thread_id) == 2
        page = await store.query_trace_graph(
            snapshot.key,
            run_ids=(_identity().run_id,),
            where=TraceGraphFilter(kinds={TraceGraphNodeKind.MODEL}),
            limit=10,
        )
        node = project_trace_graph_node(
            page.nodes[0],
            turn_id="turn:test",
            parent_id=None,
            relationship_missing=False,
        )
        async with engine.connect() as connection:
            payloads = (
                (
                    await connection.execute(
                        text("SELECT payload FROM tinkerfin_trace_events")
                    )
                )
                .scalars()
                .all()
            )

        assert node.request == {"messages": [{"content": marker}]}
        assert all(marker.encode() not in bytes(payload) for payload in payloads)
    finally:
        await writer.aclose()
        await engine.dispose()


async def test_memory_and_sql_facets_exclude_their_own_active_filter(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'facets.db'}")
    stores = (
        InMemoryTraceStore(namespace="graph-facets"),
        SqlAlchemyTraceStore(engine, namespace="graph-facets"),
    )
    now = datetime.now(UTC)
    request = _captured({"messages": []})
    try:
        for store in stores:
            writer = await store.open_writer(_identity())
            await writer.append(
                (
                    _fact("started"),
                    ModelCallFact(
                        source_observation_id="facet-success-start",
                        identity=_identity(),
                        occurred_at=now,
                        monotonic_ns=2,
                        phase="started",
                        call_id="facet-success",
                        system_message_positions=(),
                        output_message_ids=(),
                        request=request,
                    ),
                    ModelCallFact(
                        source_observation_id="facet-success-completed",
                        identity=_identity(),
                        occurred_at=now,
                        monotonic_ns=3,
                        phase="completed",
                        call_id="facet-success",
                        system_message_positions=(),
                        output_message_ids=(),
                    ),
                    ModelCallFact(
                        source_observation_id="facet-failure-start",
                        identity=_identity(),
                        occurred_at=now,
                        monotonic_ns=4,
                        phase="started",
                        call_id="facet-failure",
                        system_message_positions=(),
                        output_message_ids=(),
                        request=request,
                    ),
                    ModelCallFact(
                        source_observation_id="facet-failure-completed",
                        identity=_identity(),
                        occurred_at=now,
                        monotonic_ns=5,
                        phase="failed",
                        call_id="facet-failure",
                        system_message_positions=(),
                        output_message_ids=(),
                        error_type="builtins.RuntimeError",
                    ),
                )
            )
            await writer.append((_fact("terminal"), _fact("closed")), mandatory=True)
            await writer.aclose()
            snapshot = await store.snapshot(_identity().thread_id)

            kind_page = await store.query_trace_graph(
                snapshot.key,
                run_ids=(_identity().run_id,),
                where=TraceGraphFilter(
                    kinds={TraceGraphNodeKind.MODEL},
                    include_technical_nodes=True,
                ),
                limit=10,
            )
            status_page = await store.query_trace_graph(
                snapshot.key,
                run_ids=(_identity().run_id,),
                where=TraceGraphFilter(
                    statuses={TraceGraphNodeStatus.FAILED},
                    include_technical_nodes=True,
                ),
                limit=10,
            )

            assert kind_page.facets.kinds[TraceGraphNodeKind.RUN] == 1
            assert kind_page.facets.kinds[TraceGraphNodeKind.MODEL] == 2
            assert status_page.facets.statuses[TraceGraphNodeStatus.FAILED] == 1
            assert status_page.facets.statuses[TraceGraphNodeStatus.SUCCEEDED] == 2
    finally:
        await engine.dispose()


async def test_memory_and_sql_graph_paging_share_the_digest_tie_breaker(
    tmp_path: Path,
) -> None:
    """Equal timestamps page identically across the two built-in Stores."""

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'graph-order.db'}")
    stores = (
        InMemoryTraceStore(namespace="graph-order"),
        SqlAlchemyTraceStore(engine, namespace="graph-order"),
    )
    occurred_at = datetime.now(UTC)
    call_specs = (
        ("model-call-alpha", "model%literal"),
        ("model-call-beta", "modelXliteral"),
    )
    call_ids = tuple(call_id for call_id, _model in call_specs)
    request = _captured({"messages": []})
    try:
        for store in stores:
            writer = await store.open_writer(_identity())
            await writer.append(
                (
                    _fact("started"),
                    *(
                        fact
                        for call_id, model in call_specs
                        for fact in (
                            ModelCallFact(
                                source_observation_id=f"{call_id}-started",
                                identity=_identity(),
                                occurred_at=occurred_at,
                                monotonic_ns=2,
                                phase="started",
                                call_id=call_id,
                                system_message_positions=(),
                                output_message_ids=(),
                                model=model,
                                request=request,
                            ),
                            ModelCallFact(
                                source_observation_id=f"{call_id}-completed",
                                identity=_identity(),
                                occurred_at=occurred_at,
                                monotonic_ns=3,
                                phase="completed",
                                call_id=call_id,
                                system_message_positions=(),
                                output_message_ids=(),
                            ),
                        )
                    ),
                    ToolExecutionFact(
                        source_observation_id="tool-cross-field-started",
                        identity=_identity(),
                        occurred_at=occurred_at,
                        monotonic_ns=4,
                        phase="started",
                        execution_id="tool-execution-cross-field",
                        agent_name="bar",
                        tool_name="foo",
                        input=request,
                    ),
                    ToolExecutionFact(
                        source_observation_id="tool-cross-field-completed",
                        identity=_identity(),
                        occurred_at=occurred_at,
                        monotonic_ns=5,
                        phase="completed",
                        execution_id="tool-execution-cross-field",
                        agent_name="bar",
                        tool_name="foo",
                        output=request,
                    ),
                )
            )
            await writer.append(
                (_fact("terminal"), _fact("closed")),
                mandatory=True,
            )
            await writer.aclose()

        observed_orders: list[tuple[str, ...]] = []
        for store in stores:
            key = (await store.snapshot(_identity().thread_id)).key
            first = await store.query_trace_graph(
                key,
                run_ids=(_identity().run_id,),
                where=TraceGraphFilter(kinds={TraceGraphNodeKind.MODEL}),
                limit=1,
            )
            assert first.has_more is True
            assert first.next_started_at is not None
            assert first.next_node_id is not None
            second = await store.query_trace_graph(
                key,
                run_ids=(_identity().run_id,),
                where=TraceGraphFilter(kinds={TraceGraphNodeKind.MODEL}),
                limit=1,
                before_started_at=first.next_started_at,
                before_node_id=first.next_node_id,
            )
            observed_orders.append(
                tuple(node.node_id for node in (*first.nodes, *second.nodes))
            )

        expected = tuple(
            sorted(
                call_ids,
                key=lambda value: hashlib.sha256(value.encode()).hexdigest(),
                reverse=True,
            )
        )
        assert observed_orders == [expected, expected]
        for store in stores:
            key = (await store.snapshot(_identity().thread_id)).key
            literal = await store.query_trace_graph(
                key,
                run_ids=(_identity().run_id,),
                where=TraceGraphFilter(
                    kinds={TraceGraphNodeKind.MODEL},
                    search="%",
                ),
                limit=10,
            )
            assert tuple(node.node_id for node in literal.nodes) == (
                "model-call-alpha",
            )
            cross_field = await store.query_trace_graph(
                key,
                run_ids=(_identity().run_id,),
                where=TraceGraphFilter(
                    kinds={TraceGraphNodeKind.TOOL},
                    search="foo bar",
                ),
                limit=10,
            )
            assert cross_field.nodes == ()
    finally:
        await engine.dispose()


async def test_sqlite_store_auto_setup_round_trip_and_generation_delete(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'trace.db'}")
    borrowed_pool = engine.pool
    store = SqlAlchemyTraceStore(
        engine,
        namespace="tests",
        options=TraceStoreOptions(
            writer_lease_seconds=2,
            writer_heartbeat_interval_seconds=0.5,
            follow_poll_seconds=0.01,
        ),
    )
    try:
        writer = await store.open_writer(_identity())
        ordinary = await writer.append((_fact("started"), _fact("input")))
        terminal = await writer.append(
            (_fact("terminal"), _fact("closed")),
            mandatory=True,
        )
        await writer.aclose()

        snapshot = await store.snapshot(_identity().thread_id)
        assert snapshot.as_of_seq == 4
        assert snapshot.active_writers == ()
        assert [event.trace_seq for event in ordinary + terminal] == [1, 2, 3, 4]
        assert (
            await store.read_events(
                snapshot.key,
                after_seq=0,
                as_of_seq=4,
                limit=10,
            )
            == ordinary + terminal
        )
        assert [
            event.trace_seq
            for event in await store.read_events_reverse(
                snapshot.key,
                before_seq=5,
                limit=2,
            )
        ] == [4, 3]

        checkpoint = TraceProjectionCheckpoint(
            key=snapshot.key,
            projection_name="tests.projection",
            run_id="run-sql",
            as_of_seq=4,
            state={"count": 4},
        )
        await store.save_projection_checkpoint(checkpoint, expected_as_of_seq=None)
        assert (
            await store.load_projection_checkpoint(
                snapshot.key,
                projection_name="tests.projection",
                run_id="run-sql",
                as_of_seq=4,
            )
            == checkpoint
        )
        await store.delete(snapshot.key)
        replacement = await store.open_writer(_identity())
        assert replacement.key.generation != snapshot.key.generation
        await replacement.aclose()
        assert engine.pool is borrowed_pool

        async with engine.connect() as connection:
            table_names = await connection.run_sync(
                lambda sync_connection: tuple(
                    inspect(sync_connection).get_table_names()
                )
            )
        assert set(TRACE_TABLE_NAMES) <= set(table_names)
    finally:
        await engine.dispose()


async def test_tracer_rebuilds_graph_without_rewriting_ledger(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'rebuild.db'}")
    store = SqlAlchemyTraceStore(engine, namespace="graph-rebuild")
    writer = await store.open_writer(_identity())
    now = datetime.now(UTC)
    try:
        await writer.append(
            (
                _fact("started"),
                _fact("input"),
                CallTrackingFact(
                    source_observation_id="observation-call-tracking-rebuild",
                    identity=_identity(),
                    occurred_at=now,
                    monotonic_ns=2,
                ),
                ModelCallFact(
                    source_observation_id="observation-model-start-rebuild",
                    identity=_identity(),
                    occurred_at=now,
                    monotonic_ns=3,
                    phase="started",
                    call_id="model-call-rebuild",
                    system_message_positions=(),
                    output_message_ids=(),
                    request=_captured({"messages": [{"content": "rebuild-marker"}]}),
                ),
                ModelCallFact(
                    source_observation_id="observation-model-end-rebuild",
                    identity=_identity(),
                    occurred_at=now,
                    monotonic_ns=4,
                    phase="completed",
                    call_id="model-call-rebuild",
                    system_message_positions=(),
                    output_message_ids=(),
                ),
            )
        )
        await writer.append((_fact("terminal"), _fact("closed")), mandatory=True)
        await writer.aclose()
        async with engine.begin() as connection:
            ledger_count = await connection.scalar(
                text("SELECT COUNT(*) FROM tinkerfin_trace_events")
            )
            await connection.execute(text("DELETE FROM tinkerfin_trace_graph_nodes"))

        rebuilt = await Tracer(store=store).rebuild_graph(_identity().thread_id)
        page = await store.query_trace_graph(
            (await store.snapshot(_identity().thread_id)).key,
            run_ids=(_identity().run_id,),
            where=TraceGraphFilter(kinds={TraceGraphNodeKind.MODEL}),
            limit=10,
        )
        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT COUNT(*) FROM tinkerfin_trace_events")
                )
                == ledger_count
            )

        assert rebuilt == 2
        assert len(page.nodes) == 1
        assert project_trace_graph_node(
            page.nodes[0],
            turn_id="turn:test",
            parent_id=None,
            relationship_missing=False,
        ).request == {"messages": [{"content": "rebuild-marker"}]}
    finally:
        await engine.dispose()


async def test_rebuild_keeps_historical_task_missing_parent_evidence(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'rebuild-missing-parent.db'}"
    )
    store = SqlAlchemyTraceStore(engine, namespace="graph-rebuild-missing-parent")
    writer = await store.open_writer(_identity())
    now = datetime.now(UTC)
    try:
        await writer.append(
            (
                _fact("started"),
                TurnFact(
                    source_observation_id="historical-turn",
                    identity=_identity(),
                    occurred_at=now,
                    monotonic_ns=2,
                    turn_id="turn:historical",
                    user_message_id="human:historical",
                ),
                RuntimeTaskFact(
                    source_observation_id="historical-task-start",
                    identity=_identity(),
                    occurred_at=now,
                    monotonic_ns=2,
                    phase="started",
                    task_id="historical-task",
                    source_task_id="historical-task",
                    task_name="model",
                ),
                RuntimeTaskFact(
                    source_observation_id="historical-task-completed",
                    identity=_identity(),
                    occurred_at=now,
                    monotonic_ns=3,
                    phase="completed",
                    task_id="historical-task",
                    source_task_id="historical-task",
                    task_name="model",
                ),
            )
        )
        await writer.append((_fact("terminal"), _fact("closed")), mandatory=True)
        await writer.aclose()
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM tinkerfin_trace_graph_nodes"))

        assert await Tracer(store=store).rebuild_graph(_identity().thread_id) == 3
        page = await store.query_trace_graph(
            (await store.snapshot(_identity().thread_id)).key,
            run_ids=(_identity().run_id,),
            where=TraceGraphFilter(
                kinds={TraceGraphNodeKind.RUNTIME_TASK},
                include_ancestor_nodes=False,
                include_technical_nodes=True,
            ),
            limit=10,
        )

        assert len(page.nodes) == 1
        assert page.nodes[0].structural_parent_id is not None
        public = await Tracer(store=store).query(
            _identity().thread_id,
            where=TraceGraphFilter(
                kinds={TraceGraphNodeKind.RUNTIME_TASK},
                include_technical_nodes=True,
            ),
            limit=10,
        )
        task = next(
            node
            for node in public.nodes
            if node.kind is TraceGraphNodeKind.RUNTIME_TASK
        )
        assert task.parent_id is None
        assert task.link_issues
    finally:
        await writer.aclose()
        await engine.dispose()


async def test_rebuild_merges_lifecycle_revisions_across_event_pages(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'rebuild-page-boundary.db'}"
    )
    store = SqlAlchemyTraceStore(
        engine,
        namespace="graph-rebuild-page-boundary",
        limits=TraceLimits(follow_batch_size=1),
    )
    writer = await store.open_writer(_identity())
    now = datetime.now(UTC)
    try:
        await writer.append(
            (
                _fact("started"),
                TurnFact(
                    source_observation_id="page-boundary-turn",
                    identity=_identity(),
                    occurred_at=now,
                    monotonic_ns=2,
                    turn_id="turn:page-boundary",
                    user_message_id="human:page-boundary",
                ),
                ModelCallFact(
                    source_observation_id="page-boundary-model-start",
                    identity=_identity(),
                    occurred_at=now,
                    monotonic_ns=3,
                    phase="started",
                    call_id="model-page-boundary",
                    system_message_positions=(),
                    output_message_ids=(),
                    request=_captured({"messages": []}),
                ),
                ModelCallFact(
                    source_observation_id="page-boundary-model-completed",
                    identity=_identity(),
                    occurred_at=now,
                    monotonic_ns=4,
                    phase="completed",
                    call_id="model-page-boundary",
                    system_message_positions=(),
                    output_message_ids=(),
                ),
            )
        )
        await writer.append((_fact("terminal"), _fact("closed")), mandatory=True)
        await writer.aclose()
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM tinkerfin_trace_graph_nodes"))

        assert await Tracer(store=store).rebuild_graph(_identity().thread_id) == 3
        page = await store.query_trace_graph(
            (await store.snapshot(_identity().thread_id)).key,
            run_ids=(_identity().run_id,),
            where=TraceGraphFilter(include_technical_nodes=True),
            limit=10,
        )
        run = next(node for node in page.nodes if node.kind is TraceGraphNodeKind.RUN)
        model = next(
            node for node in page.nodes if node.kind is TraceGraphNodeKind.MODEL
        )

        assert len(page.nodes) == 3
        assert run.status is TraceGraphNodeStatus.SUCCEEDED
        assert model.status is TraceGraphNodeStatus.SUCCEEDED
        assert model.started_seq < model.updated_seq
    finally:
        await writer.aclose()
        await engine.dispose()


async def test_graph_index_scopes_replayed_runtime_tasks_to_their_run(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'task-replay.db'}")
    store = SqlAlchemyTraceStore(engine, namespace="task-replay")
    try:
        for run_id in ("run-task-first", "run-task-resume"):
            writer = await store.open_writer(_identity(run_id))
            now = datetime.now(UTC)
            await writer.append(
                (
                    _fact("started", run_id=run_id),
                    _fact("input", run_id=run_id),
                    RuntimeTaskFact(
                        source_observation_id=f"{run_id}-task-start",
                        identity=_identity(run_id),
                        occurred_at=now,
                        monotonic_ns=2,
                        phase="started",
                        task_id="task:root:replayed",
                        source_task_id="replayed",
                        task_name="tools",
                    ),
                    RuntimeTaskFact(
                        source_observation_id=f"{run_id}-task-result",
                        identity=_identity(run_id),
                        occurred_at=now,
                        monotonic_ns=3,
                        phase="completed",
                        task_id="task:root:replayed",
                        source_task_id="replayed",
                        task_name="tools",
                    ),
                )
            )
            await writer.append(
                (
                    _fact("terminal", run_id=run_id),
                    _fact("closed", run_id=run_id),
                ),
                mandatory=True,
            )
            await writer.aclose()

        snapshot = await store.snapshot(_identity().thread_id)
        page = await store.query_trace_graph(
            snapshot.key,
            run_ids=("run-task-first", "run-task-resume"),
            where=TraceGraphFilter(
                kinds={TraceGraphNodeKind.RUNTIME_TASK},
                include_technical_nodes=True,
            ),
            limit=10,
        )

        assert {node.run_id for node in page.nodes} == {
            "run-task-first",
            "run-task-resume",
        }
        assert len({node.node_id for node in page.nodes}) == 2
    finally:
        await engine.dispose()


async def test_sql_setup_retries_after_one_retained_task_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'setup-retry.db'}")
    store = SqlAlchemyTraceStore(engine)
    original = _backend(store)._setup_once
    first_attempt_started = asyncio.Event()
    release_first_attempt = asyncio.Event()
    attempts = 0

    async def fail_once() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            first_attempt_started.set()
            await release_first_attempt.wait()
            raise TraceStoreError("transient setup failure")
        await original()

    monkeypatch.setattr(_backend(store), "_setup_once", fail_once)
    try:
        first_waiter = asyncio.create_task(store.setup())
        await first_attempt_started.wait()
        second_waiter = asyncio.create_task(store.setup())
        await asyncio.sleep(0)
        release_first_attempt.set()
        first, second = await asyncio.gather(
            first_waiter,
            second_waiter,
            return_exceptions=True,
        )
        assert isinstance(first, TraceStoreError)
        assert isinstance(second, TraceStoreError)
        assert attempts == 1

        await store.setup()
        assert attempts == 2
        await store.setup()
        assert attempts == 2
    finally:
        release_first_attempt.set()
        await engine.dispose()


async def test_sql_setup_rejects_partial_existing_trace_schema(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'partial.db'}")
    store = SqlAlchemyTraceStore(engine)
    writer = await store.open_writer(_identity())
    await writer.append((_fact("started"),))
    await writer.aclose()
    async with engine.begin() as connection:
        await connection.exec_driver_sql("DROP TABLE tinkerfin_trace_events")

    replacement = SqlAlchemyTraceStore(engine)
    try:
        with pytest.raises(TraceStoreProtocolError, match="incomplete"):
            await replacement.setup()
        async with engine.connect() as connection:
            table_names = await connection.run_sync(
                lambda sync_connection: set(inspect(sync_connection).get_table_names())
            )
        assert "tinkerfin_trace_events" not in table_names
    finally:
        await engine.dispose()


async def test_sql_setup_rejects_foreign_check_constraint(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'check.db'}")
    await SqlAlchemyTraceStore(engine).setup()
    async with engine.begin() as connection:
        ddl = await connection.scalar(
            text(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'table' AND name = 'tinkerfin_trace_threads'"
            )
        )
        assert isinstance(ddl, str)
        await connection.exec_driver_sql("DROP TABLE tinkerfin_trace_threads")
        await connection.exec_driver_sql(
            ddl.rstrip()[:-1] + ", CHECK (persisted_bytes = 0))"
        )

    try:
        with pytest.raises(TraceStoreProtocolError, match="check constraints"):
            await SqlAlchemyTraceStore(engine).setup()
    finally:
        await engine.dispose()


async def test_sql_setup_caller_cancellation_keeps_the_running_shared_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'setup-cancel.db'}")
    store = SqlAlchemyTraceStore(engine)
    original = _backend(store)._setup_once
    started = asyncio.Event()
    release = asyncio.Event()
    attempts = 0

    async def delayed_setup() -> None:
        nonlocal attempts
        attempts += 1
        started.set()
        await release.wait()
        await original()

    monkeypatch.setattr(_backend(store), "_setup_once", delayed_setup)
    caller = asyncio.create_task(store.setup())
    try:
        await started.wait()
        caller.cancel("setup caller stopped")
        await asyncio.sleep(0)
        caller.cancel("repeated setup cancellation")
        await asyncio.sleep(0)
        assert not caller.done()
        release.set()
        with pytest.raises(asyncio.CancelledError) as captured:
            await caller
        assert captured.value.args == ("setup caller stopped",)
        await store.setup()
        assert attempts == 1
        await store.setup()
        assert attempts == 1
    finally:
        release.set()
        await engine.dispose()


async def test_sql_setup_retries_after_the_retained_task_cancels_itself(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'setup-self-cancel.db'}"
    )
    store = SqlAlchemyTraceStore(engine)
    original = _backend(store)._setup_once
    attempts = 0

    async def cancel_once() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise asyncio.CancelledError
        await original()

    monkeypatch.setattr(_backend(store), "_setup_once", cancel_once)
    try:
        with pytest.raises(asyncio.CancelledError):
            await store.setup()
        await store.setup()
        assert attempts == 2
    finally:
        await engine.dispose()


async def test_sqlite_unknown_commit_reuses_event_and_checkpoint_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'unknown.db'}")
    store = SqlAlchemyTraceStore(
        engine,
        namespace="unknown-commit",
        options=TraceStoreOptions(
            commit_retry_attempts=2,
            commit_retry_delay_seconds=0.001,
        ),
    )
    writer = await store.open_writer(_identity())
    original = _backend(store)._raw_write_connection

    def busy_error() -> DBAPIError:
        original_error = sqlite3.OperationalError("database is locked")
        original_error.sqlite_errorcode = sqlite3.SQLITE_BUSY
        return DBAPIError(None, None, original_error, False)

    def fail_once_after_commit() -> None:
        remaining = 1

        @asynccontextmanager
        async def transaction() -> AsyncIterator[AsyncConnection]:
            nonlocal remaining
            async with original() as connection:
                yield connection
            if remaining:
                remaining -= 1
                raise busy_error()

        monkeypatch.setattr(_backend(store), "_raw_write_connection", transaction)

    try:
        # The injected DBAPI failure happens after the real transaction committed.
        # Public append must prove the first commit by its retained event IDs.
        fail_once_after_commit()
        committed = await writer.append((_fact("started"),))
        snapshot = await store.snapshot(_identity().thread_id)
        assert snapshot.as_of_seq == 1
        assert [
            event.event_id
            for event in await store.read_events(
                snapshot.key,
                after_seq=0,
                as_of_seq=1,
                limit=10,
            )
        ] == [committed[0].event_id]

        fail_once_after_commit()
        checkpoint = TraceProjectionCheckpoint(
            key=snapshot.key,
            projection_name="unknown.projection",
            run_id="run-sql",
            as_of_seq=1,
            state={"count": 1},
        )
        assert (
            await store.save_projection_checkpoint(
                checkpoint,
                expected_as_of_seq=None,
            )
            == checkpoint
        )
        assert (
            await store.load_projection_checkpoint(
                snapshot.key,
                projection_name="unknown.projection",
                run_id="run-sql",
                as_of_seq=1,
            )
            == checkpoint
        )
        await writer.append(
            (_fact("terminal"), _fact("closed")),
            mandatory=True,
        )
        await writer.aclose()

        fail_once_after_commit()
        await store.delete(snapshot.key)
        with pytest.raises(TraceThreadNotFound):
            await store.snapshot_key(snapshot.key)
    finally:
        monkeypatch.setattr(_backend(store), "_raw_write_connection", original)
        await writer.aclose()
        await engine.dispose()


async def test_sqlite_unknown_writer_open_commit_reuses_owner_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'unknown-open.db'}")
    store = SqlAlchemyTraceStore(
        engine,
        options=TraceStoreOptions(
            writer_lease_seconds=0.2,
            writer_heartbeat_interval_seconds=0.1,
            commit_retry_attempts=2,
            commit_retry_delay_seconds=0.3,
        ),
    )
    await store.setup()
    original = _backend(store)._raw_write_connection
    remaining = 1

    @asynccontextmanager
    async def fail_after_commit() -> AsyncIterator[AsyncConnection]:
        nonlocal remaining
        async with original() as connection:
            yield connection
        if remaining:
            remaining -= 1
            original_error = sqlite3.OperationalError("database is locked")
            original_error.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise DBAPIError(None, None, original_error, False)

    writer = None
    try:
        monkeypatch.setattr(_backend(store), "_raw_write_connection", fail_after_commit)
        writer = await store.open_writer(_identity())
        committed = await writer.append((_fact("started"),))
        snapshot = await store.snapshot(_identity().thread_id)
        assert snapshot.active_run_ids == (_identity().run_id,)
        assert snapshot.as_of_seq == 1
        assert committed[0].trace_seq == 1
    finally:
        monkeypatch.setattr(_backend(store), "_raw_write_connection", original)
        if writer is not None:
            await writer.aclose()
        await engine.dispose()


async def test_sqlite_unknown_append_commit_renews_short_writer_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'append-lease.db'}")
    store = SqlAlchemyTraceStore(
        engine,
        options=TraceStoreOptions(
            writer_lease_seconds=0.2,
            writer_heartbeat_interval_seconds=0.1,
            commit_retry_attempts=2,
            commit_retry_delay_seconds=0.3,
        ),
    )
    writer = await store.open_writer(_identity())
    backend = _backend(store)
    original = backend._raw_write_connection
    remaining = 1

    @asynccontextmanager
    async def fail_after_commit() -> AsyncIterator[AsyncConnection]:
        nonlocal remaining
        async with original() as connection:
            yield connection
        if remaining:
            remaining -= 1
            original_error = sqlite3.OperationalError("database is locked")
            original_error.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise DBAPIError(None, None, original_error, False)

    try:
        monkeypatch.setattr(backend, "_raw_write_connection", fail_after_commit)
        first = await writer.append((_fact("started"),))
        second = await writer.append((_fact("input"),))

        assert [first[0].trace_seq, second[0].trace_seq] == [1, 2]
    finally:
        monkeypatch.setattr(backend, "_raw_write_connection", original)
        await writer.aclose()
        await engine.dispose()


async def test_sqlite_proven_append_survives_peer_takeover_during_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "append-takeover.db"
    first_engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    peer_engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    namespace = "append-takeover"
    options = TraceStoreOptions(
        writer_lease_seconds=0.15,
        writer_heartbeat_interval_seconds=0.14,
        commit_retry_attempts=2,
        commit_retry_delay_seconds=2,
    )
    first_store = SqlAlchemyTraceStore(
        first_engine,
        namespace=namespace,
        options=options,
    )
    peer_store = SqlAlchemyTraceStore(
        peer_engine,
        namespace=namespace,
        options=options,
    )
    identity = _identity()
    writer = await first_store.open_writer(identity)
    backend = _backend(first_store)
    peer_backend = _backend(peer_store)
    original = backend._raw_write_connection
    first_commit_finished = asyncio.Event()
    remaining = 1

    @asynccontextmanager
    async def fail_after_commit() -> AsyncIterator[AsyncConnection]:
        nonlocal remaining
        async with original() as connection:
            yield connection
        if remaining:
            remaining -= 1
            first_commit_finished.set()
            original_error = sqlite3.OperationalError("database is locked")
            original_error.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise DBAPIError(None, None, original_error, False)

    monkeypatch.setattr(backend, "_raw_write_connection", fail_after_commit)
    append = asyncio.create_task(writer.append((_fact("started"),)))
    replacement = None
    try:
        await first_commit_finished.wait()
        await asyncio.sleep(1.2)
        replacement = await peer_store.open_writer(identity)
        committed = await append
        state = await peer_backend.load_ledger_state(
            TraceLedgerStateRequest(
                namespace=namespace,
                thread_id=identity.thread_id,
                generation=replacement.key.generation,
                run_id=identity.run_id,
            )
        )
        stored = await peer_store.read_events(
            replacement.key,
            after_seq=0,
            as_of_seq=1,
            limit=10,
        )

        assert [event.event_id for event in committed] == [stored[0].event_id]
        assert state.target_writer is not None
        assert state.target_writer.fence == 2
    finally:
        monkeypatch.setattr(backend, "_raw_write_connection", original)
        if not append.done():
            append.cancel()
            await asyncio.gather(append, return_exceptions=True)
        await writer.aclose()
        if replacement is not None:
            await replacement.aclose()
        await first_engine.dispose()
        await peer_engine.dispose()


async def test_sqlite_retry_exhaustion_uses_stable_store_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'timeout.db'}")
    store = SqlAlchemyTraceStore(
        engine,
        options=TraceStoreOptions(
            commit_retry_attempts=2,
            commit_retry_delay_seconds=0.001,
        ),
    )
    writer = await store.open_writer(_identity())
    original = _backend(store)._raw_write_connection
    original_error = sqlite3.OperationalError("database is locked")
    original_error.sqlite_errorcode = sqlite3.SQLITE_BUSY

    @asynccontextmanager
    async def unavailable() -> AsyncIterator[AsyncConnection]:
        raise DBAPIError(None, None, original_error, False)
        yield  # pragma: no cover - required only by the async context manager shape

    try:
        monkeypatch.setattr(_backend(store), "_raw_write_connection", unavailable)
        with pytest.raises(TraceStoreTimeout) as captured:
            await writer.append((_fact("started"),))
        assert isinstance(captured.value.cause, DBAPIError)
    finally:
        monkeypatch.setattr(_backend(store), "_raw_write_connection", original)
        await writer.aclose()
        await engine.dispose()


async def test_sqlite_store_rejects_corrupt_opaque_payload(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'corrupt.db'}")
    store = SqlAlchemyTraceStore(engine)
    writer = await store.open_writer(_identity())
    try:
        committed = await writer.append((_fact("started"),))
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE tinkerfin_trace_events SET payload = :payload "
                    "WHERE event_id = :event_id"
                ),
                {"payload": b"{}", "event_id": committed[0].event_id},
            )
        snapshot = await store.snapshot(_identity().thread_id)
        with pytest.raises(TraceStoreProtocolError, match="digest mismatch"):
            await store.read_events(
                snapshot.key,
                after_seq=0,
                as_of_seq=1,
                limit=1,
            )
    finally:
        await writer.aclose()
        await engine.dispose()


async def test_sqlite_fixed_event_read_does_not_cross_concurrent_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fixed-read.db'}")
    store = SqlAlchemyTraceStore(
        engine,
        options=TraceStoreOptions(
            commit_retry_attempts=10,
            commit_retry_delay_seconds=0.01,
        ),
    )
    writer = await store.open_writer(_identity())
    await writer.append((_fact("started"),))
    snapshot = await store.snapshot(_identity().thread_id)
    backend = _backend(store)
    original = backend._thread_row
    head_observed = asyncio.Event()
    continue_read = asyncio.Event()

    async def pause_after_head(connection: AsyncConnection, *, thread_id: str):
        row = await original(connection, thread_id=thread_id)
        head_observed.set()
        await continue_read.wait()
        return row

    monkeypatch.setattr(backend, "_thread_row", pause_after_head)
    read = asyncio.create_task(
        store.read_events(
            snapshot.key,
            after_seq=0,
            as_of_seq=2,
            limit=10,
        )
    )
    append = None
    try:
        await head_observed.wait()
        append = asyncio.create_task(writer.append((_fact("input"),)))
        await asyncio.sleep(0.05)
        continue_read.set()
        page = await read
        await append

        assert [event.trace_seq for event in page] == [1]
    finally:
        continue_read.set()
        if not read.done():
            read.cancel()
            await asyncio.gather(read, return_exceptions=True)
        if append is not None and not append.done():
            append.cancel()
            await asyncio.gather(append, return_exceptions=True)
        monkeypatch.setattr(backend, "_thread_row", original)
        await writer.aclose()
        await engine.dispose()


async def test_connection_cleanup_survives_repeated_caller_cancellation() -> None:
    owner_started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()

    async def cleanup() -> None:
        cleanup_started.set()
        await release_cleanup.wait()
        cleanup_finished.set()

    async def owner() -> None:
        owner_started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError as error:
            await _complete_connection_cleanup(
                cleanup(),
                task_name="test-trace-connection-cleanup",
                primary_error=error,
            )
            raise

    owner_task = asyncio.create_task(owner())
    await owner_started.wait()
    owner_task.cancel("first cancellation")
    await cleanup_started.wait()
    owner_task.cancel("repeated cancellation")
    await asyncio.sleep(0)
    try:
        assert not owner_task.done()
    finally:
        release_cleanup.set()

    with pytest.raises(asyncio.CancelledError) as captured:
        await owner_task
    assert captured.value.args == ("first cancellation",)
    assert cleanup_finished.is_set()


async def test_connection_cleanup_keeps_the_operation_failure_primary() -> None:
    primary = RuntimeError("operation failed")

    async def cleanup() -> None:
        raise ValueError("close failed")

    await _complete_connection_cleanup(
        cleanup(),
        task_name="test-trace-connection-cleanup-failure",
        primary_error=primary,
    )

    assert any("builtins.ValueError" in note for note in primary.__notes__)


async def test_sqlite_cancelled_lock_wait_releases_connection_for_retry(
    tmp_path: Path,
) -> None:
    database = tmp_path / "cancel.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database}",
        connect_args={"timeout": 30},
    )
    blocker_engine = create_async_engine(
        f"sqlite+aiosqlite:///{database}",
        connect_args={"timeout": 30},
    )
    store = SqlAlchemyTraceStore(engine)
    writer = await store.open_writer(_identity())
    blocker = await blocker_engine.connect()
    try:
        await blocker.exec_driver_sql("BEGIN IMMEDIATE")
        waiting = asyncio.create_task(writer.append((_fact("started"),)))
        await asyncio.sleep(0.05)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        await blocker.rollback()

        committed = await writer.append((_fact("started"),))
        assert committed[0].trace_seq == 1
        async with engine.connect() as borrowed_again:
            assert await borrowed_again.scalar(text("PRAGMA busy_timeout")) == 30_000
    finally:
        await blocker.close()
        await writer.aclose()
        await engine.dispose()
        await blocker_engine.dispose()


async def test_sqlite_subsecond_heartbeat_keeps_writer_lease_current(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'clock.db'}")
    store = SqlAlchemyTraceStore(
        engine,
        options=TraceStoreOptions(
            writer_lease_seconds=0.4,
            writer_heartbeat_interval_seconds=0.1,
        ),
    )
    writer = await store.open_writer(_identity())
    try:
        await asyncio.sleep(1.2)
        committed = await writer.append((_fact("started"),))
        assert committed[0].trace_seq == 1
    finally:
        await writer.aclose()
        await engine.dispose()


async def test_sqlite_unknown_heartbeat_commit_renews_expired_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'renew.db'}")
    store = SqlAlchemyTraceStore(
        engine,
        options=TraceStoreOptions(
            writer_lease_seconds=0.2,
            writer_heartbeat_interval_seconds=0.1,
            commit_retry_attempts=2,
            commit_retry_delay_seconds=0.3,
        ),
    )
    writer = await store.open_writer(_identity())
    backend = _backend(store)
    original = backend._raw_write_connection
    injected = asyncio.Event()
    remaining = 1

    @asynccontextmanager
    async def fail_after_commit() -> AsyncIterator[AsyncConnection]:
        nonlocal remaining
        async with original() as connection:
            yield connection
        if remaining:
            remaining -= 1
            injected.set()
            original_error = sqlite3.OperationalError("database is locked")
            original_error.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise DBAPIError(None, None, original_error, False)

    monkeypatch.setattr(backend, "_raw_write_connection", fail_after_commit)
    try:
        await asyncio.wait_for(injected.wait(), timeout=2)
        await asyncio.sleep(0.35)
        committed = await writer.append((_fact("started"),))
        assert committed[0].trace_seq == 1
    finally:
        monkeypatch.setattr(backend, "_raw_write_connection", original)
        await writer.aclose()
        await engine.dispose()


async def test_sqlite_zero_event_close_removes_checkpoint_rows(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'orphan.db'}")
    store = SqlAlchemyTraceStore(engine)
    writer = await store.open_writer(_identity())
    await store.save_projection_checkpoint(
        TraceProjectionCheckpoint(
            key=writer.key,
            projection_name="zero.checkpoint",
            run_id=None,
            as_of_seq=0,
            state={"count": 0},
        ),
        expected_as_of_seq=None,
    )
    try:
        await writer.aclose()
        async with engine.connect() as connection:
            checkpoint_count = await connection.scalar(
                text("SELECT COUNT(*) FROM tinkerfin_trace_projection_checkpoints")
            )
        assert checkpoint_count == 0
    finally:
        await writer.aclose()
        await engine.dispose()


async def test_sqlite_unknown_checkpoint_survives_peer_advancement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "checkpoint-advance.db"
    first_engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    peer_engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    options = TraceStoreOptions(
        commit_retry_attempts=2,
        commit_retry_delay_seconds=0.5,
    )
    first = SqlAlchemyTraceStore(
        first_engine,
        namespace="checkpoint-advance",
        options=options,
    )
    peer = SqlAlchemyTraceStore(
        peer_engine,
        namespace="checkpoint-advance",
        options=options,
    )
    writer = await first.open_writer(_identity())
    await writer.append((_fact("started"), _fact("input")))
    backend = _backend(first)
    original = backend._raw_write_connection
    first_commit_finished = asyncio.Event()
    remaining = 1

    @asynccontextmanager
    async def fail_after_commit() -> AsyncIterator[AsyncConnection]:
        nonlocal remaining
        async with original() as connection:
            yield connection
        if remaining:
            remaining -= 1
            first_commit_finished.set()
            original_error = sqlite3.OperationalError("database is locked")
            original_error.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise DBAPIError(None, None, original_error, False)

    first_checkpoint = TraceProjectionCheckpoint(
        key=writer.key,
        projection_name="advancing.checkpoint",
        run_id=None,
        as_of_seq=1,
        state={"count": 1},
    )
    second_checkpoint = first_checkpoint.model_copy(
        update={"as_of_seq": 2, "state": {"count": 2}}
    )
    monkeypatch.setattr(backend, "_raw_write_connection", fail_after_commit)
    saving = asyncio.create_task(
        first.save_projection_checkpoint(
            first_checkpoint,
            expected_as_of_seq=None,
        )
    )
    try:
        await first_commit_finished.wait()
        assert (
            await peer.save_projection_checkpoint(
                second_checkpoint,
                expected_as_of_seq=1,
            )
            == second_checkpoint
        )
        assert await saving == first_checkpoint
    finally:
        monkeypatch.setattr(backend, "_raw_write_connection", original)
        if not saving.done():
            saving.cancel()
            await asyncio.gather(saving, return_exceptions=True)
        await writer.aclose()
        await first_engine.dispose()
        await peer_engine.dispose()


async def test_sqlite_rejects_historical_checkpoint_as_new_cas_retry(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'historical.db'}")
    store = SqlAlchemyTraceStore(engine)
    writer = await store.open_writer(_identity())
    await writer.append((_fact("started"), _fact("input")))
    first = TraceProjectionCheckpoint(
        key=writer.key,
        projection_name="historical.checkpoint",
        run_id=None,
        as_of_seq=1,
        state={"count": 1},
    )
    second = first.model_copy(update={"as_of_seq": 2, "state": {"count": 2}})
    try:
        await store.save_projection_checkpoint(first, expected_as_of_seq=None)
        await store.save_projection_checkpoint(second, expected_as_of_seq=1)
        with pytest.raises(TraceProjectionCheckpointConflict):
            await store.save_projection_checkpoint(first, expected_as_of_seq=None)
    finally:
        await writer.aclose()
        await engine.dispose()
