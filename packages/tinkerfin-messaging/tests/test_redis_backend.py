"""Real Redis persistence, cross-worker, cancellation, and fencing contracts."""

from __future__ import annotations

import asyncio
import hashlib
import multiprocessing
import os
from collections import Counter
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Mapping, Sequence
from multiprocessing.synchronize import Event as ProcessEvent
from typing import ClassVar, TypeVar, cast
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError
from redis.typing import KeyT, StreamIdT

from tinkerfin_messaging import (
    BackendOwnershipLost,
    BackendRunHandle,
    CodecMismatch,
    MessageSubscription,
    Messaging,
    PreparedRun,
    RecoverableMessage,
    RecoveryCheckpoint,
    RedisBackend,
    RunAlreadyActive,
    RunProducerFailed,
    StreamDeleted,
)

_REDIS_URL_ENV = "TINKERFIN_TEST_REDIS_URL"
_RedisStreamEntry = tuple[bytes, dict[bytes, bytes]]
_XReadResponse = list[tuple[bytes, list[_RedisStreamEntry]]]
_RedisT = TypeVar("_RedisT", bound=Redis)


def _redis_client(
    client_type: type[_RedisT],
    *,
    max_connections: int | None = None,
) -> _RedisT:
    redis_url = os.getenv(_REDIS_URL_ENV)
    if not redis_url:
        pytest.skip(f"real Redis configuration is missing: {_REDIS_URL_ENV}")
    return cast(
        _RedisT,
        client_type.from_url(
            redis_url,
            decode_responses=False,
            socket_connect_timeout=5,
            socket_timeout=5,
            max_connections=max_connections,
        ),
    )


class _TextCodec:
    codec_id: ClassVar[str] = "test.redis-text.v1"

    def encode(self, item: str) -> bytes:
        return item.encode()

    def decode(self, payload: bytes) -> str:
        return payload.decode()


class _Source:
    def __init__(
        self,
        *items: str,
        release: asyncio.Event | None = None,
        after_release: tuple[str, ...] = (),
    ) -> None:
        self.items = items
        self.release = release
        self.after_release = after_release
        self.started = asyncio.Event()
        self.closed = asyncio.Event()
        self.close_calls = 0
        self._iterator: AsyncGenerator[str, None] | None = None

    def __aiter__(self) -> AsyncIterator[str]:
        async def iterate() -> AsyncGenerator[str, None]:
            self.started.set()
            for item in self.items:
                yield item
            if self.release is not None:
                await self.release.wait()
            for item in self.after_release:
                yield item

        self._iterator = iterate()
        return self._iterator

    async def aclose(self) -> None:
        self.close_calls += 1
        iterator = self._iterator
        if iterator is not None:
            await iterator.aclose()
        self.closed.set()


class _RecoverableSource:
    def __init__(
        self,
        *messages: RecoverableMessage[str],
        release: asyncio.Event | None = None,
    ) -> None:
        self.messages = messages
        self.release = release
        self.started = asyncio.Event()
        self.close_calls = 0

    def __aiter__(self) -> AsyncIterator[RecoverableMessage[str]]:
        async def iterate() -> AsyncGenerator[RecoverableMessage[str], None]:
            self.started.set()
            if self.release is not None:
                await self.release.wait()
            for message in self.messages:
                yield message

        return iterate()

    async def aclose(self) -> None:
        self.close_calls += 1


class _RecoveryFactory:
    def __init__(self) -> None:
        self.checkpoints: list[RecoveryCheckpoint | None] = []
        self.sources: list[_RecoverableSource] = []

    async def open(
        self,
        checkpoint: RecoveryCheckpoint | None,
    ) -> _RecoverableSource:
        self.checkpoints.append(checkpoint)
        source = _RecoverableSource(
            RecoverableMessage(
                message_id="stable-message-2",
                data="second",
                checkpoint=RecoveryCheckpoint(
                    position=b"2",
                    last_message_id="stable-message-2",
                ),
            )
        )
        self.sources.append(source)
        return source


class _FixedRecoveryFactory:
    def __init__(self, source: _RecoverableSource) -> None:
        self.source = source
        self.checkpoints: list[RecoveryCheckpoint | None] = []

    async def open(
        self,
        checkpoint: RecoveryCheckpoint | None,
    ) -> _RecoverableSource:
        self.checkpoints.append(checkpoint)
        return self.source


class _SlowRecoveryFactory:
    def __init__(self) -> None:
        self.source = _RecoverableSource(
            RecoverableMessage(
                message_id="stable-message-1",
                data="after-slow-open",
                checkpoint=RecoveryCheckpoint(
                    position=b"1",
                    last_message_id="stable-message-1",
                ),
            )
        )

    async def open(
        self,
        checkpoint: RecoveryCheckpoint | None,
    ) -> _RecoverableSource:
        assert checkpoint is None
        await asyncio.sleep(0.8)
        return self.source


class _HangingRecoveryFactory:
    async def open(
        self,
        checkpoint: RecoveryCheckpoint | None,
    ) -> _RecoverableSource:
        if checkpoint is not None:
            raise AssertionError(
                "the initial recoverable owner must start without a checkpoint"
            )

        class _HangingSource(_RecoverableSource):
            def __aiter__(self) -> AsyncIterator[RecoverableMessage[str]]:
                async def iterate() -> AsyncGenerator[RecoverableMessage[str], None]:
                    yield RecoverableMessage(
                        message_id="stable-message-1",
                        data="first",
                        checkpoint=RecoveryCheckpoint(
                            position=b"1",
                            last_message_id="stable-message-1",
                        ),
                    )
                    await asyncio.Event().wait()

                return iterate()

        return _HangingSource()


class _GatedEvalRedis(Redis):
    """Pause immediately before or after one real atomic Redis script."""

    eval_entered: asyncio.Event
    eval_release: asyncio.Event
    eval_returned: asyncio.Event
    return_release: asyncio.Event
    append_committed: asyncio.Event
    append_release: asyncio.Event
    gate_next_eval_before = False
    gate_next_eval_after = False
    gate_append_response = False

    async def execute_command(self, *args: object, **options: object) -> object:
        command = args[0] if args else ""
        command_name = command.decode() if isinstance(command, bytes) else str(command)
        is_eval = command_name.casefold() == "eval"
        if is_eval and self.gate_next_eval_before:
            self.gate_next_eval_before = False
            self.eval_entered.set()
            await self.eval_release.wait()
        response = super().execute_command(*args, **options)
        result = await cast(Awaitable[object], response)
        script = args[1] if len(args) > 1 else ""
        script_text = script.decode() if isinstance(script, bytes) else str(script)
        if is_eval and self.gate_append_response and "'APPENDED'" in script_text:
            self.gate_append_response = False
            self.append_committed.set()
            await self.append_release.wait()
        if is_eval and self.gate_next_eval_after:
            self.gate_next_eval_after = False
            self.eval_returned.set()
            await self.return_release.wait()
        return result


class _GatedDeleteRedis(Redis):
    """Pause one real index read after logical deletion becomes authoritative."""

    delete_cleanup_entered: asyncio.Event
    delete_cleanup_release: asyncio.Event
    gate_next_delete_cleanup = False

    async def execute_command(self, *args: object, **options: object) -> object:
        command = args[0] if args else ""
        command_name = command.decode() if isinstance(command, bytes) else str(command)
        if command_name.casefold() == "srandmember" and self.gate_next_delete_cleanup:
            self.gate_next_delete_cleanup = False
            self.delete_cleanup_entered.set()
            await self.delete_cleanup_release.wait()
        response = super().execute_command(*args, **options)
        return await cast(Awaitable[object], response)


class _PublicConfigurationRedis:
    def __init__(self, *, decode_responses: bool) -> None:
        self._decode_responses = decode_responses

    def get_connection_kwargs(self) -> dict[str, object]:
        return {"decode_responses": self._decode_responses}


class _CommandCountingRedis(Redis):
    """Count real Redis commands without replacing their behavior."""

    command_counts: Counter[str]

    def client(self) -> _CommandCountingRedis:
        client = super().client()
        assert isinstance(client, _CommandCountingRedis)
        client.command_counts = self.command_counts
        return client

    async def execute_command(self, *args: object, **options: object) -> object:
        if args:
            command = args[0]
            command_name = (
                command.decode() if isinstance(command, bytes) else str(command)
            )
            self.command_counts[command_name.lower()] += 1
        response = super().execute_command(*args, **options)
        return await cast(Awaitable[object], response)


class _GatedXreadRedis(Redis):
    """Pause before one real XREAD reaches Redis."""

    xread_entered: asyncio.Event
    xread_release: asyncio.Event
    gate_next_xread = False

    def client(self) -> _GatedXreadRedis:
        client = super().client()
        assert isinstance(client, _GatedXreadRedis)
        client.xread_entered = self.xread_entered
        client.xread_release = self.xread_release
        client.gate_next_xread = self.gate_next_xread
        self.gate_next_xread = False
        return client

    async def xread(
        self,
        streams: dict[KeyT, StreamIdT],
        count: int | None = None,
        block: int | None = None,
    ) -> _XReadResponse:
        if self.gate_next_xread:
            self.gate_next_xread = False
            self.xread_entered.set()
            await self.xread_release.wait()
        response = super().xread(
            streams,
            count=count,
            block=block,
        )
        return await cast(Awaitable[_XReadResponse], response)


def _run_owner_until_killed(prefix: str, *, recoverable: bool) -> None:
    """Own one real Redis run until the parent deliberately kills this process."""

    async def run() -> None:
        client = _redis_client(Redis)
        try:
            backend = RedisBackend(
                client,
                key_prefix=prefix,
                lease_ttl=0.6,
                poll_interval=0.05,
            )
            async with Messaging(backend=backend) as messaging:
                channel = messaging.channel(name="events", codec=_TextCodec())
                if recoverable:
                    await channel.wrap_recoverable(
                        _HangingRecoveryFactory(),
                        stream="conversation-1",
                        run="run-1",
                        after=0,
                    )
                else:
                    await channel.wrap(
                        _Source("first", release=asyncio.Event()),
                        stream="conversation-1",
                        run="run-1",
                        after=0,
                    )
                await asyncio.Event().wait()
        finally:
            await client.aclose()

    asyncio.run(run())


