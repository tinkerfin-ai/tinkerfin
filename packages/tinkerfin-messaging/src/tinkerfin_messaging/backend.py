"""Backend contract and in-memory committed-message implementation."""

from __future__ import annotations

import asyncio
import time
from abc import abstractmethod
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol, runtime_checkable
from uuid import uuid4

from tinkerfin_contracts import RunIdentity

from ._identity import required_identifier, required_identity
from .errors import (
    BackendOwnershipLost,
    CancellationUnsupported,
    CodecMismatch,
    InvalidCursor,
    MessageIdConflict,
    MessagingQuotaExceeded,
    RunAlreadyActive,
    RunNotFound,
    RunProducerFailed,
    StreamDeleteConflict,
    StreamDeleted,
    StreamExpired,
)
from .limits import DEFAULT_MESSAGING_LIMITS, MessagingLimits
from .models import MessageEnvelope, RecoveryCheckpoint
from .retention import MessagingRetentionPolicy

RunStatus = Literal[
    "running",
    "cancel_requested",
    "completed",
    "cancelled",
    "failed",
    "owner_lost",
]
FinalRunStatus = Literal["completed", "cancelled", "failed", "owner_lost"]


@dataclass(frozen=True, slots=True)
class BackendRunHandle:
    """Identify one run, stream generation, and optional producer fence.

    Backends return an explicit positive `generation` from `prepare()`. A `None`
    generation is reserved for a newly constructed observer handle that resolves the
    current generation, such as `MessageChannel.cancel()`; it never grants producer
    ownership.
    """

    channel: str
    identity: RunIdentity
    owner_token: str | None
    fence: int | None
    generation: int | None = None


def _validate_append_input(
    handle: BackendRunHandle,
    *,
    message_id: str,
    codec: str,
    payload: bytes,
    checkpoint: RecoveryCheckpoint | None,
    limits: MessagingLimits,
) -> None:
    """Reject caller-controlled values before a backend can mutate state."""

    if not isinstance(handle, BackendRunHandle):
        raise TypeError("handle must be a BackendRunHandle")
    required_identifier("channel", handle.channel)
    required_identity(handle.identity)
    required_identifier("message_id", message_id)
    required_identifier("codec", codec)
    if not isinstance(payload, bytes):
        raise TypeError("payload must be bytes")
    if checkpoint is not None and not isinstance(checkpoint, RecoveryCheckpoint):
        raise TypeError("checkpoint must be a RecoveryCheckpoint or None")
    if checkpoint is not None and checkpoint.last_message_id != message_id:
        raise ValueError("checkpoint.last_message_id must match message_id")
    if len(payload) > limits.max_message_payload_bytes:
        raise MessagingQuotaExceeded(
            resource="message_payload_bytes",
            limit=limits.max_message_payload_bytes,
        )
    if (
        checkpoint is not None
        and len(checkpoint.position) > limits.max_checkpoint_bytes
    ):
        raise MessagingQuotaExceeded(
            resource="checkpoint_bytes",
            limit=limits.max_checkpoint_bytes,
        )


@dataclass(frozen=True, slots=True)
class PreparedRun:
    """Return the atomically validated cursor and start-or-attach decision."""

    handle: BackendRunHandle
    after: int
    is_owner: bool
    checkpoint: RecoveryCheckpoint | None = None
    recovered: bool = False


