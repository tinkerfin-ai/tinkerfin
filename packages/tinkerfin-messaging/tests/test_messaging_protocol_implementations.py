"""Nominal implementation links used by IDE protocol navigation."""

from tinkerfin_messaging import (
    AgUiCodec,
    MemoryBackend,
    MessageCodec,
    MessagingBackend,
    NativeStreamPartCodec,
    RedisBackend,
    SseRenderer,
)


def test_messaging_builtin_implementations_declare_their_protocols() -> None:
    implementations = {
        MemoryBackend(): (MessagingBackend,),
        object.__new__(RedisBackend): (MessagingBackend,),
        AgUiCodec(): (MessageCodec, SseRenderer),
        NativeStreamPartCodec(): (MessageCodec, SseRenderer),
    }

    for implementation, protocols in implementations.items():
        assert all(isinstance(implementation, protocol) for protocol in protocols)


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
