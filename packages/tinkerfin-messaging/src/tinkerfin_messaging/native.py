"""Optional durable codec for the canonical TinkerFin native stream schema."""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar

from tinkerfin import NativeStreamPart as NativeStreamPart
from tinkerfin.native import normalize_native_stream_part

from .protocols import MessageCodec, SseRenderer


class NativeStreamPartCodec(
    MessageCodec[Mapping[str, object], NativeStreamPart],
    SseRenderer[NativeStreamPart],
):
    """Persist verified LangGraph v2 envelopes using TinkerFin's native schema."""

    codec_id: ClassVar[str] = "langgraph.stream-part.v2.v1"
    messaging_source_type: ClassVar[type[Mapping[str, object]]] = Mapping
    messaging_replay_type: ClassVar[type[NativeStreamPart]] = NativeStreamPart

    def encode(self, item: Mapping[str, object]) -> bytes:
        """Validate a live envelope and encode its canonical finite form."""

        return (
            normalize_native_stream_part(item).model_dump_json(by_alias=True).encode()
        )

    def decode(self, payload: bytes) -> NativeStreamPart:
        """Validate one persisted native stream representation."""

        return NativeStreamPart.model_validate_json(payload)

    def render(self, *, seq: int, payload: NativeStreamPart) -> bytes:
        """Render one replay part as a typed SSE event."""

        encoded = payload.model_dump_json(by_alias=True).encode()
        return (
            b"id: "
            + str(seq).encode()
            + b"\nevent: stream-part\ndata: "
            + encoded
            + b"\n\n"
        )
