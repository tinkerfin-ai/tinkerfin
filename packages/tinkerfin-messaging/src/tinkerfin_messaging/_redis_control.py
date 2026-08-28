"""Redis control-plane, lease, deletion, and snapshot operations."""

from __future__ import annotations

__all__ = [
    "_delete_owned_generation",
    "_eval",
    "_is_current_generation",
    "_keys",
    "_keys_for_handle",
    "_raise_stream_deleted",
    "_read_control",
    "_run_snapshot",
    "_scope",
    "_settled_run_snapshot",
    "_snapshot_bytes",
    "_snapshot_integer",
    "_snapshot_text",
    "_socket_timeout_budget",
    "_wait_block_ms",
    "_wait_for_snapshot_change",
    "get_run_status",
]

import asyncio
import math
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Never, Protocol, TypeAlias, TypeVar, cast
from uuid import uuid4

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError

from tinkerfin_contracts import RunIdentity

from ._identity import required_identifier, required_identity
from ._redis_scripts import (
    _BEGIN_DELETE_SCRIPT,
    _BEGIN_EXPIRATION_SCRIPT,
    _BEGIN_SETTLEMENT_SCRIPT,
    _CANCEL_SCRIPT,
    _DELETE_BATCH_SCRIPT,
    _FINALIZE_DELETE_SCRIPT,
    _FINISH_SCRIPT,
    _READ_CONTROL_SCRIPT,
    _RENEW_SCRIPT,
    _RUN_SNAPSHOT_SCRIPT,
)
from .backend import BackendRunHandle, FinalRunStatus, RunStatus
from .errors import (
    BackendOwnershipLost,
    CancellationUnsupported,
    MessagingBackendProtocolError,
    MessagingBackendTimeout,
    MessagingBackendUnavailable,
    RunNotFound,
    StreamDeleteConflict,
    StreamDeleted,
    StreamExpired,
    UnexpectedMessagingBackendError,
)
from .models import MessageEnvelope

if TYPE_CHECKING:
    from .redis import RedisBackend


_SNAPSHOT_PAGE_SIZE = 100
_MAX_WAIT_BLOCK_MS = 5_000
_SOCKET_TIMEOUT_SAFETY_RATIO = 0.9

_RedisScriptValue: TypeAlias = bytes | list["_RedisScriptValue"]
_RedisStreamEntry: TypeAlias = tuple[bytes, dict[bytes, bytes]]
_RedisStreamRead: TypeAlias = list[tuple[bytes, list[_RedisStreamEntry]]]
_RedisResultT = TypeVar("_RedisResultT")


async def _redis_call(
    operation: str,
    awaitable: Awaitable[_RedisResultT],
) -> _RedisResultT:
    """Translate Redis transport failures without intercepting cancellation.

    Client-safe errors expose only the logical operation. The concrete Redis failure is
    retained as trusted causal evidence and never serialized into a durable message.
    """

    try:
        return await awaitable
    except RedisTimeoutError as error:
        translated = MessagingBackendTimeout(
            f"Messaging backend {operation} timed out",
            diagnostic_context={
                "implementation": "redis",
                "operation": operation,
            },
            cause=error,
        )
        raise translated from error
    except (RedisConnectionError, OSError) as error:
        translated = MessagingBackendUnavailable(
            f"Messaging backend is unavailable for {operation}",
            diagnostic_context={
                "implementation": "redis",
                "operation": operation,
            },
            cause=error,
        )
        raise translated from error
    except RedisError as error:
        translated = UnexpectedMessagingBackendError(
            f"Messaging backend {operation} failed",
            diagnostic_context={
                "implementation": "redis",
                "operation": operation,
            },
            cause=error,
        )
        raise translated from error


def _redis_protocol_error(
    detail: str,
    *,
    cause: BaseException | None = None,
) -> MessagingBackendProtocolError:
    """Separate a provider-neutral message from trusted Redis diagnostics."""

    return MessagingBackendProtocolError(
        "Messaging backend returned an invalid protocol response",
        diagnostic_context={
            "implementation": "redis",
            "operation": "protocol_validation",
            "detail": detail,
        },
        cause=cause,
    )


class _RedisConnection(Protocol):
    """Expose the cancellation-safe disconnect operation used by blocking reads."""

    async def disconnect(self, *, nowait: bool = False) -> None: ...


