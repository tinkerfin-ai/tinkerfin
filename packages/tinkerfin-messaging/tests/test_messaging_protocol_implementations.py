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
