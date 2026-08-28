"""Replaceable Trace Store contracts and bounded in-memory implementation."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable
from uuid import uuid4

from pydantic import Field, JsonValue, field_validator, model_validator

from tinkerfin_contracts import RunIdentity

from ._models import TraceModel
from .errors import (
    TraceProjectionCheckpointConflict,
    TraceQuotaExceeded,
    TraceRunConflict,
    TraceStoreProtocolError,
    TraceThreadNotFound,
)
from .facts import RunFact, TraceEvent, TraceSemanticFact
from .limits import TraceLimits


class TraceThreadKey(TraceModel):
    """Bind a Trace handle to one namespace, thread, and non-reusable generation."""

    namespace: str = Field(min_length=1, max_length=2048)
    thread_id: str = Field(min_length=1, max_length=2048)
    generation: str = Field(min_length=1, max_length=2048)

    @field_validator("namespace", "thread_id", "generation")
    @classmethod
    def components_are_canonical(cls, value: str) -> str:
        """Reject whitespace aliases before a Store operation is selected."""

        if value != value.strip():
            raise ValueError("Trace thread key components must be canonical")
        return value


class StoreWriterSnapshot(TraceModel):
    """Describe one active Run writer at a fixed Store observation point."""

    run_id: str = Field(min_length=1, max_length=1024)
    committed_events: int = Field(ge=0)

    @field_validator("run_id")
    @classmethod
    def run_id_is_canonical(cls, value: str) -> str:
        """Reject aliases before exposing active writer ownership."""

        if value != value.strip():
            raise ValueError("active writer Run ID must be canonical")
        return value


class StoreThreadSnapshot(TraceModel):
    """Return immutable generation, prefix, capacity, and writer metadata."""

    key: TraceThreadKey
    as_of_seq: int = Field(ge=0)
    persisted_bytes: int = Field(ge=0)
    active_writers: tuple[StoreWriterSnapshot, ...]

    @field_validator("active_writers")
    @classmethod
    def active_writers_are_unique(
        cls,
        values: tuple[StoreWriterSnapshot, ...],
    ) -> tuple[StoreWriterSnapshot, ...]:
        """Require a canonical unique active writer set."""

        run_ids = tuple(value.run_id for value in values)
        if len(set(run_ids)) != len(run_ids):
            raise ValueError("active writer Run IDs must be unique")
        return values

    @property
    def active_run_ids(self) -> tuple[str, ...]:
        """Return active Run IDs in the snapshot's canonical writer order."""

        return tuple(writer.run_id for writer in self.active_writers)


class TraceProjectionCheckpoint(TraceModel):
    """Store one disposable serializable Projection state at an exact prefix."""

    key: TraceThreadKey
    projection_name: str = Field(min_length=1, max_length=2048)
    run_id: str | None = Field(default=None, min_length=1, max_length=1024)
    as_of_seq: int = Field(ge=0)
    state: JsonValue

    @field_validator("projection_name", "run_id")
    @classmethod
    def identifiers_are_canonical(cls, value: str | None) -> str | None:
        """Reject whitespace aliases in Projection cache ownership."""

        if value is not None and value != value.strip():
            raise ValueError("Projection checkpoint identifiers must be canonical")
        return value

    @model_validator(mode="after")
    def scoped_checkpoints_require_events(self) -> TraceProjectionCheckpoint:
        """Reserve sequence zero for the unscoped empty core state."""

        if self.run_id is not None and self.as_of_seq == 0:
            raise ValueError("Run-scoped Projection checkpoints require an event")
        return self


