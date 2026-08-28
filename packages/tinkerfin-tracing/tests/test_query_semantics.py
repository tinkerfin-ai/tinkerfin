"""Fixed-as-of, head selection, follow, and projected status contracts."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from tinkerfin_contracts import (
    NativeMessageObservation,
    NativeMessageRecord,
    NativeStateObservation,
    NativeToolCall,
    RunClosedObservation,
    RunIdentity,
    RunInputKind,
    RunInputObservation,
    RunObservationSession,
    RunSourceContext,
    RunStartedObservation,
    RunTerminalObservation,
    RunTerminalOutcome,
)
from tinkerfin_tracing import (
    AmbiguousTraceHead,
    InvalidTraceCursor,
    StateRevisionFact,
    Tracer,
    TraceThreadNotFound,
)


def _context(
    run_id: str,
    *,
    input_kind: RunInputKind = "ordinary",
    parent_run_id: str | None = None,
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


async def test_turn_and_run_status_follow_the_latest_segment_terminal() -> None:
    tracer = Tracer()
    context = _context("status")
    session = await _start(tracer, context)

    active = await tracer.get("thread-query")
    assert active.status.execution == "running"
    assert active.tree.roots[0].status == "running"
    assert next(node for node in active.tree.nodes if node.kind == "run").status == (
        "running"
    )

    await _finish(session, context)
    finished = await tracer.get("thread-query")
    assert finished.tree.roots[0].status == "succeeded"
    assert finished.tree.roots[0].completed_at is not None
    assert next(node for node in finished.tree.nodes if node.kind == "run").status == (
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
        assert committed.tree.roots[0].status == "succeeded"
        assert (
            next(node for node in committed.tree.nodes if node.kind == "run").status
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
    tool = next(node for node in proposed.tree.nodes if node.kind == "tool")
    assert tool.status == "running"
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
    tool = next(node for node in completed.tree.nodes if node.kind == "tool")
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
    run_nodes = tuple(node for node in selected.tree.nodes if node.kind == "run")

    assert {node.run_id for node in run_nodes} == {"root", "resume-a"}
    assert selected.tree.roots[0].run_id == "resume-a"


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


async def test_nested_subgraph_nodes_parent_work_to_the_nearest_subagent() -> None:
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
    outer = next(node for node in thread.tree.nodes if node.label == "outer")
    inner = next(node for node in thread.tree.nodes if node.label == "inner")
    tool = next(node for node in thread.tree.nodes if node.kind == "tool")

    assert outer.parent_id == "run:nested"
    assert inner.parent_id == outer.id
    assert tool.parent_id == inner.id
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
    assert thread.tree.roots[0].id.startswith("turn-partial:")


async def test_default_window_contains_exactly_the_latest_one_hundred_turns() -> None:
    tracer = Tracer()
    for index in range(105):
        await _record(tracer, f"run-{index:03d}")

    thread = await tracer.get("thread-query")

    assert len(thread.messages) == 100
    assert len(thread.tree.roots) == 100
    assert thread.messages[0].source_id == "user-run-005"
    assert thread.messages[-1].source_id == "user-run-104"
    assert thread.tree.roots[0].run_id == "run-104"
    assert thread.has_older is True
    assert thread.message_count == 105
    assert thread.tool_call_count == 0
    await thread.load_older(limit=5)
    assert len(thread.messages) == 105
    assert len(thread.tree.roots) == 105
    assert thread.has_older is False
    assert thread.message_count == 105


async def test_ambiguous_head_error_exposes_every_selectable_head() -> None:
    tracer = Tracer()
    await _record(tracer, "root")
    await _record(tracer, "branch-a", input_kind="branch", parent_run_id="root")
    await _record(tracer, "branch-b", input_kind="branch", parent_run_id="root")

    with pytest.raises(AmbiguousTraceHead) as captured:
        await tracer.get("thread-query")

    assert captured.value.context["head_run_ids"] == "branch-a,branch-b"


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
    assert first_head.tree.roots[0].run_id == "ordinary-a"
    assert second_head.tree.roots[0].run_id == "ordinary-b"
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