def _run_delete_until_killed(prefix: str, cleanup_started: ProcessEvent) -> None:
    """Enter public stream deletion and wait inside one real Redis index read."""

    class _HangingDeleteRedis(Redis):
        async def execute_command(self, *args: object, **options: object) -> object:
            command = args[0] if args else ""
            command_name = (
                command.decode() if isinstance(command, bytes) else str(command)
            )
            if command_name.casefold() == "srandmember":
                cleanup_started.set()
                await asyncio.Event().wait()
                raise AssertionError("unreachable")
            response = super().execute_command(*args, **options)
            return await cast(Awaitable[object], response)

    async def run() -> None:
        client = _redis_client(_HangingDeleteRedis)
        try:
            backend = RedisBackend(
                client,
                key_prefix=prefix,
                lease_ttl=0.3,
                poll_interval=0.02,
            )
            await backend.delete_stream(
                channel="events",
                stream="conversation-1",
            )
        finally:
            await client.aclose()

    asyncio.run(run())


async def _data(subscription: MessageSubscription[str]) -> list[str]:
    return [message.data async for message in subscription]


async def _delete_prefix(client: Redis, prefix: str) -> None:
    cursor = 0
    pattern = f"{prefix}:*"
    while True:
        cursor, keys = await client.scan(cursor=cursor, match=pattern, count=200)
        if keys:
            await client.unlink(*keys)
        if cursor == 0:
            return


def _redis_text(value: bytes | str) -> str:
    return value.decode() if isinstance(value, bytes) else value


def test_redis_backend_uses_the_public_client_configuration_boundary() -> None:
    binary_client = cast(Redis, _PublicConfigurationRedis(decode_responses=False))
    text_client = cast(Redis, _PublicConfigurationRedis(decode_responses=True))

    assert RedisBackend(binary_client)
    with pytest.raises(ValueError, match="decode_responses=False"):
        RedisBackend(text_client)


@pytest.fixture
async def redis_backends() -> AsyncGenerator[
    tuple[RedisBackend, RedisBackend, Redis],
    None,
]:
    first_client = _redis_client(Redis)
    second_client = _redis_client(Redis)
    cleanup_client = _redis_client(Redis)
    prefix = f"tfmsg:test:{uuid4().hex}"
    try:
        try:
            assert await cast(Awaitable[bool], first_client.ping()) is True
            assert await cast(Awaitable[bool], second_client.ping()) is True
        except (OSError, RedisError, TimeoutError) as error:
            pytest.fail(
                f"real Redis PING failed without exposing credentials: {type(error).__name__}"
            )
        yield (
            RedisBackend(
                first_client,
                key_prefix=prefix,
                lease_ttl=0.6,
                poll_interval=0.05,
            ),
            RedisBackend(
                second_client,
                key_prefix=prefix,
                lease_ttl=0.6,
                poll_interval=0.05,
            ),
            cleanup_client,
        )
    finally:
        await _delete_prefix(cleanup_client, prefix)
        await asyncio.gather(
            first_client.aclose(),
            second_client.aclose(),
            cleanup_client.aclose(),
        )


@pytest.fixture
async def counting_redis_backend() -> AsyncGenerator[
    tuple[RedisBackend, _CommandCountingRedis, str],
    None,
]:
    """Provide one real backend whose command boundary remains observable."""

    client = _redis_client(_CommandCountingRedis)
    client.command_counts = Counter()
    prefix = f"tfmsg:counting:{uuid4().hex}"
    try:
        try:
            assert await cast(Awaitable[bool], client.ping()) is True
        except (OSError, RedisError, TimeoutError) as error:
            pytest.fail(
                "real Redis PING failed without exposing credentials: "
                f"{type(error).__name__}"
            )
        yield (
            RedisBackend(
                client,
                key_prefix=prefix,
                lease_ttl=3,
                poll_interval=0.1,
            ),
            client,
            prefix,
        )
    finally:
        await _delete_prefix(client, prefix)
        await client.aclose()


@pytest.fixture
async def gated_xread_backends() -> AsyncGenerator[
    tuple[RedisBackend, RedisBackend, _GatedXreadRedis],
    None,
]:
    """Provide separate waiting and state-changing clients for one XREAD race."""

    waiting_client = _redis_client(_GatedXreadRedis)
    actor_client = _redis_client(Redis)
    waiting_client.xread_entered = asyncio.Event()
    waiting_client.xread_release = asyncio.Event()
    prefix = f"tfmsg:gated-xread:{uuid4().hex}"
    try:
        try:
            assert await cast(Awaitable[bool], waiting_client.ping()) is True
            assert await cast(Awaitable[bool], actor_client.ping()) is True
        except (OSError, RedisError, TimeoutError) as error:
            pytest.fail(
                "real Redis PING failed without exposing credentials: "
                f"{type(error).__name__}"
            )
        yield (
            RedisBackend(
                waiting_client,
                key_prefix=prefix,
                lease_ttl=3,
                poll_interval=1,
            ),
            RedisBackend(
                actor_client,
                key_prefix=prefix,
                lease_ttl=3,
                poll_interval=0.1,
            ),
            waiting_client,
        )
    finally:
        waiting_client.xread_release.set()
        await _delete_prefix(actor_client, prefix)
        await asyncio.gather(waiting_client.aclose(), actor_client.aclose())


@pytest.fixture
async def gated_redis_backend() -> AsyncGenerator[
    tuple[RedisBackend, _GatedEvalRedis],
    None,
]:
    client = _redis_client(_GatedEvalRedis)
    client.eval_entered = asyncio.Event()
    client.eval_release = asyncio.Event()
    client.eval_returned = asyncio.Event()
    client.return_release = asyncio.Event()
    client.append_committed = asyncio.Event()
    client.append_release = asyncio.Event()
    prefix = f"tfmsg:gated-follow:{uuid4().hex}"
    try:
        try:
            assert await cast(Awaitable[bool], client.ping()) is True
        except (OSError, RedisError, TimeoutError) as error:
            pytest.fail(
                f"real Redis PING failed without exposing credentials: {type(error).__name__}"
            )
        yield RedisBackend(client, key_prefix=prefix, poll_interval=0.05), client
    finally:
        client.eval_release.set()
        client.return_release.set()
        client.append_release.set()
        await _delete_prefix(client, prefix)
        await client.aclose()


async def test_real_redis_replays_commits_across_backend_instances(
    redis_backends: tuple[RedisBackend, RedisBackend, Redis],
) -> None:
    owner, follower, _ = redis_backends
    prepared = await owner.prepare(
        channel="events",
        stream="conversation-1",
        run="run-1",
        codec="test.bytes.v1",
        identity="identity-1",
        after=0,
        cancellable=False,
        recoverable=False,
    )
    await owner.append(
        prepared.handle,
        message_id="message-1",
        codec="test.bytes.v1",
        payload=b"persisted",
    )
    await owner.finish(prepared.handle, status="completed")

    attached = await follower.prepare(
        channel="events",
        stream="conversation-1",
        run="run-1",
        codec="test.bytes.v1",
        identity="identity-1",
        after=0,
        cancellable=False,
        recoverable=False,
    )
    replay = [message async for message in follower.follow(attached.handle, after=0)]

    assert attached.is_owner is False
    assert [message.payload for message in replay] == [b"persisted"]
    assert replay[0].seq == 1


@pytest.mark.parametrize(
    ("message_id", "checkpoint"),
    [
        pytest.param("x" * 1025, None, id="overlong-message-id"),
        pytest.param(
            "message-1",
            RecoveryCheckpoint(
                position=b"invalid-position",
                last_message_id="different-message",
            ),
            id="mismatched-checkpoint",
        ),
    ],
)
async def test_real_redis_rejects_invalid_append_before_lua_or_state_change(
    counting_redis_backend: tuple[RedisBackend, _CommandCountingRedis, str],
    message_id: str,
    checkpoint: RecoveryCheckpoint | None,
) -> None:
    """Moving validation after EVAL must change this complete state snapshot."""

    backend, client, prefix = counting_redis_backend
    prepared = await backend.prepare(
        channel="events",
        stream="conversation-1",
        run="run-1",
        codec="test.bytes.v1",
        identity="identity-1",
        after=0,
        cancellable=False,
        recoverable=True,
    )
    channel_scope = hashlib.sha256(b"events").hexdigest()
    stream_digest = hashlib.sha256(b"conversation-1").hexdigest()
    run_digest = hashlib.sha256(b"run-1").hexdigest()
    message_digest = hashlib.sha256(message_id.encode()).hexdigest()
    generation_base = (
        f"{prefix}:{{{channel_scope}}}:stream:{stream_digest}:generation:1"
    )
    meta_key = f"{generation_base}:meta"
    run_key = f"{generation_base}:run:{run_digest}"
    messages_key = f"{generation_base}:messages"
    dedupe_key = f"{generation_base}:message:{message_digest}"
    index_key = f"{generation_base}:index"

    async def durable_state() -> tuple[
        int,
        bytes | None,
        dict[bytes, bytes],
        int,
        frozenset[bytes],
    ]:
        return (
            await cast(Awaitable[int], client.xlen(messages_key)),
            await cast(Awaitable[bytes | None], client.hget(meta_key, "seq")),
            await cast(
                Awaitable[dict[bytes, bytes]],
                client.hgetall(run_key),
            ),
            await cast(Awaitable[int], client.exists(dedupe_key)),
            frozenset(await cast(Awaitable[set[bytes]], client.smembers(index_key))),
        )

    before = await durable_state()
    client.command_counts.clear()

    with pytest.raises(ValueError):
        await backend.append(
            prepared.handle,
            message_id=message_id,
            codec="test.bytes.v1",
            payload=b"must-not-commit",
            checkpoint=checkpoint,
        )

    eval_calls = client.command_counts["eval"]
    after = await durable_state()
    assert eval_calls == 0
    assert after == before
    await backend.finish(prepared.handle, status="completed")


