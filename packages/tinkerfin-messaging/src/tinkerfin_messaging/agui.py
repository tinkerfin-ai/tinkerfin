"""Optional AG-UI event codec and native SSE renderer."""

from __future__ import annotations

from typing import ClassVar

from ag_ui.core import BaseEvent, Event
from pydantic import TypeAdapter

from .protocols import MessageCodec, SseRenderer

_EVENT_ADAPTER: TypeAdapter[Event] = TypeAdapter(Event)


class AgUiCodec(
    MessageCodec[BaseEvent, BaseEvent],
    SseRenderer[BaseEvent],
):
    """Persist complete AG-UI events under a schema-stable codec identifier."""

    codec_id: ClassVar[str] = "agui.event.v1"
    messaging_source_type: ClassVar[type[BaseEvent]] = BaseEvent
    messaging_replay_type: ClassVar[type[BaseEvent]] = BaseEvent

    def encode(self, item: BaseEvent) -> bytes:
        """Validate and encode one event with protocol field aliases."""

        event = _EVENT_ADAPTER.validate_python(item)
        return event.model_dump_json(
            by_alias=True,
            exclude_none=True,
        ).encode()

    def decode(self, payload: bytes) -> BaseEvent:
        """Decode and validate one complete discriminated AG-UI event."""

        return _EVENT_ADAPTER.validate_json(payload)

    def render(self, *, seq: int, payload: BaseEvent) -> bytes:
        """Render the durable sequence and native AG-UI JSON as one SSE frame."""

        encoded = self.encode(payload)
        return b"id: " + str(seq).encode() + b"\ndata: " + encoded + b"\n\n"