class _AsyncRedisClient(Protocol):
    """Describe the Redis asyncio surface used after runtime client validation.

    Redis 6 and 7 annotate several asyncio commands as a union of synchronous and
    awaitable results. The actual ``redis.asyncio.Redis`` client always returns the
    awaitable branch; this protocol records that runtime boundary without requiring
    Redis 8-only response aliases.
    """

    connection: _RedisConnection | None

    def get_connection_kwargs(self) -> dict[str, object]: ...

    def client(self) -> _AsyncRedisClient: ...

    async def initialize(self) -> _AsyncRedisClient: ...

    async def aclose(self) -> None: ...

    async def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: str | bytes,
    ) -> object: ...

    async def hget(self, name: str, key: str) -> bytes | None: ...

    async def get(self, name: str) -> bytes | None: ...

    async def hgetall(self, name: str) -> dict[bytes, bytes]: ...

    async def exists(self, *names: str) -> int: ...

    async def srandmember(
        self,
        name: str,
        number: int | None = None,
    ) -> bytes | list[bytes] | None: ...

    async def xrange(
        self,
        name: str,
        min: str,
        max: str,
        count: int | None = None,
    ) -> list[_RedisStreamEntry]: ...

    async def xread(
        self,
        streams: Mapping[str, str],
        count: int | None = None,
        block: int | None = None,
    ) -> _RedisStreamRead: ...


_ControlState = Literal[
    "active",
    "deleting",
    "deleted",
    "expiring",
    "expired",
]


@dataclass(frozen=True, slots=True)
class _RedisStreamScope:
    """Name the persistent keys shared by every generation of one stream."""

    channel_meta: str
    control: str
    delete_lease: str
    signals: str
    base: str
    stream_base: str


@dataclass(frozen=True, slots=True)
class _RedisKeys:
    """Name the shared and generation-private keys for one run lookup."""

    channel: str
    identity: RunIdentity
    channel_meta: str
    control: str
    delete_lease: str
    signals: str
    meta: str
    run_key: str
    lease_key: str
    messages: str
    index: str
    tombstone: str
    base: str
    stream_base: str
    generation_base: str
    generation: int


@dataclass(frozen=True, slots=True)
class _RedisGenerationKeys:
    """Name only the keys required for resumable generation cleanup."""

    control: str
    delete_lease: str
    index: str
    tombstone: str
    generation: int


class _GenerationCleanupKeys(Protocol):
    """Structural key subset shared by explicit deletion and expiry."""

    @property
    def control(self) -> str: ...

    @property
    def delete_lease(self) -> str: ...

    @property
    def index(self) -> str: ...

    @property
    def tombstone(self) -> str: ...

    @property
    def generation(self) -> int: ...


@dataclass(frozen=True, slots=True)
class _StreamControl:
    """Describe the authoritative generation and deletion state."""

    generation: int
    state: _ControlState


@dataclass(frozen=True, slots=True)
class _RunSnapshot:
    """Hold one generation-fenced run view returned by a single Redis script."""

    status: RunStatus
    end_seq: int
    error_class: str
    error_message: str
    signal_cursor: int
    lease_ttl_ms: int
    lease_renew_count: int
    lease_last_success_seconds: int
    lease_last_success_microseconds: int
    messages: tuple[MessageEnvelope, ...]

    @property
    def terminal(self) -> bool:
        """Return whether the snapshot fixes the run's final message boundary."""

        return self.status in {"completed", "cancelled", "failed", "owner_lost"}


async def begin_settlement(self: RedisBackend, handle: BackendRunHandle) -> bool:
    """Atomically choose an accepted cancellation or ordinary settlement."""

    generation = handle.generation
    if handle.owner_token is None or handle.fence is None or generation is None:
        raise BackendOwnershipLost(
            f"Run {handle.identity.run_id!r} has no complete producer ownership "
            "identity"
        )
    keys = self._keys(
        handle.channel,
        handle.identity,
        generation=generation,
    )
    response = await self._eval(
        _BEGIN_SETTLEMENT_SCRIPT,
        [keys.control, keys.run_key, keys.lease_key],
        [str(generation), handle.owner_token, str(handle.fence)],
    )
    code = self._text(response[0])
    if code == "CANCEL_REQUESTED":
        return True
    if code == "SETTLING":
        return False
    if code == "STREAM_DELETED":
        self._raise_stream_deleted(handle)
    if code == "OWNERSHIP_LOST":
        raise BackendOwnershipLost(
            f"Producer for run {handle.identity.run_id!r} lost its Redis fence"
        )
    raise _redis_protocol_error(f"unexpected Redis settlement response: {code}")


