"""Fixed-as-of, head selection, follow, and projected status contracts."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from tinkerfin_contracts import (
    ModelCallObservation,
    NativeInterruptRecord,
    NativeMessageObservation,
    NativeMessageRecord,
    NativeReasoningObservation,
    NativeStateObservation,
    NativeToolCall,
    ObservationBoundary,
    RunClosedObservation,
    RunIdentity,
    RunInputKind,
    RunInputObservation,
    RunObservationSession,
    RunResumeSummary,
    RunSourceContext,
    RunStartedObservation,
    RunTerminalObservation,
    RunTerminalOutcome,
)
from tinkerfin_tracing import (
    AmbiguousTraceHead,
    InMemoryTraceStore,
    InvalidTraceCursor,
    RunFact,
    StateRevisionFact,
    TraceProjectionCheckpoint,
    TraceQuotaExceeded,
    Tracer,
    TraceRunNotFound,
    TraceThreadNotFound,
    TracingErrorCode,
    TurnFact,
)
from tinkerfin_tracing.graph import (
    TraceGraphFilter,
    TraceGraphNodeKind,
    TraceGraphNodeStatus,
    TraceGraphQueryLimits,
)
from tinkerfin_tracing.store import (
    TraceGraphNodeRecordPage,
    TraceThreadKey,
    TraceWriter,
)


class _RacingGraphStore(InMemoryTraceStore):
    """Commit one child branch inside the first indexed Graph read."""

    child_writer: TraceWriter | None = None

    async def query_trace_graph(
        self,
        key: TraceThreadKey,
        *,
        run_ids: tuple[str, ...],
        where: TraceGraphFilter,
        limit: int,
        max_nodes: int = 4000,
        before_started_at: datetime | None = None,
        before_node_id: str | None = None,
    ) -> TraceGraphNodeRecordPage:
        writer = self.child_writer
        if writer is not None:
            self.child_writer = None
            now = datetime.now(UTC)
            identity = RunIdentity(threadId="thread-query", runId="race-child")
            await writer.append(
                (
                    RunFact(
                        source_observation_id="race-child-start",
                        identity=identity,
                        occurred_at=now,
                        monotonic_ns=1,
                        phase="started",
                        input_kind="branch",
                        parent_run_id="race-parent",
                    ),
                    TurnFact(
                        source_observation_id="race-child-turn",
                        identity=identity,
                        occurred_at=now,
                        monotonic_ns=2,
                        turn_id="turn-race-child",
                        user_message_id="human-race-child",
                        parent_run_id="race-parent",
                    ),
                )
            )
            await writer.append(
                (
                    RunFact(
                        source_observation_id="race-child-terminal",
                        identity=identity,
                        occurred_at=now,
                        monotonic_ns=3,
                        phase="terminal",
                        outcome="succeeded",
                    ),
                    RunFact(
                        source_observation_id="race-child-closed",
                        identity=identity,
                        occurred_at=now,
                        monotonic_ns=4,
                        phase="closed",
                        outcome="succeeded",
                    ),
                ),
                mandatory=True,
            )
            await writer.aclose()
        return await super().query_trace_graph(
            key,
            run_ids=run_ids,
            where=where,
            limit=limit,
            max_nodes=max_nodes,
            before_started_at=before_started_at,
            before_node_id=before_node_id,
        )


def _context(
    run_id: str,
    *,
    input_kind: RunInputKind = "ordinary",
    parent_run_id: str | None = None,
    resume: tuple[RunResumeSummary, ...] = (),
) -> RunSourceContext:
    return RunSourceContext(
        identity=RunIdentity(threadId="thread-query", runId=run_id),
        runtime_profile="deepagents-v2",
        input_kind=input_kind,
        parent_run_id=parent_run_id,
        input={
            "messages": [
                {
                    "role": "user",
                    "id": f"user-{run_id}",
                    "content": f"request {run_id}",
                }
            ]
        },
        config={},
        resume=resume,
    )


async def _start(
    tracer: Tracer,
    context: RunSourceContext,
) -> RunObservationSession:
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
    return session


async def _finish(
    session: RunObservationSession,
    context: RunSourceContext,
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


async def _record(
    tracer: Tracer,
    run_id: str,
    *,
    input_kind: RunInputKind = "ordinary",
    parent_run_id: str | None = None,
) -> None:
    context = _context(
        run_id,
        input_kind=input_kind,
        parent_run_id=parent_run_id,
    )
    session = await _start(tracer, context)
    await _finish(session, context)


async def _record_large_model_call(tracer: Tracer, run_id: str) -> None:
    context = _context(run_id)
    session = await _start(tracer, context)
    now = datetime.now(UTC)
    await session.observe(
        ModelCallObservation(
            identity=context.identity,
            phase="started",
            call_id=f"model-{run_id}",
            model="large-model",
            messages=(
                NativeMessageRecord(
                    message_type="system",
                    content="s" * 12_000,
                ),
                NativeMessageRecord(message_type="human", content="run"),
            ),
            observed_at=now,
            monotonic_ns=3,
        )
    )
    await session.observe(
        ModelCallObservation(
            identity=context.identity,
            phase="completed",
            call_id=f"model-{run_id}",
            model="large-model",
            output_message_ids=(f"assistant-{run_id}",),
            observed_at=now,
            monotonic_ns=4,
        )
    )
    await _finish(session, context)


async def test_graph_page_prefers_structure_and_marks_omitted_details() -> None:
    limits = TraceGraphQueryLimits(max_page_bytes=2048)
    tracer = Tracer(graph_query_limits=limits)
    await _record_large_model_call(tracer, "bounded-page")

    query = await tracer.query(
        "thread-query",
        where=TraceGraphFilter(
            kinds={TraceGraphNodeKind.MODEL},
            include_ancestor_nodes=False,
        ),
    )

    assert len(query.snapshot.model_dump_json(by_alias=True).encode()) <= 2048
    assert query.completeness.details_omitted is True
    assert query.nodes[0].request is None
    assert query.nodes[0].request_omitted is True


async def test_graph_ancestor_expansion_obeys_the_independent_total_limit() -> None:
    tracer = Tracer(
        graph_query_limits=TraceGraphQueryLimits(
            max_direct_nodes=1,
            max_total_nodes=1,
        )
    )
    await _record_large_model_call(tracer, "bounded-ancestors")

    with pytest.raises(TraceQuotaExceeded) as captured:
        await tracer.query(
            "thread-query",
            where=TraceGraphFilter(
                kinds={TraceGraphNodeKind.ASSISTANT_MESSAGE},
                include_ancestor_nodes=True,
            ),
            limit=1,
        )
    assert captured.value.context["resource"] == "graph_total_nodes"


async def test_graph_follow_applies_the_same_page_byte_budget() -> None:
    tracer = Tracer(graph_query_limits=TraceGraphQueryLimits(max_page_bytes=2048))
    context = _context("bounded-follow")
    session = await _start(tracer, context)
    query = await tracer.query(
        "thread-query",
        where=TraceGraphFilter(
            kinds={TraceGraphNodeKind.ASSISTANT_MESSAGE},
            include_ancestor_nodes=False,
        ),
    )
    updates = query.follow()
    pending = asyncio.create_task(anext(updates))
    now = datetime.now(UTC)
    await session.observe(
        NativeMessageObservation(
            identity=context.identity,
            namespace=(),
            message=NativeMessageRecord(
                message_type="assistant",
                id="assistant-bounded-follow",
                content="a" * 12_000,
            ),
            observed_at=now,
            monotonic_ns=3,
        )
    )
    await session.force(ObservationBoundary.CALL_STARTED)

    update = await asyncio.wait_for(pending, timeout=2)
    assert len(update.model_dump_json(by_alias=True).encode()) <= 2048
    assert update.completeness.details_omitted is True
    assert update.node_upserts[0].content is None
    assert update.node_upserts[0].content_omitted is True
    await updates.aclose()
    await _finish(session, context)


async def test_graph_cursor_is_fixed_to_filter_head_and_current_tail() -> None:
    tracer = Tracer()
    context = _context("graph-cursor")
    session = await _start(tracer, context)
    now = datetime.now(UTC)
    for index in (1, 2):
        await session.observe(
            ModelCallObservation(
                identity=context.identity,
                phase="started",
                call_id=f"model-cursor-{index}",
                model="cursor-model",
                messages=(NativeMessageRecord(message_type="human", content="cursor"),),
                observed_at=now,
                monotonic_ns=2 + index * 2,
            )
        )
        await session.observe(
            ModelCallObservation(
                identity=context.identity,
                phase="completed",
                call_id=f"model-cursor-{index}",
                model="cursor-model",
                observed_at=now,
                monotonic_ns=3 + index * 2,
            )
        )
    await session.force(ObservationBoundary.CALL_STARTED)
    where = TraceGraphFilter(
        kinds={TraceGraphNodeKind.MODEL},
        include_ancestor_nodes=False,
    )
    first = await tracer.query("thread-query", where=where, limit=1)
    assert first.next_cursor is not None
    second = await tracer.query(
        "thread-query",
        where=where,
        cursor=first.next_cursor,
        limit=1,
    )
    assert first.nodes[0].id != second.nodes[0].id
    assert {
        first.nodes[0].id.rsplit(":", 1)[-1],
        second.nodes[0].id.rsplit(":", 1)[-1],
    } == {"model-cursor-1", "model-cursor-2"}
    with pytest.raises(ValueError, match="current first page"):
        second.follow()

    updates = first.follow()
    pending = asyncio.create_task(anext(updates))
    await session.observe(
        NativeStateObservation(
            identity=context.identity,
            namespace=(),
            state={"cursor": "advanced-without-a-matching-node"},
            observed_at=now,
            monotonic_ns=8,
        )
    )
    await session.force(ObservationBoundary.CALL_STARTED)
    update = await asyncio.wait_for(pending, timeout=2)
    assert update.next_cursor is not None
    assert update.next_cursor != first.next_cursor
    await updates.aclose()
    with pytest.raises(InvalidTraceCursor):
        await tracer.query(
            "thread-query",
            where=where,
            cursor=first.next_cursor,
            limit=1,
        )
    await _finish(session, context)


async def test_graph_query_reselects_the_lineage_when_tail_advances_mid_read() -> None:
    store = _RacingGraphStore()
    tracer = Tracer(store=store)
    await _record(tracer, "race-parent")
    store.child_writer = await store.open_writer(
        RunIdentity(threadId="thread-query", runId="race-child")
    )

    query = await tracer.query(
        "thread-query",
        head_run_id="race-parent",
        where=TraceGraphFilter(include_technical_nodes=True),
        limit=100,
    )

    assert query.as_of_seq == (await store.snapshot("thread-query")).as_of_seq
    assert {node.run_id for node in query.nodes} == {"race-parent", "race-child"}
    assert len(query.turns) == 2
    assert "turn-race-child" in {turn.id for turn in query.turns}


async def test_turn_and_run_status_follow_the_latest_segment_terminal() -> None:
    tracer = Tracer()
    context = _context("status")
    session = await _start(tracer, context)

    active = await tracer.get("thread-query")
    assert active.status.execution == "running"
    assert next(node for node in active.graph.nodes if node.kind == "run").status == (
        "running"
    )

    await _finish(session, context)
    finished = await tracer.get("thread-query")
    finished_run = next(node for node in finished.graph.nodes if node.kind == "run")
    assert finished_run.status == "succeeded"
    assert finished_run.completed_at is not None
    assert next(node for node in finished.graph.nodes if node.kind == "run").status == (
        "succeeded"
    )


async def test_committed_terminal_precedes_the_writer_cleanup_fence() -> None:
    tracer = Tracer()
    context = _context("terminal-before-close")
    session = await _start(tracer, context)
    now = datetime.now(UTC)
    try:
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

        committed = await tracer.get("thread-query")

        assert committed.status.execution == "succeeded"
        assert committed.status.execution == "succeeded"
        assert (
            next(node for node in committed.graph.nodes if node.kind == "run").status
            == "succeeded"
        )
    finally:
        await session.aclose()


async def test_tool_end_does_not_claim_execution_success_before_a_result() -> None:
    tracer = Tracer()
    context = _context("tool-status")
    session = await _start(tracer, context)
    await session.observe(
        NativeMessageObservation(
            identity=context.identity,
            namespace=(),
            message=NativeMessageRecord(
                message_type="assistant",
                id="assistant-tool",
                content="",
                tool_calls=(
                    NativeToolCall(id="call-tool", name="search", arguments={}),
                ),
            ),
            observed_at=datetime.now(UTC),
            monotonic_ns=3,
        )
    )

    proposed = await tracer.get("thread-query")
    tool = next(node for node in proposed.graph.nodes if node.kind == "tool")
    assert tool.status == "waiting"
    assert tool.completed_at is None

    await session.observe(
        NativeMessageObservation(
            identity=context.identity,
            namespace=(),
            message=NativeMessageRecord(
                message_type="tool",
                id="tool-result",
                name="search",
                content="done",
                tool_call_id="call-tool",
                tool_status="success",
            ),
            observed_at=datetime.now(UTC),
            monotonic_ns=4,
        )
    )
    completed = await tracer.get("thread-query")
    tool = next(node for node in completed.graph.nodes if node.kind == "tool")
    assert tool.status == "succeeded"
    assert tool.completed_at is not None
    await _finish(session, context)


async def test_explicit_branch_follow_advances_to_its_only_descendant_head() -> None:
    tracer = Tracer()
    await _record(tracer, "root")
    await _record(tracer, "branch-a", input_kind="branch", parent_run_id="root")
    await _record(tracer, "branch-b", input_kind="branch", parent_run_id="root")
    branch = await tracer.get("thread-query", head_run_id="branch-a")
    follower = branch.follow()

    resumed = _context(
        "resume-a",
        input_kind="resume",
        parent_run_id="branch-a",
    )
    session = await tracer.open_run(resumed)
    now = datetime.now(UTC)
    first_update = asyncio.ensure_future(anext(follower))
    await session.observe(
        RunStartedObservation(
            identity=resumed.identity,
            observed_at=now,
            monotonic_ns=1,
        )
    )
    await session.observe(
        RunInputObservation(
            identity=resumed.identity,
            source=resumed,
            observed_at=now,
            monotonic_ns=2,
        )
    )

    update = await asyncio.wait_for(first_update, timeout=2)
    assert update.status.head_run_id == "resume-a"
    assert update.summary.status == update.status
    assert update.summary.message_count == update.message_count
    serialized = update.model_dump(mode="json", by_alias=True)
    assert serialized["summary"]["status"] == serialized["status"]
    assert serialized["summary"]["messageCount"] == serialized["messageCount"]
    assert "status" not in type(update).model_fields
    await _finish(session, resumed)
    await follower.aclose()
    await asyncio.sleep(0)
    assert all(
        getattr(task.get_coro(), "__qualname__", "") != "async_generator_athrow"
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task() and not task.done()
    )


async def test_event_pages_include_only_the_selected_head_lineage() -> None:
    tracer = Tracer()
    await _record(tracer, "root")
    await _record(tracer, "branch-a", input_kind="branch", parent_run_id="root")
    await _record(tracer, "branch-b", input_kind="branch", parent_run_id="root")

    branch = await tracer.get("thread-query", head_run_id="branch-a")
    page = await branch.events(limit=100)

    assert {event.fact.identity.run_id for event in page.items} == {
        "root",
        "branch-a",
    }


async def test_tree_includes_only_the_selected_run_lineage_within_one_turn() -> None:
    tracer = Tracer()
    await _record(tracer, "root")
    await _record(
        tracer,
        "resume-a",
        input_kind="resume",
        parent_run_id="root",
    )
    await _record(
        tracer,
        "resume-b",
        input_kind="resume",
        parent_run_id="root",
    )

    selected = await tracer.get("thread-query", head_run_id="resume-a")
    run_nodes = tuple(node for node in selected.graph.nodes if node.kind == "run")

    assert {node.run_id for node in run_nodes} == {"root", "resume-a"}
    assert {turn.id for turn in selected.graph.turns} == {run_nodes[0].turn_id}


async def test_missing_prefix_is_scoped_to_the_selected_head_lineage() -> None:
    tracer = Tracer()
    await _record(tracer, "root")
    await _record(
        tracer,
        "valid",
        input_kind="branch",
        parent_run_id="root",
    )
    await _record(
        tracer,
        "orphan",
        input_kind="resume",
        parent_run_id="missing-parent",
    )

    valid = await tracer.get("thread-query", head_run_id="valid")
    orphan = await tracer.get("thread-query", head_run_id="orphan")

    assert valid.completeness.missing_prefix is False
    assert orphan.completeness.missing_prefix is True


@pytest.mark.parametrize("input_kind", ["resume", "abandon"])
async def test_implicit_continuation_keeps_the_sole_completed_parent_lineage(
    input_kind: RunInputKind,
) -> None:
    tracer = Tracer()
    parent = _context("implicit-parent")
    parent_session = await _start(tracer, parent)
    await _finish(parent_session, parent, outcome="interrupted")
    continuation = _context("implicit-continuation", input_kind=input_kind)
    continuation_session = await _start(tracer, continuation)
    await _finish(
        continuation_session,
        continuation,
        outcome="abandoned" if input_kind == "abandon" else "succeeded",
    )

    thread = await tracer.get("thread-query")
    run_nodes = tuple(node for node in thread.graph.nodes if node.kind == "run")

    assert thread.head_run_id == "implicit-continuation"
    assert thread.completeness.missing_prefix is False
    assert len(thread.graph.turns) == 1
    assert {node.run_id for node in run_nodes} == {
        "implicit-parent",
        "implicit-continuation",
    }


@pytest.mark.parametrize("input_kind", ["resume", "abandon"])
async def test_continuation_without_any_parent_evidence_remains_partial(
    input_kind: RunInputKind,
) -> None:
    tracer = Tracer()
    context = _context("missing-predecessor", input_kind=input_kind)
    session = await _start(tracer, context)
    await _finish(
        session,
        context,
        outcome="abandoned" if input_kind == "abandon" else "succeeded",
    )

    thread = await tracer.get("thread-query")

    assert thread.completeness.missing_prefix is True
    assert thread.graph.turns[0].id.startswith("turn-partial:")


async def test_implicit_resume_with_multiple_completed_heads_remains_partial() -> None:
    tracer = Tracer()
    await _record(tracer, "ambiguous-root")
    await _record(
        tracer,
        "ambiguous-a",
        input_kind="branch",
        parent_run_id="ambiguous-root",
    )
    await _record(
        tracer,
        "ambiguous-b",
        input_kind="branch",
        parent_run_id="ambiguous-root",
    )
    await _record(tracer, "ambiguous-resume", input_kind="resume")

    thread = await tracer.get("thread-query", head_run_id="ambiguous-resume")

    assert thread.completeness.missing_prefix is True
    assert thread.graph.turns[0].id.startswith("turn-partial:")


async def test_implicit_resume_does_not_inherit_one_unterminated_head() -> None:
    tracer = Tracer()
    active = _context("active-parent")
    active_session = await _start(tracer, active)
    continuation = _context("active-resume", input_kind="resume")
    continuation_session = await _start(tracer, continuation)
    try:
        await _finish(continuation_session, continuation)
        thread = await tracer.get("thread-query", head_run_id="active-resume")

        assert thread.completeness.missing_prefix is True
        assert thread.graph.turns[0].id.startswith("turn-partial:")
    finally:
        await _finish(active_session, active)


@pytest.mark.parametrize("input_kind", ["resume", "abandon"])
async def test_explicit_continuation_parent_stays_complete(
    input_kind: RunInputKind,
) -> None:
    tracer = Tracer()
    await _record(tracer, "explicit-parent")
    await _record(
        tracer,
        "explicit-continuation",
        input_kind=input_kind,
        parent_run_id="explicit-parent",
    )

    thread = await tracer.get("thread-query")

    assert thread.completeness.missing_prefix is False
    assert len(thread.graph.turns) == 1


async def test_follow_skips_commits_from_an_unselected_sibling_branch() -> None:
    tracer = Tracer()
    await _record(tracer, "root")
    await _record(tracer, "branch-a", input_kind="branch", parent_run_id="root")
    await _record(tracer, "branch-b", input_kind="branch", parent_run_id="root")
    branch = await tracer.get("thread-query", head_run_id="branch-a")
    follower = branch.follow()
    waiting = asyncio.ensure_future(anext(follower))

    sibling = _context(
        "resume-b",
        input_kind="resume",
        parent_run_id="branch-b",
    )
    sibling_session = await _start(tracer, sibling)
    await asyncio.sleep(0)
    assert waiting.done() is False

    selected = _context(
        "resume-a",
        input_kind="resume",
        parent_run_id="branch-a",
    )
    selected_session = await _start(tracer, selected)
    update = await asyncio.wait_for(waiting, timeout=2)
    assert {fact.identity.run_id for fact in update.facts} == {"resume-a"}

    await _finish(sibling_session, sibling)
    await _finish(selected_session, selected)
    await follower.aclose()


async def test_cursor_from_a_newer_as_of_is_rejected_by_an_older_handle() -> None:
    tracer = Tracer()
    context = _context("cursor")
    session = await _start(tracer, context)
    older = await tracer.get("thread-query")
    await session.observe(
        NativeStateObservation(
            identity=context.identity,
            namespace=(),
            state={"value": "new"},
            observed_at=datetime.now(UTC),
            monotonic_ns=3,
        )
    )
    newer = await tracer.get("thread-query")
    newer_page = await newer.events(limit=1)
    assert newer_page.next_cursor is not None

    with pytest.raises(InvalidTraceCursor):
        await older.events(cursor=newer_page.next_cursor, limit=100)
    await _finish(session, context)


async def test_inactive_unterminated_head_is_marked_as_missing_tail() -> None:
    tracer = Tracer()
    context = _context("missing-tail")
    session = await _start(tracer, context)
    await session.aclose()

    thread = await tracer.get("thread-query")

    assert thread.status.execution == "unknown"
    assert thread.completeness.missing_tail is True


async def test_nested_subgraph_nodes_do_not_invent_missing_parent_evidence() -> None:
    tracer = Tracer()
    context = _context("nested")
    session = await _start(tracer, context)
    now = datetime.now(UTC)
    await session.observe(
        NativeMessageObservation(
            identity=context.identity,
            namespace=("tools:outer",),
            message=NativeMessageRecord(
                message_type="assistant_chunk",
                id="outer-message",
                content="outer",
            ),
            metadata={"lc_agent_name": "outer"},
            observed_at=now,
            monotonic_ns=3,
        )
    )
    await session.observe(
        NativeMessageObservation(
            identity=context.identity,
            namespace=("tools:outer", "tools:inner"),
            message=NativeMessageRecord(
                message_type="assistant",
                id="inner-message",
                content="",
                tool_calls=(
                    NativeToolCall(id="inner-tool", name="search", arguments={}),
                ),
            ),
            metadata={"lc_agent_name": "inner"},
            observed_at=now,
            monotonic_ns=4,
        )
    )

    thread = await tracer.get("thread-query")
    outer = next(node for node in thread.graph.nodes if node.name == "outer")
    inner = next(node for node in thread.graph.nodes if node.name == "inner")
    tool = next(node for node in thread.graph.nodes if node.kind == "tool")
    human = next(node for node in thread.graph.nodes if node.kind == "human_message")

    assert outer.parent_id == human.id
    assert inner.parent_id == human.id
    assert tool.parent_id == human.id
    assert outer.link_issues
    assert inner.link_issues
    await _finish(session, context)


@pytest.mark.parametrize(
    ("outcome", "expected"),
    (
        ("succeeded", "succeeded"),
        ("interrupted", "waiting"),
        ("failed", "failed"),
        ("cancelled", "cancelled"),
        ("abandoned", "abandoned"),
    ),
)
async def test_all_runtime_terminals_have_distinct_execution_status(
    outcome: RunTerminalOutcome,
    expected: str,
) -> None:
    tracer = Tracer()
    context = _context(f"terminal-{outcome}")
    session = await _start(tracer, context)

    await _finish(session, context, outcome=outcome)
    thread = await tracer.get("thread-query")

    assert thread.status.execution == expected


async def test_pending_interaction_keeps_active_and_success_terminal_waiting() -> None:
    tracer = Tracer()
    context = _context("pending-success")
    session = await _start(tracer, context)
    await session.observe(
        NativeStateObservation(
            identity=context.identity,
            namespace=(),
            state={},
            interrupts=(
                NativeInterruptRecord(
                    id="pending-success-interrupt",
                    value={"kind": "input_required", "message": "Wait"},
                ),
            ),
            observed_at=datetime.now(UTC),
            monotonic_ns=3,
        )
    )

    active = await tracer.get("thread-query", head_run_id=context.identity.run_id)
    assert active.status.execution == "running"
    assert [item.source_id for item in active.summary.pending_interactions] == [
        "pending-success-interrupt"
    ]

    await _finish(session, context, outcome="succeeded")
    completed = await tracer.get(
        "thread-query",
        head_run_id=context.identity.run_id,
    )
    assert completed.status.execution == "waiting"
    assert [item.source_id for item in completed.summary.pending_interactions] == [
        "pending-success-interrupt"
    ]


async def test_failed_terminal_remains_failed_with_a_pending_interaction() -> None:
    tracer = Tracer()
    context = _context("pending-failure")
    session = await _start(tracer, context)
    await session.observe(
        NativeStateObservation(
            identity=context.identity,
            namespace=(),
            state={},
            interrupts=(
                NativeInterruptRecord(
                    id="pending-failure-interrupt",
                    value={"kind": "input_required", "message": "Wait"},
                ),
            ),
            observed_at=datetime.now(UTC),
            monotonic_ns=3,
        )
    )

    await _finish(session, context, outcome="failed")
    thread = await tracer.get("thread-query", head_run_id=context.identity.run_id)

    assert thread.status.execution == "failed"
    assert [item.source_id for item in thread.summary.pending_interactions] == [
        "pending-failure-interrupt"
    ]


async def test_resume_with_a_missing_parent_builds_an_explicit_partial_turn() -> None:
    tracer = Tracer()
    context = _context(
        "partial",
        input_kind="resume",
        parent_run_id="missing-parent",
    )
    session = await _start(tracer, context)
    await _finish(session, context)

    thread = await tracer.get("thread-query")

    assert thread.completeness.missing_prefix is True
    assert thread.graph.turns[0].id.startswith("turn-partial:")


async def test_default_window_contains_exactly_the_latest_one_hundred_turns() -> None:
    tracer = Tracer()
    for index in range(105):
        await _record(tracer, f"run-{index:03d}")

    thread = await tracer.get("thread-query")

    assert len(thread.messages) == 100
    assert len(thread.graph.turns) == 100
    assert thread.messages[0].source_id == "user-run-005"
    assert thread.messages[-1].source_id == "user-run-104"
    assert thread.graph.turns[-1].id.endswith(":run-104")
    assert thread.has_older is True
    assert thread.message_count == 105
    assert thread.tool_call_count == 0
    summary_before = thread.summary
    await thread.load_older(limit=5)
    assert len(thread.messages) == 105
    assert len(thread.graph.turns) == 105
    assert thread.has_older is False
    assert thread.message_count == 105
    assert thread.summary == summary_before


async def test_summary_keeps_pending_interactions_outside_the_visible_window() -> None:
    tracer = Tracer()
    context = _context("pending-first")
    session = await _start(tracer, context)
    await session.observe(
        NativeStateObservation(
            identity=context.identity,
            namespace=(),
            state={"waiting": True},
            interrupts=(
                NativeInterruptRecord(
                    id="pending-old",
                    value={"kind": "input_required", "message": "Wait"},
                ),
            ),
            observed_at=datetime.now(UTC),
            monotonic_ns=3,
        )
    )
    await _finish(session, context, outcome="interrupted")
    for index in range(104):
        await _record(tracer, f"later-{index:03d}")

    thread = await tracer.get("thread-query")

    assert thread.has_older is True
    assert all(item.source_id != "pending-old" for item in thread.interactions)
    assert [item.source_id for item in thread.summary.pending_interactions] == [
        "pending-old"
    ]


async def test_summary_uses_the_selected_lineage_maximum_source_time() -> None:
    tracer = Tracer()
    context = _context("summary-time")
    session = await _start(tracer, context)
    future_time = datetime(2035, 1, 2, 3, 4, tzinfo=UTC)
    await session.observe(
        NativeStateObservation(
            identity=context.identity,
            namespace=(),
            state={"tinkerfin_plan": {"status": "draft"}},
            observed_at=future_time,
            monotonic_ns=3,
        )
    )
    await _finish(session, context)

    thread = await tracer.get("thread-query")
    events = (await thread.events(limit=100)).items

    assert thread.summary.last_occurred_at == future_time
    assert thread.summary.last_occurred_at == max(
        event.fact.occurred_at for event in events
    )


async def test_summary_pending_interactions_exclude_sibling_branches() -> None:
    tracer = Tracer()
    await _record(tracer, "summary-root")
    for run_id, interrupt_id in (
        ("summary-branch-a", "interrupt-a"),
        ("summary-branch-b", "interrupt-b"),
    ):
        context = _context(
            run_id,
            input_kind="branch",
            parent_run_id="summary-root",
        )
        session = await _start(tracer, context)
        await session.observe(
            NativeStateObservation(
                identity=context.identity,
                namespace=(),
                state={"branch": run_id},
                interrupts=(
                    NativeInterruptRecord(
                        id=interrupt_id,
                        value={"kind": "input_required", "message": run_id},
                    ),
                ),
                observed_at=datetime.now(UTC),
                monotonic_ns=3,
            )
        )
        await _finish(session, context, outcome="interrupted")

    branch_a = await tracer.get("thread-query", head_run_id="summary-branch-a")
    branch_b = await tracer.get("thread-query", head_run_id="summary-branch-b")

    assert [item.source_id for item in branch_a.summary.pending_interactions] == [
        "interrupt-a"
    ]
    assert [item.source_id for item in branch_b.summary.pending_interactions] == [
        "interrupt-b"
    ]


async def test_same_turn_sibling_resumes_isolate_selected_lineage_views() -> None:
    tracer = Tracer()
    await _record(tracer, "summary-root")
    for run_id, interrupt_id in (
        ("summary-resume-a", "interrupt-a"),
        ("summary-resume-b", "interrupt-b"),
    ):
        context = _context(
            run_id,
            input_kind="resume",
            parent_run_id="summary-root",
        )
        session = await _start(tracer, context)
        now = datetime.now(UTC)
        message_id = f"assistant-{run_id}"
        await session.observe(
            NativeMessageObservation(
                identity=context.identity,
                namespace=(),
                message=NativeMessageRecord(
                    message_type="assistant",
                    id=message_id,
                    content=run_id,
                    tool_calls=(
                        NativeToolCall(
                            id=f"tool-{run_id}",
                            name="search",
                            arguments={},
                        ),
                    ),
                ),
                observed_at=now,
                monotonic_ns=3,
            )
        )
        await session.observe(
            NativeReasoningObservation(
                identity=context.identity,
                namespace=(),
                message_id=message_id,
                extractor="fixture.reasoning",
                content=f"reasoning-{run_id}",
                snapshot=True,
                observed_at=now,
                monotonic_ns=4,
            )
        )
        await session.observe(
            NativeStateObservation(
                identity=context.identity,
                namespace=(),
                state={"resume": run_id},
                interrupts=(
                    NativeInterruptRecord(
                        id=interrupt_id,
                        value={"kind": "input_required", "message": run_id},
                    ),
                ),
                observed_at=now,
                monotonic_ns=5,
            )
        )
        await _finish(session, context, outcome="interrupted")

    for head, expected_interrupt in (
        ("summary-resume-a", "interrupt-a"),
        ("summary-resume-b", "interrupt-b"),
    ):
        trace = await tracer.get("thread-query", head_run_id=head)
        selected_runs = {"summary-root", head}

        assert trace.summary.message_count == 2
        assert trace.summary.tool_call_count == 1
        assert {message.run_id for message in trace.messages} == selected_runs
        assert {item.run_id for item in trace.reasoning} == {head}
        assert {item.run_id for item in trace.interactions} == {head}
        assert [item.source_id for item in trace.summary.pending_interactions] == [
            expected_interrupt
        ]
        assert all(node.run_id in selected_runs for node in trace.graph.nodes)


async def test_sibling_message_removal_does_not_mutate_another_head() -> None:
    tracer = Tracer()
    root = _context("remove-root")
    root_session = await _start(tracer, root)
    await root_session.observe(
        NativeMessageObservation(
            identity=root.identity,
            namespace=(),
            message=NativeMessageRecord(
                message_type="assistant",
                id="shared-assistant",
                content="root answer",
            ),
            observed_at=datetime.now(UTC),
            monotonic_ns=3,
        )
    )
    await _finish(root_session, root)

    branch_a = _context(
        "remove-a",
        input_kind="resume",
        parent_run_id="remove-root",
    )
    branch_a_session = await _start(tracer, branch_a)
    await _finish(branch_a_session, branch_a)

    branch_b = _context(
        "remove-b",
        input_kind="resume",
        parent_run_id="remove-root",
    )
    branch_b_session = await _start(tracer, branch_b)
    await branch_b_session.observe(
        NativeMessageObservation(
            identity=branch_b.identity,
            namespace=(),
            message=NativeMessageRecord(
                message_type="remove",
                id="shared-assistant",
                content="",
            ),
            observed_at=datetime.now(UTC),
            monotonic_ns=3,
        )
    )
    await _finish(branch_b_session, branch_b)

    selected_a = await tracer.get("thread-query", head_run_id="remove-a")
    selected_b = await tracer.get("thread-query", head_run_id="remove-b")

    assert [message.source_id for message in selected_a.messages] == [
        "user-remove-root",
        "shared-assistant",
    ]
    assert selected_a.summary.message_count == 2
    assert [message.source_id for message in selected_b.messages] == [
        "user-remove-root"
    ]
    assert selected_b.summary.message_count == 1


async def test_sibling_reconciliation_result_and_resolution_are_isolated() -> None:
    tracer = Tracer()
    root = _context("transition-root")
    root_session = await _start(tracer, root)
    await root_session.observe(
        NativeMessageObservation(
            identity=root.identity,
            namespace=(),
            message=NativeMessageRecord(
                message_type="assistant",
                id="transition-assistant",
                content="root answer",
                tool_calls=(
                    NativeToolCall(
                        id="transition-tool",
                        name="search",
                        arguments={},
                    ),
                ),
            ),
            observed_at=datetime.now(UTC),
            monotonic_ns=3,
        )
    )
    await root_session.observe(
        NativeStateObservation(
            identity=root.identity,
            namespace=(),
            state={},
            interrupts=(
                NativeInterruptRecord(
                    id="transition-interrupt",
                    value={"kind": "input_required", "message": "Continue?"},
                ),
                NativeInterruptRecord(
                    id="transition-other",
                    value={"kind": "input_required", "message": "Keep waiting?"},
                ),
            ),
            observed_at=datetime.now(UTC),
            monotonic_ns=4,
        )
    )
    await _finish(root_session, root, outcome="interrupted")

    branch_a = _context(
        "transition-a",
        input_kind="resume",
        parent_run_id="transition-root",
    )
    branch_a_session = await _start(tracer, branch_a)
    await _finish(branch_a_session, branch_a)

    branch_b = _context(
        "transition-b",
        input_kind="resume",
        parent_run_id="transition-root",
        resume=(
            RunResumeSummary(
                interrupt_id="transition-interrupt",
                status="resolved",
                decision="approve",
            ),
        ),
    )
    branch_b_session = await _start(tracer, branch_b)
    await branch_b_session.observe(
        NativeStateObservation(
            identity=branch_b.identity,
            namespace=(),
            state={},
            messages=(
                NativeMessageRecord(
                    message_type="human",
                    id="user-transition-root",
                    content="request transition-root",
                ),
                NativeMessageRecord(
                    message_type="assistant",
                    id="transition-assistant",
                    content="branch-b answer",
                    tool_calls=(
                        NativeToolCall(
                            id="transition-tool",
                            name="search",
                            arguments={},
                        ),
                    ),
                ),
            ),
            interrupts=(
                NativeInterruptRecord(
                    id="transition-other",
                    value={"kind": "input_required", "message": "Keep waiting?"},
                ),
            ),
            observed_at=datetime.now(UTC),
            monotonic_ns=3,
        )
    )
    await branch_b_session.observe(
        NativeMessageObservation(
            identity=branch_b.identity,
            namespace=(),
            message=NativeMessageRecord(
                message_type="tool",
                id="transition-result",
                name="search",
                content="branch-b result",
                tool_call_id="transition-tool",
                tool_status="success",
            ),
            observed_at=datetime.now(UTC),
            monotonic_ns=4,
        )
    )
    await _finish(branch_b_session, branch_b)

    selected_a = await tracer.get("thread-query", head_run_id="transition-a")
    selected_b = await tracer.get("thread-query", head_run_id="transition-b")
    assistant_a = next(
        message
        for message in selected_a.messages
        if message.source_id == "transition-assistant"
    )
    assistant_b = next(
        message
        for message in selected_b.messages
        if message.source_id == "transition-assistant"
    )

    assert assistant_a.content == "root answer"
    assert assistant_b.content == "branch-b answer"
    assert all(message.role != "tool" for message in selected_a.messages)
    assert any(
        message.role == "tool" and message.source_id == "transition-result"
        for message in selected_b.messages
    )
    assert [item.source_id for item in selected_a.summary.pending_interactions] == [
        "transition-interrupt",
        "transition-other",
    ]
    assert [item.source_id for item in selected_b.summary.pending_interactions] == [
        "transition-other"
    ]
    graph_a = await tracer.query("thread-query", head_run_id="transition-a")
    graph_b = await tracer.query("thread-query", head_run_id="transition-b")
    graph_assistant_a = next(
        node
        for node in graph_a.nodes
        if node.kind is TraceGraphNodeKind.ASSISTANT_MESSAGE
    )
    graph_assistant_b = next(
        node
        for node in graph_b.nodes
        if node.kind is TraceGraphNodeKind.ASSISTANT_MESSAGE
    )
    graph_tool_a = next(
        node for node in graph_a.nodes if node.kind is TraceGraphNodeKind.TOOL
    )
    graph_tool_b = next(
        node for node in graph_b.nodes if node.kind is TraceGraphNodeKind.TOOL
    )
    assert graph_assistant_a.content == "root answer"
    assert graph_assistant_b.content == "branch-b answer"
    assert graph_tool_a.status is not TraceGraphNodeStatus.SUCCEEDED
    assert graph_tool_b.status is TraceGraphNodeStatus.SUCCEEDED


async def test_core_summary_rebuild_uses_only_its_current_cache_scope() -> None:
    store = InMemoryTraceStore()
    tracer = Tracer(store=store)
    await _record(tracer, "summary-cache")
    snapshot = await store.snapshot("thread-query")
    await store.save_projection_checkpoint(
        TraceProjectionCheckpoint(
            key=snapshot.key,
            projection_name="tinkerfin.core",
            run_id=None,
            as_of_seq=snapshot.as_of_seq,
            state={"unrelated": True},
        ),
        expected_as_of_seq=None,
    )

    thread = await tracer.get("thread-query")
    current = await store.load_projection_checkpoint(
        snapshot.key,
        projection_name="tinkerfin.core.summary",
        run_id=None,
        as_of_seq=snapshot.as_of_seq,
    )

    assert thread.summary.status.execution == "succeeded"
    assert current is not None
    assert current.as_of_seq == snapshot.as_of_seq
    assert isinstance(current.state, dict)
    assert "runs" in current.state


async def test_ambiguous_head_error_exposes_every_selectable_head() -> None:
    tracer = Tracer()
    await _record(tracer, "root")
    await _record(tracer, "branch-a", input_kind="branch", parent_run_id="root")
    await _record(tracer, "branch-b", input_kind="branch", parent_run_id="root")

    with pytest.raises(AmbiguousTraceHead) as captured:
        await tracer.get("thread-query")

    assert captured.value.context["head_run_ids"] == "branch-a,branch-b"


async def test_missing_explicit_run_uses_precise_not_found_error() -> None:
    tracer = Tracer()
    await _record(tracer, "existing-run")

    with pytest.raises(TraceRunNotFound) as captured:
        await tracer.get("thread-query", head_run_id="not-started-run")

    assert captured.value.code is TracingErrorCode.RUN_NOT_FOUND
    assert captured.value.context == {"head_run_id": "not-started-run"}


async def test_concurrent_ordinary_runs_remain_independent_heads() -> None:
    tracer = Tracer()
    first_context = _context("ordinary-a")
    second_context = _context("ordinary-b")
    first = await _start(tracer, first_context)
    await first.observe(
        NativeStateObservation(
            identity=first_context.identity,
            namespace=(),
            state={"owner": "ordinary-a", "shared": 1},
            observed_at=datetime.now(UTC),
            monotonic_ns=3,
        )
    )
    second = await _start(tracer, second_context)
    await second.observe(
        NativeStateObservation(
            identity=second_context.identity,
            namespace=(),
            state={"owner": "ordinary-b", "shared": 1},
            observed_at=datetime.now(UTC),
            monotonic_ns=3,
        )
    )

    with pytest.raises(AmbiguousTraceHead) as captured:
        await tracer.get("thread-query")
    assert captured.value.context["head_run_ids"] == "ordinary-a,ordinary-b"

    await _finish(first, first_context)
    await _finish(second, second_context)
    first_head = await tracer.get("thread-query", head_run_id="ordinary-a")
    second_head = await tracer.get("thread-query", head_run_id="ordinary-b")
    assert {node.run_id for node in first_head.graph.nodes} == {"ordinary-a"}
    assert {node.run_id for node in second_head.graph.nodes} == {"ordinary-b"}
    assert first_head.state.root == {"owner": "ordinary-a", "shared": 1}
    assert second_head.state.root == {"owner": "ordinary-b", "shared": 1}


async def test_malformed_event_cursor_uses_the_stable_cursor_error() -> None:
    tracer = Tracer()
    await _record(tracer, "cursor-malformed")
    thread = await tracer.get("thread-query")

    with pytest.raises(InvalidTraceCursor):
        await thread.events(cursor="not-a-valid-cursor")


async def test_public_views_and_event_pages_cannot_mutate_the_ledger() -> None:
    tracer = Tracer()
    context = _context("immutable")
    session = await _start(tracer, context)
    await session.observe(
        NativeStateObservation(
            identity=context.identity,
            namespace=(),
            state={"nested": {"value": "original"}},
            observed_at=datetime.now(UTC),
            monotonic_ns=3,
        )
    )
    await _finish(session, context)
    thread = await tracer.get("thread-query")
    public_state = thread.state
    nested = public_state.root["nested"]
    assert isinstance(nested, dict)
    nested["value"] = "tampered-view"
    page = await thread.events(limit=100)
    state_fact = next(
        event.fact for event in page.items if isinstance(event.fact, StateRevisionFact)
    )
    assert isinstance(state_fact.changes.value, dict)
    fact_nested = state_fact.changes.value["nested"]
    assert isinstance(fact_nested, dict)
    fact_nested["value"] = "tampered-event"

    fresh = await tracer.get("thread-query")
    fresh_nested = fresh.state.root["nested"]
    assert isinstance(fresh_nested, dict)
    assert fresh_nested["value"] == "original"
    fresh_page = await fresh.events(limit=100)
    assert "tampered" not in fresh_page.model_dump_json(by_alias=True)


async def test_deleted_generation_cursor_cannot_address_a_recreated_thread() -> None:
    tracer = Tracer()
    await _record(tracer, "deleted")
    old = await tracer.get("thread-query")
    first_page = await old.events(limit=1)
    assert first_page.next_cursor is not None
    await old.delete()
    with pytest.raises(TraceThreadNotFound):
        _ = old.messages
    with pytest.raises(TraceThreadNotFound):
        await old.load_older()
    await _record(tracer, "replacement")
    replacement = await tracer.get("thread-query")

    with pytest.raises(InvalidTraceCursor):
        await replacement.events(cursor=first_page.next_cursor)