async def test_real_redis_idle_waits_do_not_run_a_fixed_polling_loop(
    counting_redis_backend: tuple[RedisBackend, _CommandCountingRedis, str],
) -> None:
    """Restoring the 100ms loop must exceed these idle command bounds."""

    backend, client, _ = counting_redis_backend
    prepared = await backend.prepare(
        channel="events",
        stream="conversation-1",
        run="run-1",
        codec="test.bytes.v1",
        identity="identity-1",
        after=0,
        cancellable=True,
        recoverable=False,
    )

    async def observe(awaitable: Awaitable[object]) -> Counter[str]:
        client.command_counts.clear()
        task = asyncio.ensure_future(awaitable)
        try:
            await asyncio.sleep(0.35)
            return client.command_counts.copy()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    follower = backend.follow(prepared.handle, after=0)
    try:
        follow_counts = await observe(anext(follower))
    finally:
        await follower.aclose()
    cancel_counts = await observe(backend.wait_for_cancel(prepared.handle))
    finish_counts = await observe(backend.wait_finished(prepared.handle))

    for counts in (follow_counts, cancel_counts, finish_counts):
        assert counts["eval"] <= 1
        assert counts["hgetall"] == 0
        assert counts["xrange"] == 0
        assert counts["xread"] <= 1
    await backend.finish(prepared.handle, status="completed")


async def test_real_redis_twenty_idle_followers_use_one_block_each(
    counting_redis_backend: tuple[RedisBackend, _CommandCountingRedis, str],
) -> None:
    """Follower count may scale blocked connections, not 100ms command churn."""

    backend, client, _ = counting_redis_backend
    prepared = await backend.prepare(
        channel="events",
        stream="conversation-1",
        run="run-1",
        codec="test.bytes.v1",
        identity="identity-1",
        after=0,
        cancellable=False,
        recoverable=False,
    )
    followers = [backend.follow(prepared.handle, after=0) for _ in range(20)]
    client.command_counts.clear()
    tasks = [asyncio.create_task(anext(follower)) for follower in followers]
    try:
        await asyncio.sleep(0.35)
        counts = client.command_counts.copy()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(follower.aclose() for follower in followers))

    assert counts["eval"] <= 20
    assert counts["hgetall"] == 0
    assert counts["xrange"] == 0
    assert counts["xread"] <= 20
    await backend.finish(prepared.handle, status="completed")


async def test_real_redis_cancel_and_finish_wake_without_poll_interval_delay(
    counting_redis_backend: tuple[RedisBackend, _CommandCountingRedis, str],
) -> None:
    """State signals, rather than a one-second poll, must control wake latency."""

    actor, client, prefix = counting_redis_backend
    waiter = RedisBackend(
        client,
        key_prefix=prefix,
        lease_ttl=3,
        poll_interval=1,
    )
    first = await actor.prepare(
        channel="events",
        stream="conversation-1",
        run="run-1",
        codec="test.bytes.v1",
        identity="identity-1",
        after=0,
        cancellable=True,
        recoverable=False,
    )
    client.command_counts.clear()
    cancel_wait = asyncio.create_task(waiter.wait_for_cancel(first.handle))
    try:
        async with asyncio.timeout(1):
            while (
                client.command_counts["hgetall"] == 0
                and client.command_counts["xread"] == 0
            ):
                await asyncio.sleep(0)
        assert await actor.request_cancel(first.handle) is True
        assert await asyncio.wait_for(cancel_wait, timeout=0.4) is True
    finally:
        cancel_wait.cancel()
        await asyncio.gather(cancel_wait, return_exceptions=True)
    assert await actor.begin_settlement(first.handle) is True
    await actor.finish(first.handle, status="cancelled")

    second = await actor.prepare(
        channel="events",
        stream="conversation-1",
        run="run-2",
        codec="test.bytes.v1",
        identity="identity-2",
        after=0,
        cancellable=False,
        recoverable=False,
    )
    client.command_counts.clear()
    finish_wait = asyncio.create_task(waiter.wait_finished(second.handle))
    try:
        async with asyncio.timeout(1):
            while (
                client.command_counts["hgetall"] == 0
                and client.command_counts["xread"] == 0
            ):
                await asyncio.sleep(0)
        await actor.finish(second.handle, status="completed")
        assert await asyncio.wait_for(finish_wait, timeout=0.4) == "completed"
    finally:
        finish_wait.cancel()
        await asyncio.gather(finish_wait, return_exceptions=True)


@pytest.mark.parametrize("transition", ["cancel", "finish"])
async def test_real_redis_signal_closes_the_snapshot_to_xread_gap(
    gated_xread_backends: tuple[RedisBackend, RedisBackend, _GatedXreadRedis],
    transition: str,
) -> None:
    """A state change before XREAD is sent must remain durably observable."""

    waiter, actor, client = gated_xread_backends
    prepared = await actor.prepare(
        channel="events",
        stream="conversation-1",
        run="run-1",
        codec="test.bytes.v1",
        identity="identity-1",
        after=0,
        cancellable=True,
        recoverable=False,
    )
    client.gate_next_xread = True
    waiting = asyncio.create_task(
        waiter.wait_for_cancel(prepared.handle)
        if transition == "cancel"
        else waiter.wait_finished(prepared.handle)
    )
    try:
        await asyncio.wait_for(client.xread_entered.wait(), timeout=0.5)
        if transition == "cancel":
            assert await actor.request_cancel(prepared.handle) is True
        else:
            await actor.finish(prepared.handle, status="completed")
        client.xread_release.set()
        result = await asyncio.wait_for(waiting, timeout=0.5)
        assert result is True if transition == "cancel" else result == "completed"
    finally:
        client.xread_release.set()
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
    if transition == "cancel":
        assert await actor.begin_settlement(prepared.handle) is True
        await actor.finish(prepared.handle, status="cancelled")


async def test_real_redis_state_change_before_snapshot_is_immediately_visible(
    redis_backends: tuple[RedisBackend, RedisBackend, Redis],
) -> None:
    owner, observer, _ = redis_backends
    prepared = await owner.prepare(
        channel="events",
        stream="conversation-1",
        run="run-1",
        codec="test.bytes.v1",
        identity="identity-1",
        after=0,
        cancellable=True,
        recoverable=False,
    )

    assert await owner.request_cancel(prepared.handle) is True
    assert (
        await asyncio.wait_for(
            observer.wait_for_cancel(prepared.handle),
            timeout=0.2,
        )
        is True
    )
    assert await owner.begin_settlement(prepared.handle) is True
    await owner.finish(prepared.handle, status="cancelled")
    assert (
        await asyncio.wait_for(observer.wait_finished(prepared.handle), timeout=0.2)
        == "cancelled"
    )


async def test_real_redis_nonrecoverable_lease_expiry_uses_its_pttl(
    redis_backends: tuple[RedisBackend, RedisBackend, Redis],
) -> None:
    _, _, client = redis_backends
    prefix = f"tfmsg:lease-aware:{uuid4().hex}"
    backend = RedisBackend(
        client,
        key_prefix=prefix,
        lease_ttl=0.15,
        poll_interval=10,
    )
    follower = None
    try:
        prepared = await backend.prepare(
            channel="events",
            stream="conversation-1",
            run="run-1",
            codec="test.bytes.v1",
            identity="identity-1",
            after=0,
            cancellable=False,
            recoverable=False,
        )
        follower = backend.follow(prepared.handle, after=0)

        with pytest.raises(RunProducerFailed, match="run-1"):
            await asyncio.wait_for(anext(follower), timeout=0.6)

        signal_keys = [
            key async for key in client.scan_iter(match=f"{prefix}:*:signals")
        ]
        assert len(signal_keys) == 1
        entries = await client.xrange(signal_keys[0])
        assert entries is not None
        signal_entries = cast(
            Sequence[tuple[bytes, Mapping[bytes, bytes]]],
            entries,
        )
        assert signal_entries[-1][1][b"kind"] == b"owner_lost"
    finally:
        if follower is not None:
            await follower.aclose()
        await _delete_prefix(client, prefix)


async def test_real_redis_recoverable_lease_expiry_waits_for_takeover_signal(
    redis_backends: tuple[RedisBackend, RedisBackend, Redis],
) -> None:
    _, _, client = redis_backends
    prefix = f"tfmsg:recover-signal:{uuid4().hex}"
    stale = RedisBackend(
        client,
        key_prefix=prefix,
        lease_ttl=0.15,
        poll_interval=10,
    )
    recovering = RedisBackend(
        client,
        key_prefix=prefix,
        lease_ttl=0.15,
        poll_interval=10,
    )
    waiting = None
    try:
        original = await stale.prepare(
            channel="events",
            stream="conversation-1",
            run="run-1",
            codec="test.bytes.v1",
            identity="identity-1",
            after=0,
            cancellable=False,
            recoverable=True,
        )
        waiting = asyncio.create_task(stale.wait_finished(original.handle))
        await asyncio.sleep(0.2)

        recovered = await recovering.prepare(
            channel="events",
            stream="conversation-1",
            run="run-1",
            codec="test.bytes.v1",
            identity="identity-1",
            after=0,
            cancellable=False,
            recoverable=True,
        )
        assert recovered.is_owner is True
        assert recovered.recovered is True
        await recovering.finish(recovered.handle, status="completed")

        assert await asyncio.wait_for(waiting, timeout=0.6) == "completed"
        signal_keys = [
            key async for key in client.scan_iter(match=f"{prefix}:*:signals")
        ]
        assert len(signal_keys) == 1
        entries = await client.xrange(signal_keys[0])
        assert entries is not None
        signal_entries = cast(
            Sequence[tuple[bytes, Mapping[bytes, bytes]]],
            entries,
        )
        assert [entry[1][b"kind"] for entry in signal_entries] == [
            b"recover",
            b"finish",
        ]
    finally:
        if waiting is not None and not waiting.done():
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
        await _delete_prefix(client, prefix)


