"""Identity-driven Redis coordination composed from the reusable lease lock."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from types import TracebackType
from typing import Self

from redis.asyncio import Redis

from tinkerfin_agui_adapter import Identity

from ._lease_lock import RedisLeaseLock

_DEFAULT_KEY_PREFIX = "tinkerfin:run:v2:"
_DEFAULT_LEASE_TTL_SECONDS = 30.0
_DEFAULT_WAIT_POLL_SECONDS = 0.1


class RedisRunCoordinator:
    """Serialize Graph runs whose Identity resolves to the same Redis lease key.

    The coordinator deliberately keeps the small `RunCoordinator` extension shape.
    `RedisLeaseLock` owns acquisition, fencing, renewal, cancellation, release, and
    Redis client lifecycle details.
    """

    def __init__(
        self,
        *,
        lease_lock: RedisLeaseLock,
        key_resolver: Callable[[Identity], str],
    ) -> None:
        if not isinstance(lease_lock, RedisLeaseLock):
            raise TypeError("lease_lock must be a RedisLeaseLock")
        if not callable(key_resolver):
            raise TypeError("key_resolver must be callable")
        self._lease_lock = lease_lock
        self._key_resolver = key_resolver

    @classmethod
    def from_client(
        cls,
        client: Redis,
        *,
        key_resolver: Callable[[Identity], str],
        key_prefix: str = _DEFAULT_KEY_PREFIX,
        lease_ttl_seconds: float = _DEFAULT_LEASE_TTL_SECONDS,
        renew_interval_seconds: float | None = None,
        wait_poll_seconds: float = _DEFAULT_WAIT_POLL_SECONDS,
    ) -> RedisRunCoordinator:
        """Create a coordinator that borrows one asynchronous Redis client."""

        if not callable(key_resolver):
            raise TypeError("key_resolver must be callable")
        return cls(
            lease_lock=RedisLeaseLock.from_client(
                client,
                key_prefix=key_prefix,
                lease_ttl_seconds=lease_ttl_seconds,
                renew_interval_seconds=renew_interval_seconds,
                wait_poll_seconds=wait_poll_seconds,
            ),
            key_resolver=key_resolver,
        )

    @classmethod
    def from_url(
        cls,
        url: str,
        *,
        key_resolver: Callable[[Identity], str],
        key_prefix: str = _DEFAULT_KEY_PREFIX,
        lease_ttl_seconds: float = _DEFAULT_LEASE_TTL_SECONDS,
        renew_interval_seconds: float | None = None,
        wait_poll_seconds: float = _DEFAULT_WAIT_POLL_SECONDS,
    ) -> RedisRunCoordinator:
        """Create a coordinator that owns the Redis client built from `url`."""

        if not callable(key_resolver):
            raise TypeError("key_resolver must be callable")
        return cls(
            lease_lock=RedisLeaseLock.from_url(
                url,
                key_prefix=key_prefix,
                lease_ttl_seconds=lease_ttl_seconds,
                renew_interval_seconds=renew_interval_seconds,
                wait_poll_seconds=wait_poll_seconds,
            ),
            key_resolver=key_resolver,
        )

    async def __aenter__(self) -> Self:
        await self._lease_lock.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self._lease_lock.__aexit__(exc_type, exc_value, traceback)

    def __call__(
        self,
        identity: Identity,
        /,
    ) -> AbstractAsyncContextManager[None]:
        return self._coordinate(identity)

    @asynccontextmanager
    async def _coordinate(self, identity: Identity) -> AsyncIterator[None]:
        if not isinstance(identity, Identity):
            raise TypeError("identity must be an Identity")
        resolved_key = self._key_resolver(identity)
        if not isinstance(resolved_key, str):
            raise TypeError("key_resolver must return a string")
        if not resolved_key or resolved_key != resolved_key.strip():
            raise ValueError(
                "key_resolver must return a non-blank string without surrounding "
                "whitespace"
            )
        async with self._lease_lock.hold(resolved_key):
            yield


__all__ = ["RedisRunCoordinator"]