async def finish(
    self: RedisBackend,
    handle: BackendRunHandle,
    *,
    status: FinalRunStatus,
    error: BaseException | None = None,
) -> None:
    """Commit one terminal status only for the current generation and fence owner.

    The Lua boundary verifies generation, owner token, and monotonic fence together, so
    a stale producer cannot overwrite a replacement owner's result. Failure class and
    message are operational diagnostics, not user Trace facts.

    Args:
        self: Redis Backend owning the current producer lease.
        handle: Exact generation, owner token, and monotonic fence.
        status: Authoritative terminal producer outcome.
        error: Optional trusted producer failure retained as bounded diagnostics.

    Raises:
        BackendOwnershipLost: The producer no longer owns the exact generation.
        MessagingError: Redis cannot commit or prove the terminal transition.
    """

    generation = handle.generation
    if handle.owner_token is None or handle.fence is None or generation is None:
        raise BackendOwnershipLost(
            f"Run {handle.identity.run_id!r} has no complete producer ownership "
            "identity"
        )
    keys = self._keys(
        handle.channel,
        handle.identity,
        generation=generation,
    )
    error_class = "" if error is None else self._qualified_name(error)
    error_message = "" if error is None else str(error)
    response = await self._eval(
        _FINISH_SCRIPT,
        [
            keys.control,
            keys.meta,
            keys.run_key,
            keys.lease_key,
            keys.signals,
        ],
        [
            str(generation),
            handle.owner_token,
            str(handle.fence),
            status,
            error_class,
            error_message,
            str(self._retention_ms),
        ],
    )
    code = self._text(response[0])
    if code == "STREAM_DELETED":
        self._raise_stream_deleted(handle)
    if code == "OWNERSHIP_LOST":
        raise BackendOwnershipLost(
            f"Producer for run {handle.identity.run_id!r} lost its Redis fence"
        )


async def request_cancel(self: RedisBackend, handle: BackendRunHandle) -> bool:
    """Record one idempotent cancellation request for a cancellable active run."""

    keys = await self._keys_for_handle(handle)
    await self._settled_run_snapshot(keys, handle.identity)
    response = await self._eval(
        _CANCEL_SCRIPT,
        [keys.control, keys.run_key, keys.signals],
        [str(keys.generation)],
    )
    code = self._text(response[0])
    if code == "STREAM_DELETED":
        self._raise_stream_deleted(handle, generation=keys.generation)
    if code == "NOT_FOUND":
        raise RunNotFound(identity=handle.identity)
    if code == "UNSUPPORTED":
        raise CancellationUnsupported(identity=handle.identity)
    if code in {"FINAL", "DUPLICATE"}:
        return False
    if code != "REQUESTED":
        raise _redis_protocol_error(f"unexpected Redis cancel response: {code}")
    return True


async def wait_for_cancel(self: RedisBackend, handle: BackendRunHandle) -> bool:
    """Wait for cancellation or terminal settlement without polling past deletion."""

    keys = await self._keys_for_handle(handle)
    while True:
        snapshot = await self._settled_run_snapshot(keys, handle.identity)
        if snapshot.status == "cancel_requested":
            return True
        if snapshot.terminal:
            return False
        await self._wait_for_snapshot_change(keys, snapshot)


async def wait_finished(self: RedisBackend, handle: BackendRunHandle) -> RunStatus:
    """Wait until the bound generation reaches one authoritative terminal status."""

    keys = await self._keys_for_handle(handle)
    while True:
        snapshot = await self._settled_run_snapshot(keys, handle.identity)
        if snapshot.terminal:
            return snapshot.status
        await self._wait_for_snapshot_change(keys, snapshot)


async def get_run_status(
    self: RedisBackend,
    *,
    channel: str,
    identity: RunIdentity,
) -> RunStatus:
    """Return current status while atomically archiving an expired owner lease."""

    required_identifier("channel", channel)
    required_identity(identity)
    scope = self._scope(channel, identity)
    while True:
        control = await self._read_control(scope)
        if control is not None and control.state == "expired":
            raise StreamExpired(
                channel=channel,
                identity=identity,
                generation=control.generation,
            )
        if control is None or control.state != "active":
            raise RunNotFound(identity=identity)
        keys = self._keys(
            channel,
            identity,
            generation=control.generation,
        )
        try:
            snapshot = await self._settled_run_snapshot(keys, identity)
        except StreamDeleted:
            # RunIdentity-only lookups follow the current generation. A stale bound
            # handle still receives StreamDeleted through _keys_for_handle().
            continue
        return snapshot.status