async def test_real_redis_cancelled_block_releases_the_only_connection() -> None:
    client = _redis_client(_CommandCountingRedis, max_connections=1)
    client.command_counts = Counter()
    prefix = f"tfmsg:cancelled-block:{uuid4().hex}"
    waiting = None
    try:
        assert await cast(Awaitable[bool], client.ping()) is True
        backend = RedisBackend(client, key_prefix=prefix, lease_ttl=3)
        prepared = await backend.prepare(
            channel="events",
            stream="conversation-1",
            run="run-1",
            codec="test.bytes.v1",
            identity="identity-1",
            after=0,
            cancellable=False,
            recoverable=False,
        )
        client.command_counts.clear()
        waiting = asyncio.create_task(backend.wait_finished(prepared.handle))
        async with asyncio.timeout(1):
            while client.command_counts["xread"] == 0:
                await asyncio.sleep(0)
        await asyncio.sleep(0.05)

        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert (
            await asyncio.wait_for(
                cast(Awaitable[bool], client.ping()),
                timeout=0.5,
            )
            is True
        )
        await backend.finish(prepared.handle, status="completed")
    finally:
        if waiting is not None and not waiting.done():
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
        await _delete_prefix(client, prefix)
        await client.aclose()


async def test_real_redis_cancellation_settles_an_inflight_snapshot(
    gated_redis_backend: tuple[RedisBackend, _GatedEvalRedis],
) -> None:
    backend, client = gated_redis_backend
    prepared = await backend.prepare(
        channel="events",
        stream="conversation-1",
        run="run-1",
        codec="test.bytes.v1",
        identity="identity-1",
        after=0,
        cancellable=True,
        recoverable=False,
    )
    client.gate_next_eval_before = True
    waiting = asyncio.create_task(backend.wait_for_cancel(prepared.handle))
    try:
        await asyncio.wait_for(client.eval_entered.wait(), timeout=0.5)
        waiting.cancel("caller cancelled during the Redis snapshot")
        await asyncio.sleep(0)
        assert not waiting.done()

        client.eval_release.set()
        with pytest.raises(
            asyncio.CancelledError,
            match="caller cancelled during the Redis snapshot",
        ):
            await asyncio.wait_for(waiting, timeout=0.5)
        assert await cast(Awaitable[bool], client.ping()) is True
        await backend.finish(prepared.handle, status="completed")
    finally:
        client.eval_release.set()
        if not waiting.done():
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)


async def test_real_redis_burst_replay_keeps_a_bounded_pull_page(
    counting_redis_backend: tuple[RedisBackend, _CommandCountingRedis, str],
) -> None:
    """Snapshot optimization must not prefetch beyond the existing 100-item page."""

    backend, client, _ = counting_redis_backend
    prepared = await backend.prepare(
        channel="events",
        stream="conversation-1",
        run="run-1",
        codec="test.bytes.v1",
        identity="identity-1",
        after=0,
        cancellable=False,
        recoverable=False,
    )
    for index in range(150):
        await backend.append(
            prepared.handle,
            message_id=f"message-{index}",
            codec="test.bytes.v1",
            payload=str(index).encode(),
        )
    await backend.finish(prepared.handle, status="completed")

    client.command_counts.clear()
    replay = backend.follow(prepared.handle, after=0)
    first = await anext(replay)
    commands_after_first_pull = client.command_counts.copy()
    await asyncio.sleep(0.15)
    assert client.command_counts == commands_after_first_pull
    messages = [first]
    messages.extend([message async for message in replay])

    assert [message.payload for message in messages] == [
        str(index).encode() for index in range(150)
    ]
    assert client.command_counts["eval"] <= 2
    assert client.command_counts["hgetall"] == 0
    assert client.command_counts["xrange"] == 0
    assert client.command_counts["xread"] == 0


async def test_real_redis_workers_bind_channel_codec_atomically(
    redis_backends: tuple[RedisBackend, RedisBackend, Redis],
) -> None:
    first, second, _ = redis_backends

    async def prepare(
        backend: RedisBackend,
        *,
        stream: str,
        run: str,
        codec: str,
    ):
        return await backend.prepare(
            channel="shared-channel",
            stream=stream,
            run=run,
            codec=codec,
            identity=f"identity:{run}",
            after=0,
            cancellable=False,
            recoverable=False,
        )

    outcomes = await asyncio.gather(
        prepare(
            first,
            stream="stream-a",
            run="run-a",
            codec="test.codec-a.v1",
        ),
        prepare(
            second,
            stream="stream-b",
            run="run-b",
            codec="test.codec-b.v1",
        ),
        return_exceptions=True,
    )

    owners = [outcome for outcome in outcomes if isinstance(outcome, PreparedRun)]
    mismatches = [outcome for outcome in outcomes if isinstance(outcome, CodecMismatch)]
    assert len(owners) == 1
    assert len(mismatches) == 1
    owner_backend = first if outcomes[0] is owners[0] else second
    await owner_backend.finish(owners[0].handle, status="completed")


async def test_real_redis_persists_channel_and_stream_metadata_separately(
    redis_backends: tuple[RedisBackend, RedisBackend, Redis],
) -> None:
    backend, _, client = redis_backends
    prepared_runs: list[PreparedRun] = []
    for index in (1, 2):
        prepared = await backend.prepare(
            channel="events",
            stream=f"stream-{index}",
            run=f"run-{index}",
            codec="test.bytes.v1",
            identity=f"identity-{index}",
            after=0,
            cancellable=False,
            recoverable=False,
        )
        await backend.append(
            prepared.handle,
            message_id=f"message-{index}",
            codec="test.bytes.v1",
            payload=f"payload-{index}".encode(),
        )
        await backend.finish(prepared.handle, status="completed")
        prepared_runs.append(prepared)

    keys = [key async for key in client.scan_iter(match="tfmsg:test:*")]
    decoded_keys = [key.decode() for key in keys]
    channel_keys = [key for key in decoded_keys if key.endswith(":channel")]
    control_keys = [key for key in decoded_keys if key.endswith(":control")]
    stream_meta_keys = [key for key in decoded_keys if key.endswith(":meta")]
    run_keys = [key for key in decoded_keys if ":generation:1:run:" in key]
    message_stream_keys = [key for key in decoded_keys if key.endswith(":messages")]
    signal_stream_keys = [key for key in decoded_keys if key.endswith(":signals")]
    index_keys = [key for key in decoded_keys if key.endswith(":index")]
    dedupe_keys = [key for key in decoded_keys if ":generation:1:message:" in key]

    assert len(channel_keys) == 1
    assert len(control_keys) == 2
    assert len(stream_meta_keys) == 2
    assert len(run_keys) == 2
    assert len(message_stream_keys) == 2
    assert len(signal_stream_keys) == 2
    assert len(index_keys) == 2
    assert len(dedupe_keys) == 2
    hash_tags = {key.split("{", 1)[1].split("}", 1)[0] for key in decoded_keys}
    assert len(hash_tags) == 1

    channel_meta = await cast(
        Awaitable[dict[bytes, bytes]], client.hgetall(channel_keys[0])
    )
    assert channel_meta == {
        b"channel": b"events",
        b"codec": b"test.bytes.v1",
        b"schema_version": b"3",
    }
    controls = [
        await cast(Awaitable[dict[bytes, bytes]], client.hgetall(key))
        for key in control_keys
    ]
    assert {control[b"stream"] for control in controls} == {
        b"stream-1",
        b"stream-2",
    }
    assert all(control[b"channel"] == b"events" for control in controls)
    assert all(control[b"generation"] == b"1" for control in controls)
    assert all(control[b"state"] == b"active" for control in controls)
    assert all(control[b"schema_version"] == b"3" for control in controls)
    assert all(control[b"signal_seq"] == b"1" for control in controls)
    stream_metadata = [
        await cast(Awaitable[dict[bytes, bytes]], client.hgetall(key))
        for key in stream_meta_keys
    ]
    assert {metadata[b"stream"] for metadata in stream_metadata} == {
        b"stream-1",
        b"stream-2",
    }
    assert all(metadata[b"channel"] == b"events" for metadata in stream_metadata)
    assert all(metadata[b"generation"] == b"1" for metadata in stream_metadata)
    assert all(metadata[b"seq"] == b"1" for metadata in stream_metadata)
    assert all(metadata[b"schema_version"] == b"3" for metadata in stream_metadata)

    run_metadata = [
        await cast(Awaitable[dict[bytes, bytes]], client.hgetall(key))
        for key in run_keys
    ]
    assert {metadata[b"status"] for metadata in run_metadata} == {b"completed"}
    assert {metadata[b"run"] for metadata in run_metadata} == {b"run-1", b"run-2"}
    for key in index_keys:
        indexed = {
            _redis_text(member)
            for member in await cast(Awaitable[set[bytes]], client.smembers(key))
        }
        assert len(indexed) == 5
        assert all(":generation:1:" in member for member in indexed)
    for key in message_stream_keys:
        entries = await client.xrange(key)
        assert entries is not None
        assert len(entries) == 1
        identifier, fields = entries[0]
        assert fields is not None
        assert identifier == b"1-0"
        assert fields[b"codec"] == b"test.bytes.v1"
    for key in signal_stream_keys:
        entries = await client.xrange(key)
        assert entries is not None
        assert len(entries) == 1
        identifier, fields = entries[0]
        assert fields is not None
        assert identifier == b"1-0"
        assert fields[b"kind"] == b"finish"
        assert fields[b"generation"] == b"1"
        assert fields[b"run"] in {b"run-1", b"run-2"}


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        pytest.param("status", "corrupt", "invalid status", id="status"),
        pytest.param("end_seq", "not-an-integer", "invalid end_seq", id="end-seq"),
    ],
)
async def test_real_redis_rejects_malformed_run_snapshot_scalars(
    redis_backends: tuple[RedisBackend, RedisBackend, Redis],
    field: str,
    value: str,
    message: str,
) -> None:
    backend, _, client = redis_backends
    prepared = await backend.prepare(
        channel="events",
        stream="conversation-1",
        run="run-1",
        codec="test.bytes.v1",
        identity="identity-1",
        after=0,
        cancellable=False,
        recoverable=False,
    )
    run_keys = [
        key async for key in client.scan_iter(match="tfmsg:test:*:generation:1:run:*")
    ]
    assert len(run_keys) == 1
    await cast(Awaitable[int], client.hset(run_keys[0], field, value))

    with pytest.raises(RuntimeError, match=message):
        await backend.failure(prepared.handle)

    await backend.finish(prepared.handle, status="completed")


