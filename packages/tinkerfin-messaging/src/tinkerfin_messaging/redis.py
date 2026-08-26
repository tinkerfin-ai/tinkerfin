"""Optional Redis backend with atomic logs, leases, and fencing."""

from __future__ import annotations

import math
from collections.abc import AsyncGenerator, Mapping, Sequence
from typing import Never, cast
from uuid import uuid4

from redis.asyncio import Redis

from tinkerfin_agui_adapter import Identity

from . import _redis_control, _redis_journal
from ._identity import required_identifier
from ._redis_control import (
    _AsyncRedisClient,
    _RedisKeys,
    _RedisScriptValue,
    _RedisStreamScope,
    _RunSnapshot,
    _StreamControl,
)
from .backend import (
    BackendRunHandle,
    FinalRunStatus,
    MessagingBackend,
    PreparedRun,
    RunStatus,
)
from .limits import DEFAULT_MESSAGING_LIMITS, MessagingLimits
from .models import MessageEnvelope, RecoveryCheckpoint


class RedisBackend(MessagingBackend):
    """Persist ordered streams and distributed run leases in real Redis.

    The injected client is borrowed and must use ``decode_responses=False`` so
    arbitrary payload bytes survive round trips unchanged.
    """

    def __init__(
        self,
        client: Redis,
        *,
        key_prefix: str = "tinkerfin-messaging",
        lease_ttl: float = 15.0,
        poll_interval: float = 0.1,
        limits: MessagingLimits = DEFAULT_MESSAGING_LIMITS,
    ) -> None:
        """Initialize a backend that borrows one binary Redis client.

        Args:
            client: Open asynchronous Redis client with response decoding disabled.
            key_prefix: Deployment namespace for every durable key.
            lease_ttl: Producer ownership expiry in seconds.
            poll_interval: Maximum fallback polling interval in seconds.
            limits: Immutable payload and per-thread capacity contract.

        Raises:
            TypeError: A timing or limits value has the wrong type.
            ValueError: An identifier, timing value, or client mode is invalid.
        """

        required_identifier("key_prefix", key_prefix)
        if isinstance(lease_ttl, bool) or not isinstance(lease_ttl, int | float):
            raise TypeError("lease_ttl must be a number")
        if isinstance(poll_interval, bool) or not isinstance(
            poll_interval, int | float
        ):
            raise TypeError("poll_interval must be a number")
        resolved_lease_ttl = float(lease_ttl)
        resolved_poll_interval = float(poll_interval)
        if not math.isfinite(resolved_lease_ttl) or resolved_lease_ttl <= 0:
            raise ValueError("lease_ttl must be a finite positive number")
        if not math.isfinite(resolved_poll_interval) or resolved_poll_interval <= 0:
            raise ValueError("poll_interval must be a finite positive number")
        if not isinstance(limits, MessagingLimits):
            raise TypeError("limits must be a MessagingLimits")
        connection_options = cast(
            Mapping[str, object],
            client.get_connection_kwargs(),
        )
        if connection_options.get("decode_responses", False):
            raise ValueError("RedisBackend requires decode_responses=False")
        self._client = cast(_AsyncRedisClient, client)
        self._prefix = key_prefix
        self._lease_ttl = resolved_lease_ttl
        self._lease_ms = max(1, math.ceil(resolved_lease_ttl * 1000))
        self._poll_interval = resolved_poll_interval
        self._socket_timeout_budget_ms = self._socket_timeout_budget(
            connection_options.get("socket_timeout")
        )
        self._worker_id = uuid4().hex
        self._limits = limits

    @property
    def limits(self) -> MessagingLimits:
        """Return the immutable limits shared by this Redis deployment."""

        return self._limits

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
        """Atomically start, recover, or attach to one Redis-backed run."""

        return await _redis_journal.prepare(
            self,
            channel=channel,
            identity=identity,
            codec=codec,
            after=after,
            cancellable=cancellable,
            recoverable=recoverable,
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
        """Idempotently append one message under lease, fence, and quota checks."""

        return await _redis_journal.append(
            self,
            handle,
            message_id=message_id,
            codec=codec,
            payload=payload,
            checkpoint=checkpoint,
        )

    async def begin_settlement(self, handle: BackendRunHandle) -> bool:
        """Atomically choose an accepted cancellation or ordinary settlement."""

        return await _redis_control.begin_settlement(
            self,
            handle,
        )

    async def finish(
        self,
        handle: BackendRunHandle,
        *,
        status: FinalRunStatus,
        error: BaseException | None = None,
    ) -> None:
        """Commit a terminal run status and release its Redis lease."""

        return await _redis_control.finish(
            self,
            handle,
            status=status,
            error=error,
        )

    async def latest_seq(self, *, channel: str, identity: Identity) -> int:
        """Return the active generation's latest committed sequence."""

        return await _redis_journal.latest_seq(
            self,
            channel=channel,
            identity=identity,
        )

    async def get_run_status(
        self,
        *,
        channel: str,
        identity: Identity,
    ) -> RunStatus:
        """Return one run status after atomically settling an expired lease."""

        return await _redis_control.get_run_status(
            self,
            channel=channel,
            identity=identity,
        )

    async def read(
        self,
        *,
        channel: str,
        identity: Identity,
        after: int = 0,
        limit: int = 100,
    ) -> tuple[MessageEnvelope, ...]:
        """Read a bounded Redis page after a strictly validated cursor."""

        return await _redis_journal.read(
            self,
            channel=channel,
            identity=identity,
            after=after,
            limit=limit,
        )

    async def bind_follow(
        self,
        *,
        channel: str,
        identity: Identity,
        after: int,
    ) -> BackendRunHandle:
        """Resolve one follower and validate its cursor against the bound generation."""

        return await _redis_journal.bind_follow(
            self,
            channel=channel,
            identity=identity,
            after=after,
        )

    def follow(
        self,
        handle: BackendRunHandle,
        *,
        after: int,
    ) -> AsyncGenerator[MessageEnvelope, None]:
        """Follow one fenced generation until its run becomes terminal."""

        return _redis_journal.follow(
            self,
            handle,
            after=after,
        )

    async def request_cancel(self, handle: BackendRunHandle) -> bool:
        """Durably request cancellation while the run still accepts it."""

        return await _redis_control.request_cancel(
            self,
            handle,
        )

    async def wait_for_cancel(self, handle: BackendRunHandle) -> bool:
        """Wait for a cancellation signal or terminal run state."""

        return await _redis_control.wait_for_cancel(
            self,
            handle,
        )

    async def wait_finished(self, handle: BackendRunHandle) -> RunStatus:
        """Wait for and return the durable terminal status."""

        return await _redis_control.wait_finished(
            self,
            handle,
        )

    async def failure(self, handle: BackendRunHandle) -> BaseException | None:
        """Reconstruct the trusted producer failure for a terminal run."""

        return await _redis_control.failure(
            self,
            handle,
        )

    async def renew(self, handle: BackendRunHandle) -> bool:
        """Renew a lease only while its complete fencing identity still matches."""

        return await _redis_control.renew(
            self,
            handle,
        )

    @property
    def lease_renew_interval(self) -> float:
        """Return an interval that renews well before the configured TTL."""

        return self._lease_ttl / 3

    @property
    def lease_timeout(self) -> float:
        """Return the Redis ownership expiry budget for trusted diagnostics."""

        return self._lease_ttl

    async def delete_stream(self, *, channel: str, identity: Identity) -> None:
        """Delete one stream through a leased, generation-fenced cleanup."""

        return await _redis_control.delete_stream(
            self,
            channel=channel,
            identity=identity,
        )

    async def _delete_owned_generation(
        self,
        *,
        keys: _RedisKeys,
        delete_owner: str,
    ) -> bool:
        """Clear one fenced generation while periodically renewing ownership."""

        return await _redis_control._delete_owned_generation(
            self,
            keys=keys,
            delete_owner=delete_owner,
        )

    def _scope(self, channel: str, identity: Identity) -> _RedisStreamScope:
        return _redis_control._scope(
            self,
            channel,
            identity,
        )

    def _keys(
        self,
        channel: str,
        identity: Identity,
        *,
        generation: int,
    ) -> _RedisKeys:
        return _redis_control._keys(
            self,
            channel,
            identity,
            generation=generation,
        )

    async def _read_control(
        self,
        scope: _RedisStreamScope,
    ) -> _StreamControl | None:
        return await _redis_control._read_control(
            self,
            scope,
        )

    async def _keys_for_handle(self, handle: BackendRunHandle) -> _RedisKeys:
        return await _redis_control._keys_for_handle(
            self,
            handle,
        )

    async def _is_current_generation(self, keys: _RedisKeys) -> bool:
        return await _redis_control._is_current_generation(
            self,
            keys,
        )

    async def _run_snapshot(
        self,
        keys: _RedisKeys,
        identity: Identity,
        *,
        after: int | None = None,
    ) -> _RunSnapshot:
        """Read one authoritative run state and optional bounded message page."""

        return await _redis_control._run_snapshot(
            self,
            keys,
            identity,
            after=after,
        )

    async def _settled_run_snapshot(
        self,
        keys: _RedisKeys,
        identity: Identity,
        *,
        after: int | None = None,
    ) -> _RunSnapshot:
        """Settle one potentially mutating Lua snapshot before caller cancellation."""

        return await _redis_control._settled_run_snapshot(
            self,
            keys,
            identity,
            after=after,
        )

    async def _wait_for_snapshot_change(
        self,
        keys: _RedisKeys,
        snapshot: _RunSnapshot,
    ) -> None:
        """Block on durable data or lifecycle signals, then require a new snapshot."""

        return await _redis_control._wait_for_snapshot_change(
            self,
            keys,
            snapshot,
        )

    def _wait_block_ms(self, lease_ttl_ms: int) -> int:
        """Bound XREAD by lease expiry, fallback progress, and socket timeout."""

        return _redis_control._wait_block_ms(
            self,
            lease_ttl_ms,
        )

    @staticmethod
    def _socket_timeout_budget(value: object) -> int | None:
        """Return a positive XREAD budget below the Redis socket timeout."""

        return _redis_control._socket_timeout_budget(
            value,
        )

    @classmethod
    def _snapshot_integer(
        cls,
        value: _RedisScriptValue,
        *,
        field: str,
        minimum: int,
    ) -> int:
        """Decode one canonical integer from a snapshot scalar."""

        return _redis_control._snapshot_integer(
            cls,
            value,
            field=field,
            minimum=minimum,
        )

    @staticmethod
    def _snapshot_text(value: _RedisScriptValue, *, field: str) -> str:
        """Decode one UTF-8 scalar and reject nested or malformed responses."""

        return _redis_control._snapshot_text(
            value,
            field=field,
        )

    @staticmethod
    def _snapshot_bytes(value: _RedisScriptValue, *, field: str) -> bytes:
        """Return one binary scalar and reject nested snapshot structures."""

        return _redis_control._snapshot_bytes(
            value,
            field=field,
        )

    def _snapshot_messages(
        self,
        value: _RedisScriptValue,
        *,
        channel: str,
        identity: Identity,
        after: int | None,
        end_seq: int,
    ) -> tuple[MessageEnvelope, ...]:
        """Decode the exact nested XRANGE representation returned through EVAL."""

        return _redis_journal._snapshot_messages(
            self,
            value,
            channel=channel,
            identity=identity,
            after=after,
            end_seq=end_seq,
        )

    @staticmethod
    def _raise_stream_deleted(
        handle: BackendRunHandle,
        *,
        generation: int | None = None,
    ) -> Never:
        return _redis_control._raise_stream_deleted(
            handle,
            generation=generation,
        )

    def _decode_entry(
        self,
        channel: str,
        identity: Identity,
        entry: tuple[bytes, Mapping[bytes, bytes]],
    ) -> MessageEnvelope:
        return _redis_journal._decode_entry(
            self,
            channel,
            identity,
            entry,
        )

    async def _eval(
        self,
        script: str,
        keys: Sequence[str],
        arguments: Sequence[str | bytes],
    ) -> list[bytes]:
        return await _redis_control._eval(
            self,
            script,
            keys,
            arguments,
        )

    @staticmethod
    def _message_signature(
        *,
        identity: Identity,
        codec: str,
        payload: bytes,
        checkpoint: RecoveryCheckpoint | None,
    ) -> str:
        return _redis_journal._message_signature(
            identity=identity,
            codec=codec,
            payload=payload,
            checkpoint=checkpoint,
        )

    @staticmethod
    def _digest(value: str) -> str:
        return _redis_journal._digest(
            value,
        )

    @staticmethod
    def _text(value: bytes | str | int) -> str:
        return _redis_journal._text(
            value,
        )

    @staticmethod
    def _bytes(value: bytes | str | int) -> bytes:
        return _redis_journal._bytes(
            value,
        )

    @staticmethod
    def _qualified_name(value: BaseException) -> str:
        return _redis_journal._qualified_name(
            value,
        )

    @staticmethod
    def _remote_error(snapshot: _RunSnapshot) -> RuntimeError:
        return _redis_journal._remote_error(
            snapshot,
        )