async def failure(self: RedisBackend, handle: BackendRunHandle) -> BaseException | None:
    """Reconstruct bounded remote failure evidence for a settled producer."""

    keys = await self._keys_for_handle(handle)
    snapshot = await self._settled_run_snapshot(keys, handle.identity)
    if not snapshot.error_class and not snapshot.error_message:
        return None
    return self._remote_error(snapshot)


async def renew(self: RedisBackend, handle: BackendRunHandle) -> bool:
    """Renew a lease only while its complete fencing identity still matches."""

    generation = handle.generation
    if handle.owner_token is None or handle.fence is None or generation is None:
        return False
    keys = self._keys(
        handle.channel,
        handle.identity,
        generation=generation,
    )
    expected = f"{handle.owner_token}:{handle.fence}"
    response = await self._eval(
        _RENEW_SCRIPT,
        [keys.control, keys.run_key, keys.lease_key],
        [str(generation), expected, str(self._lease_ms)],
    )
    code = self._text(response[0])
    if code == "STREAM_DELETED":
        self._raise_stream_deleted(handle)
    if code == "OWNERSHIP_LOST":
        return False
    if code != "RENEWED" or len(response) != 4:
        raise _redis_protocol_error(f"unexpected Redis renew response: {code}")
    self._snapshot_integer(
        response[1],
        field="lease renew count",
        minimum=1,
    )
    self._snapshot_integer(
        response[2],
        field="lease last success seconds",
        minimum=0,
    )
    self._snapshot_integer(
        response[3],
        field="lease last success microseconds",
        minimum=0,
    )
    return True


async def delete_stream(
    self: RedisBackend, *, channel: str, identity: RunIdentity
) -> None:
    """Delete one stream through a leased, generation-fenced cleanup."""

    required_identifier("channel", channel)
    required_identity(identity)
    scope = self._scope(channel, identity)
    delete_owner = f"{self._worker_id}:delete:{uuid4().hex}"
    while True:
        control = await self._read_control(scope)
        generation = 1 if control is None else control.generation
        keys = self._keys(
            channel,
            identity,
            generation=generation,
        )
        expected_active_lease = ""
        active_lease_key = f"{keys.generation_base}:no-active-lease"
        if control is not None and control.state == "active":
            active_lease = await _redis_call(
                "active lease lookup",
                self._client.hget(keys.meta, "active_lease"),
            )
            if active_lease is not None:
                expected_active_lease = self._text(active_lease)
                active_lease_key = expected_active_lease
        response = await self._eval(
            _BEGIN_DELETE_SCRIPT,
            [
                keys.control,
                keys.meta,
                keys.delete_lease,
                active_lease_key,
                keys.signals,
            ],
            [
                str(generation),
                delete_owner,
                str(self._lease_ms),
                expected_active_lease,
            ],
        )
        code = self._text(response[0])
        if code == "DONE":
            return
        if code == "ACTIVE":
            raise StreamDeleteConflict(
                channel=channel,
                identity=identity,
                active_identity=RunIdentity(
                    threadId=identity.thread_id,
                    runId=self._text(response[1]),
                ),
            )
        if code in {"RETRY", "LEASE_LOST"}:
            continue
        if code == "WAIT":
            await asyncio.sleep(self._poll_interval)
            continue
        if code == "INVALID_CONTROL_STATE":
            raise _redis_protocol_error(
                f"Redis stream control has invalid state: {self._text(response[1])!r}"
            )
        if code != "OWNED":
            raise _redis_protocol_error(f"unexpected Redis delete response: {code}")
        if await self._delete_owned_generation(
            keys=keys,
            delete_owner=delete_owner,
        ):
            return


