"""Framework-owned Trace Store lifecycle over a five-operation Ledger backend."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from tinkerfin_contracts import RunIdentity

from ._entry_index import entry_mutations
from ._prepared import PreparedTraceFact, prepare_trace_facts
from .backend import (
    StoredTraceCheckpoint,
    StoredTraceEntry,
    StoredTraceEntryPage,
    StoredTraceEvent,
    StoredTraceEventPage,
    TraceCheckpointRequest,
    TraceEntryMutation,
    TraceEntryQueryRequest,
    TraceEntryRebuildBackend,
    TraceEntryRebuildRequest,
    TraceEventPageRequest,
    TraceLedgerBackend,
    TraceLedgerChange,
    TraceLedgerCommitResult,
    TraceLedgerState,
    TraceLedgerStateRequest,
    TraceLedgerStorageEffect,
    TraceLedgerThreadState,
    TraceLedgerWriterState,
    TraceQueryBackend,
    TraceStoreOptions,
    resolve_ledger_change,
)
from .codec import CanonicalTracePayloadCodec
from .entries import TraceEntryKind, TraceEntryStatus, TraceFacets, TraceFilter
from .errors import (
    TraceStoreProtocolError,
    TraceThreadNotFound,
)
from .facts import TraceEvent, TraceSemanticFact
from .limits import TraceLimits
from .store import (
    StoreThreadSnapshot,
    StoreWriterSnapshot,
    TraceEntryRecord,
    TraceEntryRecordPage,
    TraceProjectionCheckpoint,
    TraceThreadKey,
    TraceWriter,
    _checkpoint_lookup,
    _event,
)


async def _join_retained_task(task: asyncio.Task[None]) -> None:
    """Wait for one retained lifecycle task across repeated caller cancellation."""

    current = asyncio.current_task()
    cancel_count = current.cancelling() if current is not None else 0
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            next_count = current.cancelling() if current is not None else 0
            if next_count > cancel_count or (cancellation is None and next_count > 0):
                cancellation = cancellation or error
                cancel_count = next_count
                continue
            if task.done():
                break
            raise
        except BaseException:  # noqa: BLE001 - inspect the retained result below
            break

    task_error: BaseException | None = None
    try:
        task.result()
    except BaseException as error:  # noqa: BLE001 - preserve exact task result
        task_error = error
    if cancellation is not None:
        if task_error is not None:
            cancellation.add_note(
                "retained Trace lifecycle task also failed: "
                f"{type(task_error).__module__}.{type(task_error).__qualname__}"
            )
        raise cancellation.with_traceback(cancellation.__traceback__)
    if task_error is not None:
        raise task_error.with_traceback(task_error.__traceback__)


class _DurableTraceWriter:
    """Own one backend-fenced Run writer and its framework heartbeat task."""

    def __init__(
        self,
        store: DurableTraceStore,
        *,
        key: TraceThreadKey,
        run_id: str,
        owner_token: str,
        fence: int,
    ) -> None:
        self._store = store
        self._key = key
        self._run_id = run_id
        self._owner_token = owner_token
        self._fence = fence
        self._closed = False
        self._failure: BaseException | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._mutation_lock = asyncio.Lock()
        self._heartbeat: asyncio.Task[None] | None = None
        if self._store._enforce_writer_leases:
            self._heartbeat = asyncio.create_task(
                self._heartbeat_forever(),
                name=f"tinkerfin-trace-ledger-heartbeat:{run_id}",
            )
            self._heartbeat.add_done_callback(_consume_task_exception)

    @property
    def key(self) -> TraceThreadKey:
        """Return the exact generation protected by this writer."""

        return self._key

    @property
    def run_id(self) -> str:
        """Return the semantic Run protected by this writer."""

        return self._run_id

    async def append(
        self,
        facts: tuple[TraceSemanticFact, ...],
        *,
        mandatory: bool = False,
    ) -> tuple[TraceEvent, ...]:
        """Canonically prepare and atomically append one ordered fact batch."""

        if not facts:
            raise ValueError("facts must not be empty")
        if self._closed:
            raise TraceStoreProtocolError("Trace writer is closed")
        if self._failure is not None:
            raise TraceStoreProtocolError(
                "Trace writer heartbeat failed",
                cause=self._failure,
            )
        prepared = prepare_trace_facts(facts, codec=self._store._codec)
        return await self._append_prepared(prepared, mandatory=mandatory)

    async def _append_prepared(
        self,
        prepared: tuple[PreparedTraceFact, ...],
        *,
        mandatory: bool,
    ) -> tuple[TraceEvent, ...]:
        """Append using canonical evidence already computed before queue admission."""

        if self._closed:
            raise TraceStoreProtocolError("Trace writer is closed")
        if self._failure is not None:
            raise TraceStoreProtocolError(
                "Trace writer heartbeat failed",
                cause=self._failure,
            )
        async with self._mutation_lock:
            result = await self._store._commit_ledger_change(
                TraceLedgerChange(
                    kind="append_events",
                    namespace=self._store.namespace,
                    limits=self._store.limits,
                    options=self._store.options,
                    key=self._key,
                    run_id=self._run_id,
                    owner_token=self._owner_token,
                    fence=self._fence,
                    facts=prepared,
                    mandatory=mandatory,
                    enforce_writer_lease=self._store._enforce_writer_leases,
                    persist_canonical_event_records=(
                        self._store._enforce_writer_leases
                    ),
                )
            )
        return result.events

    async def aclose(self) -> None:
        """Settle heartbeat and owned persistence exactly once under cancellation."""

        task = self._close_task
        if task is None:
            task = asyncio.create_task(
                self._close_once(),
                name=f"tinkerfin-trace-ledger-close:{self._run_id}",
            )
            task.add_done_callback(_consume_task_exception)
            self._close_task = task
        await _join_retained_task(task)

    async def _heartbeat_forever(self) -> None:
        try:
            while True:
                await asyncio.sleep(
                    self._store.options.writer_heartbeat_interval_seconds
                )
                async with self._mutation_lock:
                    await self._store._commit_ledger_change(
                        TraceLedgerChange(
                            kind="renew_writer",
                            namespace=self._store.namespace,
                            limits=self._store.limits,
                            options=self._store.options,
                            key=self._key,
                            run_id=self._run_id,
                            owner_token=self._owner_token,
                            fence=self._fence,
                        )
                    )
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - writer observes background health
            self._failure = error

    async def _close_once(self) -> None:
        self._closed = True
        heartbeat = self._heartbeat
        if heartbeat is not None:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        async with self._mutation_lock:
            await self._store._commit_ledger_change(
                TraceLedgerChange(
                    kind="close_writer",
                    namespace=self._store.namespace,
                    limits=self._store.limits,
                    options=self._store.options,
                    key=self._key,
                    run_id=self._run_id,
                    owner_token=self._owner_token,
                    fence=self._fence,
                    enforce_writer_lease=self._store._enforce_writer_leases,
                )
            )


class DurableTraceStore:
    """Provide the complete Trace Store contract over borrowed durable storage.

    The framework owns writer objects, heartbeats, canonical payload validation,
    following, and every semantic Ledger transition. The supplied backend owns only
    storage preparation, atomic effects, and consistent raw reads. The Store never
    closes the backend or its database client.

    Args:
        backend: Borrowed five-operation shared Ledger backend.
        namespace: Stable logical isolation key shared by cooperating Store instances.
        limits: Ledger, event, thread, reserve, and follow capacity limits.
        options: Writer lease, heartbeat, polling, and commit retry settings.
        codec: Canonical fact and Projection-state codec.

    Raises:
        TypeError: An argument has the wrong public type.
        ValueError: The namespace or options are invalid.
    """

    def __init__(
        self,
        backend: TraceLedgerBackend,
        *,
        namespace: str = "default",
        limits: TraceLimits | None = None,
        options: TraceStoreOptions | None = None,
        codec: CanonicalTracePayloadCodec | None = None,
    ) -> None:
        """Initialize a Store over a borrowed durable Backend.

        Args:
            backend: Borrowed shared Ledger Backend.
            namespace: Stable logical isolation key.
            limits: Optional capacity limits.
            options: Optional writer and retry settings.
            codec: Optional canonical payload codec.
        """

        if not isinstance(backend, TraceLedgerBackend):
            raise TypeError("backend must implement TraceLedgerBackend")
        if (
            not isinstance(namespace, str)
            or not namespace
            or namespace != namespace.strip()
            or len(namespace) > 2048
        ):
            raise ValueError("namespace must be canonical text of at most 2048 chars")
        if limits is not None and not isinstance(limits, TraceLimits):
            raise TypeError("limits must be a TraceLimits or None")
        if options is not None and not isinstance(options, TraceStoreOptions):
            raise TypeError("options must be a TraceStoreOptions or None")
        if codec is not None and not isinstance(codec, CanonicalTracePayloadCodec):
            raise TypeError("codec must be a CanonicalTracePayloadCodec or None")
        self._backend = backend
        self._namespace = namespace
        self._limits = limits or TraceLimits()
        self._options = options or TraceStoreOptions()
        self._codec = codec or CanonicalTracePayloadCodec()
        self._setup_lock = asyncio.Lock()
        self._setup_task: asyncio.Task[None] | None = None
        self._local_change = asyncio.Condition()
        self._enforce_writer_leases = True

    @property
    def namespace(self) -> str:
        """Return the immutable logical Store namespace."""

        return self._namespace

    @property
    def limits(self) -> TraceLimits:
        """Return the immutable capacity limits enforced by the Store."""

        return self._limits

    @property
    def options(self) -> TraceStoreOptions:
        """Return immutable writer, follow, and retry settings."""

        return self._options

    @property
    def backend(self) -> TraceLedgerBackend:
        """Return the borrowed Ledger backend without transferring ownership."""

        return self._backend

    async def setup(self) -> None:
        """Prepare storage once and settle retained I/O before cancellation returns."""

        async with self._setup_lock:
            task = self._setup_task
            if (
                task is not None
                and task.done()
                and (task.cancelled() or task.exception() is not None)
            ):
                self._setup_task = None
                task = None
            if task is None:
                task = asyncio.create_task(
                    self._backend.prepare_storage(),
                    name="tinkerfin-trace-prepare-storage",
                )
                task.add_done_callback(_consume_task_exception)
                self._setup_task = task
        await _join_retained_task(task)

    async def open_writer(self, identity: RunIdentity) -> TraceWriter:
        """Open one exclusive backend-fenced writer for a semantic Run."""

        if not isinstance(identity, RunIdentity):
            raise TypeError("identity must be a RunIdentity")
        owner_token = uuid4().hex
        result = await self._commit_ledger_change(
            TraceLedgerChange(
                kind="open_writer",
                namespace=self._namespace,
                limits=self._limits,
                options=self._options,
                identity=identity,
                owner_token=owner_token,
                enforce_writer_lease=self._enforce_writer_leases,
            )
        )
        if result.key is None or result.fence is None:
            raise TraceStoreProtocolError(
                "Trace Ledger backend returned an invalid writer result"
            )
        return _DurableTraceWriter(
            self,
            key=result.key,
            run_id=identity.run_id,
            owner_token=owner_token,
            fence=result.fence,
        )

    async def snapshot(self, thread_id: str) -> StoreThreadSnapshot:
        """Return one consistent prefix for the current generation."""

        _validate_thread_id(thread_id)
        await self.setup()
        state = await self._backend.load_ledger_state(
            TraceLedgerStateRequest(
                namespace=self._namespace,
                thread_id=thread_id,
                include_active_writers=True,
            )
        )
        return _snapshot_from_state(
            state,
            expected_namespace=self._namespace,
            expected_thread_id=thread_id,
            expected_key=None,
        )

    async def snapshot_key(self, key: TraceThreadKey) -> StoreThreadSnapshot:
        """Return one consistent prefix for an exact generation."""

        self._validate_key(key)
        await self.setup()
        state = await self._backend.load_ledger_state(
            TraceLedgerStateRequest(
                namespace=self._namespace,
                thread_id=key.thread_id,
                generation=key.generation,
                include_active_writers=True,
            )
        )
        return _snapshot_from_state(
            state,
            expected_namespace=self._namespace,
            expected_thread_id=key.thread_id,
            expected_key=key,
        )

    async def read_events(
        self,
        key: TraceThreadKey,
        *,
        after_seq: int,
        as_of_seq: int,
        limit: int,
    ) -> tuple[TraceEvent, ...]:
        """Read and verify one bounded ascending fixed-prefix event page."""

        self._validate_key(key)
        if after_seq < 0 or as_of_seq < 0 or limit < 1:
            raise ValueError("event cursor values must be non-negative and bounded")
        await self.setup()
        request = TraceEventPageRequest(
            key=key,
            direction="forward",
            after_seq=after_seq,
            as_of_seq=as_of_seq,
            limit=limit,
        )
        page = await self._backend.read_event_page(request)
        return self._decode_event_page(page, expected_request=request)

    async def read_events_reverse(
        self,
        key: TraceThreadKey,
        *,
        before_seq: int,
        limit: int,
    ) -> tuple[TraceEvent, ...]:
        """Read and verify one bounded newest-first event page."""

        self._validate_key(key)
        if before_seq < 1 or limit < 1:
            raise ValueError("reverse event cursor values must be positive")
        await self.setup()
        request = TraceEventPageRequest(
            key=key,
            direction="reverse",
            before_seq=before_seq,
            limit=limit,
        )
        page = await self._backend.read_event_page(request)
        events = self._decode_event_page(page, expected_request=request)
        if before_seq > page.tail_seq + 1:
            raise ValueError("before_seq exceeds the current Trace tail")
        return events

    async def query_trace_entries(
        self,
        key: TraceThreadKey,
        *,
        run_ids: tuple[str, ...],
        where: TraceFilter,
        limit: int,
        before_started_at: datetime | None = None,
        before_entry_id: str | None = None,
    ) -> TraceEntryRecordPage:
        """Run one direct backend query and decode only referenced Ledger events."""

        self._validate_key(key)
        if not isinstance(where, TraceFilter):
            raise TypeError("where must be a TraceFilter")
        if limit < 1:
            raise ValueError("entry page limit must be positive")
        if (before_started_at is None) != (before_entry_id is None):
            raise ValueError("entry cursor time and ID must be supplied together")
        if before_started_at is not None and (
            before_started_at.tzinfo is None
            or before_started_at.utcoffset() != UTC.utcoffset(before_started_at)
        ):
            raise ValueError("entry cursor time must be aware UTC")
        backend = self._backend
        if not isinstance(backend, TraceQueryBackend):
            raise TraceStoreProtocolError(
                "Trace Store backend does not provide indexed entry queries"
            )
        await self.setup()
        stored = await backend.query_trace_entries(
            TraceEntryQueryRequest(
                key=key,
                run_ids=run_ids,
                where=where,
                limit=limit,
                before_started_at=before_started_at,
                before_entry_id=before_entry_id,
            )
        )
        if stored.key != key:
            raise TraceStoreProtocolError(
                "Trace entry page belongs to another generation"
            )
        return TraceEntryRecordPage(
            key=stored.key,
            as_of_seq=stored.as_of_seq,
            entries=tuple(
                TraceEntryRecord(
                    entry_id=item.entry_id,
                    parent_id=item.parent_id,
                    kind=item.kind,
                    status=item.status,
                    name=item.name,
                    run_id=item.run_id,
                    namespace=item.namespace,
                    agent_name=item.agent_name,
                    provider=item.provider,
                    model=item.model,
                    started_at=item.started_at,
                    first_output_at=item.first_output_at,
                    completed_at=item.completed_at,
                    started_seq=item.started_seq,
                    updated_seq=item.updated_seq,
                    started_event=self._decode_entry_event(
                        item.started_event,
                        key=key,
                        expected_seq=item.started_seq,
                    ),
                    updated_event=self._decode_entry_event(
                        item.updated_event,
                        key=key,
                        expected_seq=item.updated_seq,
                    ),
                )
                for item in stored.entries
            ),
            facets=stored.facets.model_copy(deep=True),
            has_more=stored.has_more,
            next_started_at=stored.next_started_at,
            next_entry_id=stored.next_entry_id,
            call_tracking_present=stored.call_tracking_present,
        )

    async def rebuild_trace_entries(self, key: TraceThreadKey) -> int:
        """Reconstruct one disposable entry index from its authoritative Ledger.

        Args:
            key: Exact generation to read and rebuild.

        Returns:
            Number of reconstructed entry rows.

        Raises:
            TraceThreadNotFound: The exact generation is unavailable.
            TraceStoreProtocolError: The Backend lacks reconstruction support, the
                Ledger has a gap, or its tail changes during replacement.
            TraceStoreError: A bounded Ledger read or atomic replacement fails.
        """

        self._validate_key(key)
        backend = self._backend
        if not isinstance(backend, TraceEntryRebuildBackend):
            raise TraceStoreProtocolError(
                "Trace Store backend does not rebuild indexed entries"
            )
        snapshot = await self.snapshot_key(key)
        mutations: list[TraceEntryMutation] = []
        after_seq = 0
        while after_seq < snapshot.as_of_seq:
            page = await self.read_events(
                key,
                after_seq=after_seq,
                as_of_seq=snapshot.as_of_seq,
                limit=self._limits.follow_batch_size,
            )
            if not page:
                raise TraceStoreProtocolError(
                    "Trace Ledger ended before the rebuild prefix"
                )
            mutations.extend(entry_mutations(page))
            after_seq = page[-1].trace_seq
        # Historical Ledgers can predate a semantic parent fact. Because this operation
        # replaces the whole derived index, only parents rebuilt from the same fixed
        # prefix can remain; unknown ancestry stays at the root instead of becoming an
        # orphaned relationship.
        rebuilt_entry_ids = {
            mutation.entry_id
            for mutation in mutations
            if mutation.started_seq is not None
        }
        mutations = [
            replace(mutation, parent_id=None)
            if mutation.started_seq is not None
            and mutation.parent_id is not None
            and mutation.parent_id not in rebuilt_entry_ids
            else mutation
            for mutation in mutations
        ]
        return await backend.rebuild_trace_entries(
            TraceEntryRebuildRequest(
                key=key,
                as_of_seq=snapshot.as_of_seq,
                mutations=tuple(mutations),
            )
        )

    async def load_projection_checkpoint(
        self,
        key: TraceThreadKey,
        *,
        projection_name: str,
        run_id: str | None,
        as_of_seq: int,
    ) -> TraceProjectionCheckpoint | None:
        """Load and verify the newest checkpoint inside a fixed prefix."""

        self._validate_key(key)
        _checkpoint_lookup(
            key,
            projection_name=projection_name,
            run_id=run_id,
            as_of_seq=as_of_seq,
        )
        await self.setup()
        request = TraceCheckpointRequest(
            key=key,
            projection_name=projection_name,
            run_id=run_id,
            as_of_seq=as_of_seq,
        )
        stored = await self._backend.load_projection_checkpoint(request)
        if stored is None:
            return None
        return self._decode_checkpoint(stored, expected_request=request)

    async def save_projection_checkpoint(
        self,
        checkpoint: TraceProjectionCheckpoint,
        *,
        expected_as_of_seq: int | None,
    ) -> TraceProjectionCheckpoint:
        """Atomically advance one canonical Projection checkpoint."""

        if not isinstance(checkpoint, TraceProjectionCheckpoint):
            raise TypeError("checkpoint must be a TraceProjectionCheckpoint")
        self._validate_key(checkpoint.key)
        if expected_as_of_seq is not None and expected_as_of_seq < 0:
            raise ValueError("expected_as_of_seq must be non-negative or None")
        stored_checkpoint = checkpoint.model_copy(deep=True)
        encoded = self._codec.encode_json(stored_checkpoint.state)
        result = await self._commit_ledger_change(
            TraceLedgerChange(
                kind="save_projection_checkpoint",
                namespace=self._namespace,
                limits=self._limits,
                options=self._options,
                key=stored_checkpoint.key,
                checkpoint=stored_checkpoint,
                canonical_checkpoint_state=encoded.data,
                checkpoint_state_digest=encoded.digest,
                expected_checkpoint_as_of_seq=expected_as_of_seq,
            )
        )
        if result.checkpoint is None:
            raise TraceStoreProtocolError(
                "Trace Ledger backend returned an invalid checkpoint result"
            )
        return result.checkpoint.model_copy(deep=True)

    def follow(
        self,
        key: TraceThreadKey,
        *,
        after_seq: int,
    ) -> AsyncGenerator[tuple[TraceEvent, ...], None]:
        """Follow bounded committed pages with local wakeups and polling fallback."""

        self._validate_key(key)
        if after_seq < 0:
            raise ValueError("after_seq must be non-negative")

        async def iterate() -> AsyncGenerator[tuple[TraceEvent, ...], None]:
            cursor = after_seq
            while True:
                snapshot = await self.snapshot_key(key)
                batch = await self.read_events(
                    key,
                    after_seq=cursor,
                    as_of_seq=snapshot.as_of_seq,
                    limit=self._limits.follow_batch_size,
                )
                if batch:
                    cursor = batch[-1].trace_seq
                    yield batch
                    continue
                async with self._local_change:
                    try:
                        await asyncio.wait_for(
                            self._local_change.wait(),
                            timeout=self._options.follow_poll_seconds,
                        )
                    except TimeoutError:
                        pass

        return iterate()

    async def delete(self, key: TraceThreadKey) -> None:
        """Delete one exact inactive generation through the atomic change boundary."""

        self._validate_key(key)
        await self._commit_ledger_change(
            TraceLedgerChange(
                kind="delete_generation",
                namespace=self._namespace,
                limits=self._limits,
                options=self._options,
                key=key,
            )
        )

    async def _commit_ledger_change(
        self,
        change: TraceLedgerChange,
    ) -> TraceLedgerCommitResult:
        await self.setup()
        result = await self._backend.commit_ledger_change(change)
        if (
            not isinstance(result, TraceLedgerCommitResult)
            or result.kind != change.kind
        ):
            raise TraceStoreProtocolError(
                "Trace Ledger backend returned an invalid commit result"
            )
        _validate_commit_result(change, result)
        async with self._local_change:
            self._local_change.notify_all()
        return result

    def _decode_event_page(
        self,
        page: StoredTraceEventPage,
        *,
        expected_request: TraceEventPageRequest,
    ) -> tuple[TraceEvent, ...]:
        if page.key != expected_request.key:
            raise TraceStoreProtocolError(
                "Trace event page does not match the requested generation"
            )
        records = page.events
        if len(records) > expected_request.limit:
            raise TraceStoreProtocolError(
                "Trace event page exceeds its requested limit"
            )
        sequences = tuple(record.trace_seq for record in records)
        if expected_request.direction == "forward":
            after = (
                0 if expected_request.after_seq is None else expected_request.after_seq
            )
            as_of = (
                page.tail_seq
                if expected_request.as_of_seq is None
                else expected_request.as_of_seq
            )
            invalid = any(
                sequence <= after or sequence > as_of or sequence > page.tail_seq
                for sequence in sequences
            ) or any(left >= right for left, right in zip(sequences, sequences[1:]))
        else:
            before = (
                page.tail_seq + 1
                if expected_request.before_seq is None
                else expected_request.before_seq
            )
            invalid = any(
                sequence < 1 or sequence >= before or sequence > page.tail_seq
                for sequence in sequences
            ) or any(left <= right for left, right in zip(sequences, sequences[1:]))
        if invalid:
            raise TraceStoreProtocolError(
                "Trace event page violates requested ordering"
            )
        return tuple(
            self._decode_event(expected_request.key, record) for record in records
        )

    def _decode_event(
        self,
        key: TraceThreadKey,
        record: StoredTraceEvent,
    ) -> TraceEvent:
        if isinstance(self._backend, _InMemoryTraceLedgerBackend) and isinstance(
            record, _MemoryStoredTraceEvent
        ):
            if self._codec.digest(record.canonical_payload) != record.payload_digest:
                raise TraceStoreProtocolError("Trace event payload digest mismatch")
            cached = record.validated_event
            if (
                cached.event_id != record.event_id
                or cached.trace_seq != record.trace_seq
                or cached.generation != key.generation
                or cached.persisted_bytes != record.persisted_bytes
                or cached.fact.identity.thread_id != key.thread_id
                or cached.fact.identity.run_id != record.run_id
                or cached.fact.kind != record.fact_kind
                or cached.fact.occurred_at != record.occurred_at
            ):
                raise TraceStoreProtocolError(
                    "In-memory Trace event evidence conflicts"
                )
            return cached.model_copy(deep=True)
        if self._codec.digest(record.canonical_payload) != record.payload_digest:
            raise TraceStoreProtocolError("Trace event payload digest mismatch")
        fact = self._codec.decode_fact(record.canonical_payload)
        if (
            fact.identity.thread_id != key.thread_id
            or fact.identity.run_id != record.run_id
            or fact.kind != record.fact_kind
            or fact.occurred_at != record.occurred_at
        ):
            raise TraceStoreProtocolError(
                "Trace event searchable metadata conflicts with its payload"
            )
        event = _event(
            event_id=record.event_id,
            trace_seq=record.trace_seq,
            generation=key.generation,
            fact=fact,
        )
        if event.persisted_bytes != record.persisted_bytes:
            raise TraceStoreProtocolError("Trace event size metadata conflicts")
        return event

    def _decode_entry_event(
        self,
        record: StoredTraceEvent,
        *,
        key: TraceThreadKey,
        expected_seq: int,
    ) -> TraceEvent:
        if record.trace_seq != expected_seq:
            raise TraceStoreProtocolError(
                "Trace entry references a different Ledger sequence"
            )
        return self._decode_event(key, record)

    def _decode_checkpoint(
        self,
        stored: StoredTraceCheckpoint,
        *,
        expected_request: TraceCheckpointRequest,
    ) -> TraceProjectionCheckpoint:
        if (
            stored.key != expected_request.key
            or stored.projection_name != expected_request.projection_name
            or stored.run_id != expected_request.run_id
            or stored.as_of_seq > expected_request.as_of_seq
        ):
            raise TraceStoreProtocolError(
                "Projection checkpoint does not match the requested scope"
            )
        if self._codec.digest(stored.canonical_state) != stored.state_digest:
            raise TraceStoreProtocolError("Projection checkpoint digest mismatch")
        return TraceProjectionCheckpoint(
            key=stored.key,
            projection_name=stored.projection_name,
            run_id=stored.run_id,
            as_of_seq=stored.as_of_seq,
            state=self._codec.decode_json(stored.canonical_state),
        )

    def _validate_key(self, key: TraceThreadKey) -> None:
        if not isinstance(key, TraceThreadKey):
            raise TypeError("key must be a TraceThreadKey")
        if key.namespace != self._namespace:
            raise TraceThreadNotFound("Trace namespace does not exist")


@dataclass(slots=True)
class _MemoryTraceEntry:
    entry_id: str
    parent_id: str | None
    kind: TraceEntryKind
    status: TraceEntryStatus
    name: str
    run_id: str
    namespace: tuple[str, ...]
    agent_name: str | None
    provider: str | None
    model: str | None
    started_at: datetime
    first_output_at: datetime | None
    completed_at: datetime | None
    started_seq: int
    updated_seq: int


@dataclass(slots=True)
class _MemoryThread:
    state: TraceLedgerThreadState
    writers: dict[str, TraceLedgerWriterState] = field(
        default_factory=lambda: dict[str, TraceLedgerWriterState]()
    )
    events: list[_MemoryStoredTraceEvent] = field(
        default_factory=lambda: list[_MemoryStoredTraceEvent]()
    )
    entries: dict[str, _MemoryTraceEntry] = field(
        default_factory=lambda: dict[str, _MemoryTraceEntry]()
    )
    checkpoints: dict[
        tuple[str, str | None],
        list[StoredTraceCheckpoint],
    ] = field(
        default_factory=lambda: dict[
            tuple[str, str | None],
            list[StoredTraceCheckpoint],
        ]()
    )


@dataclass(frozen=True, slots=True)
class _MemoryStoredTraceEvent(StoredTraceEvent):
    """Retain framework-validated event evidence for process-local reads."""

    validated_event: TraceEvent


class _InMemoryTraceLedgerBackend:
    """Apply the public backend contract to bounded process-local raw records."""

    def __init__(self) -> None:
        self._threads: dict[tuple[str, str], _MemoryThread] = {}
        self._condition = asyncio.Condition()

    def _shared_peer(self) -> _InMemoryTraceLedgerBackend:
        """Create an independent Backend value over the same process-local storage."""

        peer = _InMemoryTraceLedgerBackend()
        peer._threads = self._threads
        peer._condition = self._condition
        return peer

    async def prepare_storage(self) -> None:
        """Require no preparation for process-local memory."""

    async def commit_ledger_change(
        self,
        change: TraceLedgerChange,
    ) -> TraceLedgerCommitResult:
        """Resolve and apply one complete effect under the backend condition."""

        async with self._condition:
            state = self._state_for_change(change)
            effect = resolve_ledger_change(change, state)
            self._apply_effect(change, effect)
            self._condition.notify_all()
            return effect.result

    async def load_ledger_state(
        self,
        request: TraceLedgerStateRequest,
    ) -> TraceLedgerState:
        """Return a defensive consistent process-local state snapshot."""

        async with self._condition:
            return self._state(
                namespace=request.namespace,
                thread_id=request.thread_id,
                run_id=request.run_id,
                checkpoint_request=None,
                include_active_writers=request.include_active_writers,
            )

    async def read_event_page(
        self,
        request: TraceEventPageRequest,
    ) -> StoredTraceEventPage:
        """Return one bounded raw page from an exact generation."""

        async with self._condition:
            thread = self._threads.get((request.key.namespace, request.key.thread_id))
            if thread is None or thread.state.key != request.key:
                raise TraceThreadNotFound("Trace generation does not exist")
            tail = thread.state.next_seq - 1
            if request.direction == "forward":
                after = 0 if request.after_seq is None else request.after_seq
                as_of = tail if request.as_of_seq is None else request.as_of_seq
                start = min(after, tail)
                stop = min(as_of, tail, start + request.limit)
                records = tuple(thread.events[start:stop])
            else:
                before = tail + 1 if request.before_seq is None else request.before_seq
                if before > tail + 1:
                    records = ()
                else:
                    stop = before - 1
                    start = max(0, stop - request.limit)
                    records = tuple(reversed(thread.events[start:stop]))
            return StoredTraceEventPage(
                key=request.key,
                tail_seq=tail,
                events=records,
            )

    async def query_trace_entries(
        self,
        request: TraceEntryQueryRequest,
    ) -> StoredTraceEntryPage:
        """Filter the process-local index without decoding Ledger payloads."""

        async with self._condition:
            thread = self._threads.get((request.key.namespace, request.key.thread_id))
            if thread is None or thread.state.key != request.key:
                raise TraceThreadNotFound("Trace generation does not exist")
            tail = thread.state.next_seq - 1
            run_ids = frozenset(request.run_ids)
            candidates = [
                row for row in thread.entries.values() if row.run_id in run_ids
            ]
            matching = [row for row in candidates if _entry_matches(row, request.where)]
            matching.sort(
                key=lambda row: (row.started_at, _entry_order_key(row.entry_id)),
                reverse=True,
            )
            facets = _entry_facets(candidates, request.where)
            if request.before_started_at is not None:
                if request.before_entry_id is None:
                    raise ValueError("entry cursor requires an entry ID")
                cursor = (
                    request.before_started_at,
                    _entry_order_key(request.before_entry_id),
                )
                matching = [
                    row
                    for row in matching
                    if (row.started_at, _entry_order_key(row.entry_id)) < cursor
                ]
            has_more = len(matching) > request.limit
            selected = matching[: request.limit]
            cursor_row = selected[-1] if has_more and selected else None
            result_rows: dict[str, _MemoryTraceEntry] = {
                row.entry_id: row for row in selected
            }
            if request.where.include_ancestors:
                pending = [
                    row.parent_id for row in selected if row.parent_id is not None
                ]
                while pending:
                    entry_id = pending.pop()
                    if entry_id in result_rows:
                        continue
                    parent = thread.entries.get(entry_id)
                    if parent is None or parent.run_id not in run_ids:
                        continue
                    result_rows[entry_id] = parent
                    if parent.parent_id is not None:
                        pending.append(parent.parent_id)
            tracked_runs = {
                event.run_id
                for event in thread.events
                if event.fact_kind == "call.tracking"
            }
            return StoredTraceEntryPage(
                key=request.key,
                as_of_seq=tail,
                entries=tuple(
                    _stored_memory_entry(thread, row)
                    for row in sorted(
                        result_rows.values(),
                        key=lambda item: (
                            item.started_at,
                            _entry_order_key(item.entry_id),
                        ),
                        reverse=True,
                    )
                ),
                facets=facets,
                has_more=has_more,
                next_started_at=(None if cursor_row is None else cursor_row.started_at),
                next_entry_id=None if cursor_row is None else cursor_row.entry_id,
                call_tracking_present=bool(run_ids) and run_ids <= tracked_runs,
            )

    async def rebuild_trace_entries(
        self,
        request: TraceEntryRebuildRequest,
    ) -> int:
        """Atomically replace process-local derived entries at one exact tail."""

        async with self._condition:
            thread = self._threads.get((request.key.namespace, request.key.thread_id))
            if thread is None or thread.state.key != request.key:
                raise TraceThreadNotFound("Trace generation does not exist")
            if thread.state.next_seq - 1 != request.as_of_seq:
                raise TraceStoreProtocolError(
                    "Trace Ledger changed while rebuilding entries"
                )
            rebuilt: dict[str, _MemoryTraceEntry] = {}
            current = thread.entries
            thread.entries = rebuilt
            try:
                for mutation in request.mutations:
                    _apply_memory_entry(thread, mutation)
            except BaseException:
                thread.entries = current
                raise
            self._condition.notify_all()
            return len(rebuilt)

    async def load_projection_checkpoint(
        self,
        request: TraceCheckpointRequest,
    ) -> StoredTraceCheckpoint | None:
        """Return the newest raw checkpoint inside one fixed prefix."""

        async with self._condition:
            thread = self._threads.get((request.key.namespace, request.key.thread_id))
            if thread is None or thread.state.key != request.key:
                raise TraceThreadNotFound("Trace generation does not exist")
            return next(
                (
                    item
                    for item in reversed(
                        thread.checkpoints.get(
                            (request.projection_name, request.run_id),
                            (),
                        )
                    )
                    if item.as_of_seq <= request.as_of_seq
                ),
                None,
            )

    def _state_for_change(self, change: TraceLedgerChange) -> TraceLedgerState:
        thread_id: str
        run_id = change.run_id
        if change.identity is not None:
            thread_id = change.identity.thread_id
            run_id = change.identity.run_id
        elif change.key is not None:
            thread_id = change.key.thread_id
        else:
            raise TraceStoreProtocolError("Trace Ledger change has no thread identity")
        checkpoint_request = None
        if change.checkpoint is not None:
            checkpoint_request = TraceCheckpointRequest(
                key=change.checkpoint.key,
                projection_name=change.checkpoint.projection_name,
                run_id=change.checkpoint.run_id,
                as_of_seq=change.checkpoint.as_of_seq,
            )
        return self._state(
            namespace=change.namespace,
            thread_id=thread_id,
            run_id=run_id,
            checkpoint_request=checkpoint_request,
            include_active_writers=change.kind == "delete_generation",
        )

    def _state(
        self,
        *,
        namespace: str,
        thread_id: str,
        run_id: str | None,
        checkpoint_request: TraceCheckpointRequest | None,
        include_active_writers: bool,
    ) -> TraceLedgerState:
        now = datetime.now(UTC)
        namespace_threads = tuple(
            thread
            for (stored_namespace, _thread_id), thread in self._threads.items()
            if stored_namespace == namespace
        )
        namespace_reserved = sum(
            writer.remaining_byte_reserve
            for thread in namespace_threads
            for writer in thread.writers.values()
            if writer.active
        )
        thread = self._threads.get((namespace, thread_id))
        writers = () if thread is None else tuple(thread.writers.values())
        target = (
            None if run_id is None or thread is None else thread.writers.get(run_id)
        )
        active = (
            tuple(
                StoreWriterSnapshot(
                    run_id=writer.run_id,
                    committed_events=writer.committed_events,
                )
                for writer in sorted(writers, key=lambda item: item.run_id)
                if writer.active and writer.lease_expires_at > now
            )
            if include_active_writers
            else ()
        )
        checkpoint = None
        if thread is not None and checkpoint_request is not None:
            checkpoint = next(
                reversed(
                    thread.checkpoints.get(
                        (
                            checkpoint_request.projection_name,
                            checkpoint_request.run_id,
                        ),
                        (),
                    )
                ),
                None,
            )
        return TraceLedgerState(
            observed_at=now,
            namespace_thread_count=len(namespace_threads),
            namespace_persisted_bytes=sum(
                item.state.persisted_bytes for item in namespace_threads
            ),
            namespace_reserved_bytes=namespace_reserved,
            thread=None if thread is None else thread.state,
            target_writer=target,
            writer_count=len(writers),
            thread_reserved_events=sum(
                writer.remaining_event_reserve for writer in writers if writer.active
            ),
            thread_reserved_bytes=sum(
                writer.remaining_byte_reserve for writer in writers if writer.active
            ),
            active_writers=active,
            current_checkpoint=checkpoint,
        )

    def _apply_effect(
        self,
        change: TraceLedgerChange,
        effect: TraceLedgerStorageEffect,
    ) -> None:
        key = effect.result.key or change.key
        if effect.delete_generation:
            if key is not None:
                self._threads.pop((key.namespace, key.thread_id), None)
            return
        if effect.remove_thread:
            if key is not None:
                self._threads.pop((key.namespace, key.thread_id), None)
            return
        if effect.thread is not None:
            storage_key = (
                effect.thread.key.namespace,
                effect.thread.key.thread_id,
            )
            thread = self._threads.get(storage_key)
            if thread is None:
                thread = _MemoryThread(state=effect.thread)
                self._threads[storage_key] = thread
            else:
                thread.state = effect.thread
        if key is None:
            return
        thread = self._threads.get((key.namespace, key.thread_id))
        if thread is None:
            return
        if effect.remove_writer_run_id is not None:
            thread.writers.pop(effect.remove_writer_run_id, None)
        if effect.writer is not None:
            thread.writers[effect.writer.run_id] = effect.writer
        if effect.validated_events:
            thread.events.extend(
                _memory_stored_event(
                    event,
                    prepared.canonical_payload,
                    prepared.payload_digest,
                )
                for event, prepared in zip(
                    effect.validated_events,
                    change.facts,
                    strict=True,
                )
            )
        for mutation in effect.entry_mutations:
            _apply_memory_entry(thread, mutation)
        if effect.checkpoint is not None:
            thread.checkpoints.setdefault(
                (effect.checkpoint.projection_name, effect.checkpoint.run_id),
                [],
            ).append(effect.checkpoint)


class InMemoryTraceStore(DurableTraceStore):
    """Bounded process-local Trace Store using the shared Ledger reducer."""

    def __init__(
        self,
        *,
        namespace: str = "default",
        limits: TraceLimits | None = None,
    ) -> None:
        """Initialize a bounded process-local Store.

        Args:
            namespace: Stable logical isolation key.
            limits: Optional capacity limits.
        """

        super().__init__(
            _InMemoryTraceLedgerBackend(),
            namespace=namespace,
            limits=limits,
        )
        self._enforce_writer_leases = False


def _memory_stored_event(
    event: TraceEvent,
    canonical_payload: bytes,
    payload_digest: str,
) -> _MemoryStoredTraceEvent:
    return _MemoryStoredTraceEvent(
        event_id=event.event_id,
        trace_seq=event.trace_seq,
        run_id=event.fact.identity.run_id,
        fact_kind=event.fact.kind,
        occurred_at=event.fact.occurred_at,
        canonical_payload=canonical_payload,
        payload_digest=payload_digest,
        persisted_bytes=event.persisted_bytes,
        validated_event=event,
    )


def _apply_memory_entry(
    thread: _MemoryThread,
    mutation: TraceEntryMutation,
) -> None:
    row = thread.entries.get(mutation.entry_id)
    if row is None:
        kind = mutation.kind
        status = mutation.status
        name = mutation.name
        run_id = mutation.run_id
        namespace = mutation.namespace
        started_at = mutation.started_at
        started_seq = mutation.started_seq
        if (
            kind is None
            or status is None
            or name is None
            or run_id is None
            or namespace is None
            or started_at is None
            or started_seq is None
        ):
            return
        row = _MemoryTraceEntry(
            entry_id=mutation.entry_id,
            parent_id=mutation.parent_id,
            kind=kind,
            status=status,
            name=name,
            run_id=run_id,
            namespace=namespace,
            agent_name=mutation.agent_name,
            provider=mutation.provider,
            model=mutation.model,
            started_at=started_at,
            first_output_at=mutation.first_output_at,
            completed_at=mutation.completed_at,
            started_seq=started_seq,
            updated_seq=mutation.updated_seq,
        )
        thread.entries[mutation.entry_id] = row
        return
    if mutation.updated_seq < row.updated_seq:
        raise TraceStoreProtocolError("Trace entry sequence moved backwards")
    if mutation.kind is not None and mutation.kind != row.kind:
        raise TraceStoreProtocolError("Trace entry kind changed")
    if mutation.name is not None and mutation.name != row.name:
        raise TraceStoreProtocolError("Trace entry name changed")
    if mutation.run_id is not None and mutation.run_id != row.run_id:
        raise TraceStoreProtocolError("Trace entry Run changed")
    if mutation.namespace is not None and mutation.namespace != row.namespace:
        raise TraceStoreProtocolError("Trace entry namespace changed")
    if mutation.started_seq is not None:
        if mutation.started_at is None:
            row.started_seq = mutation.started_seq
            row.updated_seq = mutation.updated_seq
        return
    if mutation.parent_id is not None:
        if row.parent_id is not None and mutation.parent_id != row.parent_id:
            raise TraceStoreProtocolError("Trace entry parent changed")
        row.parent_id = mutation.parent_id
    if mutation.status is not None:
        row.status = mutation.status
    if mutation.agent_name is not None:
        row.agent_name = mutation.agent_name
    if mutation.provider is not None:
        row.provider = mutation.provider
    if mutation.model is not None:
        row.model = mutation.model
    if mutation.first_output_at is not None:
        row.first_output_at = mutation.first_output_at
    if mutation.completed_at is not None:
        row.completed_at = mutation.completed_at
    row.updated_seq = mutation.updated_seq


def _entry_matches(
    row: _MemoryTraceEntry,
    where: TraceFilter,
    *,
    exclude: Literal[
        "kinds",
        "statuses",
        "agent_names",
        "middleware_names",
        "skill_names",
        "providers",
        "models",
    ]
    | None = None,
) -> bool:
    return not (
        (where.kinds and exclude != "kinds" and row.kind not in where.kinds)
        or (
            where.statuses
            and exclude != "statuses"
            and row.status not in where.statuses
        )
        or (where.parent_id is not None and row.parent_id != where.parent_id)
        or (
            where.agent_names
            and exclude != "agent_names"
            and row.agent_name not in where.agent_names
        )
        or (
            where.middleware_names
            and exclude != "middleware_names"
            and (
                row.kind is not TraceEntryKind.MIDDLEWARE
                or row.name not in where.middleware_names
            )
        )
        or (
            where.skill_names
            and exclude != "skill_names"
            and (
                row.kind is not TraceEntryKind.SKILL
                or row.name not in where.skill_names
            )
        )
        or (
            where.providers
            and exclude != "providers"
            and row.provider not in where.providers
        )
        or (where.models and exclude != "models" and row.model not in where.models)
        or (where.namespaces and row.namespace not in where.namespaces)
        or (
            where.search is not None
            and not any(
                where.search.lower() in value.lower()
                for value in (
                    row.name,
                    row.agent_name,
                    row.provider,
                    row.model,
                )
                if value is not None
            )
        )
        or (where.started_after is not None and row.started_at <= where.started_after)
        or (where.started_before is not None and row.started_at >= where.started_before)
    )


def _entry_facets(
    rows: list[_MemoryTraceEntry],
    where: TraceFilter,
) -> TraceFacets:
    kinds: dict[TraceEntryKind, int] = {}
    statuses: dict[TraceEntryStatus, int] = {}
    agents: dict[str, int] = {}
    middleware: dict[str, int] = {}
    skills: dict[str, int] = {}
    providers: dict[str, int] = {}
    models: dict[str, int] = {}
    for row in rows:
        if _entry_matches(row, where, exclude="kinds"):
            kinds[row.kind] = kinds.get(row.kind, 0) + 1
        if _entry_matches(row, where, exclude="statuses"):
            statuses[row.status] = statuses.get(row.status, 0) + 1
        if row.agent_name is not None and _entry_matches(
            row, where, exclude="agent_names"
        ):
            agents[row.agent_name] = agents.get(row.agent_name, 0) + 1
        if row.kind is TraceEntryKind.MIDDLEWARE and _entry_matches(
            row,
            where,
            exclude="middleware_names",
        ):
            middleware[row.name] = middleware.get(row.name, 0) + 1
        if row.kind is TraceEntryKind.SKILL and _entry_matches(
            row,
            where,
            exclude="skill_names",
        ):
            skills[row.name] = skills.get(row.name, 0) + 1
        if row.provider is not None and _entry_matches(
            row,
            where,
            exclude="providers",
        ):
            providers[row.provider] = providers.get(row.provider, 0) + 1
        if row.model is not None and _entry_matches(
            row,
            where,
            exclude="models",
        ):
            models[row.model] = models.get(row.model, 0) + 1
    return TraceFacets(
        kinds=kinds,
        statuses=statuses,
        agents=agents,
        middleware=middleware,
        skills=skills,
        providers=providers,
        models=models,
    )


def _entry_order_key(entry_id: str) -> str:
    """Match the collision-checked SQL ordering for equal entry timestamps."""

    return hashlib.sha256(entry_id.encode("utf-8")).hexdigest()


def _stored_memory_entry(
    thread: _MemoryThread,
    row: _MemoryTraceEntry,
) -> StoredTraceEntry:
    try:
        started_event = thread.events[row.started_seq - 1]
        updated_event = thread.events[row.updated_seq - 1]
    except IndexError as error:
        raise TraceStoreProtocolError(
            "Trace entry references an unavailable Ledger event",
            cause=error,
        ) from error
    return StoredTraceEntry(
        entry_id=row.entry_id,
        parent_id=row.parent_id,
        kind=row.kind,
        status=row.status,
        name=row.name,
        run_id=row.run_id,
        namespace=row.namespace,
        agent_name=row.agent_name,
        provider=row.provider,
        model=row.model,
        started_at=row.started_at,
        first_output_at=row.first_output_at,
        completed_at=row.completed_at,
        started_seq=row.started_seq,
        updated_seq=row.updated_seq,
        started_event=started_event,
        updated_event=updated_event,
    )


def _snapshot_from_state(
    state: TraceLedgerState,
    *,
    expected_namespace: str,
    expected_thread_id: str,
    expected_key: TraceThreadKey | None,
) -> StoreThreadSnapshot:
    thread = state.thread
    if (
        thread is None
        or thread.key.namespace != expected_namespace
        or thread.key.thread_id != expected_thread_id
        or (expected_key is not None and thread.key != expected_key)
    ):
        raise TraceThreadNotFound("Trace generation does not exist")
    return StoreThreadSnapshot(
        key=thread.key,
        as_of_seq=thread.next_seq - 1,
        persisted_bytes=thread.persisted_bytes,
        active_writers=state.active_writers,
    )


def _validate_thread_id(thread_id: str) -> None:
    if (
        not isinstance(thread_id, str)
        or not thread_id
        or thread_id != thread_id.strip()
        or len(thread_id) > 2048
    ):
        raise ValueError("thread_id must be canonical text of at most 2048 chars")


def _validate_commit_result(
    change: TraceLedgerChange,
    result: TraceLedgerCommitResult,
) -> None:
    if change.kind == "open_writer":
        identity = change.identity
        key = result.key
        if (
            identity is None
            or key is None
            or key.namespace != change.namespace
            or key.thread_id != identity.thread_id
            or result.run_id != identity.run_id
            or result.owner_token != change.owner_token
            or result.fence is None
            or result.fence < 1
        ):
            raise TraceStoreProtocolError(
                "Trace Ledger writer result conflicts with its request"
            )
        return
    if result.key != change.key:
        raise TraceStoreProtocolError(
            "Trace Ledger commit result conflicts with its generation"
        )
    if (
        change.kind in {"append_events", "renew_writer", "close_writer"}
        and result.run_id != change.run_id
    ):
        raise TraceStoreProtocolError(
            "Trace Ledger commit result conflicts with its Run"
        )
    if change.kind == "append_events":
        expected_ids = tuple(item.event_id for item in change.facts)
        if change.proven_events:
            expected_ids = tuple(event.event_id for event in change.proven_events)
        if tuple(event.event_id for event in result.events) != expected_ids:
            raise TraceStoreProtocolError(
                "Trace Ledger append result conflicts with its event evidence"
            )
        if change.key is None or len(result.events) != len(change.facts):
            raise TraceStoreProtocolError(
                "Trace Ledger append result has incomplete event evidence"
            )
        for event, draft in zip(result.events, change.facts, strict=True):
            expected = _event(
                event_id=draft.event_id,
                trace_seq=event.trace_seq,
                generation=change.key.generation,
                fact=draft.fact,
                copy_fact=False,
            )
            if event != expected:
                raise TraceStoreProtocolError(
                    "Trace Ledger append result conflicts with its committed event"
                )
        sequences = tuple(event.trace_seq for event in result.events)
        if any(right != left + 1 for left, right in zip(sequences, sequences[1:])):
            raise TraceStoreProtocolError(
                "Trace Ledger append result sequence is discontinuous"
            )
    if (
        change.kind == "save_projection_checkpoint"
        and result.checkpoint != change.checkpoint
    ):
        raise TraceStoreProtocolError(
            "Trace Ledger checkpoint result conflicts with its request"
        )


def _consume_task_exception(task: asyncio.Task[object]) -> None:
    if not task.cancelled():
        task.exception()


__all__ = ["DurableTraceStore", "InMemoryTraceStore"]