async def test_real_redis_rejects_a_malformed_snapshot_message_entry(
    redis_backends: tuple[RedisBackend, RedisBackend, Redis],
) -> None:
    backend, _, client = redis_backends
    prepared = await backend.prepare(
        channel="events",
        stream="conversation-1",
        run="run-1",
        codec="test.bytes.v1",
        identity="identity-1",
        after=0,
        cancellable=False,
        recoverable=False,
    )
    controls = [key async for key in client.scan_iter(match="tfmsg:test:*:control")]
    assert len(controls) == 1
    stream_base = _redis_text(controls[0]).removesuffix(":control")
    generation_base = f"{stream_base}:generation:1"
    run_keys = [key async for key in client.scan_iter(match=f"{generation_base}:run:*")]
    assert len(run_keys) == 1
    await client.xadd(
        f"{generation_base}:messages",
        {"message_id": "missing-required-fields"},
        id="1-0",
    )
    await cast(Awaitable[int], client.hset(run_keys[0], "end_seq", "1"))
    follower = backend.follow(prepared.handle, after=0)

    try:
        with pytest.raises(RuntimeError, match="incomplete message fields"):
            await anext(follower)
    finally:
        await follower.aclose()
    await backend.finish(prepared.handle, status="completed")


async def test_real_redis_rejected_prepare_does_not_index_phantom_run_keys(
    redis_backends: tuple[RedisBackend, RedisBackend, Redis],
) -> None:
    first, second, client = redis_backends
    prepared = await first.prepare(
        channel="events",
        stream="conversation-1",
        run="run-active",
        codec="test.bytes.v1",
        identity="identity-active",
        after=0,
        cancellable=False,
        recoverable=False,
    )

    with pytest.raises(RunAlreadyActive):
        await second.prepare(
            channel="events",
            stream="conversation-1",
            run="run-rejected",
            codec="test.bytes.v1",
            identity="identity-rejected",
            after=0,
            cancellable=False,
            recoverable=False,
        )

    index_keys = [
        key async for key in client.scan_iter(match="tfmsg:test:*:generation:1:index")
    ]
    assert len(index_keys) == 1
    indexed = {
        _redis_text(member)
        for member in await cast(Awaitable[set[bytes]], client.smembers(index_keys[0]))
    }
    rejected_digest = hashlib.sha256(b"run-rejected").hexdigest()
    assert not any(rejected_digest in member for member in indexed)
    await first.finish(prepared.handle, status="completed")


async def test_real_redis_delete_unlinks_only_the_target_generation(
    redis_backends: tuple[RedisBackend, RedisBackend, Redis],
) -> None:
    backend, _, client = redis_backends
    for stream in ("stream-deleted", "stream-retained"):
        run = f"run-{stream}"
        owner = await backend.prepare(
            channel="events",
            stream=stream,
            run=run,
            codec="test.bytes.v1",
            identity=f"identity:{run}",
            after=0,
            cancellable=False,
            recoverable=False,
        )
        await backend.append(
            owner.handle,
            message_id=f"message-{stream}",
            codec="test.bytes.v1",
            payload=stream.encode(),
        )
        await backend.finish(owner.handle, status="completed")

    controls = [key async for key in client.scan_iter(match="tfmsg:test:*:control")]
    control_by_stream = {
        _redis_text(stream_value): _redis_text(key)
        for key in controls
        if (
            stream_value := await cast(
                Awaitable[bytes | None], client.hget(key, "stream")
            )
        )
        is not None
    }
    deleted_base = control_by_stream["stream-deleted"].removesuffix(":control")
    retained_base = control_by_stream["stream-retained"].removesuffix(":control")

    await backend.delete_stream(channel="events", stream="stream-deleted")

    deleted_control = await cast(
        Awaitable[dict[bytes, bytes]], client.hgetall(f"{deleted_base}:control")
    )
    assert deleted_control[b"generation"] == b"1"
    assert deleted_control[b"state"] == b"deleted"
    assert [
        key
        async for key in client.scan_iter(
            match=f"{deleted_base}:generation:*",
        )
    ] == []
    assert await client.exists(f"{deleted_base}:delete-lease") == 0
    assert [
        key
        async for key in client.scan_iter(
            match=f"{retained_base}:generation:*",
        )
    ]
    channel_keys = [key async for key in client.scan_iter(match="tfmsg:test:*:channel")]
    assert len(channel_keys) == 1
    assert (
        await cast(Awaitable[bytes | None], client.hget(channel_keys[0], "codec"))
        == b"test.bytes.v1"
    )

    rebuilt = await backend.prepare(
        channel="events",
        stream="stream-deleted",
        run="run-rebuilt",
        codec="test.bytes.v1",
        identity="identity:run-rebuilt",
        after=0,
        cancellable=False,
        recoverable=False,
    )
    assert rebuilt.handle.generation == 2
    assert [
        key
        async for key in client.scan_iter(
            match=f"{deleted_base}:generation:1:*",
        )
    ] == []
    assert [
        key
        async for key in client.scan_iter(
            match=f"{deleted_base}:generation:2:*",
        )
    ]
    await backend.finish(rebuilt.handle, status="completed")


async def test_real_redis_delete_fences_an_expired_producer(
    redis_backends: tuple[RedisBackend, RedisBackend, Redis],
) -> None:
    _, _, client = redis_backends
    prefix = f"tfmsg:delete-expired:{uuid4().hex}"
    stale = RedisBackend(client, key_prefix=prefix, lease_ttl=0.15)
    deleter = RedisBackend(client, key_prefix=prefix, lease_ttl=0.15)
    try:
        prepared = await stale.prepare(
            channel="events",
            stream="conversation-1",
            run="run-1",
            codec="test.bytes.v1",
            identity="identity-1",
            after=0,
            cancellable=False,
            recoverable=False,
        )
        await asyncio.sleep(0.2)

        await deleter.delete_stream(channel="events", stream="conversation-1")

        with pytest.raises(StreamDeleted):
            await stale.append(
                prepared.handle,
                message_id="stale-message",
                codec="test.bytes.v1",
                payload=b"stale",
            )
        with pytest.raises(StreamDeleted):
            await stale.finish(prepared.handle, status="completed")
        with pytest.raises(StreamDeleted):
            await stale.renew(prepared.handle)
        remaining = [key async for key in client.scan_iter(match=f"{prefix}:*")]
        assert len(remaining) == 3
        assert any(key.endswith(b":channel") for key in remaining)
        assert any(key.endswith(b":control") for key in remaining)
        assert any(key.endswith(b":signals") for key in remaining)
    finally:
        await _delete_prefix(client, prefix)


async def test_real_redis_delete_wakes_a_follower_of_an_expired_producer(
    redis_backends: tuple[RedisBackend, RedisBackend, Redis],
) -> None:
    _, _, client = redis_backends
    prefix = f"tfmsg:delete-follower:{uuid4().hex}"
    stale = RedisBackend(
        client,
        key_prefix=prefix,
        lease_ttl=0.15,
        poll_interval=0.5,
    )
    deleter = RedisBackend(
        client,
        key_prefix=prefix,
        lease_ttl=0.15,
        poll_interval=0.02,
    )
    try:
        prepared = await stale.prepare(
            channel="events",
            stream="conversation-1",
            run="run-1",
            codec="test.bytes.v1",
            identity="identity-1",
            after=0,
            cancellable=False,
            recoverable=True,
        )
        follower = stale.follow(prepared.handle, after=0)
        waiting = asyncio.create_task(anext(follower))
        await asyncio.sleep(0.2)

        await deleter.delete_stream(channel="events", stream="conversation-1")

        try:
            with pytest.raises(StreamDeleted):
                await asyncio.wait_for(waiting, timeout=1)
        finally:
            await follower.aclose()
    finally:
        await _delete_prefix(client, prefix)


async def test_real_redis_concurrent_deletes_converge_after_multiple_batches(
    redis_backends: tuple[RedisBackend, RedisBackend, Redis],
) -> None:
    first, second, client = redis_backends
    prepared = await first.prepare(
        channel="events",
        stream="conversation-1",
        run="run-1",
        codec="test.bytes.v1",
        identity="identity-1",
        after=0,
        cancellable=False,
        recoverable=False,
    )
    for index in range(130):
        await first.append(
            prepared.handle,
            message_id=f"message-{index}",
            codec="test.bytes.v1",
            payload=str(index).encode(),
        )
    await first.finish(prepared.handle, status="completed")

    await asyncio.gather(
        first.delete_stream(channel="events", stream="conversation-1"),
        second.delete_stream(channel="events", stream="conversation-1"),
    )

    controls = [key async for key in client.scan_iter(match="tfmsg:test:*:control")]
    assert len(controls) == 1
    assert (
        await cast(Awaitable[bytes | None], client.hget(controls[0], "state"))
        == b"deleted"
    )
    generation_base = _redis_text(controls[0]).removesuffix(":control")
    assert [
        key async for key in client.scan_iter(match=f"{generation_base}:generation:*")
    ] == []