async def _delete_owned_generation(
    self: RedisBackend,
    *,
    keys: _GenerationCleanupKeys,
    delete_owner: str,
    working_state: Literal["deleting", "expiring"] = "deleting",
    final_state: Literal["deleted", "expired"] = "deleted",
) -> bool:
    """Clear one fenced generation while periodically renewing ownership."""

    while True:
        raw_members = await _redis_call(
            "stream index read",
            self._client.srandmember(keys.index, number=64),
        )
        members = tuple(
            self._text(member)
            for member in cast(Sequence[bytes | str], raw_members or ())
        )
        if members:
            response = await self._eval(
                _DELETE_BATCH_SCRIPT,
                [keys.control, keys.delete_lease, keys.index, *members],
                [
                    str(keys.generation),
                    delete_owner,
                    str(self._lease_ms),
                    working_state,
                ],
            )
            code = self._text(response[0])
            if code in {"RETRY", "LEASE_LOST"}:
                return False
            if code != "OK":
                raise _redis_protocol_error(
                    f"unexpected Redis delete batch response: {code}"
                )
            continue

        response = await self._eval(
            _FINALIZE_DELETE_SCRIPT,
            [keys.control, keys.delete_lease, keys.index, keys.tombstone],
            [
                str(keys.generation),
                delete_owner,
                working_state,
                final_state,
            ],
        )
        code = self._text(response[0])
        if code == "DONE":
            return True
        if code == "MORE":
            continue
        if code in {"RETRY", "LEASE_LOST"}:
            return False
        raise _redis_protocol_error(
            f"unexpected Redis delete finalization response: {code}"
        )


async def _expire_generation(
    self: RedisBackend,
    scope: _RedisStreamScope,
    *,
    generation: int,
) -> None:
    """Resume lazy physical cleanup after the Redis deadline becomes authoritative."""

    generation_base = f"{scope.stream_base}:generation:{generation}"
    keys = _RedisGenerationKeys(
        control=scope.control,
        delete_lease=scope.delete_lease,
        index=f"{generation_base}:index",
        tombstone=f"{generation_base}:tombstone",
        generation=generation,
    )
    delete_owner = f"{self._worker_id}:expire:{uuid4().hex}"
    while True:
        response = await self._eval(
            _BEGIN_EXPIRATION_SCRIPT,
            [keys.control, keys.delete_lease],
            [str(generation), delete_owner, str(self._lease_ms)],
        )
        code = self._text(response[0])
        if code == "DONE":
            return
        if code == "WAIT":
            await asyncio.sleep(self._poll_interval)
            continue
        if code == "RETRY":
            return
        if code != "OWNED":
            raise _redis_protocol_error(f"unexpected Redis expiration response: {code}")
        completed = await _delete_owned_generation(
            self,
            keys=keys,
            delete_owner=delete_owner,
            working_state="expiring",
            final_state="expired",
        )
        if completed:
            return


def _scope(
    self: RedisBackend, channel: str, identity: RunIdentity
) -> _RedisStreamScope:
    channel_scope = self._digest(channel)
    base = f"{self._prefix}:{{{channel_scope}}}"
    stream_digest = self._digest(identity.thread_id)
    stream_base = f"{base}:stream:{stream_digest}"
    return _RedisStreamScope(
        channel_meta=f"{base}:channel",
        control=f"{stream_base}:control",
        delete_lease=f"{stream_base}:delete-lease",
        signals=f"{stream_base}:signals",
        base=base,
        stream_base=stream_base,
    )


def _keys(
    self: RedisBackend,
    channel: str,
    identity: RunIdentity,
    *,
    generation: int,
) -> _RedisKeys:
    """Derive generation-scoped keys under one Redis Cluster hash slot.

    Channel and thread digests select the shared hash tag; generation and run digests
    then isolate replacement histories and producer ownership without exposing caller
    identifiers in raw Redis keys.
    """

    scope = self._scope(channel, identity)
    generation_base = f"{scope.stream_base}:generation:{generation}"
    run_digest = self._digest(identity.run_id)
    return _RedisKeys(
        channel=channel,
        identity=identity,
        channel_meta=scope.channel_meta,
        control=scope.control,
        delete_lease=scope.delete_lease,
        signals=scope.signals,
        meta=f"{generation_base}:meta",
        run_key=f"{generation_base}:run:{run_digest}",
        lease_key=f"{generation_base}:lease:{run_digest}",
        messages=f"{generation_base}:messages",
        index=f"{generation_base}:index",
        tombstone=f"{generation_base}:tombstone",
        base=scope.base,
        stream_base=scope.stream_base,
        generation_base=generation_base,
        generation=generation,
    )