@runtime_checkable
class TraceWriter(Protocol):
    """Append facts for one Run under one exact thread generation."""

    @property
    def key(self) -> TraceThreadKey:
        """Return the immutable generation bound to this writer."""

        ...

    @property
    def run_id(self) -> str:
        """Return the semantic Run identity owned by this writer."""

        ...

    async def append(
        self,
        facts: tuple[TraceSemanticFact, ...],
        *,
        mandatory: bool = False,
    ) -> tuple[TraceEvent, ...]:
        """Atomically append one ordered fact batch.

        ``mandatory=False`` accepts only pre-terminal facts. ``mandatory=True`` spends
        this Run's reserved capacity and accepts the exactly-once terminal followed by
        the exactly-once closed fact, either together or in two ordered transactions.

        Args:
            facts: Non-empty ordered semantic facts for this writer's exact Run.
            mandatory: Whether the transaction may spend terminal reserve.

        Returns:
            Committed immutable events with contiguous global Trace sequences.

        Raises:
            TraceStoreProtocolError: Identity, ordering, ownership, terminal, or
                idempotency invariants fail.
            TraceQuotaExceeded: Ordinary or reserved capacity is exhausted.
            TraceStoreError: The transaction cannot be committed or proven committed.
        """

        ...

    async def aclose(self) -> None:
        """Release writer ownership idempotently.

        A distributed implementation treats ownership already transferred after lease
        expiry as successful local cleanup and must not disturb the replacement fence.

        Raises:
            TraceStoreError: Local heartbeat or owned-resource settlement fails.
        """

        ...


