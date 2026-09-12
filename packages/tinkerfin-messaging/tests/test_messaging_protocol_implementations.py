"""Built-in instances satisfy the public runtime-checkable protocols."""

from redis.asyncio import Redis

from tinkerfin_messaging import (
    AgUiCodec,
    MemoryBackend,
    MessageCodec,
    NativeStreamPartCodec,
    RedisBackend,
    SseRenderer,
)
from tinkerfin_messaging.backend_contract import MessagingBackend


async def test_messaging_builtin_implementations_declare_their_protocols() -> None:
    # Construction opens no connection. Runtime checks may read settings properties,
    # which belong to initialized instances just like ordinary public access.
    client = Redis()
    try:
        implementations = {
            MemoryBackend(): (MessagingBackend,),
            RedisBackend(client): (MessagingBackend,),
            AgUiCodec(): (MessageCodec, SseRenderer),
            NativeStreamPartCodec(): (MessageCodec, SseRenderer),
        }

        for implementation, protocols in implementations.items():
            assert all(isinstance(implementation, protocol) for protocol in protocols)
    finally:
        await client.aclose()


def test_builtin_backends_do_not_expose_framework_lifecycle_operations() -> None:
    removed_backend_members = {
        "append",
        "begin_settlement",
        "bind_follow",
        "delete_stream",
        "failure",
        "finish",
        "follow",
        "get_run_status",
        "latest_seq",
        "prepare",
        "read",
        "renew",
        "request_cancel",
        "wait_finished",
        "wait_for_cancel",
        "limits",
        "retention_policy",
    }

    for backend_type in (MemoryBackend, RedisBackend):
        assert not removed_backend_members.intersection(vars(backend_type))