async def _read_control(
    self: RedisBackend,
    scope: _RedisStreamScope,
) -> _StreamControl | None:
    """Read control, atomically recognize deadlines, and resume expired cleanup."""

    while True:
        raw_response = await _redis_call(
            "stream control read",
            self._client.eval(
                _READ_CONTROL_SCRIPT,
                1,
                scope.control,
            ),
        )
        if not isinstance(raw_response, list):
            raise _redis_protocol_error(
                "Redis stream control returned a non-list response"
            )
        response = cast(list[_RedisScriptValue], raw_response)
        if not response:
            raise _redis_protocol_error("Redis stream control returned no fields")
        code = self._snapshot_text(response[0], field="control response code")
        if code == "NONE":
            return None
        if code != "OK" or len(response) != 3:
            raise _redis_protocol_error(f"unexpected Redis control response: {code}")
        try:
            generation = int(
                self._snapshot_text(response[1], field="control generation")
            )
        except ValueError as error:
            raise _redis_protocol_error(
                "Redis stream control has invalid generation",
                cause=error,
            ) from error
        if generation < 1:
            raise _redis_protocol_error("Redis stream control has invalid generation")
        state = self._snapshot_text(response[2], field="control state")
        if state not in {"active", "deleting", "deleted", "expiring", "expired"}:
            raise _redis_protocol_error(
                f"Redis stream control has invalid state: {state!r}"
            )
        if state == "expiring":
            await _expire_generation(self, scope, generation=generation)
            continue
        return _StreamControl(
            generation=generation,
            state=cast(_ControlState, state),
        )


async def _keys_for_handle(self: RedisBackend, handle: BackendRunHandle) -> _RedisKeys:
    generation = handle.generation
    if generation is None:
        scope = self._scope(handle.channel, handle.identity)
        control = await self._read_control(scope)
        if control is None:
            raise RunNotFound(identity=handle.identity)
        if control.state != "active":
            await _raise_generation_unavailable(
                self,
                handle,
                generation=control.generation,
            )
        generation = control.generation
    keys = self._keys(
        handle.channel,
        handle.identity,
        generation=generation,
    )
    if handle.generation is not None:
        reason = await _redis_call(
            "generation tombstone lookup",
            self._client.get(keys.tombstone),
        )
        if reason is not None:
            if self._text(reason) == "expired":
                raise StreamExpired(
                    channel=handle.channel,
                    identity=handle.identity,
                    generation=generation,
                )
            self._raise_stream_deleted(handle, generation=generation)
    return keys


async def _raise_generation_unavailable(
    self: RedisBackend,
    handle: BackendRunHandle,
    *,
    generation: int,
) -> Never:
    """Distinguish retention expiry from explicit deletion for a stale handle."""

    keys = self._keys(
        handle.channel,
        handle.identity,
        generation=generation,
    )
    reason = await _redis_call(
        "generation tombstone lookup",
        self._client.get(keys.tombstone),
    )
    if reason is not None and self._text(reason) == "expired":
        raise StreamExpired(
            channel=handle.channel,
            identity=handle.identity,
            generation=generation,
        )
    self._raise_stream_deleted(handle, generation=generation)


async def _is_current_generation(self: RedisBackend, keys: _RedisKeys) -> bool:
    control = await self._read_control(
        _RedisStreamScope(
            channel_meta=keys.channel_meta,
            control=keys.control,
            delete_lease=keys.delete_lease,
            signals=keys.signals,
            base=keys.base,
            stream_base=keys.stream_base,
        )
    )
    return (
        control is not None
        and control.state == "active"
        and control.generation == keys.generation
    )