async def test_real_redis_cancelled_delete_is_taken_over_after_lease_expiry() -> None:
    gated_client = _redis_client(_GatedDeleteRedis)
    takeover_client = _redis_client(Redis)
    prefix = f"tfmsg:delete-takeover:{uuid4().hex}"
    gated_client.delete_cleanup_entered = asyncio.Event()
    gated_client.delete_cleanup_release = asyncio.Event()
    owner = RedisBackend(gated_client, key_prefix=prefix, lease_ttl=0.15)
    takeover = RedisBackend(takeover_client, key_prefix=prefix, lease_ttl=0.15)
    try:
        prepared = await owner.prepare(
            channel="events",
            stream="conversation-1",
            run="run-1",
            codec="test.bytes.v1",
            identity="identity-1",
            after=0,
            cancellable=False,
            recoverable=False,
        )
        await owner.append(
            prepared.handle,
            message_id="message-1",
            codec="test.bytes.v1",
            payload=b"persisted",
        )
        await owner.finish(prepared.handle, status="completed")
        gated_client.gate_next_delete_cleanup = True

        deleting = asyncio.create_task(
            owner.delete_stream(channel="events", stream="conversation-1")
        )
        await asyncio.wait_for(
            gated_client.delete_cleanup_entered.wait(),
            timeout=1,
        )
        with pytest.raises(StreamDeleted):
            await takeover.prepare(
                channel="events",
                stream="conversation-1",
                run="run-during-delete",
                codec="test.bytes.v1",
                identity="identity-during-delete",
                after=0,
                cancellable=False,
                recoverable=False,
            )
        assert (
            await takeover.latest_seq(
                channel="events",
                stream="conversation-1",
            )
            == 0
        )
        assert (
            await takeover.read(
                channel="events",
                stream="conversation-1",
                after=0,
            )
            == ()
        )
        with pytest.raises(StreamDeleted):
            await takeover.request_cancel(prepared.handle)
        with pytest.raises(StreamDeleted):
            await takeover.append(
                prepared.handle,
                message_id="during-delete",
                codec="test.bytes.v1",
                payload=b"must-not-commit",
            )
        with pytest.raises(StreamDeleted):
            await takeover.finish(prepared.handle, status="completed")
        with pytest.raises(StreamDeleted):
            await takeover.renew(prepared.handle)
        deleting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await deleting
        controls = [
            key async for key in takeover_client.scan_iter(match=f"{prefix}:*:control")
        ]
        assert len(controls) == 1
        assert (
            await cast(
                Awaitable[bytes | None], takeover_client.hget(controls[0], "state")
            )
            == b"deleting"
        )

        await asyncio.sleep(0.2)
        await takeover.delete_stream(channel="events", stream="conversation-1")

        assert (
            await cast(
                Awaitable[bytes | None], takeover_client.hget(controls[0], "state")
            )
            == b"deleted"
        )
        generation_base = _redis_text(controls[0]).removesuffix(":control")
        assert [
            key
            async for key in takeover_client.scan_iter(
                match=f"{generation_base}:generation:*",
            )
        ] == []
    finally:
        await _delete_prefix(takeover_client, prefix)
        await asyncio.gather(gated_client.aclose(), takeover_client.aclose())


async def test_real_redis_delete_is_taken_over_after_worker_process_is_killed(
    redis_backends: tuple[RedisBackend, RedisBackend, Redis],
) -> None:
    _, _, client = redis_backends
    prefix = f"tfmsg:delete-process:{uuid4().hex}"
    backend = RedisBackend(
        client,
        key_prefix=prefix,
        lease_ttl=0.3,
        poll_interval=0.02,
    )
    prepared = await backend.prepare(
        channel="events",
        stream="conversation-1",
        run="run-1",
        codec="test.bytes.v1",
        identity="identity-1",
        after=0,
        cancellable=False,
        recoverable=False,
    )
    await backend.append(
        prepared.handle,
        message_id="message-1",
        codec="test.bytes.v1",
        payload=b"persisted",
    )
    await backend.finish(prepared.handle, status="completed")
    context = multiprocessing.get_context("spawn")
    cleanup_started = context.Event()
    process = context.Process(
        target=_run_delete_until_killed,
        args=(prefix, cleanup_started),
    )
    try:
        await asyncio.to_thread(process.start)
        assert await asyncio.to_thread(cleanup_started.wait, 5)
        controls = [key async for key in client.scan_iter(match=f"{prefix}:*:control")]
        assert len(controls) == 1
        assert (
            await cast(Awaitable[bytes | None], client.hget(controls[0], "state"))
            == b"deleting"
        )

        process.kill()
        await asyncio.to_thread(process.join, 5)
        assert process.exitcode is not None and process.exitcode != 0
        await asyncio.sleep(0.4)

        await backend.delete_stream(channel="events", stream="conversation-1")

        assert (
            await cast(Awaitable[bytes | None], client.hget(controls[0], "state"))
            == b"deleted"
        )
        generation_base = _redis_text(controls[0]).removesuffix(":control")
        assert [
            key
            async for key in client.scan_iter(
                match=f"{generation_base}:generation:*",
            )
        ] == []
    finally:
        if process.is_alive():
            process.kill()
        await asyncio.to_thread(process.join, 5)
        await _delete_prefix(client, prefix)


async def test_real_redis_lifecycle_signal_is_bounded_and_cross_generation(
    redis_backends: tuple[RedisBackend, RedisBackend, Redis],
) -> None:
    _, _, client = redis_backends
    prefix = f"tfmsg:cancel-state:{uuid4().hex}"
    backend = RedisBackend(
        client,
        key_prefix=prefix,
        lease_ttl=0.6,
        poll_interval=0.05,
    )
    try:
        prepared = await backend.prepare(
            channel="events",
            stream="conversation-1",
            run="run-1",
            codec="test.bytes.v1",
            identity="identity-1",
            after=0,
            cancellable=True,
            recoverable=False,
        )

        assert await backend.request_cancel(prepared.handle) is True
        assert await backend.wait_for_cancel(prepared.handle) is True
        assert await backend.begin_settlement(prepared.handle) is True
        await backend.finish(prepared.handle, status="cancelled")
        signal_keys = [
            key async for key in client.scan_iter(match=f"{prefix}:*:signals")
        ]

        assert len(signal_keys) == 1
        signal_key = signal_keys[0]
        for index in range(2, 132):
            current = await backend.prepare(
                channel="events",
                stream="conversation-1",
                run=f"run-{index}",
                codec="test.bytes.v1",
                identity=f"identity-{index}",
                after=0,
                cancellable=True,
                recoverable=False,
            )
            assert await backend.request_cancel(current.handle) is True
            assert await backend.begin_settlement(current.handle) is True
            await backend.finish(current.handle, status="cancelled")

        entries = await client.xrange(signal_key)
        assert entries is not None
        signal_entries = cast(
            Sequence[tuple[bytes, Mapping[bytes, bytes]]],
            entries,
        )
        assert len(signal_entries) == 256
        assert signal_entries[-1][0] == b"262-0"
        assert signal_entries[-1][1][b"kind"] == b"finish"
        assert signal_entries[-1][1][b"generation"] == b"1"
        assert signal_entries[-1][1][b"run"] == b"run-131"
        await backend.delete_stream(channel="events", stream="conversation-1")
        assert await client.xlen(signal_key) == 256
        entries = await client.xrange(signal_key)
        assert entries is not None
        assert entries[-1] == (
            b"263-0",
            {b"kind": b"delete", b"generation": b"1", b"run": b""},
        )

        rebuilt = await backend.prepare(
            channel="events",
            stream="conversation-1",
            run="run-rebuilt",
            codec="test.bytes.v1",
            identity="identity-rebuilt",
            after=0,
            cancellable=False,
            recoverable=False,
        )
        assert rebuilt.handle.generation == 2
        await backend.finish(rebuilt.handle, status="completed")
        assert await client.xlen(signal_key) == 256
        entries = await client.xrange(signal_key)
        assert entries is not None
        assert entries[-1] == (
            b"264-0",
            {
                b"kind": b"finish",
                b"generation": b"2",
                b"run": b"run-rebuilt",
            },
        )
        with pytest.raises(StreamDeleted):
            await backend.wait_finished(prepared.handle)
    finally:
        await _delete_prefix(client, prefix)


async def test_real_redis_shutdown_settles_during_the_first_commit(
    gated_redis_backend: tuple[RedisBackend, _GatedEvalRedis],
) -> None:
    backend, client = gated_redis_backend
    release = asyncio.Event()
    source = _Source("first", release=release)
    messaging = Messaging(backend=backend)

    async def cancel() -> None:
        return None

    await messaging.__aenter__()
    client.gate_append_response = True
    try:
        await messaging.channel(name="events", codec=_TextCodec()).wrap(
            source,
            stream="conversation-1",
            run="run-1",
            after=0,
            cancel=cancel,
        )
        await asyncio.wait_for(client.append_committed.wait(), timeout=1)

        closing = asyncio.create_task(messaging.__aexit__(None, None, None))
        await asyncio.sleep(0)
        assert not closing.done()
        client.append_release.set()
        await asyncio.wait_for(closing, timeout=2)

        assert source.close_calls == 1
    finally:
        client.append_release.set()
        release.set()
        await messaging.aclose()


async def test_real_redis_running_follower_never_crosses_into_a_later_run(
    gated_redis_backend: tuple[RedisBackend, _GatedEvalRedis],
) -> None:
    backend, client = gated_redis_backend
    first = await backend.prepare(
        channel="events",
        stream="conversation-1",
        run="run-1",
        codec="test.redis-text.v1",
        identity="identity-1",
        after=0,
        cancellable=False,
        recoverable=False,
    )
    await backend.append(
        first.handle,
        message_id="run-1:1",
        codec="test.redis-text.v1",
        payload=b"first",
    )
    follower = backend.follow(first.handle, after=1)
    client.gate_next_eval_after = True
    next_message = asyncio.create_task(anext(follower))
    await asyncio.wait_for(client.eval_returned.wait(), timeout=1)

    await backend.finish(first.handle, status="completed")
    second = await backend.prepare(
        channel="events",
        stream="conversation-1",
        run="run-2",
        codec="test.redis-text.v1",
        identity="identity-2",
        after=1,
        cancellable=False,
        recoverable=False,
    )
    await backend.append(
        second.handle,
        message_id="run-2:1",
        codec="test.redis-text.v1",
        payload=b"second",
    )
    await backend.finish(second.handle, status="completed")
    client.return_release.set()

    try:
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(next_message, timeout=1)
    finally:
        await follower.aclose()