@runtime_checkable
class TraceStore(Protocol):
    """Persist, read, follow, and delete semantic Trace Ledger generations."""

    @property
    def namespace(self) -> str:
        """Return the immutable logical Store namespace."""

        ...

    @property
    def limits(self) -> TraceLimits:
        """Return the immutable limits enforced by this Store."""

        ...

    async def open_writer(self, identity: RunIdentity) -> TraceWriter:
        """Open one exclusive writer for a previously unseen Run ID.

        Args:
            identity: Canonical thread and Run identity.

        Returns:
            Writer owning one Run in the current exact generation.

        Raises:
            TraceRunConflict: The Run exists or another live writer conflicts.
            TraceQuotaExceeded: Store thread or byte capacity is exhausted.
            TraceStoreError: Durable writer admission fails.
        """

        ...

    async def snapshot(self, thread_id: str) -> StoreThreadSnapshot:
        """Return one consistent event prefix for the current generation.

        Args:
            thread_id: Canonical thread identity.

        Returns:
            Metadata-only generation snapshot with active writer evidence.

        Raises:
            TraceThreadNotFound: The thread has no current generation.
            TraceStoreError: The consistent read fails.
        """

        ...

    async def snapshot_key(self, key: TraceThreadKey) -> StoreThreadSnapshot:
        """Return one consistent event prefix for an exact generation.

        Args:
            key: Exact namespace, thread, and generation handle.

        Returns:
            Metadata-only prefix and active writer evidence.

        Raises:
            TraceThreadNotFound: The generation was deleted or replaced.
            TraceStoreError: The consistent read fails.
        """

        ...

    async def read_events(
        self,
        key: TraceThreadKey,
        *,
        after_seq: int,
        as_of_seq: int,
        limit: int,
    ) -> tuple[TraceEvent, ...]:
        """Read a bounded ascending page inside one fixed as-of prefix.

        Args:
            key: Exact generation handle.
            after_seq: Exclusive lower global sequence.
            as_of_seq: Inclusive immutable upper global sequence.
            limit: Positive maximum event count.

        Returns:
            Defensive immutable events in ascending sequence order.

        Raises:
            ValueError: Cursor values or limit are invalid.
            TraceThreadNotFound: The exact generation is unavailable.
            TraceStoreProtocolError: Stored event content is corrupt.
            TraceStoreError: The bounded read fails.
        """

        ...

    async def read_events_reverse(
        self,
        key: TraceThreadKey,
        *,
        before_seq: int,
        limit: int,
    ) -> tuple[TraceEvent, ...]:
        """Read a bounded newest-first page below one exclusive sequence.

        Args:
            key: Exact generation handle.
            before_seq: Exclusive upper global sequence.
            limit: Positive maximum event count.

        Returns:
            Defensive immutable events in descending sequence order.

        Raises:
            ValueError: Cursor values or limit are invalid.
            TraceThreadNotFound: The exact generation is unavailable.
            TraceStoreError: The bounded read fails.
        """

        ...

    async def load_projection_checkpoint(
        self,
        key: TraceThreadKey,
        *,
        projection_name: str,
        run_id: str | None,
        as_of_seq: int,
    ) -> TraceProjectionCheckpoint | None:
        """Return the newest matching checkpoint at or below one fixed prefix.

        Args:
            key: Exact generation handle.
            projection_name: Registered Projection identity.
            run_id: Optional Run-scoped cache identity.
            as_of_seq: Inclusive fixed lookup prefix.

        Returns:
            Defensive checkpoint copy, or ``None`` when no cache is available.

        Raises:
            TraceThreadNotFound: The exact generation is unavailable.
            TraceStoreError: Checkpoint lookup fails.
        """

        ...

    async def save_projection_checkpoint(
        self,
        checkpoint: TraceProjectionCheckpoint,
        *,
        expected_as_of_seq: int | None,
    ) -> TraceProjectionCheckpoint:
        """Compare-and-swap one disposable Projection checkpoint.

        Repeating the exact current checkpoint succeeds even with its prior expected
        prefix so a caller can resolve an unknown commit. The same identity and prefix
        with different state is a Store protocol violation.

        Args:
            checkpoint: Complete disposable cache state to persist.
            expected_as_of_seq: Previous prefix required for compare-and-swap, or
                ``None`` when no prior checkpoint is expected.

        Returns:
            Defensive copy of the committed checkpoint.

        Raises:
            TraceProjectionCheckpointConflict: A newer checkpoint already exists.
            TraceStoreProtocolError: Equal identity/prefix carries different state.
            TraceStoreError: The compare-and-swap cannot be completed or proven.
        """

        ...

    def follow(
        self,
        key: TraceThreadKey,
        *,
        after_seq: int,
    ) -> AsyncGenerator[tuple[TraceEvent, ...], None]:
        """Return a closeable iterator of bounded committed event pages.

        Args:
            key: Exact generation to follow.
            after_seq: Last sequence already consumed by the caller.

        Returns:
            Iterator preserving sequence order and backpressure. A page may split one
            append or combine multiple committed appends.

        Raises:
            TraceThreadNotFound: The generation is deleted or replaced.
            TraceStoreError: The follower cannot continue.
        """

        ...

    async def delete(self, key: TraceThreadKey) -> None:
        """Delete an inactive exact generation and invalidate old handles.

        Args:
            key: Exact generation selected for deletion.

        Raises:
            TraceRunConflict: One or more active writers still own it.
            TraceThreadNotFound: The generation is unavailable.
            TraceStoreError: Durable deletion fails.
        """

        ...


@dataclass(slots=True)
class _ThreadState:
    key: TraceThreadKey
    events: list[TraceEvent] = field(default_factory=lambda: list[TraceEvent]())
    persisted_bytes: int = 0
    known_run_ids: set[str] = field(default_factory=lambda: set[str]())
    active_run_ids: set[str] = field(default_factory=lambda: set[str]())
    committed_events_by_run: dict[str, int] = field(
        default_factory=lambda: dict[str, int]()
    )
    terminal_committed_run_ids: set[str] = field(default_factory=lambda: set[str]())
    closed_committed_run_ids: set[str] = field(default_factory=lambda: set[str]())
    remaining_event_reserve: dict[str, int] = field(
        default_factory=lambda: dict[str, int]()
    )
    remaining_byte_reserve: dict[str, int] = field(
        default_factory=lambda: dict[str, int]()
    )
    projection_checkpoints: dict[
        tuple[str, str | None],
        list[TraceProjectionCheckpoint],
    ] = field(
        default_factory=lambda: dict[
            tuple[str, str | None],
            list[TraceProjectionCheckpoint],
        ]()
    )