@runtime_checkable
class MessagingBackend(Protocol):
    """Replaceable persistence, replay, and distributed run boundary."""

    @property
    def limits(self) -> MessagingLimits:
        """Return immutable capacity limits enforced before durable mutation."""

        ...

    @property
    def retention_policy(self) -> MessagingRetentionPolicy:
        """Return the immutable terminal replay retention policy."""

        ...

    async def prepare(
        self,
        *,
        channel: str,
        identity: RunIdentity,
        codec: str,
        after: int | None,
        cancellable: bool,
        recoverable: bool,
    ) -> PreparedRun:
        """Atomically start, recover, or attach to one durable semantic run.

        Args:
            channel: Canonical logical channel containing the stream.
            identity: Exact thread and semantic Run identity.
            codec: Stable codec identity required for every generation attachment.
            after: Exclusive replay cursor, or ``None`` to attach at the current tail.
            cancellable: Whether the new producer accepts remote cancellation.
            recoverable: Whether a lost producer may resume from a durable checkpoint.

        Returns:
            Exact generation handle, validated cursor, ownership decision, and optional
            recovery checkpoint.

        Raises:
            RunAlreadyActive: Another producer already owns the semantic Run.
            RunNotFound: A requested attachment or recovery target does not exist.
            CodecMismatch: Existing durable data uses another codec identity.
            InvalidCursor: ``after`` is outside the current committed range.
            StreamDeleted: The selected stream generation was deleted.
            StreamExpired: Terminal replay retention has elapsed.
            MessagingError: Durable state cannot be classified or mutated safely.
            TypeError: A public input has the wrong type.
            ValueError: An identifier or option is not canonical.
        """

        ...

    async def append(
        self,
        handle: BackendRunHandle,
        *,
        message_id: str,
        codec: str,
        payload: bytes,
        checkpoint: RecoveryCheckpoint | None = None,
    ) -> MessageEnvelope:
        """Idempotently commit one encoded message under the producer fence.

        Args:
            handle: Owned exact-generation handle returned by ``prepare()``.
            message_id: Stable semantic message identity used for idempotency.
            codec: Codec identity that must match the prepared generation.
            payload: Already encoded bounded message bytes.
            checkpoint: Optional recovery position committed with this message.

        Returns:
            The existing or newly committed immutable envelope and sequence.

        Raises:
            BackendOwnershipLost: The producer token or fence is no longer current.
            MessageIdConflict: The same message ID already names different evidence.
            CodecMismatch: The supplied codec differs from the durable generation.
            MessagingQuotaExceeded: Payload or checkpoint limits are exceeded.
            MessagingError: Durable append evidence is unavailable or inconsistent.
            TypeError: A public input has the wrong type.
            ValueError: Payload metadata is internally inconsistent.
        """

        ...

    async def begin_settlement(self, handle: BackendRunHandle) -> bool:
        """Atomically claim finalization and report an accepted cancellation.

        `True` transfers an existing durable cancellation request to the producer,
        which must invoke its callback before finishing. `False` closes the run to
        later cancellation so ordinary producer settlement can proceed.

        Args:
            handle: Owned run and its current fencing identity.

        Returns:
            Whether cancellation was durably accepted before settlement began.

        Raises:
            BackendOwnershipLost: The handle no longer owns the run or has already
                begun settlement.
        """

        ...

    async def finish(
        self,
        handle: BackendRunHandle,
        *,
        status: FinalRunStatus,
        error: BaseException | None = None,
    ) -> None:
        """Commit one terminal run status and release producer ownership.

        Args:
            handle: Current producer handle after settlement has begun.
            status: Exactly one durable terminal classification.
            error: Optional trusted failure retained for in-process followers.

        Raises:
            BackendOwnershipLost: The producer handle no longer owns settlement.
            MessagingError: The terminal transition cannot be committed safely.
        """

        ...

    async def latest_seq(self, *, channel: str, identity: RunIdentity) -> int:
        """Return the current generation's last committed sequence.

        Args:
            channel: Canonical logical channel containing the stream.
            identity: Exact thread and semantic Run identity.

        Returns:
            Greatest committed sequence, or zero for an empty generation.

        Raises:
            StreamExpired: Its terminal replay retention has elapsed.
            MessagingError: Durable sequence evidence cannot be read safely.
            ValueError: The channel or identity is not canonical.
        """

        ...

    @abstractmethod
    async def get_run_status(
        self,
        *,
        channel: str,
        identity: RunIdentity,
    ) -> RunStatus:
        """Return one durable run's current authoritative status.

        A backend may atomically settle an expired producer lease as
        ``owner_lost`` while obtaining this status.

        Args:
            channel: Canonical logical channel containing the durable run.
            identity: Exact thread and semantic Run identity to inspect.

        Returns:
            Current running, cancellation, completion, failure, or ownership-loss state.

        Raises:
            RunNotFound: No durable record exists for the exact channel and identity.
            MessagingBackendError: Durable state cannot be read or classified safely.
            ValueError: The channel or identity is not canonical.
        """

        ...

    async def read(
        self,
        *,
        channel: str,
        identity: RunIdentity,
        after: int = 0,
        limit: int = 100,
    ) -> tuple[MessageEnvelope, ...]:
        """Read a bounded page strictly after a validated thread cursor.

        Args:
            channel: Canonical logical channel containing the stream.
            identity: Exact thread and semantic Run identity.
            after: Exclusive non-negative committed sequence.
            limit: Positive maximum number of envelopes returned.

        Returns:
            Immutable ascending committed envelopes from one generation.

        Raises:
            RunNotFound: No current generation exists for the identity.
            InvalidCursor: ``after`` is beyond the committed tail.
            StreamDeleted: The selected generation was explicitly deleted.
            StreamExpired: Its terminal replay retention has elapsed.
            MessagingError: Durable payload evidence cannot be read safely.
            ValueError: A cursor, limit, channel, or identity is invalid.
        """

        ...

    async def bind_follow(
        self,
        *,
        channel: str,
        identity: RunIdentity,
        after: int,
    ) -> BackendRunHandle:
        """Atomically bind a follower and validate its thread cursor.

        Args:
            channel: Canonical logical channel containing the stream.
            identity: Exact thread and semantic Run identity.
            after: Exclusive sequence already delivered to the follower.

        Returns:
            Observer-only handle pinned to the validated current generation.

        Raises:
            RunNotFound: No current generation exists for the identity.
            InvalidCursor: ``after`` is beyond the committed tail.
            StreamDeleted: The selected generation was explicitly deleted.
            StreamExpired: Its terminal replay retention has elapsed.
            MessagingError: A generation cannot be bound atomically.
            ValueError: The cursor, channel, or identity is invalid.
        """

        ...

    def follow(
        self,
        handle: BackendRunHandle,
        *,
        after: int,
    ) -> AsyncIterator[MessageEnvelope]:
        """Return an ordered follower bound to the handle's exact generation.

        Args:
            handle: Observer-only exact-generation handle from ``bind_follow()``.
            after: Exclusive sequence already delivered to the caller.

        Returns:
            Cancellation-responsive iterator of ascending committed envelopes.

        Raises:
            InvalidCursor: ``after`` is beyond the bound generation tail.
            RunProducerFailed: The producer terminates as failed or owner-lost.
            StreamDeleted: The bound generation is deleted during iteration.
            StreamExpired: Its terminal replay retention elapses.
            MessagingError: Durable follow evidence becomes unavailable or corrupt.
            ValueError: The handle or cursor is invalid.
        """

        ...

    async def request_cancel(self, handle: BackendRunHandle) -> bool:
        """Durably request producer cancellation if the run still accepts it.

        Args:
            handle: Observer or producer handle selecting one exact generation.

        Returns:
            Whether a new cancellation request was durably recorded.

        Raises:
            CancellationUnsupported: The active producer is not cancellable.
            RunNotFound: The selected run or generation does not exist.
            StreamDeleted: The selected generation was explicitly deleted.
            MessagingError: Cancellation state cannot be changed safely.
        """

        ...

    async def wait_for_cancel(self, handle: BackendRunHandle) -> bool:
        """Wait until cancellation is requested or the run becomes terminal.

        Args:
            handle: Owned producer handle whose cancellation state is observed.

        Returns:
            ``True`` for a durable cancellation request, otherwise ``False`` after
            terminal settlement.

        Raises:
            BackendOwnershipLost: The handle no longer owns the producer fence.
            StreamDeleted: The selected generation was explicitly deleted.
            MessagingError: Cancellation evidence cannot be observed safely.
        """

        ...

    async def wait_finished(self, handle: BackendRunHandle) -> RunStatus:
        """Wait for and return the durable terminal run status.

        Args:
            handle: Exact-generation handle selecting the run to observe.

        Returns:
            Completed, cancelled, failed, or owner-lost terminal status.

        Raises:
            RunNotFound: The selected run or generation does not exist.
            StreamDeleted: The selected generation was explicitly deleted.
            MessagingError: Terminal evidence cannot be observed safely.
        """

        ...

    async def failure(self, handle: BackendRunHandle) -> BaseException | None:
        """Return the trusted producer failure associated with a terminal run.

        Args:
            handle: Exact-generation handle selecting a settled run.

        Returns:
            In-process producer failure, bounded remote evidence, or ``None``.

        Raises:
            RunNotFound: The selected run or generation does not exist.
            StreamDeleted: The selected generation was explicitly deleted.
            MessagingError: Failure evidence cannot be reconstructed safely.
        """

        ...

    @property
    def lease_renew_interval(self) -> float | None:
        """Return the producer lease renewal interval, or ``None`` if unneeded."""

        ...

    @property
    def lease_timeout(self) -> float | None:
        """Return the ownership expiry budget used only for trusted diagnostics."""

        ...

    async def renew(self, handle: BackendRunHandle) -> bool:
        """Renew producer ownership and report whether its fence remains current.

        Args:
            handle: Owned producer handle with token, fence, and generation.

        Returns:
            ``True`` only while the same producer fence remains authoritative.

        Raises:
            MessagingError: Lease state cannot be read or renewed safely.
            TypeError: ``handle`` is not a backend run handle.
        """

        ...

    async def delete_stream(self, *, channel: str, identity: RunIdentity) -> None:
        """Delete one inactive stream and all of its durable run data.

        Missing and previously deleted streams are successful no-ops. An active
        producer is rejected without requesting cancellation.

        Args:
            channel: Durable codec namespace containing the stream.
            identity: Thread and run identity whose thread stream is deleted.

        Raises:
            StreamDeleteConflict: The stream still has an active producer.
            MessagingError: Durable stream data cannot be deleted safely.
            ValueError: The channel or identity is not canonical.
        """

        ...


