"""Keep ordinary history usable with a Ledger-only storage integration."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tinkerfin_contracts import RunIdentity
from tinkerfin_tracing import (
    DurableTraceStore,
    RunFact,
    Tracer,
    TraceStoreProtocolError,
    TurnFact,
)
from tinkerfin_tracing.durable_store import _InMemoryTraceLedgerBackend


class _LedgerOnly:
    def __init__(self) -> None:
        backend = _InMemoryTraceLedgerBackend()
        self._implementation = backend
        self.prepare_storage = backend.prepare_storage
        self.commit_ledger_change = backend.commit_ledger_change
        self.load_ledger_state = backend.load_ledger_state
        self.read_event_page = backend.read_event_page
        self.load_projection_checkpoint = backend.load_projection_checkpoint


class _Queryable(_LedgerOnly):
    def __init__(self) -> None:
        super().__init__()
        self.query_trace_graph = self._implementation.query_trace_graph


class _Rebuildable(_Queryable):
    def __init__(self) -> None:
        super().__init__()
        self.rebuild_trace_graph = self._implementation.rebuild_trace_graph


async def _seed(store: DurableTraceStore) -> RunIdentity:
    identity = RunIdentity(namespace="user", thread_id="thread", run_id="run")
    now = datetime.now(UTC)
    writer = await store.open_writer(identity)
    try:
        await writer.append(
            (
                RunFact(
                    source_observation_id="start",
                    identity=identity,
                    occurred_at=now,
                    monotonic_ns=1,
                    phase="started",
                    input_kind="ordinary",
                ),
                TurnFact(
                    source_observation_id="turn",
                    identity=identity,
                    occurred_at=now,
                    monotonic_ns=2,
                    turn_id="turn",
                ),
            ),
        )
    finally:
        await writer.aclose()
    return identity


async def test_ledger_only_history_uses_recorded_facts() -> None:
    store = DurableTraceStore(_LedgerOnly())
    identity = await _seed(store)
    history = await Tracer(store=store).get(identity.thread)
    assert history.head_run_id == identity.run_id
    assert history.graph.as_of_seq == 2


@pytest.mark.parametrize(
    "backend_type,queries,rebuild",
    [
        (_LedgerOnly, False, False),
        (_Queryable, True, False),
        (_Rebuildable, True, True),
    ],
)
async def test_graph_capabilities_match_backend_and_explicit_operations(
    backend_type, queries, rebuild
) -> None:
    store = DurableTraceStore(backend_type())
    assert store.supports_graph_queries is queries
    assert store.supports_graph_rebuild is rebuild
    identity = await _seed(store)
    tracer = Tracer(store=store)
    history = await tracer.get(identity.thread)
    assert history.head_run_id == identity.run_id
    if queries:
        await tracer.query(identity.thread)
    else:
        with pytest.raises(TraceStoreProtocolError, match="indexed Graph queries"):
            await tracer.query(identity.thread)
    if rebuild:
        assert await tracer.rebuild_graph(identity.thread) == len(history.graph.nodes)
    else:
        with pytest.raises(TraceStoreProtocolError, match="rebuild"):
            await tracer.rebuild_graph(identity.thread)


async def test_supported_graph_failure_is_not_hidden_by_history_reconstruction() -> (
    None
):
    backend = _Queryable()
    failure = TraceStoreProtocolError("Graph index is corrupt")

    async def broken(request):
        raise failure

    backend.query_trace_graph = broken
    store = DurableTraceStore(backend)
    identity = await _seed(store)
    with pytest.raises(TraceStoreProtocolError) as caught:
        await Tracer(store=store).get(identity.thread)
    assert caught.value is failure
