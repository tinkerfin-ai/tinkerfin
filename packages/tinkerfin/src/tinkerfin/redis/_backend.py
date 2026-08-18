"""Redis-backed per-principal Graph run coordination."""

from __future__ import annotations

import asyncio
import hashlib
import math
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar, cast
from uuid import uuid4

from redis.asyncio import Redis
from redis.asyncio.cluster import RedisCluster
from redis.exceptions import RedisError

from .._tasks import join_task

PrincipalT = TypeVar("PrincipalT")

_DEFAULT_KEY_PREFIX = "tinkerfin:run:v1:"
_DEFAULT_LEASE_TTL_SECONDS = 30.0
_DEFAULT_WAIT_POLL_SECONDS = 0.1

_ACQUIRE_SCRIPT = """
if redis.call('EXISTS', KEYS[1]) == 1 then
    return 0
end
redis.call('PSETEX', KEYS[1], ARGV[2], ARGV[1])
return 1
"""

_RENEW_SCRIPT = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then
    return 0
end
redis.call('PEXPIRE', KEYS[1], ARGV[2])
return 1
"""

_RELEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then
    return 0
end
return redis.call('DEL', KEYS[1])
"""

_RedisClient = Redis


class _AsyncRedisClient(Protocol):
    """Record the awaitable branch implemented by ``redis.asyncio.Redis``."""

    async def ping(self) -> bool: ...

    async def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: str | bytes | int,
    ) -> object: ...

    async def aclose(self) -> None: ...