async def _run_snapshot(
    self: RedisBackend,
    keys: _RedisKeys,
    identity: RunIdentity,
    *,
    after: int | None = None,
) -> _RunSnapshot:
    """Read one authoritative run state and optional bounded message page."""

    raw_response = await _redis_call(
        "run snapshot",
        self._client.eval(
            _RUN_SNAPSHOT_SCRIPT,
            6,
            keys.control,
            keys.meta,
            keys.run_key,
            keys.lease_key,
            keys.messages,
            keys.signals,
            str(keys.generation),
            "__none__" if after is None else str(after),
            str(self._retention_ms),
        ),
    )
    if not isinstance(raw_response, list):
        raise _redis_protocol_error("Redis run snapshot returned a non-list response")
    response = cast(list[_RedisScriptValue], raw_response)
    if not response:
        raise _redis_protocol_error("Redis run snapshot returned an empty response")
    code = self._snapshot_text(response[0], field="response code")
    if code == "STREAM_DELETED":
        raise StreamDeleted(
            channel=keys.channel,
            identity=keys.identity,
            generation=keys.generation,
        )
    if code == "NOT_FOUND":
        raise RunNotFound(identity=identity)
    if code == "INVALID_STATUS":
        status = (
            self._snapshot_text(response[1], field="invalid status")
            if len(response) > 1
            else ""
        )
        raise _redis_protocol_error(
            f"Redis run snapshot has invalid status: {status!r}"
        )
    if code == "INVALID_BOUNDARY":
        raise _redis_protocol_error(
            "Redis run snapshot has an invalid message boundary"
        )
    if code != "OK" or len(response) != 11:
        raise _redis_protocol_error(f"unexpected Redis run snapshot response: {code}")

    status_text = self._snapshot_text(response[1], field="status")
    if status_text not in {
        "running",
        "cancel_requested",
        "completed",
        "cancelled",
        "failed",
        "owner_lost",
    }:
        raise _redis_protocol_error(
            f"Redis run snapshot has invalid status: {status_text!r}"
        )
    end_seq = self._snapshot_integer(
        response[2],
        field="end_seq",
        minimum=0,
    )
    error_class = self._snapshot_text(response[3], field="error_class")
    error_message = self._snapshot_text(response[4], field="error_message")
    signal_cursor = self._snapshot_integer(
        response[5],
        field="signal cursor",
        minimum=0,
    )
    lease_ttl_ms = self._snapshot_integer(
        response[6],
        field="lease TTL",
        minimum=-2,
    )
    lease_renew_count = self._snapshot_integer(
        response[7],
        field="lease renew count",
        minimum=0,
    )
    lease_last_success_seconds = self._snapshot_integer(
        response[8],
        field="lease last success seconds",
        minimum=0,
    )
    lease_last_success_microseconds = self._snapshot_integer(
        response[9],
        field="lease last success microseconds",
        minimum=0,
    )
    messages = self._snapshot_messages(
        response[10],
        channel=keys.channel,
        identity=keys.identity,
        after=after,
        end_seq=end_seq,
    )
    return _RunSnapshot(
        status=cast(RunStatus, status_text),
        end_seq=end_seq,
        error_class=error_class,
        error_message=error_message,
        signal_cursor=signal_cursor,
        lease_ttl_ms=lease_ttl_ms,
        lease_renew_count=lease_renew_count,
        lease_last_success_seconds=lease_last_success_seconds,
        lease_last_success_microseconds=lease_last_success_microseconds,
        messages=messages,
    )


async def _settled_run_snapshot(
    self: RedisBackend,
    keys: _RedisKeys,
    identity: RunIdentity,
    *,
    after: int | None = None,
) -> _RunSnapshot:
    """Settle one potentially mutating Lua snapshot before caller cancellation."""

    snapshot_task = asyncio.create_task(
        self._run_snapshot(keys, identity, after=after),
        name=f"tinkerfin-messaging-redis-snapshot:{identity.run_id}",
    )
    current = asyncio.current_task()
    cancel_count = current.cancelling() if current is not None else 0
    caller_cancellation: asyncio.CancelledError | None = None
    while not snapshot_task.done():
        try:
            await asyncio.shield(snapshot_task)
        except asyncio.CancelledError as cancellation:
            next_cancel_count = current.cancelling() if current is not None else 0
            if next_cancel_count > cancel_count:
                if caller_cancellation is None:
                    caller_cancellation = cancellation
                cancel_count = next_cancel_count
                continue
            if snapshot_task.done():
                break
            raise
        except BaseException:
            if snapshot_task.done():
                break
            raise

    snapshot_error: BaseException | None = None
    snapshot: _RunSnapshot | None = None
    try:
        snapshot = snapshot_task.result()
    except BaseException as error:  # noqa: BLE001 - preserve Redis outcome
        snapshot_error = error
    if caller_cancellation is not None:
        if snapshot_error is not None:
            caller_cancellation.add_note(
                "Redis run snapshot also failed during cancellation: "
                f"{type(snapshot_error).__name__}: {snapshot_error}"
            )
        raise caller_cancellation.with_traceback(caller_cancellation.__traceback__)
    if snapshot_error is not None:
        raise snapshot_error.with_traceback(snapshot_error.__traceback__)
    assert snapshot is not None
    return snapshot