class _MemoryWriter:
    """Own one Run registration in an InMemoryTraceStore."""

    def __init__(
        self,
        store: InMemoryTraceStore,
        *,
        key: TraceThreadKey,
        run_id: str,
    ) -> None:
        """Bind one exact generation and Run registration owned by the Store."""

        self._store = store
        self._key = key
        self._run_id = run_id
        self._closed = False
        self._close_lock = asyncio.Lock()

    @property
    def key(self) -> TraceThreadKey:
        """Return the exact generation owned by this writer."""

        return self._key

    @property
    def run_id(self) -> str:
        """Return the semantic Run registered by this writer."""

        return self._run_id

    async def append(
        self,
        facts: tuple[TraceSemanticFact, ...],
        *,
        mandatory: bool = False,
    ) -> tuple[TraceEvent, ...]:
        """Append one atomic batch while this Run registration remains active."""

        if self._closed:
            raise TraceStoreProtocolError("Trace writer is closed")
        return await self._store._append(
            self._key,
            run_id=self._run_id,
            facts=facts,
            mandatory=mandatory,
        )

    async def aclose(self) -> None:
        """Release the Run registration once and preserve committed identity."""

        async with self._close_lock:
            if self._closed:
                return
            await self._store._close_writer(self._key, run_id=self._run_id)
            self._closed = True