def _positive_seconds(name: str, value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{name} must be a number")
    resolved = float(value)
    if not math.isfinite(resolved) or resolved <= 0:
        raise ValueError(f"{name} must be finite and greater than zero")
    return resolved


def _key_prefix(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("key_prefix must be a non-blank string")
    return value if value.endswith(":") else f"{value}:"


def _redis_integer(value: object, *, operation: str) -> int:
    if isinstance(value, bool):
        raise TypeError(f"Redis {operation} returned an invalid boolean reply")
    if isinstance(value, int):
        return value
    if isinstance(value, bytes | str):
        try:
            return int(value)
        except ValueError as error:
            raise RuntimeError(
                f"Redis {operation} returned a non-integer reply"
            ) from error
    raise RuntimeError(f"Redis {operation} returned a non-integer reply")


@dataclass(frozen=True, slots=True)
class _Settings:
    key_prefix: str
    lease_ttl_seconds: float
    renew_interval_seconds: float
    wait_poll_seconds: float

    @classmethod
    def resolve(
        cls,
        *,
        key_prefix: str,
        lease_ttl_seconds: float,
        renew_interval_seconds: float | None,
        wait_poll_seconds: float,
    ) -> _Settings:
        ttl = _positive_seconds("lease_ttl_seconds", lease_ttl_seconds)
        renewal = (
            ttl / 3
            if renew_interval_seconds is None
            else _positive_seconds("renew_interval_seconds", renew_interval_seconds)
        )
        if renewal >= ttl / 2:
            raise ValueError(
                "renew_interval_seconds must be less than half lease_ttl_seconds"
            )
        return cls(
            key_prefix=_key_prefix(key_prefix),
            lease_ttl_seconds=ttl,
            renew_interval_seconds=renewal,
            wait_poll_seconds=_positive_seconds("wait_poll_seconds", wait_poll_seconds),
        )

    @property
    def lease_ttl_milliseconds(self) -> int:
        return max(1, math.ceil(self.lease_ttl_seconds * 1000))


class RedisRunCoordinator(Generic[PrincipalT]):
    """Serialize Graph runs by a caller-defined key across Redis clients.

    Enter the coordinator before giving it to `TinkerFin`. `from_client()` borrows
    its Redis client; `from_url()` owns and closes the client it creates. A lost
    lease cancels the task currently executing the coordinated Graph run.

    Args:
        client: Asynchronous Redis client used for lock scripts.
        owns_client: Whether closing this coordinator also closes `client`.
        key_resolver: Convert an opaque principal into a stable non-blank key.
        settings: Validated lease and polling settings.
    """

    def __init__(
        self,
        *,
        client: _RedisClient,
        owns_client: bool,
        key_resolver: Callable[[PrincipalT], str],
        settings: _Settings,
    ) -> None:
        if not callable(key_resolver):
            raise TypeError("key_resolver must be callable")
        self._client = cast(_AsyncRedisClient, client)
        self._owns_client = owns_client
        self._key_resolver = key_resolver
        self._settings = settings
        self._state = "new"
        self._state_lock = asyncio.Lock()
        self._active_scopes = 0
        self._active_scopes_done = asyncio.Event()
        self._active_scopes_done.set()
        self._close_task: asyncio.Task[None] | None = None

    @classmethod
    def from_client(
        cls,
        client: _RedisClient,
        *,
        key_resolver: Callable[[PrincipalT], str],
        key_prefix: str = _DEFAULT_KEY_PREFIX,
        lease_ttl_seconds: float = _DEFAULT_LEASE_TTL_SECONDS,
        renew_interval_seconds: float | None = None,
        wait_poll_seconds: float = _DEFAULT_WAIT_POLL_SECONDS,
    ) -> RedisRunCoordinator[PrincipalT]:
        """Create a coordinator that borrows a non-cluster asynchronous Redis client."""

        if isinstance(client, RedisCluster):
            raise TypeError(
                "Redis Cluster is unsupported by the RedisRunCoordinator "
                "client boundary"
            )
        return cls(
            client=client,
            owns_client=False,
            key_resolver=key_resolver,
            settings=_Settings.resolve(
                key_prefix=key_prefix,
                lease_ttl_seconds=lease_ttl_seconds,
                renew_interval_seconds=renew_interval_seconds,
                wait_poll_seconds=wait_poll_seconds,
            ),
        )

    @classmethod
    def from_url(
        cls,
        url: str,
        *,
        key_resolver: Callable[[PrincipalT], str],
        key_prefix: str = _DEFAULT_KEY_PREFIX,
        lease_ttl_seconds: float = _DEFAULT_LEASE_TTL_SECONDS,
        renew_interval_seconds: float | None = None,
        wait_poll_seconds: float = _DEFAULT_WAIT_POLL_SECONDS,
    ) -> RedisRunCoordinator[PrincipalT]:
        """Create a coordinator that owns a Redis client built from `url`."""

        client = Redis.from_url(url, decode_responses=False)
        return cls(
            client=client,
            owns_client=True,
            key_resolver=key_resolver,
            settings=_Settings.resolve(
                key_prefix=key_prefix,
                lease_ttl_seconds=lease_ttl_seconds,
                renew_interval_seconds=renew_interval_seconds,
                wait_poll_seconds=wait_poll_seconds,
            ),
        )

    async def __aenter__(self) -> RedisRunCoordinator[PrincipalT]:
        startup_error: BaseException | None = None
        # Startup is the only transition from `new`; holding the state lock across
        # ping prevents a second enter or close from observing a half-open resource.
        async with self._state_lock:
            if self._state != "new":
                raise RuntimeError("RedisRunCoordinator is single-use")
            self._state = "opening"
            try:
                await self._client.ping()
            except BaseException as error:  # noqa: BLE001 - owned cleanup is mandatory
                self._state = "closed"
                startup_error = error
            else:
                self._state = "open"
                return self

        assert startup_error is not None
        if self._owns_client:
            close_task = asyncio.create_task(
                self._client.aclose(),
                name="tinkerfin-redis-run-coordinator-startup-close",
            )
            try:
                await join_task(close_task)
            except asyncio.CancelledError as cancellation:
                cancellation.add_note(
                    "Redis startup also failed: "
                    f"{type(startup_error).__name__}: {startup_error}"
                )
                raise
            except BaseException as cleanup_error:  # noqa: BLE001 - preserve primary
                startup_error.add_note(
                    "Redis startup cleanup also failed: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
        if isinstance(startup_error, RedisError):
            health_error = RuntimeError("Redis run coordination health check failed")
            raise health_error from startup_error
        raise startup_error.with_traceback(startup_error.__traceback__)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        del exc_type, exc_value, traceback
        task = self._close_task
        if task is None:
            task = asyncio.create_task(
                self._close_once(),
                name="tinkerfin-redis-run-coordinator-close",
            )
            self._close_task = task
        await join_task(task)

    async def _close_once(self) -> None:
        async with self._state_lock:
            if self._state == "closed":
                return
            self._state = "closing"
        await self._active_scopes_done.wait()
        if self._owns_client:
            await self._client.aclose()
        async with self._state_lock:
            self._state = "closed"

    def __call__(
        self,
        principal: PrincipalT,
        /,
    ) -> AbstractAsyncContextManager[None]:
        return self._coordinate(principal)

    @asynccontextmanager
    async def _coordinate(self, principal: PrincipalT) -> AsyncIterator[None]:
        resolved_key = self._key_resolver(principal)
        if not isinstance(resolved_key, str):
            raise TypeError("key_resolver must return a string")
        if not resolved_key or resolved_key != resolved_key.strip():
            raise ValueError(
                "key_resolver must return a non-blank string without surrounding "
                "whitespace"
            )
        async with self._state_lock:
            if self._state != "open":
                raise RuntimeError("RedisRunCoordinator must be entered before use")
            self._active_scopes += 1
            self._active_scopes_done.clear()
        try:
            async with self._lease(resolved_key):
                yield
        finally:
            async with self._state_lock:
                self._active_scopes -= 1
                if self._active_scopes == 0:
                    self._active_scopes_done.set()

    @asynccontextmanager
    async def _lease(self, resolved_key: str) -> AsyncIterator[None]:
        digest = hashlib.sha256(resolved_key.encode("utf-8")).hexdigest()
        lock_key = f"{self._settings.key_prefix}{digest}:lease"
        owner_token = uuid4().hex
        acquired = False
        renewal: asyncio.Task[None] | None = None
        primary: BaseException | None = None
        try:
            await self._acquire(
                lock_key=lock_key,
                owner_token=owner_token,
            )
            acquired = True
            async with self._state_lock:
                can_enter = self._state == "open"
            if not can_enter:
                raise RuntimeError("RedisRunCoordinator is closing")
            owner_task = asyncio.current_task()
            if owner_task is None:  # pragma: no cover - async contexts run in Tasks
                raise RuntimeError("Redis coordination requires an asyncio task")
            renewal = asyncio.create_task(
                self._renew_forever(
                    lock_key=lock_key,
                    owner_token=owner_token,
                    owner_task=owner_task,
                ),
                name="tinkerfin-redis-run-renewal",
            )
            yield
        except BaseException as error:
            primary = error
            raise
        finally:
            cleanup_errors: list[BaseException] = []
            if renewal is not None:
                try:
                    await join_task(
                        renewal,
                        cancel=True,
                        suppress_task_cancellation=True,
                    )
                except BaseException as error:  # noqa: BLE001 - finish all cleanup
                    cleanup_errors.append(error)
            if acquired:
                try:
                    release = asyncio.create_task(
                        self._release(lock_key=lock_key, owner_token=owner_token),
                        name="tinkerfin-redis-run-release",
                    )
                    await join_task(release)
                except BaseException as error:  # noqa: BLE001 - resolve below
                    cleanup_errors.append(error)

            cancellation = next(
                (
                    error
                    for error in cleanup_errors
                    if isinstance(error, asyncio.CancelledError)
                ),
                None,
            )
            if cancellation is not None:
                if primary is not None:
                    cancellation.add_note(
                        "coordinated run also failed: "
                        f"{type(primary).__name__}: {primary}"
                    )
                for cleanup_error in cleanup_errors:
                    if cleanup_error is not cancellation:
                        cancellation.add_note(
                            "Redis coordination cleanup also failed: "
                            f"{type(cleanup_error).__name__}: {cleanup_error}"
                        )
                raise cancellation.with_traceback(cancellation.__traceback__)
            if primary is not None:
                for cleanup_error in cleanup_errors:
                    primary.add_note(
                        "Redis coordination cleanup also failed: "
                        f"{type(cleanup_error).__name__}: {cleanup_error}"
                    )
            elif len(cleanup_errors) == 1:
                raise cleanup_errors[0]
            elif cleanup_errors:
                raise BaseExceptionGroup(
                    "Redis coordination cleanup failed",
                    cleanup_errors,
                )

    async def _acquire(
        self,
        *,
        lock_key: str,
        owner_token: str,
    ) -> int:
        while True:
            if self._state != "open":
                raise RuntimeError("RedisRunCoordinator is closing")
            try:

                async def evaluate() -> object:
                    return await self._client.eval(
                        _ACQUIRE_SCRIPT,
                        1,
                        lock_key,
                        owner_token,
                        self._settings.lease_ttl_milliseconds,
                    )

                attempt = asyncio.create_task(
                    evaluate(),
                    name="tinkerfin-redis-run-acquire",
                )
                reply = await join_task(attempt)
                token = _redis_integer(reply, operation="acquisition")
            except RedisError as error:
                await self._release_uncertain_acquisition(
                    lock_key=lock_key,
                    owner_token=owner_token,
                    primary=error,
                )
                raise RuntimeError(
                    "Redis run coordination acquisition failed"
                ) from error
            except BaseException as error:
                await self._release_uncertain_acquisition(
                    lock_key=lock_key,
                    owner_token=owner_token,
                    primary=error,
                )
                raise
            if token > 0:
                return token
            await asyncio.sleep(self._settings.wait_poll_seconds)

    async def _release_uncertain_acquisition(
        self,
        *,
        lock_key: str,
        owner_token: str,
        primary: BaseException,
    ) -> None:
        """Release a lease that may have committed without returning its result."""

        release = asyncio.create_task(
            self._release(lock_key=lock_key, owner_token=owner_token),
            name="tinkerfin-redis-run-release-uncertain-acquire",
        )
        try:
            await join_task(release)
        except asyncio.CancelledError as cancellation:
            if cancellation is not primary:
                cancellation.add_note(
                    "Redis acquisition also failed: "
                    f"{type(primary).__name__}: {primary}"
                )
            raise
        except BaseException as cleanup_error:  # noqa: BLE001 - retain primary error
            primary.add_note(
                "Redis acquisition cleanup also failed: "
                f"{type(cleanup_error).__name__}: {cleanup_error}"
            )

    async def _renew_forever(
        self,
        *,
        lock_key: str,
        owner_token: str,
        owner_task: asyncio.Task[object],
    ) -> None:
        try:
            while True:
                await asyncio.sleep(self._settings.renew_interval_seconds)
                try:
                    reply = await self._client.eval(
                        _RENEW_SCRIPT,
                        1,
                        lock_key,
                        owner_token,
                        self._settings.lease_ttl_milliseconds,
                    )
                    renewed = _redis_integer(reply, operation="renewal")
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - uncertain renewal must fail closed
                    renewed = 0
                if renewed != 1:
                    owner_task.cancel("Redis run coordination lease lost")
                    return
        except asyncio.CancelledError:
            raise

    async def _release(self, *, lock_key: str, owner_token: str) -> None:
        try:
            reply = await self._client.eval(
                _RELEASE_SCRIPT,
                1,
                lock_key,
                owner_token,
            )
            _redis_integer(reply, operation="release")
        except RedisError as error:
            raise RuntimeError("Redis run coordination release failed") from error


__all__ = ["RedisRunCoordinator"]
