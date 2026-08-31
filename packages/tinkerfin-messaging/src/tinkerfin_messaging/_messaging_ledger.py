"""Framework-owned Messaging lifecycle over a storage-oriented backend."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from uuid import uuid4

from tinkerfin_contracts import RunIdentity

from ._identity import required_identifier, required_identity
from .backend import (
    FinalRunStatus,
    RunStatus,
    _BackendRunHandle,
    _PreparedRun,
    _validate_append_input,
    is_failed_run_status,
    is_final_run_status,
)
from .backend_contract import (
    CommittedMessageQuery,
    MessagingBackend,
    MessagingChangeCursor,
    MessagingChangeWait,
    MessagingCleanupReason,
    MessagingRunReference,
    MessagingStateQuery,
    MessagingStateSnapshot,
    MessagingTransition,
    StoredMessagingRun,
    StreamGenerationPurge,
)
from .errors import (
    InvalidCursor,
    MessagingBackendProtocolError,
    RunNotFound,
    RunProducerFailed,
    StreamDeleted,
    StreamExpired,
)
from .limits import MessagingLimits
from .models import MessageEnvelope, RecoveryCheckpoint
from .retention import MessagingRetentionPolicy


class _MessagingLedger:
    """Own Messaging lifecycle semantics while borrowing one storage backend."""

    def __init__(self, backend: MessagingBackend) -> None:
        self._backend = backend

    @property
    def backend(self) -> MessagingBackend:
        """Return the borrowed storage backend without transferring ownership."""

        return self._backend

    @property
    def limits(self) -> MessagingLimits:
        """Return immutable capacity limits used for transition validation."""

        return self._backend.messaging_settings.limits

    @property
    def retention_policy(self) -> MessagingRetentionPolicy:
        """Return the immutable terminal replay retention policy."""

        return self._backend.messaging_settings.retention_policy

    @property
    def lease_renew_interval(self) -> float | None:
        """Return the producer renewal interval in seconds, when enabled."""

        return self._backend.messaging_settings.producer_renew_interval_seconds

    @property
    def lease_timeout(self) -> float | None:
        """Return the producer ownership duration in seconds, when enabled."""

        return self._backend.messaging_settings.producer_lease_seconds

    async def prepare_storage(self) -> None:
        """Prepare the borrowed backend before any source can open."""

        await self._backend.prepare_messaging_storage()

    async def prepare(
        self,
        *,
        channel: str,
        identity: RunIdentity,
        codec: str,
        after: int | None,
        cancellable: bool,
        recoverable: bool,
    ) -> _PreparedRun:
        """Atomically start, recover, or attach to one semantic run."""

        if self.retention_policy.terminal_ttl_seconds is not None:
            await self._load_current_state(channel=channel, identity=identity)
        result = await self._backend.commit_messaging_transition(
            MessagingTransition(
                kind="prepare_run",
                transition_id=uuid4().hex,
                channel=channel,
                identity=identity,
                settings=self._backend.messaging_settings,
                codec_id=codec,
                after_sequence=after,
                cancellable=cancellable,
                recoverable=recoverable,
            )
        )
        reference = result.run_reference
        if (
            reference is None
            or result.after_sequence is None
            or result.is_producer_owner is None
        ):
            raise MessagingBackendProtocolError(
                "Messaging backend returned an incomplete preparation result"
            )
        return _PreparedRun(
            handle=self._internal_handle(reference),
            after=result.after_sequence,
            is_owner=result.is_producer_owner,
            checkpoint=result.checkpoint,
            recovered=result.recovered,
        )

    async def append(
        self,
        handle: _BackendRunHandle,
        *,
        message_id: str,
        codec: str,
        payload: bytes,
        checkpoint: RecoveryCheckpoint | None = None,
    ) -> MessageEnvelope:
        """Validate and idempotently commit one encoded message."""

        _validate_append_input(
            handle,
            message_id=message_id,
            codec=codec,
            payload=payload,
            checkpoint=checkpoint,
            limits=self.limits,
        )
        result = await self._backend.commit_messaging_transition(
            MessagingTransition(
                kind="append_message",
                transition_id=message_id,
                channel=handle.channel,
                identity=handle.identity,
                settings=self._backend.messaging_settings,
                run_reference=self._storage_reference(handle, require_owner=True),
                codec_id=codec,
                message_id=message_id,
                payload=payload,
                checkpoint=checkpoint,
            )
        )
        if result.envelope is None:
            raise MessagingBackendProtocolError(
                "Messaging backend returned no envelope for an append"
            )
        return result.envelope

    async def begin_settlement(self, handle: _BackendRunHandle) -> bool:
        """Atomically claim settlement and report earlier cancellation."""

        result = await self._backend.commit_messaging_transition(
            MessagingTransition(
                kind="begin_settlement",
                transition_id=uuid4().hex,
                channel=handle.channel,
                identity=handle.identity,
                settings=self._backend.messaging_settings,
                run_reference=self._storage_reference(handle, require_owner=True),
            )
        )
        accepted = result.cancellation_preceded_settlement
        if accepted is None:
            raise MessagingBackendProtocolError(
                "Messaging backend returned no settlement decision"
            )
        return accepted

    async def finish(
        self,
        handle: _BackendRunHandle,
        *,
        status: FinalRunStatus,
        error: BaseException | None = None,
    ) -> None:
        """Commit one terminal status and release producer ownership."""

        result = await self._backend.commit_messaging_transition(
            MessagingTransition(
                kind="finish_run",
                transition_id=uuid4().hex,
                channel=handle.channel,
                identity=handle.identity,
                settings=self._backend.messaging_settings,
                run_reference=self._storage_reference(handle, require_owner=True),
                final_status=status,
                failure=error,
            )
        )
        if result.run_status is None or not is_final_run_status(result.run_status):
            raise MessagingBackendProtocolError(
                "Messaging backend returned no terminal status"
            )

    async def latest_seq(self, *, channel: str, identity: RunIdentity) -> int:
        """Return the current generation tail or zero when no stream exists."""

        required_identifier("channel", channel)
        required_identity(identity)
        state = await self._load_current_state(
            channel=channel,
            identity=identity,
        )
        self._raise_tombstone(state, channel=channel, identity=identity)
        stream = state.stream
        if stream is None or stream.disposition in {"deleted", "deleting"}:
            return 0
        if stream.disposition in {"expired", "expiring"}:
            raise StreamExpired(
                channel=channel,
                identity=identity,
                generation=stream.generation,
            )
        return stream.latest_sequence

    async def get_run_status(
        self,
        *,
        channel: str,
        identity: RunIdentity,
    ) -> RunStatus:
        """Return one authoritative status after reconciling ownership loss."""

        required_identifier("channel", channel)
        required_identity(identity)
        state = await self._load_current_state(
            channel=channel,
            identity=identity,
        )
        if state.stream is None and state.tombstone_reason == "deleted":
            raise RunNotFound(identity=identity)
        self._raise_tombstone(state, channel=channel, identity=identity)
        result = await self._backend.commit_messaging_transition(
            MessagingTransition(
                kind="reconcile_producer_ownership",
                transition_id=uuid4().hex,
                channel=channel,
                identity=identity,
                settings=self._backend.messaging_settings,
            )
        )
        if result.run_status is None:
            raise MessagingBackendProtocolError(
                "Messaging backend returned no run status"
            )
        return result.run_status

    async def read(
        self,
        *,
        channel: str,
        identity: RunIdentity,
        after: int = 0,
        limit: int = 100,
    ) -> tuple[MessageEnvelope, ...]:
        """Read a finite current-generation page after a validated cursor."""

        self._validate_page(after=after, limit=limit)
        required_identifier("channel", channel)
        required_identity(identity)
        state = await self._load_current_state(
            channel=channel,
            identity=identity,
        )
        self._raise_tombstone(state, channel=channel, identity=identity)
        stream = state.stream
        if stream is None or stream.disposition in {"deleted", "deleting"}:
            if after > 0:
                raise InvalidCursor(after=after, latest=0)
            return ()
        if stream.disposition in {"expired", "expiring"}:
            raise StreamExpired(
                channel=channel,
                identity=identity,
                generation=stream.generation,
            )
        if after > stream.latest_sequence:
            raise InvalidCursor(after=after, latest=stream.latest_sequence)
        page = await self._backend.read_committed_messages(
            CommittedMessageQuery(
                channel=channel,
                identity=identity,
                generation=stream.generation,
                after_sequence=after,
                through_sequence=None,
                limit=limit,
            )
        )
        return page.messages

    async def bind_follow(
        self,
        *,
        channel: str,
        identity: RunIdentity,
        after: int,
    ) -> _BackendRunHandle:
        """Bind a follower to one exact generation after cursor validation."""

        self._validate_page(after=after, limit=1)
        required_identifier("channel", channel)
        required_identity(identity)
        state = await self._load_current_state(
            channel=channel,
            identity=identity,
        )
        self._raise_tombstone(state, channel=channel, identity=identity)
        stream = state.stream
        if stream is None or state.target_run is None:
            raise RunNotFound(identity=identity)
        self._raise_stream_disposition(state, channel=channel, identity=identity)
        if after > stream.latest_sequence:
            raise InvalidCursor(after=after, latest=stream.latest_sequence)
        return _BackendRunHandle(
            channel=channel,
            identity=identity,
            owner_token=None,
            fence=None,
            generation=stream.generation,
        )

    def follow(
        self,
        handle: _BackendRunHandle,
        *,
        after: int,
    ) -> AsyncGenerator[MessageEnvelope, None]:
        """Follow one exact run through its terminal committed boundary."""

        async def iterate() -> AsyncGenerator[MessageEnvelope, None]:
            generation = await self._resolve_handle_generation(handle)
            cursor = after
            while True:
                page = await self._backend.read_committed_messages(
                    CommittedMessageQuery(
                        channel=handle.channel,
                        identity=handle.identity,
                        generation=generation,
                        after_sequence=cursor,
                        through_sequence=None,
                        limit=1000,
                        stop_at_run_terminal=True,
                    )
                )
                run = page.run_state
                if run is None:
                    raise MessagingBackendProtocolError(
                        "Messaging backend omitted the requested run boundary"
                    )
                terminal = is_final_run_status(run.status)
                for message in page.messages:
                    cursor = message.seq
                    yield message
                if terminal and cursor >= run.end_sequence:
                    if is_failed_run_status(run.status):
                        raise RunProducerFailed(
                            identity=handle.identity,
                            cause=self._run_failure(run),
                        )
                    return
                if not terminal and not run.producer_lease_active:
                    await self._reconcile_bound_ownership(
                        self._with_generation(handle, generation)
                    )
                    continue
                if cursor < run.end_sequence:
                    continue
                await self._backend.wait_for_messaging_change(
                    MessagingChangeWait(
                        channel=handle.channel,
                        identity=handle.identity,
                        generation=generation,
                        after=MessagingChangeCursor(
                            message_sequence=page.change_cursor.message_sequence,
                            control_sequence=page.change_cursor.control_sequence,
                        ),
                        timeout_seconds=(self._change_wait_timeout(run)),
                    )
                )

        return iterate()

    async def request_cancel(self, handle: _BackendRunHandle) -> bool:
        """Request cancellation and report whether this call created it."""

        result = await self._backend.commit_messaging_transition(
            MessagingTransition(
                kind="request_cancellation",
                transition_id=uuid4().hex,
                channel=handle.channel,
                identity=handle.identity,
                settings=self._backend.messaging_settings,
                run_reference=(
                    None
                    if handle.generation is None
                    else self._storage_reference(handle, require_owner=False)
                ),
            )
        )
        initiated = result.cancellation_requested_by_transition
        if initiated is None:
            raise MessagingBackendProtocolError(
                "Messaging backend returned no cancellation decision"
            )
        return initiated

    async def wait_for_cancel(self, handle: _BackendRunHandle) -> bool:
        """Wait until cancellation is requested or the run becomes terminal."""

        generation = await self._resolve_handle_generation(handle)
        bound_handle = self._with_generation(handle, generation)
        while True:
            state = await self._load_bound_state(bound_handle)
            run = state.target_run
            stream = state.stream
            if run is None or stream is None:
                raise RunNotFound(identity=handle.identity)
            if run.status == "cancel_requested":
                return True
            if is_final_run_status(run.status):
                return False
            if not run.producer_lease_active:
                await self._reconcile_bound_ownership(bound_handle)
                continue
            await self._backend.wait_for_messaging_change(
                MessagingChangeWait(
                    channel=handle.channel,
                    identity=handle.identity,
                    generation=generation,
                    after=MessagingChangeCursor(
                        message_sequence=stream.message_sequence,
                        control_sequence=stream.control_sequence,
                    ),
                    timeout_seconds=(self._change_wait_timeout(run)),
                )
            )

    async def wait_finished(self, handle: _BackendRunHandle) -> RunStatus:
        """Wait for and return one exact run's terminal status."""

        generation = await self._resolve_handle_generation(handle)
        bound_handle = self._with_generation(handle, generation)
        while True:
            state = await self._load_bound_state(bound_handle)
            run = state.target_run
            stream = state.stream
            if run is None or stream is None:
                raise RunNotFound(identity=handle.identity)
            if is_final_run_status(run.status):
                return run.status
            if not run.producer_lease_active:
                await self._reconcile_bound_ownership(bound_handle)
                continue
            await self._backend.wait_for_messaging_change(
                MessagingChangeWait(
                    channel=handle.channel,
                    identity=handle.identity,
                    generation=generation,
                    after=MessagingChangeCursor(
                        message_sequence=stream.message_sequence,
                        control_sequence=stream.control_sequence,
                    ),
                    timeout_seconds=(self._change_wait_timeout(run)),
                )
            )

    async def failure(self, handle: _BackendRunHandle) -> BaseException | None:
        """Return trusted local or bounded remote failure evidence for one run."""

        generation = await self._resolve_handle_generation(handle)
        state = await self._load_bound_state(self._with_generation(handle, generation))
        run = state.target_run
        if run is None:
            raise RunNotFound(identity=handle.identity)
        if (
            not run.failure_class
            and not run.failure_message
            and run.local_failure is None
        ):
            return None
        return self._run_failure(run)

    async def renew(self, handle: _BackendRunHandle) -> bool:
        """Renew producer ownership and report whether its fence remains current."""

        result = await self._backend.commit_messaging_transition(
            MessagingTransition(
                kind="renew_producer_ownership",
                transition_id=uuid4().hex,
                channel=handle.channel,
                identity=handle.identity,
                settings=self._backend.messaging_settings,
                run_reference=self._storage_reference(handle, require_owner=True),
            )
        )
        confirmed = result.producer_ownership_confirmed
        if confirmed is None:
            raise MessagingBackendProtocolError(
                "Messaging backend returned no producer ownership decision"
            )
        return confirmed

    async def delete_stream(self, *, channel: str, identity: RunIdentity) -> None:
        """Logically seal, physically purge, and tombstone an inactive generation."""

        required_identifier("channel", channel)
        required_identity(identity)
        state = await self._load_current_state(
            channel=channel,
            identity=identity,
            include_active_run=True,
        )
        stream = state.stream
        if stream is None:
            return
        await self._cleanup_generation(
            channel=channel,
            identity=identity,
            reason="deleted",
        )

    async def _cleanup_generation(
        self,
        *,
        channel: str,
        identity: RunIdentity,
        reason: MessagingCleanupReason,
    ) -> None:
        begin = await self._backend.commit_messaging_transition(
            MessagingTransition(
                kind="begin_generation_cleanup",
                transition_id=uuid4().hex,
                channel=channel,
                identity=identity,
                settings=self._backend.messaging_settings,
                cleanup_reason=reason,
            )
        )
        if not begin.cleanup_required:
            return
        cleanup_generation = begin.cleanup_generation
        cleanup_reason = begin.cleanup_reason
        cleanup_token = begin.cleanup_token
        if cleanup_generation is None or cleanup_reason is None:
            raise MessagingBackendProtocolError(
                "Messaging backend returned incomplete generation cleanup evidence"
            )
        if cleanup_token is not None and not isinstance(cleanup_token, str):
            raise MessagingBackendProtocolError(
                "Messaging backend returned an invalid generation cleanup token"
            )
        while True:
            progress = await self._backend.purge_stream_generation(
                StreamGenerationPurge(
                    channel=channel,
                    identity=identity,
                    generation=cleanup_generation,
                    maximum_records=64,
                    cleanup_token=cleanup_token,
                )
            )
            if progress.complete:
                break
        await self._backend.commit_messaging_transition(
            MessagingTransition(
                kind="finish_generation_cleanup",
                transition_id=uuid4().hex,
                channel=channel,
                identity=identity,
                settings=self._backend.messaging_settings,
                cleanup_generation=cleanup_generation,
                cleanup_reason=cleanup_reason,
                cleanup_token=cleanup_token,
            )
        )

    async def _load_current_state(
        self,
        *,
        channel: str,
        identity: RunIdentity,
        include_active_run: bool = False,
    ) -> MessagingStateSnapshot:
        state = await self._backend.load_messaging_state(
            MessagingStateQuery(
                channel=channel,
                identity=identity,
                include_active_run=include_active_run,
            )
        )
        stream = state.stream
        if stream is None or not (
            stream.retention_expired or stream.disposition == "expiring"
        ):
            return state
        await self._cleanup_generation(
            channel=channel,
            identity=identity,
            reason="expired",
        )
        return await self._backend.load_messaging_state(
            MessagingStateQuery(
                channel=channel,
                identity=identity,
                include_active_run=include_active_run,
            )
        )

    async def _reconcile_bound_ownership(
        self,
        handle: _BackendRunHandle,
    ) -> RunStatus:
        """Commit ownership loss for one exact generation before waiting again."""

        result = await self._backend.commit_messaging_transition(
            MessagingTransition(
                kind="reconcile_producer_ownership",
                transition_id=uuid4().hex,
                channel=handle.channel,
                identity=handle.identity,
                settings=self._backend.messaging_settings,
                run_reference=self._storage_reference(handle, require_owner=False),
            )
        )
        if result.run_status is None:
            raise MessagingBackendProtocolError(
                "Messaging backend returned no reconciled run status"
            )
        return result.run_status

    async def _load_bound_state(
        self,
        handle: _BackendRunHandle,
    ) -> MessagingStateSnapshot:
        generation = self._required_generation(handle)
        state = await self._backend.load_messaging_state(
            MessagingStateQuery(
                channel=handle.channel,
                identity=handle.identity,
                generation=generation,
            )
        )
        self._raise_tombstone(
            state,
            channel=handle.channel,
            identity=handle.identity,
            generation=generation,
        )
        self._raise_stream_disposition(
            state,
            channel=handle.channel,
            identity=handle.identity,
        )
        return state

    async def _resolve_handle_generation(self, handle: _BackendRunHandle) -> int:
        generation = handle.generation
        if generation is not None:
            return generation
        state = await self._load_current_state(
            channel=handle.channel,
            identity=handle.identity,
        )
        self._raise_tombstone(
            state,
            channel=handle.channel,
            identity=handle.identity,
        )
        stream = state.stream
        if stream is None or state.target_run is None:
            raise RunNotFound(identity=handle.identity)
        self._raise_stream_disposition(
            state,
            channel=handle.channel,
            identity=handle.identity,
        )
        return stream.generation

    @staticmethod
    def _with_generation(
        handle: _BackendRunHandle,
        generation: int,
    ) -> _BackendRunHandle:
        if handle.generation == generation:
            return handle
        return _BackendRunHandle(
            channel=handle.channel,
            identity=handle.identity,
            owner_token=handle.owner_token,
            fence=handle.fence,
            generation=generation,
        )

    @staticmethod
    def _internal_handle(reference: MessagingRunReference) -> _BackendRunHandle:
        return _BackendRunHandle(
            channel=reference.channel,
            identity=reference.identity,
            owner_token=reference.producer_token,
            fence=reference.producer_fence,
            generation=reference.generation,
        )

    @staticmethod
    def _storage_reference(
        handle: _BackendRunHandle,
        *,
        require_owner: bool,
    ) -> MessagingRunReference:
        generation = _MessagingLedger._required_generation(handle)
        if require_owner and (handle.owner_token is None or handle.fence is None):
            raise ValueError("producer handle requires owner token and fence")
        return MessagingRunReference(
            channel=handle.channel,
            identity=handle.identity,
            generation=generation,
            producer_token=handle.owner_token,
            producer_fence=handle.fence,
        )

    @staticmethod
    def _required_generation(handle: _BackendRunHandle) -> int:
        generation = handle.generation
        if generation is None:
            raise ValueError("bound handle requires a generation")
        return generation

    @staticmethod
    def _validate_page(*, after: int, limit: int) -> None:
        if isinstance(after, bool) or not isinstance(after, int):
            raise TypeError("after must be an integer")
        if after < 0:
            raise ValueError("after must be greater than or equal to zero")
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError("limit must be an integer")
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")

    @staticmethod
    def _raise_tombstone(
        state: MessagingStateSnapshot,
        *,
        channel: str,
        identity: RunIdentity,
        generation: int | None = None,
    ) -> None:
        tombstone_generation = state.tombstone_generation
        if tombstone_generation is None:
            return
        if generation is None and state.stream is not None:
            return
        if generation is None and state.tombstone_reason == "deleted":
            return
        if generation is not None and generation != tombstone_generation:
            return
        if state.tombstone_reason == "expired":
            raise StreamExpired(
                channel=channel,
                identity=identity,
                generation=tombstone_generation,
            )
        if state.tombstone_reason == "deleted":
            raise StreamDeleted(
                channel=channel,
                identity=identity,
                generation=tombstone_generation,
            )

    @staticmethod
    def _raise_stream_disposition(
        state: MessagingStateSnapshot,
        *,
        channel: str,
        identity: RunIdentity,
    ) -> None:
        stream = state.stream
        if stream is None:
            return
        if stream.disposition in {"expiring", "expired"}:
            raise StreamExpired(
                channel=channel,
                identity=identity,
                generation=stream.generation,
            )
        if stream.disposition in {"deleting", "deleted"}:
            raise StreamDeleted(
                channel=channel,
                identity=identity,
                generation=stream.generation,
            )

    @staticmethod
    def _run_failure(run: StoredMessagingRun) -> BaseException:
        if run.local_failure is not None:
            return run.local_failure
        summary = run.failure_message or "producer stopped without failure details"
        if run.failure_class:
            summary = f"{run.failure_class}: {summary}"
        return RuntimeError(summary)

    def _change_wait_timeout(self, run: StoredMessagingRun) -> float | None:
        configured = self._backend.messaging_settings.change_wait_timeout_seconds
        remaining = run.producer_lease_remaining_seconds
        if remaining is None:
            return configured
        if configured is None:
            return max(0.001, remaining)
        return max(0.001, min(configured, remaining))


__all__ = ["_MessagingLedger"]