class InMemoryTraceStore:
    """Bounded process-local Trace Store with generation-safe live following."""

    def __init__(
        self,
        *,
        namespace: str = "default",
        limits: TraceLimits | None = None,
    ) -> None:
        """Create an isolated process-local generation registry with hard limits."""

        if (
            not isinstance(namespace, str)
            or not namespace
            or namespace != namespace.strip()
            or len(namespace) > 2048
        ):
            raise ValueError("namespace must be canonical text of at most 2048 chars")
        self._namespace = namespace
        self._limits = limits or TraceLimits()
        self._threads: dict[str, _ThreadState] = {}
        self._total_bytes = 0
        self._condition = asyncio.Condition()

    @property
    def namespace(self) -> str:
        """Return the immutable logical Store namespace."""

        return self._namespace

    @property
    def limits(self) -> TraceLimits:
        """Return the capacity limits enforced by this Store."""

        return self._limits

    async def open_writer(self, identity: RunIdentity) -> TraceWriter:
        """Reserve one exclusive bounded writer for a new Run identity."""

        if not isinstance(identity, RunIdentity):
            raise TypeError("identity must be a RunIdentity")
        async with self._condition:
            state = self._threads.get(identity.thread_id)
            created = state is None
            if state is None:
                if len(self._threads) >= self._limits.max_tracer_threads:
                    raise TraceQuotaExceeded(
                        "Tracer thread quota is exhausted",
                        context={"resource": "tracer_threads"},
                    )
                state = _ThreadState(
                    key=TraceThreadKey(
                        namespace=self._namespace,
                        thread_id=identity.thread_id,
                        generation=str(uuid4()),
                    )
                )
                self._threads[identity.thread_id] = state
            if identity.run_id in state.known_run_ids:
                raise TraceRunConflict(
                    "Trace Run identity already exists",
                    context={"run_id": identity.run_id},
                )
            try:
                self._ensure_reserve(state)
            except BaseException:
                if created:
                    del self._threads[identity.thread_id]
                raise
            state.known_run_ids.add(identity.run_id)
            state.active_run_ids.add(identity.run_id)
            state.committed_events_by_run[identity.run_id] = 0
            state.remaining_event_reserve[identity.run_id] = (
                self._limits.terminal_reserve_events_per_run
            )
            state.remaining_byte_reserve[identity.run_id] = (
                self._limits.terminal_reserve_bytes_per_run
            )
            self._condition.notify_all()
            return _MemoryWriter(
                self,
                key=state.key,
                run_id=identity.run_id,
            )

    def _ensure_reserve(self, state: _ThreadState) -> None:
        """Reserve terminal capacity without letting one Run spend another's tail."""

        event_reserve = sum(state.remaining_event_reserve.values())
        event_reserve += self._limits.terminal_reserve_events_per_run
        byte_reserve = sum(state.remaining_byte_reserve.values())
        byte_reserve += self._limits.terminal_reserve_bytes_per_run
        if len(state.events) + event_reserve > self._limits.max_thread_events:
            raise TraceQuotaExceeded(
                "Thread cannot reserve terminal events",
                context={"resource": "thread_events"},
            )
        if state.persisted_bytes + byte_reserve > self._limits.max_thread_bytes:
            raise TraceQuotaExceeded(
                "Thread cannot reserve terminal bytes",
                context={"resource": "thread_bytes"},
            )
        total_reserve = sum(
            sum(item.remaining_byte_reserve.values()) for item in self._threads.values()
        )
        total_reserve += self._limits.terminal_reserve_bytes_per_run
        if self._total_bytes + total_reserve > self._limits.max_tracer_bytes:
            raise TraceQuotaExceeded(
                "Tracer cannot reserve terminal bytes",
                context={"resource": "tracer_bytes"},
            )

    async def _append(
        self,
        key: TraceThreadKey,
        *,
        run_id: str,
        facts: tuple[TraceSemanticFact, ...],
        mandatory: bool,
    ) -> tuple[TraceEvent, ...]:
        """Apply the shared ordered terminal and capacity contract under one lock."""

        if not facts:
            return ()
        terminal_batch = all(
            isinstance(fact, RunFact) and fact.phase in {"terminal", "closed"}
            for fact in facts
        )
        if mandatory != terminal_batch:
            raise TraceStoreProtocolError(
                "mandatory append is reserved for terminal and closed Run facts"
            )
        async with self._condition:
            state = self._require_state(key)
            if run_id not in state.active_run_ids:
                raise TraceStoreProtocolError("Trace writer no longer owns its Run")
            terminal_committed = run_id in state.terminal_committed_run_ids
            closed_committed = run_id in state.closed_committed_run_ids
            if mandatory:
                phases = tuple(
                    fact.phase for fact in facts if isinstance(fact, RunFact)
                )
                if phases.count("terminal") > 1 or phases.count("closed") > 1:
                    raise TraceStoreProtocolError(
                        "Run terminal and closed facts must be exactly-once"
                    )
                if terminal_committed and "terminal" in phases:
                    raise TraceStoreProtocolError(
                        "Run terminal fact is already committed"
                    )
                if closed_committed:
                    raise TraceStoreProtocolError(
                        "Run closed fact is already committed"
                    )
                if "closed" in phases and not (
                    terminal_committed
                    or (
                        "terminal" in phases
                        and phases.index("terminal") < phases.index("closed")
                    )
                ):
                    raise TraceStoreProtocolError(
                        "Run closed fact requires a committed terminal fact"
                    )
                terminal_committed = terminal_committed or "terminal" in phases
                closed_committed = "closed" in phases
            elif terminal_committed or closed_committed:
                raise TraceStoreProtocolError(
                    "Ordinary Trace facts cannot follow the Run terminal"
                )
            prepared: list[TraceEvent] = []
            next_seq = len(state.events) + 1
            for fact in facts:
                if (
                    fact.identity.thread_id != key.thread_id
                    or fact.identity.run_id != run_id
                ):
                    raise TraceStoreProtocolError(
                        "Trace writer can append facts only for its bound Run"
                    )
                event = _event(
                    event_id=str(uuid4()),
                    trace_seq=next_seq,
                    generation=key.generation,
                    fact=fact,
                )
                if event.persisted_bytes > self._limits.max_event_bytes:
                    raise TraceQuotaExceeded(
                        "Trace event exceeds max_event_bytes",
                        context={"resource": "event_bytes"},
                    )
                prepared.append(event)
                next_seq += 1
            added_bytes = sum(event.persisted_bytes for event in prepared)
            event_limit = self._limits.max_thread_events
            byte_limit = self._limits.max_thread_bytes
            tracer_limit = self._limits.max_tracer_bytes
            if mandatory:
                remaining_events = state.remaining_event_reserve[run_id]
                remaining_bytes = state.remaining_byte_reserve[run_id]
                if len(prepared) > remaining_events or added_bytes > remaining_bytes:
                    raise TraceQuotaExceeded(
                        "Run terminal reserve is exhausted",
                        context={"resource": "terminal_reserve"},
                    )
            else:
                event_limit -= sum(state.remaining_event_reserve.values())
                byte_limit -= sum(state.remaining_byte_reserve.values())
                tracer_limit -= sum(
                    sum(item.remaining_byte_reserve.values())
                    for item in self._threads.values()
                )
            if len(state.events) + len(prepared) > event_limit:
                raise TraceQuotaExceeded(
                    "Thread event quota is exhausted",
                    context={"resource": "thread_events"},
                )
            if state.persisted_bytes + added_bytes > byte_limit:
                raise TraceQuotaExceeded(
                    "Thread byte quota is exhausted",
                    context={"resource": "thread_bytes"},
                )
            if self._total_bytes + added_bytes > tracer_limit:
                raise TraceQuotaExceeded(
                    "Tracer byte quota is exhausted",
                    context={"resource": "tracer_bytes"},
                )
            state.events.extend(prepared)
            state.persisted_bytes += added_bytes
            self._total_bytes += added_bytes
            state.committed_events_by_run[run_id] += len(prepared)
            if mandatory:
                if terminal_committed:
                    state.terminal_committed_run_ids.add(run_id)
                if closed_committed:
                    state.closed_committed_run_ids.add(run_id)
                    state.remaining_event_reserve[run_id] = 0
                    state.remaining_byte_reserve[run_id] = 0
                else:
                    state.remaining_event_reserve[run_id] -= len(prepared)
                    state.remaining_byte_reserve[run_id] -= added_bytes
            self._condition.notify_all()
            return tuple(event.model_copy(deep=True) for event in prepared)

    async def _close_writer(self, key: TraceThreadKey, *, run_id: str) -> None:
        async with self._condition:
            state = self._require_state(key)
            state.active_run_ids.discard(run_id)
            state.remaining_event_reserve.pop(run_id, None)
            state.remaining_byte_reserve.pop(run_id, None)
            state.terminal_committed_run_ids.discard(run_id)
            state.closed_committed_run_ids.discard(run_id)
            committed = state.committed_events_by_run.pop(run_id, 0)
            if committed == 0:
                state.known_run_ids.discard(run_id)
            if not state.events and not state.active_run_ids:
                del self._threads[key.thread_id]
            self._condition.notify_all()

    async def snapshot(self, thread_id: str) -> StoreThreadSnapshot:
        """Return a consistent snapshot of the current thread generation."""

        async with self._condition:
            state = self._threads.get(thread_id)
            if state is None:
                raise TraceThreadNotFound(
                    "Trace thread does not exist",
                    context={"thread_id": thread_id},
                )
            return _snapshot(state)

    async def snapshot_key(self, key: TraceThreadKey) -> StoreThreadSnapshot:
        """Return a consistent snapshot for one exact generation."""

        async with self._condition:
            return _snapshot(self._require_state(key))

    async def read_events(
        self,
        key: TraceThreadKey,
        *,
        after_seq: int,
        as_of_seq: int,
        limit: int,
    ) -> tuple[TraceEvent, ...]:
        """Read one ascending bounded batch inside a fixed prefix."""

        if after_seq < 0 or as_of_seq < 0 or limit < 1:
            raise ValueError("event cursor values must be non-negative and bounded")
        async with self._condition:
            state = self._require_state(key)
            start = min(after_seq, len(state.events))
            stop = min(as_of_seq, len(state.events), start + limit)
            return tuple(
                event.model_copy(deep=True) for event in state.events[start:stop]
            )

    async def read_events_reverse(
        self,
        key: TraceThreadKey,
        *,
        before_seq: int,
        limit: int,
    ) -> tuple[TraceEvent, ...]:
        """Read one descending batch below an exclusive sequence boundary."""

        if before_seq < 1 or limit < 1:
            raise ValueError("reverse event cursor values must be positive")
        async with self._condition:
            state = self._require_state(key)
            if before_seq > len(state.events) + 1:
                raise ValueError("before_seq exceeds the current Trace tail")
            stop = before_seq - 1
            start = max(0, stop - limit)
            return tuple(
                event.model_copy(deep=True)
                for event in reversed(state.events[start:stop])
            )

    async def load_projection_checkpoint(
        self,
        key: TraceThreadKey,
        *,
        projection_name: str,
        run_id: str | None,
        as_of_seq: int,
    ) -> TraceProjectionCheckpoint | None:
        """Load the newest scoped cache state inside one fixed Ledger prefix."""

        _checkpoint_lookup(
            key,
            projection_name=projection_name,
            run_id=run_id,
            as_of_seq=as_of_seq,
        )
        async with self._condition:
            state = self._require_state(key)
            checkpoints = state.projection_checkpoints.get(
                (projection_name, run_id),
                (),
            )
            return next(
                (
                    item.model_copy(deep=True)
                    for item in reversed(checkpoints)
                    if item.as_of_seq <= as_of_seq
                ),
                None,
            )

    async def save_projection_checkpoint(
        self,
        checkpoint: TraceProjectionCheckpoint,
        *,
        expected_as_of_seq: int | None,
    ) -> TraceProjectionCheckpoint:
        """Atomically advance one scoped cache state under compare-and-swap."""

        if not isinstance(checkpoint, TraceProjectionCheckpoint):
            raise TypeError("checkpoint must be a TraceProjectionCheckpoint")
        if expected_as_of_seq is not None and expected_as_of_seq < 0:
            raise ValueError("expected_as_of_seq must be non-negative or None")
        async with self._condition:
            state = self._require_state(checkpoint.key)
            if checkpoint.as_of_seq > len(state.events):
                raise TraceStoreProtocolError(
                    "Projection checkpoint exceeds the committed Trace prefix"
                )
            scope = (checkpoint.projection_name, checkpoint.run_id)
            checkpoints = state.projection_checkpoints.setdefault(scope, [])
            current = checkpoints[-1] if checkpoints else None
            current_seq = None if current is None else current.as_of_seq
            if current is not None and current.as_of_seq == checkpoint.as_of_seq:
                if current != checkpoint:
                    raise TraceStoreProtocolError(
                        "Projection checkpoint idempotency evidence conflicts"
                    )
                return current.model_copy(deep=True)
            if current_seq != expected_as_of_seq:
                raise TraceProjectionCheckpointConflict(
                    "Projection checkpoint compare-and-swap failed",
                    context={
                        "projection": checkpoint.projection_name,
                        "current_as_of_seq": current_seq,
                    },
                )
            if current is not None and checkpoint.as_of_seq <= current.as_of_seq:
                raise TraceProjectionCheckpointConflict(
                    "Projection checkpoint must advance its fixed prefix",
                    context={
                        "projection": checkpoint.projection_name,
                        "current_as_of_seq": current.as_of_seq,
                    },
                )
            stored = checkpoint.model_copy(deep=True)
            checkpoints.append(stored)
            self._condition.notify_all()
            return stored.model_copy(deep=True)

    async def _follow(
        self,
        key: TraceThreadKey,
        *,
        after_seq: int,
    ) -> AsyncGenerator[tuple[TraceEvent, ...], None]:
        """Follow committed batches until cancellation or generation deletion."""

        cursor = after_seq
        while True:
            async with self._condition:
                state = self._require_state(key)
                start = min(cursor, len(state.events))
                stop = min(
                    len(state.events),
                    start + self._limits.follow_batch_size,
                )
                available = tuple(state.events[start:stop])
                if not available:
                    await self._condition.wait()
                    continue
            cursor = available[-1].trace_seq
            yield tuple(event.model_copy(deep=True) for event in available)

    def follow(
        self,
        key: TraceThreadKey,
        *,
        after_seq: int,
    ) -> AsyncGenerator[tuple[TraceEvent, ...], None]:
        """Follow committed batches until cancellation or generation deletion."""

        if after_seq < 0:
            raise ValueError("after_seq must be non-negative")
        return self._follow(key, after_seq=after_seq)

    async def delete(self, key: TraceThreadKey) -> None:
        """Delete an inactive exact generation and release its byte budget."""

        async with self._condition:
            state = self._require_state(key)
            if state.active_run_ids:
                raise TraceRunConflict(
                    "Active Trace writers prevent deletion",
                    context={"active_run_count": len(state.active_run_ids)},
                )
            self._total_bytes -= state.persisted_bytes
            del self._threads[key.thread_id]
            self._condition.notify_all()

    def _require_state(self, key: TraceThreadKey) -> _ThreadState:
        if key.namespace != self._namespace:
            raise TraceThreadNotFound("Trace namespace does not exist")
        state = self._threads.get(key.thread_id)
        if state is None or state.key.generation != key.generation:
            raise TraceThreadNotFound(
                "Trace generation does not exist",
                context={"thread_id": key.thread_id},
            )
        return state


