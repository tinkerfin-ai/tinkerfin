"""Backend contract and in-memory committed-message implementation."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol, runtime_checkable
from uuid import uuid4

from tinkerfin_agui_adapter import Identity

from ._identity import required_identifier, required_identity
from .errors import (
    BackendOwnershipLost,
    CancellationUnsupported,
    CodecMismatch,
    InvalidCursor,
    MessageIdConflict,
    RunAlreadyActive,
    RunNotFound,
    RunProducerFailed,
    StreamDeleteConflict,
    StreamDeleted,
)
from .models import MessageEnvelope, RecoveryCheckpoint

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
    identity: Identity
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

    async def prepare(
        self,
        *,
        channel: str,
        identity: Identity,
        codec: str,
        after: int | None,
        cancellable: bool,
        recoverable: bool,
    ) -> PreparedRun: ...

    async def append(
        self,
        handle: BackendRunHandle,
        *,
        message_id: str,
        codec: str,
        payload: bytes,
        checkpoint: RecoveryCheckpoint | None = None,
    ) -> MessageEnvelope: ...

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
    ) -> None: ...

    async def latest_seq(self, *, channel: str, identity: Identity) -> int: ...

    async def read(
        self,
        *,
        channel: str,
        identity: Identity,
        after: int = 0,
        limit: int = 100,
    ) -> tuple[MessageEnvelope, ...]: ...

    async def bind_follow(
        self,
        *,
        channel: str,
        identity: Identity,
    ) -> BackendRunHandle:
        """Bind a read-only follower to the run's authoritative generation."""

        ...

    def follow(
        self,
        handle: BackendRunHandle,
        *,
        after: int,
    ) -> AsyncIterator[MessageEnvelope]: ...

    async def request_cancel(self, handle: BackendRunHandle) -> bool: ...

    async def wait_for_cancel(self, handle: BackendRunHandle) -> bool: ...

    async def wait_finished(self, handle: BackendRunHandle) -> RunStatus: ...

    async def failure(self, handle: BackendRunHandle) -> BaseException | None: ...

    @property
    def lease_renew_interval(self) -> float | None: ...

    @property
    def lease_timeout(self) -> float | None:
        """Return the ownership expiry budget used only for trusted diagnostics."""

        ...

    async def renew(self, handle: BackendRunHandle) -> bool: ...

    async def delete_stream(self, *, channel: str, identity: Identity) -> None:
        """Delete one inactive stream and all of its durable run data.

        Missing and previously deleted streams are successful no-ops. An active
        producer is rejected without requesting cancellation.

        Args:
            channel: Durable codec namespace containing the stream.
            identity: Thread and run identity whose thread stream is deleted.

        Raises:
            StreamDeleteConflict: The stream still has an active producer.
        """

        ...


class _RunRecord:
    def __init__(
        self,
        *,
        identity: Identity,
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
        self.active_identity: Identity | None = None


class _ChannelState:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.codec: str | None = None
        self.streams: dict[str, _StreamState] = {}
        self.next_generations: dict[str, int] = {}


class MemoryBackend(MessagingBackend):
    """Keep ordered logs and run coordination in one event loop process."""

    def __init__(self) -> None:
        self._channels: dict[str, _ChannelState] = {}

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

    async def delete_stream(self, *, channel: str, identity: Identity) -> None:
        """Atomically delete one inactive in-memory stream."""

        required_identifier("channel", channel)
        required_identity(identity)
        channel_state = self._channels.get(channel)
        if channel_state is None:
            return
        async with channel_state.lock:
            state = channel_state.streams.get(identity.thread_id)
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
                state.condition.notify_all()

    def _state_for_handle(self, handle: BackendRunHandle) -> _StreamState:
        channel_state = self._channels.get(handle.channel)
        state = (
            None
            if channel_state is None
            else channel_state.streams.get(handle.identity.thread_id)
        )
        if state is None:
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
        identity: Identity,
        codec: str,
        after: int | None,
        cancellable: bool,
        recoverable: bool,
    ) -> PreparedRun:
        required_identifier("channel", channel)
        required_identity(identity)
        required_identifier("codec", codec)
        channel_state = self._channel(channel)
        async with channel_state.lock:
            state = channel_state.streams.get(identity.thread_id)
            if state is None:
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
        _validate_append_input(
            handle,
            message_id=message_id,
            codec=codec,
            payload=payload,
            checkpoint=checkpoint,
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
            state.condition.notify_all()

    async def latest_seq(self, *, channel: str, identity: Identity) -> int:
        """Return the latest committed sequence or zero for an empty stream."""

        required_identifier("channel", channel)
        required_identity(identity)
        channel_state = self._channels.get(channel)
        if channel_state is None:
            return 0
        async with channel_state.lock:
            state = channel_state.streams.get(identity.thread_id)
            if state is None:
                return 0
            async with state.condition:
                return len(state.messages)

    async def read(
        self,
        *,
        channel: str,
        identity: Identity,
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
            return ()
        async with channel_state.lock:
            state = channel_state.streams.get(identity.thread_id)
            if state is None:
                return ()
            async with state.condition:
                return tuple(
                    message.model_copy(deep=True)
                    for message in state.messages[after : after + limit]
                )

    async def bind_follow(
        self,
        *,
        channel: str,
        identity: Identity,
    ) -> BackendRunHandle:
        """Bind one follower without creating a stream or attaching a producer."""

        required_identifier("channel", channel)
        required_identity(identity)
        channel_state = self._channels.get(channel)
        if channel_state is None:
            raise RunNotFound(identity=identity)
        async with channel_state.lock:
            state = channel_state.streams.get(identity.thread_id)
            if state is None:
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