class _RunRecord:
    def __init__(
        self,
        *,
        identity: RunIdentity,
        start_seq: int,
        owner_token: str,
        cancellable: bool,
        recoverable: bool,
    ) -> None:
        self.identity = identity
        self.start_seq = start_seq
        self.end_seq = start_seq
        self.owner_token = owner_token
        self.fence = 1
        self.cancellable = cancellable
        self.recoverable = recoverable
        self.status: RunStatus = "running"
        self.settling = False
        self.error: BaseException | None = None
        self.checkpoint: RecoveryCheckpoint | None = None


class _StreamState:
    """Own one in-memory generation, its active Run, and terminal deadline."""

    def __init__(self, *, generation: int) -> None:
        self.generation = generation
        self.deleted = False
        self.condition = asyncio.Condition()
        self.messages: list[MessageEnvelope] = []
        self.by_message_id: dict[str, MessageEnvelope] = {}
        self.signatures: dict[
            str, tuple[str, str, bytes, RecoveryCheckpoint | None]
        ] = {}
        self.runs: dict[str, _RunRecord] = {}
        self.active_identity: RunIdentity | None = None
        self.payload_bytes = 0
        self.expires_at_monotonic: float | None = None


class _ChannelState:
    """Coordinate thread generations and retain stale-handle dispositions."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.codec: str | None = None
        self.streams: dict[str, _StreamState] = {}
        self.next_generations: dict[str, int] = {}
        self.tombstones: dict[str, dict[int, Literal["deleted", "expired"]]] = {}


class MemoryBackend(MessagingBackend):
    """Keep ordered logs and run coordination in one event loop process."""

    def __init__(
        self,
        *,
        limits: MessagingLimits = DEFAULT_MESSAGING_LIMITS,
        retention_policy: MessagingRetentionPolicy = MessagingRetentionPolicy(),
    ) -> None:
        """Initialize process-local streams with capacity and retention limits.

        Args:
            limits: Immutable payload and per-thread capacity contract.
            retention_policy: Terminal replay deadline policy; disabled by default.

        Raises:
            TypeError: Either policy has the wrong public type.
        """

        if not isinstance(limits, MessagingLimits):
            raise TypeError("limits must be a MessagingLimits")
        if not isinstance(retention_policy, MessagingRetentionPolicy):
            raise TypeError("retention_policy must be a MessagingRetentionPolicy")
        self._channels: dict[str, _ChannelState] = {}
        self._limits = limits
        self._retention_policy = retention_policy

    @property
    def limits(self) -> MessagingLimits:
        """Return the immutable limits applied to every in-memory generation."""

        return self._limits

    @property
    def retention_policy(self) -> MessagingRetentionPolicy:
        """Return the terminal replay policy applied to every stream."""

        return self._retention_policy

    @property
    def lease_renew_interval(self) -> float | None:
        """In-process ownership has no expiring external lease."""

        return None

    @property
    def lease_timeout(self) -> float | None:
        """In-process ownership has no expiry budget."""

        return None

    async def renew(self, handle: BackendRunHandle) -> bool:
        """Confirm the in-memory producer still owns its run record."""

        state = self._state_for_handle(handle)
        async with state.condition:
            self._owned_record(state, handle)
            return True

    async def delete_stream(self, *, channel: str, identity: RunIdentity) -> None:
        """Atomically delete one inactive in-memory stream."""

        required_identifier("channel", channel)
        required_identity(identity)
        channel_state = self._channels.get(channel)
        if channel_state is None:
            return
        async with channel_state.lock:
            state = self._expire_if_due(channel_state, identity.thread_id)
            if state is None:
                return
            async with state.condition:
                active_identity = state.active_identity
                if active_identity is not None:
                    raise StreamDeleteConflict(
                        channel=channel,
                        identity=identity,
                        active_identity=active_identity,
                    )
                state.deleted = True
                del channel_state.streams[identity.thread_id]
                channel_state.next_generations[identity.thread_id] = (
                    state.generation + 1
                )
                self._record_tombstone(
                    channel_state,
                    identity.thread_id,
                    generation=state.generation,
                    reason="deleted",
                )
                state.condition.notify_all()

    @staticmethod
    def _record_tombstone(
        channel_state: _ChannelState,
        thread_id: str,
        *,
        generation: int,
        reason: Literal["deleted", "expired"],
    ) -> None:
        """Retain why an old generation can no longer satisfy a bound cursor."""

        channel_state.tombstones.setdefault(thread_id, {})[generation] = reason

    def _expire_if_due(
        self,
        channel_state: _ChannelState,
        thread_id: str,
    ) -> _StreamState | None:
        """Lazily retire one terminal generation using a monotonic deadline."""

        state = channel_state.streams.get(thread_id)
        if state is None:
            return None
        deadline = state.expires_at_monotonic
        if deadline is None or time.monotonic() < deadline:
            return state
        state.deleted = True
        del channel_state.streams[thread_id]
        channel_state.next_generations[thread_id] = state.generation + 1
        self._record_tombstone(
            channel_state,
            thread_id,
            generation=state.generation,
            reason="expired",
        )
        return None

    @staticmethod
    def _tombstone_reason(
        channel_state: _ChannelState,
        thread_id: str,
        generation: int | None,
    ) -> tuple[int, Literal["deleted", "expired"]] | None:
        """Resolve an exact or latest unavailable generation disposition."""

        tombstones = channel_state.tombstones.get(thread_id, {})
        if generation is not None:
            reason = tombstones.get(generation)
            return None if reason is None else (generation, reason)
        if not tombstones:
            return None
        latest = max(tombstones)
        return latest, tombstones[latest]

    @staticmethod
    def _raise_tombstone(
        *,
        channel: str,
        identity: RunIdentity,
        generation: int,
        reason: Literal["deleted", "expired"],
    ) -> None:
        """Raise the stable error associated with one generation tombstone."""

        if reason == "expired":
            raise StreamExpired(
                channel=channel,
                identity=identity,
                generation=generation,
            )
        raise StreamDeleted(
            channel=channel,
            identity=identity,
            generation=generation,
        )

    def _state_for_handle(self, handle: BackendRunHandle) -> _StreamState:
        channel_state = self._channels.get(handle.channel)
        state = None
        if channel_state is not None:
            state = self._expire_if_due(channel_state, handle.identity.thread_id)
            if state is not None and (
                handle.generation is not None and handle.generation != state.generation
            ):
                tombstone = self._tombstone_reason(
                    channel_state,
                    handle.identity.thread_id,
                    handle.generation,
                )
                if tombstone is not None:
                    generation, reason = tombstone
                    self._raise_tombstone(
                        channel=handle.channel,
                        identity=handle.identity,
                        generation=generation,
                        reason=reason,
                    )
        if state is None:
            if channel_state is not None:
                tombstone = self._tombstone_reason(
                    channel_state,
                    handle.identity.thread_id,
                    handle.generation,
                )
                if tombstone is not None:
                    generation, reason = tombstone
                    self._raise_tombstone(
                        channel=handle.channel,
                        identity=handle.identity,
                        generation=generation,
                        reason=reason,
                    )
            if handle.generation is not None:
                raise StreamDeleted(
                    channel=handle.channel,
                    identity=handle.identity,
                    generation=handle.generation,
                )
            raise RunNotFound(identity=handle.identity)
        self._require_generation(state, handle)
        return state

    def _channel(self, channel: str) -> _ChannelState:
        state = self._channels.get(channel)
        if state is None:
            state = _ChannelState()
            self._channels[channel] = state
        return state

    async def prepare(
        self,
        *,
        channel: str,
        identity: RunIdentity,
        codec: str,
        after: int | None,
        cancellable: bool,
        recoverable: bool,
    ) -> PreparedRun:
        """Start, recover, or attach to one in-memory semantic run atomically."""

        required_identifier("channel", channel)
        required_identity(identity)
        required_identifier("codec", codec)
        channel_state = self._channel(channel)
        async with channel_state.lock:
            state = self._expire_if_due(channel_state, identity.thread_id)
            if state is None:
                expired = self._tombstone_reason(
                    channel_state,
                    identity.thread_id,
                    None,
                )
                if (
                    expired is not None
                    and expired[1] == "expired"
                    and after
                    not in {
                        None,
                        0,
                    }
                ):
                    raise StreamExpired(
                        channel=channel,
                        identity=identity,
                        generation=expired[0],
                    )
                generation = channel_state.next_generations.get(identity.thread_id, 1)
                state = _StreamState(generation=generation)
                channel_state.streams[identity.thread_id] = state
            async with state.condition:
                latest = len(state.messages)
                cursor = latest if after is None else after
                if isinstance(cursor, bool) or not isinstance(cursor, int):
                    raise TypeError("after must be an integer or None")
                if cursor < 0 or cursor > latest:
                    raise InvalidCursor(after=cursor, latest=latest)
                if channel_state.codec is not None and channel_state.codec != codec:
                    raise CodecMismatch(expected=channel_state.codec, actual=codec)

                existing = state.runs.get(identity.run_id)
                if existing is not None:
                    if channel_state.codec is None:
                        channel_state.codec = codec
                    return PreparedRun(
                        handle=BackendRunHandle(
                            channel=channel,
                            identity=identity,
                            owner_token=None,
                            fence=None,
                            generation=state.generation,
                        ),
                        after=cursor,
                        is_owner=False,
                    )

                active_identity = state.active_identity
                if active_identity is not None:
                    raise RunAlreadyActive(
                        active_identity=active_identity,
                        requested_identity=identity,
                    )

                owner_token = uuid4().hex
                record = _RunRecord(
                    identity=identity,
                    start_seq=latest,
                    owner_token=owner_token,
                    cancellable=cancellable,
                    recoverable=recoverable,
                )
                if channel_state.codec is None:
                    channel_state.codec = codec
                state.runs[identity.run_id] = record
                state.active_identity = identity
                # Only a genuinely new Run clears terminal retention. Attaching to an
                # existing terminal Run leaves its original deadline unchanged.
                state.expires_at_monotonic = None
                return PreparedRun(
                    handle=BackendRunHandle(
                        channel=channel,
                        identity=identity,
                        owner_token=owner_token,
                        fence=record.fence,
                        generation=state.generation,
                    ),
                    after=cursor,
                    is_owner=True,
                )

    async def append(
        self,
        handle: BackendRunHandle,
        *,
        message_id: str,
        codec: str,
        payload: bytes,
        checkpoint: RecoveryCheckpoint | None = None,
    ) -> MessageEnvelope:
        """Idempotently append one message after ownership and quota checks."""

        _validate_append_input(
            handle,
            message_id=message_id,
            codec=codec,
            payload=payload,
            checkpoint=checkpoint,
            limits=self._limits,
        )
        state = self._state_for_handle(handle)
        channel_state = self._channel(handle.channel)
        signature = (handle.identity.run_id, codec, payload, checkpoint)
        async with state.condition:
            record = self._owned_record(state, handle)
            if channel_state.codec != codec:
                raise CodecMismatch(expected=channel_state.codec or "", actual=codec)
            existing = state.by_message_id.get(message_id)
            if existing is not None:
                if state.signatures[message_id] != signature:
                    raise MessageIdConflict(
                        identity=handle.identity,
                        message_id=message_id,
                    )
                return existing.model_copy(deep=True)

            if len(state.messages) >= self._limits.max_thread_messages:
                raise MessagingQuotaExceeded(
                    resource="thread_messages",
                    limit=self._limits.max_thread_messages,
                )
            if (
                state.payload_bytes + len(payload)
                > self._limits.max_thread_payload_bytes
            ):
                raise MessagingQuotaExceeded(
                    resource="thread_payload_bytes",
                    limit=self._limits.max_thread_payload_bytes,
                )

            envelope = MessageEnvelope(
                channel=handle.channel,
                identity=handle.identity,
                seq=len(state.messages) + 1,
                message_id=message_id,
                codec=codec,
                payload=bytes(payload),
                created_at=datetime.now(UTC),
            )
            state.messages.append(envelope)
            state.by_message_id[message_id] = envelope
            state.signatures[message_id] = signature
            state.payload_bytes += len(payload)
            record.end_seq = envelope.seq
            record.checkpoint = checkpoint
            state.condition.notify_all()
            return envelope.model_copy(deep=True)

    async def begin_settlement(self, handle: BackendRunHandle) -> bool:
        """Atomically choose an accepted cancellation or ordinary settlement."""

        state = self._state_for_handle(handle)
        async with state.condition:
            record = self._owned_record(state, handle)
            if record.settling:
                raise BackendOwnershipLost(
                    f"Producer for run {handle.identity.run_id!r} cannot begin "
                    "settlement"
                )
            if record.status == "cancel_requested":
                record.settling = True
                return True
            if record.status != "running":
                raise BackendOwnershipLost(
                    f"Producer for run {handle.identity.run_id!r} cannot begin "
                    "settlement"
                )
            record.settling = True
            return False

    async def finish(
        self,
        handle: BackendRunHandle,
        *,
        status: FinalRunStatus,
        error: BaseException | None = None,
    ) -> None:
        """Commit the producer terminal state and release its active identity."""

        state = self._state_for_handle(handle)
        async with state.condition:
            record = self._owned_record(state, handle)
            if record.status == "cancel_requested" and status == "completed":
                status = "failed"
                error = RuntimeError(
                    "producer completed without settling accepted cancellation"
                )
            record.status = status
            record.settling = True
            record.error = error
            record.end_seq = len(state.messages)
            if state.active_identity == handle.identity:
                state.active_identity = None
            terminal_ttl = self._retention_policy.terminal_ttl_seconds
            state.expires_at_monotonic = (
                None if terminal_ttl is None else time.monotonic() + terminal_ttl
            )
            state.condition.notify_all()

    async def latest_seq(self, *, channel: str, identity: RunIdentity) -> int:
        """Return the latest committed sequence or zero for an empty stream."""

        required_identifier("channel", channel)
        required_identity(identity)
        channel_state = self._channels.get(channel)
        if channel_state is None:
            return 0
        async with channel_state.lock:
            state = self._expire_if_due(channel_state, identity.thread_id)
            if state is None:
                tombstone = self._tombstone_reason(
                    channel_state,
                    identity.thread_id,
                    None,
                )
                if tombstone is not None and tombstone[1] == "expired":
                    raise StreamExpired(
                        channel=channel,
                        identity=identity,
                        generation=tombstone[0],
                    )
                return 0
            async with state.condition:
                return len(state.messages)

    async def get_run_status(
        self,
        *,
        channel: str,
        identity: RunIdentity,
    ) -> RunStatus:
        """Return the current in-memory status for one exact semantic run."""

        required_identifier("channel", channel)
        required_identity(identity)
        channel_state = self._channels.get(channel)
        if channel_state is None:
            raise RunNotFound(identity=identity)
        async with channel_state.lock:
            state = self._expire_if_due(channel_state, identity.thread_id)
            if state is None:
                tombstone = self._tombstone_reason(
                    channel_state,
                    identity.thread_id,
                    None,
                )
                if tombstone is not None and tombstone[1] == "expired":
                    raise StreamExpired(
                        channel=channel,
                        identity=identity,
                        generation=tombstone[0],
                    )
                raise RunNotFound(identity=identity)
            async with state.condition:
                record = state.runs.get(identity.run_id)
                if record is None:
                    raise RunNotFound(identity=identity)
                return record.status

    async def read(
        self,
        *,
        channel: str,
        identity: RunIdentity,
        after: int = 0,
        limit: int = 100,
    ) -> tuple[MessageEnvelope, ...]:
        """Read one ascending defensive page after an exclusive cursor."""

        required_identifier("channel", channel)
        required_identity(identity)
        if isinstance(after, bool) or not isinstance(after, int):
            raise TypeError("after must be an integer")
        if after < 0:
            raise ValueError("after must be greater than or equal to zero")
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError("limit must be an integer")
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        channel_state = self._channels.get(channel)
        if channel_state is None:
            if after > 0:
                raise InvalidCursor(after=after, latest=0)
            return ()
        async with channel_state.lock:
            state = self._expire_if_due(channel_state, identity.thread_id)
            if state is None:
                tombstone = self._tombstone_reason(
                    channel_state,
                    identity.thread_id,
                    None,
                )
                if tombstone is not None and tombstone[1] == "expired":
                    raise StreamExpired(
                        channel=channel,
                        identity=identity,
                        generation=tombstone[0],
                    )
                if after > 0:
                    raise InvalidCursor(after=after, latest=0)
                return ()
            async with state.condition:
                latest = len(state.messages)
                if after > latest:
                    raise InvalidCursor(after=after, latest=latest)
                return tuple(
                    message.model_copy(deep=True)
                    for message in state.messages[after : after + limit]
                )

    async def bind_follow(
        self,
        *,
        channel: str,
        identity: RunIdentity,
        after: int,
    ) -> BackendRunHandle:
        """Bind one follower after validating its cursor under the stream lock."""

        required_identifier("channel", channel)
        required_identity(identity)
        if isinstance(after, bool) or not isinstance(after, int):
            raise TypeError("after must be an integer")
        if after < 0:
            raise ValueError("after must be greater than or equal to zero")
        channel_state = self._channels.get(channel)
        if channel_state is None:
            raise RunNotFound(identity=identity)
        async with channel_state.lock:
            state = self._expire_if_due(channel_state, identity.thread_id)
            if state is None:
                tombstone = self._tombstone_reason(
                    channel_state,
                    identity.thread_id,
                    None,
                )
                if tombstone is not None and tombstone[1] == "expired":
                    raise StreamExpired(
                        channel=channel,
                        identity=identity,
                        generation=tombstone[0],
                    )
                raise RunNotFound(identity=identity)
            async with state.condition:
                if state.deleted:
                    raise StreamDeleted(
                        channel=channel,
                        identity=identity,
                        generation=state.generation,
                    )
                if identity.run_id not in state.runs:
                    raise RunNotFound(identity=identity)
                latest = len(state.messages)
                if after > latest:
                    raise InvalidCursor(after=after, latest=latest)
                return BackendRunHandle(
                    channel=channel,
                    identity=identity,
                    owner_token=None,
                    fence=None,
                    generation=state.generation,
                )

    def follow(
        self,
        handle: BackendRunHandle,
        *,
        after: int,
    ) -> AsyncIterator[MessageEnvelope]:
        """Follow committed messages until this exact run reaches a terminal state."""

        async def iterate() -> AsyncGenerator[MessageEnvelope, None]:
            state = self._state_for_handle(handle)
            cursor = after
            while True:
                page: tuple[MessageEnvelope, ...] = ()
                terminal_status: RunStatus | None = None
                terminal_error: BaseException | None = None
                async with state.condition:
                    self._require_generation(state, handle)
                    record = state.runs.get(handle.identity.run_id)
                    if record is None:
                        raise RunNotFound(identity=handle.identity)
                    is_terminal = record.status in {
                        "completed",
                        "cancelled",
                        "failed",
                        "owner_lost",
                    }
                    available_end = (
                        record.end_seq if is_terminal else len(state.messages)
                    )
                    if is_terminal:
                        terminal_status = record.status
                        terminal_error = record.error
                    if cursor < available_end:
                        page = tuple(
                            message.model_copy(deep=True)
                            for message in state.messages[cursor:available_end]
                        )
                    elif not is_terminal:
                        await state.condition.wait()
                        continue

                if page:
                    for message in page:
                        cursor = message.seq
                        yield message
                if terminal_status in {"failed", "owner_lost"}:
                    cause = terminal_error or RuntimeError(
                        f"Producer for run {handle.identity.run_id!r} stopped"
                    )
                    raise RunProducerFailed(identity=handle.identity, cause=cause)
                if terminal_status is not None:
                    return

        return iterate()

    async def request_cancel(self, handle: BackendRunHandle) -> bool:
        """Request cancellation unless settlement or termination already won."""

        state = self._state_for_handle(handle)
        async with state.condition:
            self._require_generation(state, handle)
            record = state.runs.get(handle.identity.run_id)
            if record is None:
                raise RunNotFound(identity=handle.identity)
            if record.status in {"completed", "cancelled", "failed", "owner_lost"}:
                return False
            if record.settling:
                return False
            if not record.cancellable:
                raise CancellationUnsupported(identity=handle.identity)
            if record.status == "cancel_requested":
                return False
            record.status = "cancel_requested"
            state.condition.notify_all()
            return True

    async def wait_for_cancel(self, handle: BackendRunHandle) -> bool:
        """Wait for cancellation or return ``False`` after terminal settlement."""

        state = self._state_for_handle(handle)
        async with state.condition:
            while True:
                self._require_generation(state, handle)
                record = state.runs.get(handle.identity.run_id)
                if record is None:
                    raise RunNotFound(identity=handle.identity)
                if record.status == "cancel_requested":
                    return True
                if record.status in {
                    "completed",
                    "cancelled",
                    "failed",
                    "owner_lost",
                }:
                    return False
                await state.condition.wait()

    async def wait_finished(self, handle: BackendRunHandle) -> RunStatus:
        """Wait for and return this run's terminal in-memory status."""

        state = self._state_for_handle(handle)
        async with state.condition:
            while True:
                self._require_generation(state, handle)
                record = state.runs.get(handle.identity.run_id)
                if record is None:
                    raise RunNotFound(identity=handle.identity)
                if record.status in {
                    "completed",
                    "cancelled",
                    "failed",
                    "owner_lost",
                }:
                    return record.status
                await state.condition.wait()

    async def failure(self, handle: BackendRunHandle) -> BaseException | None:
        """Return the recorded producer failure after a run has settled."""

        state = self._state_for_handle(handle)
        async with state.condition:
            self._require_generation(state, handle)
            record = state.runs.get(handle.identity.run_id)
            if record is None:
                raise RunNotFound(identity=handle.identity)
            return record.error

    @staticmethod
    def _owned_record(
        state: _StreamState,
        handle: BackendRunHandle,
    ) -> _RunRecord:
        MemoryBackend._require_generation(state, handle)
        record = state.runs.get(handle.identity.run_id)
        if record is None:
            raise RunNotFound(identity=handle.identity)
        if (
            handle.owner_token is None
            or handle.fence is None
            or record.owner_token != handle.owner_token
            or record.fence != handle.fence
        ):
            raise BackendOwnershipLost(
                f"Producer for run {handle.identity.run_id!r} no longer owns its fence"
            )
        return record

    @staticmethod
    def _require_generation(
        state: _StreamState,
        handle: BackendRunHandle,
    ) -> None:
        generation = handle.generation
        if state.deleted or (generation is not None and generation != state.generation):
            raise StreamDeleted(
                channel=handle.channel,
                identity=handle.identity,
                generation=generation,
            )
