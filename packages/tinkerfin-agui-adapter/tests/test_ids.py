from __future__ import annotations

import pytest

from tinkerfin_agui_adapter.ids import ScopedIdCodec


def test_scoped_id_codec_is_collision_safe_and_reversible() -> None:
    codec = ScopedIdCodec()

    root_message = codec.encode("message", (), "shared:id/值")
    child_message = codec.encode(
        "message",
        ("tools:graph-a", "nested/value"),
        "shared:id/值",
    )

    assert root_message != child_message
    assert codec.decode(root_message) == (
        "message",
        (),
        "shared:id/值",
    )
    assert codec.decode(child_message) == (
        "message",
        ("tools:graph-a", "nested/value"),
        "shared:id/值",
    )


def test_scoped_id_codec_rejects_the_removed_versioned_payload_shape() -> None:
    with pytest.raises(ValueError, match="invalid scoped ID"):
        ScopedIdCodec().decode("tf:tool:WzEsWyJ0b29sczpwYXJlbnQiXSwiY2FsbC10YXNrIl0")
