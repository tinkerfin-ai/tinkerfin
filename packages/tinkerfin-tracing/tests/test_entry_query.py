"""Direct Trace entry filtering, paging, following, and Skill classification."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Literal

import pytest
from deepagents.backends.protocol import FileData
from deepagents.backends.state import StateBackend
from deepagents.backends.utils import create_file_data
from langchain.agents.middleware.types import InputAgentState
from langchain.tools import tool
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from pydantic import JsonValue

from tinkerfin import RunIdentity, TinkerFin, trace_contribution
from tinkerfin_contracts import (
    AgentStepObservation,
    ModelCallObservation,
    NativeMessageObservation,
    NativeMessageRecord,
    NativeTaskObservation,
    NativeToolCall,
    RunClosedObservation,
    RunInputKind,
    RunInputObservation,
    RunObservationSession,
    RunSourceContext,
    RunStartedObservation,
    RunTerminalObservation,
    SkillSourceDescriptor,
    ToolExecutionObservation,
)
from tinkerfin_tracing import (
    CapturePolicy,
    InMemoryTraceStore,
    InvalidTraceCursor,
    MessageFact,
    SkillFact,
    ToolExecutionFact,
    ToolTraceCapture,
    TraceEntryCompleteness,
    TraceEntryKind,
    TraceEntryPage,
    TraceEntryStatus,
    TraceFacets,
    TraceFilter,
    TraceFollowLifecycleError,
    TraceQuery,
    Tracer,
    TraceStoreProtocolError,
    TraceThreadKey,
    TracingError,
    TracingErrorCode,
    TurnFact,
)


class _Model(FakeMessagesListChatModel):
    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        **kwargs: Any,
    ) -> Runnable:
        del tools, kwargs
        return self


class _SkillInput(InputAgentState):
    files: dict[str, FileData]


class _ReadCountingStore(InMemoryTraceStore):
    def __init__(self) -> None:
        super().__init__()
        self.read_calls = 0

    async def read_events(
        self,
        key: TraceThreadKey,
        *,
        after_seq: int,
        as_of_seq: int,
        limit: int,
    ):
        self.read_calls += 1
        return await super().read_events(
            key,
            after_seq=after_seq,
            as_of_seq=as_of_seq,
            limit=limit,
        )


class _SlowClosingFollowStore(InMemoryTraceStore):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.cleanup_started = asyncio.Event()
        self.release_cleanup = asyncio.Event()
        self.closed = asyncio.Event()

    def follow(self, key: TraceThreadKey, *, after_seq: int):
        del key, after_seq

        async def iterate():
            try:
                self.started.set()
                await asyncio.Future()
                if False:
                    yield ()
            finally:
                self.cleanup_started.set()
                await self.release_cleanup.wait()
                self.closed.set()

        return iterate()


class _CloseFailingFollowStore(InMemoryTraceStore):
    def follow(self, key: TraceThreadKey, *, after_seq: int):
        del key, after_seq

        async def iterate():
            try:
                yield ()
                await asyncio.Future()
            finally:
                raise ValueError("close failed")

        return iterate()


class _CloseTrackingFollowStore(InMemoryTraceStore):
    def __init__(self) -> None:
        super().__init__()
        self.closed = asyncio.Event()

    def follow(self, key: TraceThreadKey, *, after_seq: int):
        del key, after_seq

        async def iterate():
            try:
                yield ()
                await asyncio.Future()
            finally:
                self.closed.set()

        return iterate()


class _YieldThenSlowClosingFollowStore(InMemoryTraceStore):
    def __init__(self) -> None:
        super().__init__()
        self.cleanup_started = asyncio.Event()
        self.release_cleanup = asyncio.Event()
        self.closed = asyncio.Event()

    def follow(self, key: TraceThreadKey, *, after_seq: int):
        del key, after_seq

        async def iterate():
            try:
                yield ()
            finally:
                self.cleanup_started.set()
                await self.release_cleanup.wait()
                self.closed.set()

        return iterate()


def _identity(run_id: str) -> RunIdentity:
    return RunIdentity(threadId="thread-entry-query", runId=run_id)


@pytest.mark.asyncio
async def test_trace_follow_repeated_cancellation_waits_for_store_cleanup() -> None:
    store = _SlowClosingFollowStore()
    page = TraceEntryPage(
        items=(),
        as_of_seq=1,
        facets=TraceFacets(),
        completeness=TraceEntryCompleteness(),
    )

    async def refresh() -> TraceEntryPage:
        return page

    query = TraceQuery(
        page,
        store=store,
        key=TraceThreadKey(
            namespace="trace-follow-test",
            thread_id="thread-entry-query",
            generation="generation-1",
        ),
        refresh=refresh,
    )
    updates = query.follow()
    pull = asyncio.create_task(anext(updates))
    await store.started.wait()

    with pytest.raises(TraceFollowLifecycleError) as concurrent:
        await anext(updates)
    assert isinstance(concurrent.value, TracingError)
    assert isinstance(concurrent.value, RuntimeError)
    assert concurrent.value.code is TracingErrorCode.FOLLOW_LIFECYCLE

    pull.cancel("first cancellation")
    await store.cleanup_started.wait()
    pull.cancel("repeated cancellation")
    await asyncio.sleep(0)
    try:
        assert not pull.done()
    finally:
        store.release_cleanup.set()

    with pytest.raises(asyncio.CancelledError) as captured:
        await pull
    assert captured.value.args == ("first cancellation",)
    assert store.closed.is_set()


@pytest.mark.asyncio
async def test_trace_follow_rejects_context_entry_after_close() -> None:
    page = TraceEntryPage(
        items=(),
        as_of_seq=1,
        facets=TraceFacets(),
        completeness=TraceEntryCompleteness(),
    )

    async def refresh() -> TraceEntryPage:
        return page

    updates = TraceQuery(
        page,
        store=InMemoryTraceStore(),
        key=TraceThreadKey(
            namespace="trace-follow-test",
            thread_id="thread-entry-query",
            generation="generation-1",
        ),
        refresh=refresh,
    ).follow()
    await updates.aclose()

    with pytest.raises(TraceFollowLifecycleError) as closed:
        async with updates:
            pytest.fail("a closed follower must not enter a new scope")
    assert isinstance(closed.value, TracingError)
    assert isinstance(closed.value, RuntimeError)
    assert closed.value.code is TracingErrorCode.FOLLOW_LIFECYCLE


@pytest.mark.asyncio
async def test_entry_follow_keeps_refresh_failure_ahead_of_close_failure() -> None:
    store = _CloseFailingFollowStore()
    page = TraceEntryPage(
        items=(),
        as_of_seq=1,
        facets=TraceFacets(),
        completeness=TraceEntryCompleteness(),
    )

    async def refresh() -> TraceEntryPage:
        raise RuntimeError("refresh failed")

    query = TraceQuery(
        page,
        store=store,
        key=TraceThreadKey(
            namespace="trace-follow-test",
            thread_id="thread-entry-query",
            generation="generation-1",
        ),
        refresh=refresh,
    )

    with pytest.raises(RuntimeError, match="refresh failed") as captured:
        await anext(query.follow())
    assert any("ValueError: close failed" in note for note in captured.value.__notes__)


@pytest.mark.asyncio
async def test_semantic_follow_keeps_projection_failure_ahead_of_close_failure() -> (
    None
):
    store = _CloseFailingFollowStore()
    tracer = Tracer(store=store)
    await _record_context(tracer, _context("follow-close-primary"))
    thread = await tracer.get("thread-entry-query")

    with pytest.raises(TraceStoreProtocolError) as captured:
        await anext(thread.follow())
    assert any("ValueError: close failed" in note for note in captured.value.__notes__)


@pytest.mark.asyncio
async def test_entry_follow_context_closes_after_consumer_break() -> None:
    store = _CloseTrackingFollowStore()
    page = TraceEntryPage(
        items=(),
        as_of_seq=1,
        facets=TraceFacets(),
        completeness=TraceEntryCompleteness(),
    )
    refreshed = page.model_copy(
        update={
            "as_of_seq": 2,
            "facets": TraceFacets(kinds={TraceEntryKind.RUN: 1}),
        }
    )

    async def refresh() -> TraceEntryPage:
        return refreshed

    query = TraceQuery(
        page,
        store=store,
        key=TraceThreadKey(
            namespace="trace-follow-test",
            thread_id="thread-entry-query",
            generation="generation-1",
        ),
        refresh=refresh,
    )

    updates = query.follow()
    async with updates:
        async for update in updates:
            assert update.facets == refreshed.facets
            break

    assert store.closed.is_set()


@pytest.mark.asyncio
async def test_trace_follow_scope_does_not_hide_cancellation_during_close() -> None:
    store = _YieldThenSlowClosingFollowStore()
    page = TraceEntryPage(
        items=(),
        as_of_seq=1,
        facets=TraceFacets(),
        completeness=TraceEntryCompleteness(),
    )
    refreshed = page.model_copy(
        update={
            "as_of_seq": 2,
            "facets": TraceFacets(kinds={TraceEntryKind.RUN: 1}),
        }
    )

    async def refresh() -> TraceEntryPage:
        return refreshed

    updates = TraceQuery(
        page,
        store=store,
        key=TraceThreadKey(
            namespace="trace-follow-test",
            thread_id="thread-entry-query",
            generation="generation-1",
        ),
        refresh=refresh,
    ).follow()

    async def consume() -> None:
        async with updates:
            await anext(updates)
            raise RuntimeError("consumer failed")

    owner = asyncio.create_task(consume())
    await store.cleanup_started.wait()
    owner.cancel("caller cancelled")
    await asyncio.sleep(0)
    try:
        assert not owner.done()
    finally:
        store.release_cleanup.set()

    with pytest.raises(asyncio.CancelledError) as captured:
        await owner
    assert captured.value.args == ("caller cancelled",)
    assert isinstance(captured.value.__cause__, RuntimeError)
    assert str(captured.value.__cause__) == "consumer failed"
    assert store.closed.is_set()


def _context(
    run_id: str,
    *,
    input_kind: RunInputKind = "ordinary",
    parent_run_id: str | None = None,
    content: str | None = None,
    call_tracking_enabled: bool = False,
) -> RunSourceContext:
    messages: list[JsonValue] = []
    if content is not None:
        messages.append(
            {
                "role": "user",
                "id": f"user-{run_id}",
                "content": content,
            }
        )
    input_value: JsonValue = {"messages": messages}
    return RunSourceContext(
        identity=_identity(run_id),
        runtime_profile="deepagents-v2",
        input_kind=input_kind,
        parent_run_id=parent_run_id,
        input=input_value,
        config={},
        call_tracking_enabled=call_tracking_enabled,
    )


async def _start_context(
    tracer: Tracer,
    context: RunSourceContext,
) -> RunObservationSession:
    session = await tracer.open_run(context)
    observed_at, monotonic_ns = _observed_at()
    await session.observe(
        RunStartedObservation(
            identity=context.identity,
            observed_at=observed_at,
            monotonic_ns=monotonic_ns,
        )
    )
    observed_at, monotonic_ns = _observed_at()
    await session.observe(
        RunInputObservation(
            identity=context.identity,
            source=context,
            observed_at=observed_at,
            monotonic_ns=monotonic_ns,
        )
    )
    return session


async def _finish_context(
    session: RunObservationSession,
    context: RunSourceContext,
) -> None:
    observed_at, monotonic_ns = _observed_at()
    await session.observe(
        RunTerminalObservation(
            identity=context.identity,
            outcome="succeeded",
            observed_at=observed_at,
            monotonic_ns=monotonic_ns,
        )
    )
    observed_at, monotonic_ns = _observed_at()
    await session.observe(
        RunClosedObservation(
            identity=context.identity,
            outcome="succeeded",
            observed_at=observed_at,
            monotonic_ns=monotonic_ns,
        )
    )
    await session.aclose()


async def _record_context(tracer: Tracer, context: RunSourceContext) -> None:
    session = await _start_context(tracer, context)
    await _finish_context(session, context)


@pytest.mark.parametrize(
    "values",
    [
        {"agent_names": {f"agent-{index}" for index in range(65)}},
        {"models": {"m" * 1025}},
        {"namespaces": {(" child",)}},
        {"namespaces": {tuple(f"scope-{index}" for index in range(65))}},
    ],
)
def test_trace_filter_rejects_unbounded_or_ambiguous_values(
    values: dict[str, object],
) -> None:
    """Filter validation bounds database work before a backend is selected."""

    with pytest.raises(ValueError):
        TraceFilter.model_validate(values)


@pytest.mark.parametrize(
    ("outcome", "expected_status"),
    [
        ("interrupted", TraceEntryStatus.WAITING),
        ("cancelled", TraceEntryStatus.CANCELLED),
    ],
)
@pytest.mark.asyncio
async def test_run_terminal_settles_every_unmatched_native_entry_without_failure(
    outcome: Literal["interrupted", "cancelled"],
    expected_status: TraceEntryStatus,
) -> None:
    tracer = Tracer()
    context = _context(
        f"native-{outcome}",
        content="delegate work",
        call_tracking_enabled=True,
    )
    session = await _start_context(tracer, context)
    observed_at, monotonic_ns = _observed_at()
    await session.observe(
        AgentStepObservation(
            identity=context.identity,
            phase="started",
            call_id="agent-root",
            step_kind="agent",
            name="Agent",
            observed_at=observed_at,
            monotonic_ns=monotonic_ns,
        )
    )
    observed_at, monotonic_ns = _observed_at()
    await session.observe(
        NativeMessageObservation(
            identity=context.identity,
            namespace=(),
            message=NativeMessageRecord(
                message_type="assistant",
                id="delegate-message",
                content="",
                tool_calls=(
                    NativeToolCall(
                        id="call-task",
                        name="task",
                        arguments={
                            "description": "Inspect the workspace",
                            "subagent_type": "researcher",
                        },
                    ),
                ),
            ),
            observed_at=observed_at,
            monotonic_ns=monotonic_ns,
        )
    )
    observed_at, monotonic_ns = _observed_at()
    await session.observe(
        NativeTaskObservation(
            identity=context.identity,
            namespace=(),
            phase="start",
            task_id="task-tools",
            name="tools",
            input=[
                {
                    "name": "task",
                    "id": "call-task",
                    "args": {
                        "description": "Inspect the workspace",
                        "subagent_type": "researcher",
                    },
                }
            ],
            observed_at=observed_at,
            monotonic_ns=monotonic_ns,
        )
    )
    observed_at, monotonic_ns = _observed_at()
    await session.observe(
        NativeTaskObservation(
            identity=context.identity,
            namespace=("tools:task-tools",),
            phase="start",
            task_id="child-model",
            name="model",
            observed_at=observed_at,
            monotonic_ns=monotonic_ns,
        )
    )
    observed_at, monotonic_ns = _observed_at()
    await session.observe(
        AgentStepObservation(
            identity=context.identity,
            phase=outcome,
            call_id="agent-root",
            step_kind="agent",
            name="Agent",
            observed_at=observed_at,
            monotonic_ns=monotonic_ns,
        )
    )
    observed_at, monotonic_ns = _observed_at()
    await session.observe(
        RunTerminalObservation(
            identity=context.identity,
            outcome=outcome,
            interrupt_ids=("interrupt-native",) if outcome == "interrupted" else (),
            observed_at=observed_at,
            monotonic_ns=monotonic_ns,
        )
    )
    observed_at, monotonic_ns = _observed_at()
    await session.observe(
        RunClosedObservation(
            identity=context.identity,
            outcome=outcome,
            observed_at=observed_at,
            monotonic_ns=monotonic_ns,
        )
    )
    await session.aclose()

    page = await tracer.query(context.identity.thread_id, limit=100)
    lifecycle_entries = tuple(
        item
        for item in page.items
        if item.kind
        in {
            TraceEntryKind.AGENT,
            TraceEntryKind.TASK,
            TraceEntryKind.SUBAGENT,
            TraceEntryKind.TOOL_PROPOSAL,
        }
    )
    assert lifecycle_entries
    assert {item.status for item in lifecycle_entries} == {expected_status}
    assert all(item.failure is None for item in page.items)
    assert not any(item.status is TraceEntryStatus.RUNNING for item in page.items)


@pytest.mark.asyncio
async def test_real_runtime_cancellation_has_no_running_or_error_entries() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    @tool
    async def wait_for_cancellation() -> str:
        """Wait until the owning Runtime cancels this Tool.

        Returns:
            An unreachable value.
        """

        entered.set()
        await release.wait()
        return "unreachable"

    model = _Model(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "wait_for_cancellation",
                        "args": {},
                        "id": "call-cancel-query",
                        "type": "tool_call",
                    }
                ],
            )
        ]
    )
    tracer = Tracer()
    tinkerfin = TinkerFin().observe(tracer)
    definition = tinkerfin.create_deep_agent(
        model=model,
        tools=[wait_for_cancellation],
    )
    identity = _identity("real-cancel-query")
    stream = await tinkerfin.open_run(
        identity,
        agent=definition,
        input={"messages": [HumanMessage(content="wait", id="cancel-user")]},
    )

    async def consume() -> None:
        async for _part in stream:
            pass

    consumer = asyncio.create_task(consume())
    await asyncio.wait_for(entered.wait(), timeout=1)
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer

    page = await tracer.query(identity.thread_id, limit=100)
    assert any(item.status is TraceEntryStatus.CANCELLED for item in page.items)
    assert all(item.failure is None for item in page.items)
    assert not any(
        item.status in {TraceEntryStatus.RUNNING, TraceEntryStatus.WAITING}
        for item in page.items
    )


@pytest.mark.asyncio
async def test_query_filters_in_the_store_and_returns_facets_ancestors_and_cursors() -> (
    None
):
    """Filtering uses the entry index and loads only referenced Ledger events."""

    @tool
    async def add_values(a: int, b: int) -> str:
        """Add two values.

        Args:
            a: First value.
            b: Second value.

        Returns:
            Decimal sum.
        """

        return str(a + b)

    model = _Model(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "add_values",
                        "args": {"a": 2, "b": 3},
                        "id": "call-add",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    store = _ReadCountingStore()
    tracer = Tracer(store=store)
    tinkerfin = TinkerFin().observe(tracer)
    definition = tinkerfin.create_deep_agent(model=model, tools=[add_values])
    stream = await tinkerfin.open_run(
        _identity("run-query"),
        agent=definition,
        input={"messages": [HumanMessage(content="add", id="user-add")]},
    )
    async for _part in stream:
        pass
    store.read_calls = 0

    complete = await tracer.query("thread-entry-query", limit=100)
    by_id = {item.id: item for item in complete.items}
    assert len(complete.turns) == 1
    assert complete.turns[0].ordinal == 1
    assert complete.turns[0].user_message is not None
    assert complete.turns[0].user_message.content == "add"
    assert all(item.turn_id == complete.turns[0].id for item in complete.items)
    agent = next(item for item in complete.items if item.kind is TraceEntryKind.AGENT)
    model_steps = tuple(
        item for item in complete.items if item.kind is TraceEntryKind.MODEL
    )
    providers = tuple(
        item for item in complete.items if item.kind is TraceEntryKind.PROVIDER
    )
    tools_step = next(
        item for item in complete.items if item.kind is TraceEntryKind.TOOLS
    )
    execution = next(
        item for item in complete.items if item.kind is TraceEntryKind.TOOL
    )
    assert model_steps and providers
    assert all(item.parent_id == agent.id for item in model_steps)
    assert all(
        item.parent_id is not None
        and by_id[item.parent_id].kind is TraceEntryKind.MODEL
        for item in providers
    )
    assert tools_step.parent_id == agent.id
    assert execution.parent_id == tools_step.id
    assert store.read_calls == 0

    first = await tracer.query(
        "thread-entry-query",
        where=TraceFilter(
            kinds={TraceEntryKind.MODEL, TraceEntryKind.TOOL},
            statuses={TraceEntryStatus.SUCCEEDED},
            include_ancestors=True,
        ),
        limit=1,
    )

    assert store.read_calls == 0
    assert first.next_cursor is not None
    assert first.completeness.call_tracking_missing is False
    assert first.completeness.execution_tree_missing is False
    assert first.facets.kinds[TraceEntryKind.MODEL] == 2
    assert first.facets.kinds[TraceEntryKind.TOOL] == 1
    assert any(item.kind is TraceEntryKind.AGENT for item in first.items)
    assert any(item.kind is TraceEntryKind.MODEL for item in first.items)
    next_page = await tracer.query(
        "thread-entry-query",
        where=TraceFilter(
            kinds={TraceEntryKind.MODEL, TraceEntryKind.TOOL},
            statuses={TraceEntryStatus.SUCCEEDED},
            include_ancestors=True,
        ),
        cursor=first.next_cursor,
        limit=1,
    )
    assert next_page.items
    with pytest.raises(InvalidTraceCursor):
        await tracer.query(
            "thread-entry-query",
            where=TraceFilter(kinds={TraceEntryKind.SKILL}),
            cursor=first.next_cursor,
            limit=1,
        )
    padding = "=" * (-len(first.next_cursor) % 4)
    cursor_payload = json.loads(
        base64.urlsafe_b64decode((first.next_cursor + padding).encode())
    )
    cursor_payload["beforeStartedAt"] = (
        cursor_payload["beforeStartedAt"].removesuffix("Z").removesuffix("+00:00")
    )
    forged_cursor = (
        base64.urlsafe_b64encode(
            json.dumps(cursor_payload, separators=(",", ":")).encode()
        )
        .decode()
        .rstrip("=")
    )
    with pytest.raises(InvalidTraceCursor, match="invalid"):
        await tracer.query(
            "thread-entry-query",
            where=TraceFilter(
                kinds={TraceEntryKind.MODEL, TraceEntryKind.TOOL},
                statuses={TraceEntryStatus.SUCCEEDED},
                include_ancestors=True,
            ),
            cursor=forged_cursor,
            limit=1,
        )
    snapshot = await store.snapshot("thread-entry-query")
    with pytest.raises(ValueError, match="aware UTC"):
        await store.query_trace_entries(
            snapshot.key,
            run_ids=("run-query",),
            where=TraceFilter(),
            limit=1,
            before_started_at=datetime.now(UTC).replace(tzinfo=None),
            before_entry_id="entry",
        )
    with pytest.raises(InvalidTraceCursor):
        await tracer.query(
            "thread-entry-query",
            cursor="x" * 16_385,
            limit=1,
        )
    proposals = await tracer.query(
        "thread-entry-query",
        where=TraceFilter(kinds={TraceEntryKind.TOOL_PROPOSAL}),
    )
    assert len(proposals.items) == 1
    assert proposals.items[0].request == {"a": 2, "b": 3}
    proposal = proposals.items[0]
    assert proposal.parent_id is not None
    assert by_id[proposal.parent_id].kind is TraceEntryKind.PROVIDER
    assert execution.proposal_id == proposal.id
    assert execution.parent_id != execution.proposal_id


@pytest.mark.asyncio
async def test_subagent_provider_is_nested_under_its_named_agent() -> None:
    root_model = _Model(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "task",
                        "args": {
                            "description": "delegate",
                            "subagent_type": "researcher",
                        },
                        "id": "call-task",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="root done"),
        ]
    )
    child_model = _Model(responses=[AIMessage(content="child done")])
    tracer = Tracer()
    tinkerfin = TinkerFin().observe(tracer)
    definition = tinkerfin.create_deep_agent(
        model=root_model,
        tools=[],
        subagents=[
            {
                "name": "researcher",
                "description": "Complete delegated work",
                "system_prompt": "Complete the child request",
                "model": child_model,
                "tools": [],
            }
        ],
    )
    stream = await tinkerfin.open_run(
        _identity("run-subagent-model"),
        agent=definition,
        input={"messages": [HumanMessage(content="delegate", id="user-delegate")]},
    )
    async for _part in stream:
        pass

    query = await tracer.query("thread-entry-query", limit=100)
    by_id = {item.id: item for item in query.items}
    subagent = next(
        item
        for item in query.items
        if item.kind is TraceEntryKind.SUBAGENT and item.agent_name == "researcher"
    )
    child_model_step = next(
        item
        for item in query.items
        if item.kind is TraceEntryKind.MODEL and item.agent_name == "researcher"
    )
    child_provider = next(
        item
        for item in query.items
        if item.kind is TraceEntryKind.PROVIDER and item.agent_name == "researcher"
    )
    assert subagent.parent_id is not None
    parent_execution = by_id[subagent.parent_id]
    assert parent_execution.kind is TraceEntryKind.TOOL
    assert parent_execution.name == "task"
    assert child_model_step.parent_id == subagent.id
    assert child_provider.parent_id == child_model_step.id
    provider_parent = child_provider.parent_id
    assert provider_parent is not None
    assert by_id[provider_parent].agent_name == "researcher"


@pytest.mark.asyncio
async def test_provider_failure_owns_the_bounded_error_message() -> None:
    tracer = Tracer(
        capture_policy=CapturePolicy.public_history(include_error_messages=True)
    )
    tinkerfin = TinkerFin().observe(tracer)
    definition = tinkerfin.create_deep_agent(
        model=_Model(responses=[]),
        tools=[],
    )
    stream = await tinkerfin.open_run(
        _identity("run-provider-error"),
        agent=definition,
        input={"messages": [HumanMessage(content="fail", id="user-fail")]},
    )
    with pytest.raises(IndexError):
        async for _part in stream:
            pass

    query = await tracer.query("thread-entry-query", limit=100)
    provider = next(
        item for item in query.items if item.kind is TraceEntryKind.PROVIDER
    )
    agent = next(item for item in query.items if item.kind is TraceEntryKind.AGENT)
    assert provider.status is TraceEntryStatus.FAILED
    assert provider.failure is not None
    assert provider.failure.error_type.endswith("IndexError")
    assert provider.failure.message == "list index out of range"
    assert agent.status is TraceEntryStatus.FAILED
    assert agent.failure is None
    assert [item.id for item in query.items if item.failure is not None] == [
        provider.id
    ]
    for kind in (TraceEntryKind.RUN, TraceEntryKind.TASK):
        filtered = await tracer.query(
            "thread-entry-query",
            where=TraceFilter(kinds={kind}),
            limit=100,
        )
        assert all(item.failure is None for item in filtered.items)
    provider_only = await tracer.query(
        "thread-entry-query",
        where=TraceFilter(kinds={TraceEntryKind.PROVIDER}),
        limit=100,
    )
    assert [item.id for item in provider_only.items if item.failure is not None] == [
        provider.id
    ]
    paged_failures: list[str] = []
    cursor: str | None = None
    while True:
        page = await tracer.query(
            "thread-entry-query",
            cursor=cursor,
            limit=1,
        )
        paged_failures.extend(
            item.id for item in page.items if item.failure is not None
        )
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    assert paged_failures == [provider.id]


@pytest.mark.asyncio
async def test_failed_tool_contribution_owns_one_error_under_its_execution() -> None:
    @tool
    async def retrieve_failure() -> str:
        """Fail inside one explicit retrieval contribution."""

        async with trace_contribution(kind="retrieval", name="lookup"):
            raise RuntimeError("retrieval failed")

    tracer = Tracer(
        capture_policy=CapturePolicy.public_history(include_error_messages=True)
    )
    tinkerfin = TinkerFin().observe(tracer)
    definition = tinkerfin.create_deep_agent(
        model=_Model(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "retrieve_failure",
                            "args": {},
                            "id": "call-retrieval-failure",
                            "type": "tool_call",
                        }
                    ],
                )
            ]
        ),
        tools=[retrieve_failure],
    )
    stream = await tinkerfin.open_run(
        _identity("run-retrieval-error"),
        agent=definition,
        input={"messages": [HumanMessage(content="retrieve", id="user-retrieve")]},
    )
    with pytest.raises(RuntimeError, match="retrieval failed"):
        async for _part in stream:
            pass

    query = await tracer.query("thread-entry-query", limit=100)
    contribution = next(
        item for item in query.items if item.kind is TraceEntryKind.RETRIEVAL
    )
    execution = next(
        item
        for item in query.items
        if item.kind is TraceEntryKind.TOOL and item.name == "retrieve_failure"
    )

    assert contribution.parent_id == execution.id
    assert contribution.failure is not None
    assert contribution.failure.error_type.endswith("RuntimeError")
    assert execution.status is TraceEntryStatus.FAILED
    assert execution.failure is None
    assert [item.id for item in query.items if item.failure is not None] == [
        contribution.id
    ]
    for kind in (TraceEntryKind.RUN, TraceEntryKind.TASK, TraceEntryKind.TOOL):
        filtered = await tracer.query(
            "thread-entry-query",
            where=TraceFilter(kinds={kind}),
            limit=100,
        )
        assert all(item.failure is None for item in filtered.items)
    contribution_only = await tracer.query(
        "thread-entry-query",
        where=TraceFilter(kinds={TraceEntryKind.RETRIEVAL}),
        limit=100,
    )
    assert [
        item.id for item in contribution_only.items if item.failure is not None
    ] == [contribution.id]


@pytest.mark.asyncio
async def test_two_human_messages_open_two_ordered_turns() -> None:
    tracer = Tracer()
    tinkerfin = TinkerFin().observe(tracer)
    definition = tinkerfin.create_deep_agent(
        model=_Model(
            responses=[
                AIMessage(content="first answer"),
                AIMessage(content="second answer"),
            ]
        ),
        tools=[],
    )
    for run_id, message_id, content in (
        ("run-turn-one", "user-one", "first request"),
        ("run-turn-two", "user-two", "second request"),
    ):
        stream = await tinkerfin.open_run(
            _identity(run_id),
            agent=definition,
            input={"messages": [HumanMessage(content=content, id=message_id)]},
        )
        async for _part in stream:
            pass

    query = await tracer.query("thread-entry-query", limit=100)
    turn_messages = tuple(turn.user_message for turn in query.turns)
    assert all(message is not None for message in turn_messages)
    assert [
        (turn.ordinal, message.content)
        for turn, message in zip(query.turns, turn_messages, strict=True)
        if message is not None
    ] == [
        (1, "first request"),
        (2, "second request"),
    ]
    turn_by_run = {
        item.run_id: item.turn_id
        for item in query.items
        if item.kind is TraceEntryKind.AGENT
    }
    assert turn_by_run == {
        "run-turn-one": query.turns[0].id,
        "run-turn-two": query.turns[1].id,
    }
    thread = await tracer.get("thread-entry-query")
    events = (await thread.events(limit=100)).items
    user_messages = [
        event.fact
        for event in events
        if isinstance(event.fact, MessageFact) and event.fact.role == "user"
    ]
    turns = [event.fact for event in events if isinstance(event.fact, TurnFact)]
    assert [fact.source_message_id for fact in user_messages] == [
        "user-one",
        "user-two",
    ]
    assert [
        fact.content.value for fact in user_messages if fact.content is not None
    ] == [
        "first request",
        "second request",
    ]
    assert [fact.user_message_id for fact in turns] == ["user-one", "user-two"]
    assert all("content" not in fact.model_dump() for fact in turns)


@pytest.mark.asyncio
async def test_resume_reuses_its_parent_turn_without_copying_a_human_message() -> None:
    tracer = Tracer()
    initial = _context("resume-parent", content="initial request")
    await _record_context(tracer, initial)
    resumed = _context(
        "resume-child",
        input_kind="resume",
        parent_run_id="resume-parent",
    )
    await _record_context(tracer, resumed)

    query = await tracer.query(
        "thread-entry-query",
        head_run_id="resume-child",
        where=TraceFilter(kinds={TraceEntryKind.RUN}),
        limit=100,
    )

    assert len(query.turns) == 1
    assert query.turns[0].ordinal == 1
    assert query.turns[0].user_message is not None
    assert query.turns[0].user_message.content == "initial request"
    assert {item.run_id for item in query.items} == {"resume-parent", "resume-child"}
    assert {item.turn_id for item in query.items} == {query.turns[0].id}
    thread = await tracer.get("thread-entry-query", head_run_id="resume-child")
    events = (await thread.events(limit=100)).items
    assert sum(isinstance(event.fact, TurnFact) for event in events) == 1
    assert (
        sum(
            isinstance(event.fact, MessageFact) and event.fact.role == "user"
            for event in events
        )
        == 1
    )


@pytest.mark.asyncio
async def test_branch_turn_ordinals_are_local_to_the_selected_lineage() -> None:
    tracer = Tracer()
    await _record_context(tracer, _context("branch-root", content="root request"))
    await _record_context(
        tracer,
        _context(
            "branch-a",
            input_kind="branch",
            parent_run_id="branch-root",
            content="branch A request",
        ),
    )
    await _record_context(
        tracer,
        _context(
            "branch-b",
            input_kind="branch",
            parent_run_id="branch-root",
            content="branch B request",
        ),
    )

    branch_a = await tracer.query(
        "thread-entry-query",
        head_run_id="branch-a",
        where=TraceFilter(kinds={TraceEntryKind.RUN}),
        limit=100,
    )
    branch_b = await tracer.query(
        "thread-entry-query",
        head_run_id="branch-b",
        where=TraceFilter(kinds={TraceEntryKind.RUN}),
        limit=100,
    )

    assert [turn.ordinal for turn in branch_a.turns] == [1, 2]
    assert [turn.ordinal for turn in branch_b.turns] == [1, 2]
    branch_a_messages = tuple(turn.user_message for turn in branch_a.turns)
    branch_b_messages = tuple(turn.user_message for turn in branch_b.turns)
    assert all(message is not None for message in branch_a_messages)
    assert all(message is not None for message in branch_b_messages)
    assert [
        message.content for message in branch_a_messages if message is not None
    ] == [
        "root request",
        "branch A request",
    ]
    assert [
        message.content for message in branch_b_messages if message is not None
    ] == [
        "root request",
        "branch B request",
    ]
    assert {item.run_id for item in branch_a.items} == {"branch-root", "branch-a"}
    assert {item.run_id for item in branch_b.items} == {"branch-root", "branch-b"}


@pytest.mark.asyncio
async def test_follow_upserts_a_new_turn_before_any_model_output() -> None:
    tracer = Tracer()
    await _record_context(tracer, _context("follow-first", content="first request"))
    query = await tracer.query(
        "thread-entry-query",
        where=TraceFilter(kinds={TraceEntryKind.RUN}),
        limit=100,
    )
    updates = query.follow()
    pending_update = asyncio.create_task(anext(updates))
    second = _context("follow-second", content="second request")
    session = await _start_context(tracer, second)
    update = await asyncio.wait_for(pending_update, timeout=1)
    await updates.aclose()
    await _finish_context(session, second)

    turn_messages = tuple(turn.user_message for turn in update.turn_upserts)
    assert all(message is not None for message in turn_messages)
    assert [
        (turn.ordinal, message.content)
        for turn, message in zip(update.turn_upserts, turn_messages, strict=True)
        if message is not None
    ] == [(2, "second request")]
    assert update.turn_removes == ()
    assert any(item.run_id == "follow-second" for item in update.upserts)
    assert all(item.kind is TraceEntryKind.RUN for item in update.upserts)


@pytest.mark.parametrize("proposal_before_completion", [True, False])
@pytest.mark.asyncio
async def test_tool_proposal_belongs_to_the_successful_provider_attempt(
    proposal_before_completion: bool,
) -> None:
    tracer = Tracer()
    context = _context(
        "provider-retry",
        content="retry the provider",
        call_tracking_enabled=True,
    )
    session = await _start_context(tracer, context)
    message = NativeMessageRecord(
        message_type="human",
        id="provider-user",
        content="retry the provider",
    )
    tool_message = NativeMessageObservation(
        identity=context.identity,
        namespace=(),
        message=NativeMessageRecord(
            message_type="assistant",
            id="provider-answer",
            content="",
            tool_calls=(
                NativeToolCall(
                    id="call-provider-retry",
                    name="search",
                    arguments={"query": "current data"},
                ),
            ),
        ),
        observed_at=datetime.now(UTC),
        monotonic_ns=time.monotonic_ns(),
    )

    async def observe_step(
        *,
        phase: str,
        call_id: str,
        parent_call_id: str | None,
        step_kind: str,
        name: str,
    ) -> None:
        await session.observe(
            AgentStepObservation.model_validate(
                {
                    "identity": context.identity,
                    "phase": phase,
                    "call_id": call_id,
                    "parent_call_id": parent_call_id,
                    "step_kind": step_kind,
                    "name": name,
                    "observed_at": datetime.now(UTC),
                    "monotonic_ns": time.monotonic_ns(),
                }
            )
        )

    await observe_step(
        phase="started",
        call_id="agent-raw",
        parent_call_id=None,
        step_kind="agent",
        name="Agent",
    )
    await observe_step(
        phase="started",
        call_id="model-step-raw",
        parent_call_id="agent-raw",
        step_kind="model",
        name="Model",
    )
    await session.observe(
        ModelCallObservation(
            identity=context.identity,
            phase="started",
            call_id="provider-failed",
            parent_call_id="model-step-raw",
            provider="fake",
            model="retry-model",
            messages=(message,),
            observed_at=datetime.now(UTC),
            monotonic_ns=time.monotonic_ns(),
        )
    )
    await session.observe(
        ModelCallObservation(
            identity=context.identity,
            phase="failed",
            call_id="provider-failed",
            parent_call_id="model-step-raw",
            provider="fake",
            model="retry-model",
            error_type="builtins.TimeoutError",
            observed_at=datetime.now(UTC),
            monotonic_ns=time.monotonic_ns(),
        )
    )
    await session.observe(
        ModelCallObservation(
            identity=context.identity,
            phase="started",
            call_id="provider-succeeded",
            parent_call_id="model-step-raw",
            provider="fake",
            model="retry-model",
            messages=(message,),
            observed_at=datetime.now(UTC),
            monotonic_ns=time.monotonic_ns(),
        )
    )
    if proposal_before_completion:
        await session.observe(tool_message)
    await session.observe(
        ModelCallObservation(
            identity=context.identity,
            phase="completed",
            call_id="provider-succeeded",
            parent_call_id="model-step-raw",
            provider="fake",
            model="retry-model",
            tool_call_ids=("call-provider-retry",),
            observed_at=datetime.now(UTC),
            monotonic_ns=time.monotonic_ns(),
        )
    )
    if not proposal_before_completion:
        await session.observe(tool_message)
    await observe_step(
        phase="completed",
        call_id="model-step-raw",
        parent_call_id="agent-raw",
        step_kind="model",
        name="Model",
    )
    await observe_step(
        phase="completed",
        call_id="agent-raw",
        parent_call_id=None,
        step_kind="agent",
        name="Agent",
    )
    await _finish_context(session, context)

    query = await tracer.query("thread-entry-query", limit=100)
    providers = [item for item in query.items if item.kind is TraceEntryKind.PROVIDER]
    proposal = next(
        item for item in query.items if item.kind is TraceEntryKind.TOOL_PROPOSAL
    )
    succeeded = next(
        item for item in providers if item.status is TraceEntryStatus.SUCCEEDED
    )
    failed = next(item for item in providers if item.status is TraceEntryStatus.FAILED)
    assert proposal.parent_id == succeeded.id
    assert proposal.parent_id != failed.id


@pytest.mark.asyncio
async def test_filtered_follow_removes_an_entry_after_its_status_changes() -> None:
    """A live result emits remove when an indexed entry stops matching."""

    entered = asyncio.Event()
    release = asyncio.Event()

    @tool
    async def wait_for_release() -> str:
        """Wait for the test to release this Tool.

        Returns:
            Completion marker.
        """

        entered.set()
        await release.wait()
        return "released"

    model = _Model(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "wait_for_release",
                        "args": {},
                        "id": "call-wait",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    tracer = Tracer(store=InMemoryTraceStore())
    tinkerfin = TinkerFin().observe(tracer)
    definition = tinkerfin.create_deep_agent(model=model, tools=[wait_for_release])
    stream = await tinkerfin.open_run(
        _identity("run-follow"),
        agent=definition,
        input={"messages": [HumanMessage(content="wait")]},
    )

    async def consume() -> None:
        async for _part in stream:
            pass

    consumer = asyncio.create_task(consume())
    await asyncio.wait_for(entered.wait(), timeout=1)
    query = await tracer.query(
        "thread-entry-query",
        where=TraceFilter(
            kinds={TraceEntryKind.TOOL},
            statuses={TraceEntryStatus.RUNNING},
        ),
    )
    running = tuple(query.items)
    assert len(running) == 1
    assert len(query.turns) == 1
    running_turn_id = query.turns[0].id
    updates = query.follow()
    pending_update = asyncio.create_task(anext(updates))
    release.set()
    await consumer
    update = await asyncio.wait_for(pending_update, timeout=1)
    await updates.aclose()

    assert update.removes == (running[0].id,)
    assert update.upserts == ()
    assert update.turn_removes == (running_turn_id,)
    assert update.turn_upserts == ()


@pytest.mark.asyncio
async def test_generic_runtime_history_is_not_marked_as_call_complete() -> None:
    tracer = Tracer(store=InMemoryTraceStore())
    context = RunSourceContext(
        identity=_identity("run-generic"),
        runtime_profile="custom-runtime",
        input_kind="ordinary",
        input={"messages": []},
        config={},
    )
    session = await tracer.open_run(context)
    observed_at, monotonic_ns = _observed_at()
    await session.observe(
        RunStartedObservation(
            identity=context.identity,
            observed_at=observed_at,
            monotonic_ns=monotonic_ns,
        )
    )
    observed_at, monotonic_ns = _observed_at()
    await session.observe(
        RunInputObservation(
            identity=context.identity,
            source=context,
            observed_at=observed_at,
            monotonic_ns=monotonic_ns,
        )
    )
    observed_at, monotonic_ns = _observed_at()
    await session.observe(
        RunTerminalObservation(
            identity=context.identity,
            outcome="succeeded",
            observed_at=observed_at,
            monotonic_ns=monotonic_ns,
        )
    )
    observed_at, monotonic_ns = _observed_at()
    await session.observe(
        RunClosedObservation(
            identity=context.identity,
            outcome="succeeded",
            observed_at=observed_at,
            monotonic_ns=monotonic_ns,
        )
    )
    await session.aclose()

    query = await tracer.query("thread-entry-query")

    assert query.completeness.call_tracking_missing is True
    assert query.completeness.execution_tree_missing is True


def _observed_at() -> tuple[datetime, int]:
    return datetime.now(UTC), time.monotonic_ns()


@pytest.mark.asyncio
async def test_only_a_successful_registered_exact_skill_read_is_invoked() -> None:
    """Catalog availability, ordinary files, and failed reads never become Skill facts."""

    tracer = Tracer(store=InMemoryTraceStore())
    context = RunSourceContext(
        identity=_identity("run-skill"),
        runtime_profile="deepagents-v2",
        input_kind="ordinary",
        input={"messages": []},
        config={},
        skill_sources=(SkillSourceDescriptor(path="/skills"),),
    )
    session = await tracer.open_run(context)
    observed_at, monotonic_ns = _observed_at()
    await session.observe(
        RunStartedObservation(
            identity=context.identity,
            observed_at=observed_at,
            monotonic_ns=monotonic_ns,
        )
    )
    observed_at, monotonic_ns = _observed_at()
    await session.observe(
        RunInputObservation(
            identity=context.identity,
            source=context,
            observed_at=observed_at,
            monotonic_ns=monotonic_ns,
        )
    )

    async def execute(
        execution_id: str,
        file_path: str,
        *,
        success: bool,
    ) -> None:
        observed_at, monotonic_ns = _observed_at()
        await session.observe(
            ToolExecutionObservation(
                identity=context.identity,
                observed_at=observed_at,
                monotonic_ns=monotonic_ns,
                phase="started",
                execution_id=execution_id,
                tool_call_id=f"call-{execution_id}",
                tool_name="read_file",
                input={"file_path": file_path},
            )
        )
        observed_at, monotonic_ns = _observed_at()
        await session.observe(
            ToolExecutionObservation(
                identity=context.identity,
                observed_at=observed_at,
                monotonic_ns=monotonic_ns,
                phase="completed" if success else "failed",
                execution_id=execution_id,
                tool_call_id=f"call-{execution_id}",
                tool_name="read_file",
                output="instructions" if success else None,
                error_type=None if success else "builtins.OSError",
            )
        )

    await execute("registered", "/skills/research/SKILL.md", success=True)
    await execute("ordinary", "/tmp/research/SKILL.md", success=True)
    await execute("failed", "/skills/writing/SKILL.md", success=False)
    observed_at, monotonic_ns = _observed_at()
    await session.observe(
        RunTerminalObservation(
            identity=context.identity,
            observed_at=observed_at,
            monotonic_ns=monotonic_ns,
            outcome="succeeded",
        )
    )
    observed_at, monotonic_ns = _observed_at()
    await session.observe(
        RunClosedObservation(
            identity=context.identity,
            observed_at=observed_at,
            monotonic_ns=monotonic_ns,
            outcome="succeeded",
        )
    )
    await session.aclose()

    thread = await tracer.get("thread-entry-query")
    events = []
    cursor = None
    while True:
        page = await thread.events(cursor=cursor, limit=100)
        events.extend(page.items)
        cursor = page.next_cursor
        if cursor is None:
            break
    skills = [event.fact for event in events if isinstance(event.fact, SkillFact)]
    assert [(fact.name, fact.source_path) for fact in skills] == [
        ("research", "/skills/research/SKILL.md")
    ]


@pytest.mark.asyncio
async def test_deep_agent_skill_read_creates_one_query_entry() -> None:
    model = _Model(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_file",
                        "args": {"file_path": "/skills/research/SKILL.md"},
                        "id": "call-skill-read",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    tracer = Tracer(store=InMemoryTraceStore())
    tinkerfin = TinkerFin().observe(tracer)
    definition = tinkerfin.create_deep_agent(
        model=model,
        tools=[],
        backend=StateBackend(),
        skills=["/skills/"],
    )
    graph_input: _SkillInput = {
        "messages": [HumanMessage(content="use the research skill")],
        "files": {
            "/skills/research/SKILL.md": create_file_data(
                "---\n"
                "name: research\n"
                "description: Verify one real configured Skill read\n"
                "---\n"
                "# Research\n"
                "Use primary sources.\n"
            )
        },
    }
    stream = await tinkerfin.open_run(
        _identity("run-real-skill"),
        agent=definition,
        input=graph_input,
    )
    async for _part in stream:
        pass

    query = await tracer.query(
        "thread-entry-query",
        where=TraceFilter(kinds={TraceEntryKind.TOOL, TraceEntryKind.SKILL}),
    )

    by_id = {item.id: item for item in query.items}
    skill = next(item for item in query.items if item.kind is TraceEntryKind.SKILL)
    assert (skill.name, skill.source_path) == (
        "research",
        "/skills/research/SKILL.md",
    )
    assert skill.parent_id is not None
    assert by_id[skill.parent_id].kind is TraceEntryKind.TOOL
    assert by_id[skill.parent_id].name == "read_file"


@pytest.mark.asyncio
async def test_disabled_skill_read_creates_no_tool_or_skill_entry() -> None:
    tracer = Tracer(
        store=InMemoryTraceStore(),
        capture_policy=CapturePolicy.public_history(
            tool_overrides={"read_file": ToolTraceCapture.disabled()}
        ),
    )
    context = RunSourceContext(
        identity=_identity("run-disabled-skill"),
        runtime_profile="deepagents-v2",
        input_kind="ordinary",
        input={"messages": []},
        config={},
        skill_sources=(SkillSourceDescriptor(path="/skills"),),
    )
    session = await tracer.open_run(context)
    observed_at, monotonic_ns = _observed_at()
    await session.observe(
        RunStartedObservation(
            identity=context.identity,
            observed_at=observed_at,
            monotonic_ns=monotonic_ns,
        )
    )
    observed_at, monotonic_ns = _observed_at()
    await session.observe(
        RunInputObservation(
            identity=context.identity,
            source=context,
            observed_at=observed_at,
            monotonic_ns=monotonic_ns,
        )
    )
    observed_at, monotonic_ns = _observed_at()
    await session.observe(
        ToolExecutionObservation(
            identity=context.identity,
            phase="started",
            execution_id="disabled-read",
            tool_call_id="call-disabled-read",
            tool_name="read_file",
            input={"file_path": "/skills/research/SKILL.md"},
            observed_at=observed_at,
            monotonic_ns=monotonic_ns,
        )
    )
    observed_at, monotonic_ns = _observed_at()
    await session.observe(
        ToolExecutionObservation(
            identity=context.identity,
            phase="completed",
            execution_id="disabled-read",
            tool_call_id="call-disabled-read",
            tool_name="read_file",
            output="instructions",
            observed_at=observed_at,
            monotonic_ns=monotonic_ns,
        )
    )
    observed_at, monotonic_ns = _observed_at()
    await session.observe(
        RunTerminalObservation(
            identity=context.identity,
            outcome="succeeded",
            observed_at=observed_at,
            monotonic_ns=monotonic_ns,
        )
    )
    observed_at, monotonic_ns = _observed_at()
    await session.observe(
        RunClosedObservation(
            identity=context.identity,
            outcome="succeeded",
            observed_at=observed_at,
            monotonic_ns=monotonic_ns,
        )
    )
    await session.aclose()

    query = await tracer.query(
        "thread-entry-query",
        where=TraceFilter(kinds={TraceEntryKind.TOOL, TraceEntryKind.SKILL}),
    )
    thread = await tracer.get("thread-entry-query")
    events = (await thread.events(limit=100)).items

    assert query.items == ()
    assert not any(
        isinstance(event.fact, (ToolExecutionFact, SkillFact)) for event in events
    )