async def test_real_redis_terminal_snapshot_precedes_later_deletion(
    gated_redis_backend: tuple[RedisBackend, _GatedEvalRedis],
) -> None:
    backend, client = gated_redis_backend
    prepared = await backend.prepare(
        channel="events",
        stream="conversation-1",
        run="run-1",
        codec="test.redis-text.v1",
        identity="identity-1",
        after=0,
        cancellable=False,
        recoverable=False,
    )
    await backend.append(
        prepared.handle,
        message_id="message-1",
        codec="test.redis-text.v1",
        payload=b"first",
    )
    await backend.finish(prepared.handle, status="completed")
    follower = backend.follow(prepared.handle, after=1)
    client.gate_next_eval_after = True
    next_message = asyncio.create_task(anext(follower))
    await asyncio.wait_for(client.eval_returned.wait(), timeout=1)

    await backend.delete_stream(channel="events", stream="conversation-1")
    client.return_release.set()

    try:
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(next_message, timeout=1)
    finally:
        await follower.aclose()


async def test_real_redis_deletion_precedes_the_atomic_run_snapshot(
    gated_redis_backend: tuple[RedisBackend, _GatedEvalRedis],
) -> None:
    backend, client = gated_redis_backend
    prepared = await backend.prepare(
        channel="events",
        stream="conversation-1",
        run="run-1",
        codec="test.redis-text.v1",
        identity="identity-1",
        after=0,
        cancellable=False,
        recoverable=False,
    )
    await backend.finish(prepared.handle, status="completed")
    follower = backend.follow(prepared.handle, after=0)
    client.gate_next_eval_before = True
    next_message = asyncio.create_task(anext(follower))
    await asyncio.wait_for(client.eval_entered.wait(), timeout=1)

    await backend.delete_stream(channel="events", stream="conversation-1")
    client.eval_release.set()

    try:
        with pytest.raises(StreamDeleted):
            await asyncio.wait_for(next_message, timeout=1)
    finally:
        await follower.aclose()


async def test_real_redis_cross_worker_attach_uses_one_producer(
    redis_backends: tuple[RedisBackend, RedisBackend, Redis],
) -> None:
    owner_backend, follower_backend, _ = redis_backends
    release = asyncio.Event()
    owner_source = _Source("one", release=release)
    unused_source = _Source("must-not-run")

    async with (
        Messaging(backend=owner_backend) as owner_messaging,
        Messaging(backend=follower_backend) as follower_messaging,
    ):
        owner_channel = owner_messaging.channel(name="events", codec=_TextCodec())
        follower_channel = follower_messaging.channel(
            name="events",
            codec=_TextCodec(),
        )
        first = await owner_channel.wrap(
            owner_source,
            stream="conversation-1",
            run="run-1",
            after=0,
            attach_identity={"input": "same"},
        )
        await asyncio.wait_for(owner_source.started.wait(), timeout=1)
        second = await follower_channel.wrap(
            unused_source,
            stream="conversation-1",
            run="run-1",
            after=0,
            attach_identity={"input": "same"},
        )
        release.set()

        first_data, second_data = await asyncio.gather(_data(first), _data(second))

    assert first_data == ["one"]
    assert second_data == ["one"]
    assert unused_source.close_calls == 1
    assert not unused_source.started.is_set()


async def test_real_redis_remote_cancel_reaches_the_owner_callback(
    redis_backends: tuple[RedisBackend, RedisBackend, Redis],
) -> None:
    owner_backend, remote_backend, _ = redis_backends
    release = asyncio.Event()
    source = _Source("started", release=release)
    cancel_calls = 0

    async def cancel_run() -> None:
        nonlocal cancel_calls
        cancel_calls += 1
        release.set()

    async with (
        Messaging(backend=owner_backend) as owner_messaging,
        Messaging(backend=remote_backend) as remote_messaging,
    ):
        owner_channel = owner_messaging.channel(name="events", codec=_TextCodec())
        remote_channel = remote_messaging.channel(name="events", codec=_TextCodec())
        subscription = await owner_channel.wrap(
            source,
            stream="conversation-1",
            run="run-1",
            after=0,
            cancel=cancel_run,
        )
        await asyncio.wait_for(source.started.wait(), timeout=1)

        assert await asyncio.wait_for(
            remote_channel.cancel(stream="conversation-1", run="run-1"),
            timeout=2,
        )
        assert await _data(subscription) == ["started"]

    assert cancel_calls == 1


async def test_real_redis_expired_owner_is_fenced_and_prefix_remains_replayable(
    redis_backends: tuple[RedisBackend, RedisBackend, Redis],
) -> None:
    stale, observer, _ = redis_backends
    prepared = await stale.prepare(
        channel="events",
        stream="conversation-1",
        run="run-1",
        codec="test.bytes.v1",
        identity="identity-1",
        after=0,
        cancellable=False,
        recoverable=False,
    )
    await stale.append(
        prepared.handle,
        message_id="message-1",
        codec="test.bytes.v1",
        payload=b"committed",
    )
    await asyncio.sleep(0.8)

    attached = await observer.prepare(
        channel="events",
        stream="conversation-1",
        run="run-1",
        codec="test.bytes.v1",
        identity="identity-1",
        after=0,
        cancellable=False,
        recoverable=False,
    )

    with pytest.raises(BackendOwnershipLost):
        await stale.append(
            prepared.handle,
            message_id="message-2",
            codec="test.bytes.v1",
            payload=b"stale",
        )
    replay = observer.follow(attached.handle, after=0)
    assert (await anext(replay)).payload == b"committed"
    with pytest.raises(RunProducerFailed):
        await anext(replay)


async def test_real_redis_follower_observes_nonrecoverable_lease_expiry(
    redis_backends: tuple[RedisBackend, RedisBackend, Redis],
) -> None:
    owner, follower, _ = redis_backends
    prepared = await owner.prepare(
        channel="events",
        stream="conversation-1",
        run="run-1",
        codec="test.bytes.v1",
        identity="identity-1",
        after=0,
        cancellable=False,
        recoverable=False,
    )
    await owner.append(
        prepared.handle,
        message_id="message-1",
        codec="test.bytes.v1",
        payload=b"committed",
    )
    replay = follower.follow(prepared.handle, after=0)

    assert (await anext(replay)).payload == b"committed"
    with pytest.raises(RunProducerFailed, match="run-1"):
        await asyncio.wait_for(anext(replay), timeout=2)


async def test_real_redis_messaging_renews_the_owner_lease(
    redis_backends: tuple[RedisBackend, RedisBackend, Redis],
) -> None:
    owner_backend, follower_backend, _ = redis_backends
    release = asyncio.Event()
    source = _Source(
        "before-renewal",
        release=release,
        after_release=("after-renewal",),
    )

    async with (
        Messaging(backend=owner_backend) as owner_messaging,
        Messaging(backend=follower_backend) as follower_messaging,
    ):
        owner_channel = owner_messaging.channel(name="events", codec=_TextCodec())
        follower_channel = follower_messaging.channel(
            name="events",
            codec=_TextCodec(),
        )
        subscription = await owner_channel.wrap(
            source,
            stream="conversation-1",
            run="run-1",
            after=0,
        )
        await asyncio.wait_for(source.started.wait(), timeout=1)
        await asyncio.sleep(1.3)
        release.set()
        await asyncio.wait_for(source.closed.wait(), timeout=1)

        unused = _Source("must-not-run")
        attached = await follower_channel.wrap(
            unused,
            stream="conversation-1",
            run="run-1",
            after=0,
        )

        assert await _data(subscription) == ["before-renewal", "after-renewal"]
        assert await _data(attached) == ["before-renewal", "after-renewal"]
        assert unused.close_calls == 1


async def test_real_redis_recoverable_takeover_uses_checkpoint_and_higher_fence(
    redis_backends: tuple[RedisBackend, RedisBackend, Redis],
) -> None:
    stale, recovering, _ = redis_backends
    identity = hashlib.sha256(
        b"tinkerfin-messaging:attach-identity:v1\0null"
    ).hexdigest()
    original = await stale.prepare(
        channel="events",
        stream="conversation-1",
        run="run-1",
        codec="test.redis-text.v1",
        identity=identity,
        after=0,
        cancellable=False,
        recoverable=True,
    )
    checkpoint = RecoveryCheckpoint(
        position=b"1",
        last_message_id="stable-message-1",
    )
    await stale.append(
        original.handle,
        message_id="stable-message-1",
        codec="test.redis-text.v1",
        payload=b"first",
        checkpoint=checkpoint,
    )
    await asyncio.sleep(0.8)

    recovered = await recovering.prepare(
        channel="events",
        stream="conversation-1",
        run="run-1",
        codec="test.redis-text.v1",
        identity=identity,
        after=0,
        cancellable=False,
        recoverable=True,
    )

    assert recovered.is_owner is True
    assert recovered.recovered is True
    assert recovered.checkpoint == checkpoint
    assert recovered.handle.fence is not None
    assert original.handle.fence is not None
    assert recovered.handle.fence > original.handle.fence
    with pytest.raises(BackendOwnershipLost):
        await stale.append(
            original.handle,
            message_id="stale-message",
            codec="test.redis-text.v1",
            payload=b"stale",
        )


async def test_real_redis_recovery_preserves_an_existing_cancel_request(
    redis_backends: tuple[RedisBackend, RedisBackend, Redis],
) -> None:
    stale, recovering, _ = redis_backends
    await stale.prepare(
        channel="events",
        stream="conversation-1",
        run="run-1",
        codec="test.redis-text.v1",
        identity="identity-1",
        after=0,
        cancellable=True,
        recoverable=True,
    )
    requested = await recovering.request_cancel(
        BackendRunHandle(
            channel="events",
            stream="conversation-1",
            run="run-1",
            owner_token=None,
            fence=None,
        )
    )
    await asyncio.sleep(0.8)

    recovered = await recovering.prepare(
        channel="events",
        stream="conversation-1",
        run="run-1",
        codec="test.redis-text.v1",
        identity="identity-1",
        after=0,
        cancellable=True,
        recoverable=True,
    )

    assert requested is True
    assert recovered.is_owner is True
    assert recovered.recovered is True
    assert await recovering.wait_for_cancel(recovered.handle) is True
    await recovering.finish(recovered.handle, status="cancelled")