def _event(
    *,
    event_id: str,
    trace_seq: int,
    generation: str,
    fact: TraceSemanticFact,
) -> TraceEvent:
    persisted_bytes = 1
    stored_fact = fact.model_copy(deep=True)
    for _attempt in range(4):
        event = TraceEvent(
            event_id=event_id,
            trace_seq=trace_seq,
            generation=generation,
            fact=stored_fact,
            persisted_bytes=persisted_bytes,
        )
        encoded = event.model_dump_json(by_alias=True).encode()
        if len(encoded) == persisted_bytes:
            return event
        persisted_bytes = len(encoded)
    raise TraceStoreProtocolError("Trace event size did not converge")


def _snapshot(state: _ThreadState) -> StoreThreadSnapshot:
    return StoreThreadSnapshot(
        key=state.key,
        as_of_seq=len(state.events),
        persisted_bytes=state.persisted_bytes,
        active_writers=tuple(
            StoreWriterSnapshot(
                run_id=run_id,
                committed_events=state.committed_events_by_run[run_id],
            )
            for run_id in sorted(state.active_run_ids)
        ),
    )


def _checkpoint_lookup(
    key: TraceThreadKey,
    *,
    projection_name: str,
    run_id: str | None,
    as_of_seq: int,
) -> None:
    """Validate a Projection checkpoint lookup before Store state is touched."""

    if not isinstance(key, TraceThreadKey):
        raise TypeError("key must be a TraceThreadKey")
    for name, value in (("projection_name", projection_name), ("run_id", run_id)):
        if value is not None and (
            not isinstance(value, str) or not value or value != value.strip()
        ):
            raise ValueError(f"{name} must be canonical text")
    if as_of_seq < 0:
        raise ValueError("as_of_seq must be non-negative")


__all__ = [
    "InMemoryTraceStore",
    "StoreThreadSnapshot",
    "StoreWriterSnapshot",
    "TraceProjectionCheckpoint",
    "TraceStore",
    "TraceThreadKey",
    "TraceWriter",
]