async def _wait_for_snapshot_change(
    self: RedisBackend,
    keys: _RedisKeys,
    snapshot: _RunSnapshot,
) -> None:
    """Block on durable data or lifecycle signals, then require a new snapshot."""

    blocking_client = self._client.client()
    read_task: asyncio.Task[None] | None = None
    try:
        await _redis_call(
            "blocking client initialization",
            blocking_client.initialize(),
        )

        async def read() -> None:
            await _redis_call(
                "stream wait",
                blocking_client.xread(
                    {
                        keys.messages: f"{snapshot.end_seq}-0",
                        keys.signals: f"{snapshot.signal_cursor}-0",
                    },
                    count=1,
                    block=self._wait_block_ms(snapshot.lease_ttl_ms),
                ),
            )

        read_task = asyncio.create_task(
            read(),
            name="tinkerfin-messaging-redis-xread",
        )
        try:
            await asyncio.shield(read_task)
        except asyncio.CancelledError as cancellation:
            connection = blocking_client.connection
            if connection is not None:
                await asyncio.shield(
                    _redis_call(
                        "blocking read cancellation",
                        connection.disconnect(nowait=True),
                    )
                )
            read_task.cancel()
            await asyncio.gather(read_task, return_exceptions=True)
            raise cancellation.with_traceback(cancellation.__traceback__)
    finally:
        if read_task is not None and not read_task.done():
            read_task.cancel()
            await asyncio.gather(read_task, return_exceptions=True)
        await asyncio.shield(
            _redis_call("blocking client close", blocking_client.aclose())
        )


def _wait_block_ms(self: RedisBackend, lease_ttl_ms: int) -> int:
    """Bound XREAD by lease expiry, fallback progress, and socket timeout."""

    candidates = [_MAX_WAIT_BLOCK_MS]
    if lease_ttl_ms >= 0:
        candidates.append(max(1, lease_ttl_ms))
    if self._socket_timeout_budget_ms is not None:
        candidates.append(self._socket_timeout_budget_ms)
    return max(1, min(candidates))


def _socket_timeout_budget(value: object) -> int | None:
    """Return a positive XREAD budget below the Redis socket timeout."""

    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    timeout = float(value)
    if not math.isfinite(timeout) or timeout <= 0:
        return None
    return max(
        1,
        math.floor(timeout * 1000 * _SOCKET_TIMEOUT_SAFETY_RATIO),
    )


def _snapshot_integer(
    cls: type[RedisBackend],
    value: _RedisScriptValue,
    *,
    field: str,
    minimum: int,
) -> int:
    """Decode one canonical integer from a snapshot scalar."""

    text = cls._snapshot_text(value, field=field)
    try:
        parsed = int(text)
    except ValueError as error:
        raise _redis_protocol_error(
            f"Redis run snapshot has invalid {field}",
            cause=error,
        ) from error
    if str(parsed) != text or parsed < minimum:
        raise _redis_protocol_error(f"Redis run snapshot has invalid {field}")
    return parsed


def _snapshot_text(value: _RedisScriptValue, *, field: str) -> str:
    """Decode one UTF-8 scalar and reject nested or malformed responses."""

    if not isinstance(value, bytes):
        raise _redis_protocol_error(f"Redis run snapshot has invalid {field}")
    try:
        return value.decode()
    except UnicodeDecodeError as error:
        raise _redis_protocol_error(
            f"Redis run snapshot has invalid {field}",
            cause=error,
        ) from error


def _snapshot_bytes(value: _RedisScriptValue, *, field: str) -> bytes:
    """Return one binary scalar and reject nested snapshot structures."""

    if not isinstance(value, bytes):
        raise _redis_protocol_error(f"Redis run snapshot has invalid {field}")
    return value


def _raise_stream_deleted(
    handle: BackendRunHandle,
    *,
    generation: int | None = None,
) -> Never:
    raise StreamDeleted(
        channel=handle.channel,
        identity=handle.identity,
        generation=(handle.generation if generation is None else generation),
    )


async def _eval(
    self: RedisBackend,
    script: str,
    keys: Sequence[str],
    arguments: Sequence[str | bytes],
) -> list[bytes]:
    response = await _redis_call(
        "script evaluation",
        self._client.eval(
            script,
            len(keys),
            *keys,
            *arguments,
        ),
    )
    if not isinstance(response, list):
        raise _redis_protocol_error("Redis script returned a non-list response")
    return cast(list[bytes], response)