async def test_real_redis_does_not_recover_a_claimed_cancel_settlement(
    redis_backends: tuple[RedisBackend, RedisBackend, Redis],
) -> None:
    stale, recovering, _ = redis_backends
    original = await stale.prepare(
        channel="events",
        stream="conversation-1",
        run="run-1",
        codec="test.redis-text.v1",
        identity="identity-1",
        after=0,
        cancellable=True,
        recoverable=True,
    )
    assert await stale.request_cancel(original.handle) is True
    assert await stale.begin_settlement(original.handle) is True
    await asyncio.sleep(0.8)

    attached = await recovering.prepare(
        channel="events",
        stream="conversation-1",
        run="run-1",
        codec="test.redis-text.v1",
        identity="identity-1",
        after=0,
        cancellable=True,
        recoverable=True,
    )
    status = await recovering.wait_finished(attached.handle)
    failure = await recovering.failure(attached.handle)

    assert attached.is_owner is False
    assert status == "owner_lost"
    assert failure is not None
    assert "producer lease expired during cancellation settlement" in str(failure)


async def test_recovered_pending_cancel_survives_source_completion(
    monkeypatch: pytest.MonkeyPatch,
    redis_backends: tuple[RedisBackend, RedisBackend, Redis],
) -> None:
    stale, recovering, _ = redis_backends
    identity = hashlib.sha256(
        b"tinkerfin-messaging:attach-identity:v1\0null"
    ).hexdigest()
    original = await stale.prepare(
        channel="events",
        stream="conversation-1",
        run="run-1",
        codec="test.redis-text.v1",
        identity=identity,
        after=0,
        cancellable=True,
        recoverable=True,
    )
    assert await recovering.request_cancel(original.handle) is True
    await asyncio.sleep(0.8)

    cancel_observed = asyncio.Event()
    release_observer = asyncio.Event()
    original_wait_for_cancel = recovering.wait_for_cancel

    async def delayed_wait_for_cancel(handle: BackendRunHandle) -> bool:
        requested = await original_wait_for_cancel(handle)
        if requested:
            cancel_observed.set()
            await release_observer.wait()
        return requested

    monkeypatch.setattr(recovering, "wait_for_cancel", delayed_wait_for_cancel)
    source_release = asyncio.Event()
    source = _RecoverableSource(release=source_release)
    factory = _FixedRecoveryFactory(source)
    callback_calls = 0
    tail = RecoverableMessage(
        message_id="stable-cancel-terminal",
        data="cancelled-tail",
        checkpoint=RecoveryCheckpoint(
            position=b"cancelled",
            last_message_id="stable-cancel-terminal",
        ),
    )

    def cancel() -> tuple[RecoverableMessage[str], ...]:
        nonlocal callback_calls
        callback_calls += 1
        return (tail,)

    async with Messaging(backend=recovering) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        subscription = await channel.wrap_recoverable(
            factory,
            stream="conversation-1",
            run="run-1",
            after=0,
            cancel=cancel,
        )
        await asyncio.wait_for(source.started.wait(), timeout=1)
        await asyncio.wait_for(cancel_observed.wait(), timeout=1)
        source_release.set()
        status = await asyncio.wait_for(
            recovering.wait_finished(original.handle),
            timeout=1,
        )
        release_observer.set()
        replay = await _data(subscription)

    assert status == "cancelled"
    assert callback_calls == 1
    assert replay == ["cancelled-tail"]
    assert factory.checkpoints == [None]
    assert source.close_calls == 1


async def test_public_cancel_settles_an_ownerless_recoverable_run(
    redis_backends: tuple[RedisBackend, RedisBackend, Redis],
) -> None:
    stale, recovering, _ = redis_backends
    await stale.prepare(
        channel="events",
        stream="conversation-1",
        run="run-1",
        codec="test.redis-text.v1",
        identity="identity-1",
        after=0,
        cancellable=True,
        recoverable=True,
    )
    await asyncio.sleep(0.8)

    async with Messaging(backend=recovering) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        with pytest.raises(RunProducerFailed) as captured:
            await asyncio.wait_for(
                channel.cancel(stream="conversation-1", run="run-1"),
                timeout=1,
            )

    replay = recovering.follow(
        BackendRunHandle(
            channel="events",
            stream="conversation-1",
            run="run-1",
            owner_token=None,
            fence=None,
        ),
        after=0,
    )
    with pytest.raises(RunProducerFailed):
        await anext(replay)

    assert captured.value.cause is not None
    assert "producer lease expired during cancellation" in str(captured.value.cause)


async def test_wrap_recoverable_reopens_from_the_last_real_redis_checkpoint(
    redis_backends: tuple[RedisBackend, RedisBackend, Redis],
) -> None:
    stale, recovering, _ = redis_backends
    identity = hashlib.sha256(
        b"tinkerfin-messaging:attach-identity:v1\0null"
    ).hexdigest()
    original = await stale.prepare(
        channel="events",
        stream="conversation-1",
        run="run-1",
        codec="test.redis-text.v1",
        identity=identity,
        after=0,
        cancellable=False,
        recoverable=True,
    )
    checkpoint = RecoveryCheckpoint(
        position=b"1",
        last_message_id="stable-message-1",
    )
    await stale.append(
        original.handle,
        message_id="stable-message-1",
        codec="test.redis-text.v1",
        payload=b"first",
        checkpoint=checkpoint,
    )
    await asyncio.sleep(0.8)
    factory = _RecoveryFactory()

    async with Messaging(backend=recovering) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        subscription = await channel.wrap_recoverable(
            factory,
            stream="conversation-1",
            run="run-1",
            after=0,
        )

        assert await _data(subscription) == ["first", "second"]

    assert factory.checkpoints == [checkpoint]
    assert factory.sources[0].close_calls == 1


async def test_recoverable_source_resumes_after_owner_process_is_killed(
    redis_backends: tuple[RedisBackend, RedisBackend, Redis],
) -> None:
    _, _, client = redis_backends
    prefix = f"tfmsg:process:{uuid4().hex}"
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_run_owner_until_killed,
        args=(prefix,),
        kwargs={"recoverable": True},
    )
    recovering = RedisBackend(
        client,
        key_prefix=prefix,
        lease_ttl=0.6,
        poll_interval=0.05,
    )
    try:
        await asyncio.to_thread(process.start)
        async with asyncio.timeout(5):
            while (
                await recovering.latest_seq(
                    channel="events",
                    stream="conversation-1",
                )
                != 1
            ):
                if process.exitcode is not None:
                    raise AssertionError(
                        f"recoverable owner exited before its first commit: {process.exitcode}"
                    )
                await asyncio.sleep(0.05)

        process.kill()
        await asyncio.to_thread(process.join, 5)
        assert process.exitcode is not None and process.exitcode != 0
        await asyncio.sleep(0.8)

        factory = _RecoveryFactory()
        async with Messaging(backend=recovering) as messaging:
            channel = messaging.channel(name="events", codec=_TextCodec())
            subscription = await channel.wrap_recoverable(
                factory,
                stream="conversation-1",
                run="run-1",
                after=0,
            )
            messages = [message async for message in subscription]

        assert [message.envelope.seq for message in messages] == [1, 2]
        assert [message.envelope.message_id for message in messages] == [
            "stable-message-1",
            "stable-message-2",
        ]
        assert [message.data for message in messages] == ["first", "second"]
        assert factory.checkpoints == [
            RecoveryCheckpoint(
                position=b"1",
                last_message_id="stable-message-1",
            )
        ]
    finally:
        if process.is_alive():
            process.kill()
        await asyncio.to_thread(process.join, 5)
        await _delete_prefix(client, prefix)


async def test_ordinary_source_is_not_restarted_after_owner_process_is_killed(
    redis_backends: tuple[RedisBackend, RedisBackend, Redis],
) -> None:
    _, _, client = redis_backends
    prefix = f"tfmsg:process:{uuid4().hex}"
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_run_owner_until_killed,
        args=(prefix,),
        kwargs={"recoverable": False},
    )
    observer = RedisBackend(
        client,
        key_prefix=prefix,
        lease_ttl=0.6,
        poll_interval=0.05,
    )
    try:
        await asyncio.to_thread(process.start)
        async with asyncio.timeout(5):
            while (
                await observer.latest_seq(
                    channel="events",
                    stream="conversation-1",
                )
                != 1
            ):
                if process.exitcode is not None:
                    raise AssertionError(
                        f"ordinary owner exited before its first commit: {process.exitcode}"
                    )
                await asyncio.sleep(0.05)

        process.kill()
        await asyncio.to_thread(process.join, 5)
        assert process.exitcode is not None and process.exitcode != 0
        await asyncio.sleep(0.8)

        unused = _Source("must-not-run")
        async with Messaging(backend=observer) as messaging:
            channel = messaging.channel(name="events", codec=_TextCodec())
            subscription = await channel.wrap(
                unused,
                stream="conversation-1",
                run="run-1",
                after=0,
            )
            iterator = aiter(subscription)
            first = await anext(iterator)
            with pytest.raises(RunProducerFailed, match="run-1"):
                await anext(iterator)

        assert first.envelope.seq == 1
        assert first.envelope.message_id == "run-1:1"
        assert first.data == "first"
        assert unused.close_calls == 1
        assert not unused.started.is_set()
    finally:
        if process.is_alive():
            process.kill()
        await asyncio.to_thread(process.join, 5)
        await _delete_prefix(client, prefix)


async def test_slow_recoverable_open_renews_the_real_redis_owner_lease(
    redis_backends: tuple[RedisBackend, RedisBackend, Redis],
) -> None:
    backend, _, _ = redis_backends
    factory = _SlowRecoveryFactory()

    async with Messaging(backend=backend) as messaging:
        channel = messaging.channel(name="events", codec=_TextCodec())
        subscription = await channel.wrap_recoverable(
            factory,
            stream="conversation-1",
            run="run-1",
            after=0,
        )

        assert await asyncio.wait_for(_data(subscription), timeout=2) == [
            "after-slow-open"
        ]

    assert factory.source.close_calls == 1
