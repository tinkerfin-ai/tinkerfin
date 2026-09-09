from __future__ import annotations

import pytest
from ag_ui.core import RunStartedEvent
from ag_ui.encoder import EventEncoder

from tinkerfin_agui_adapter import encode_sse


@pytest.mark.parametrize("event_id", [None, 0, 17, "cursor-线程"])
def test_encode_sse_matches_the_locked_ag_ui_json_contract(
    event_id: str | int | None,
) -> None:
    event = RunStartedEvent(
        thread_id="线程-1",
        run_id="运行-1",
        parent_run_id=None,
    )
    expected = EventEncoder().encode(event)
    if event_id is not None:
        expected = f"id: {event_id}\n{expected}"

    assert encode_sse(event, event_id=event_id) == expected


def test_encode_sse_rejects_boolean_event_ids() -> None:
    event = RunStartedEvent(thread_id="thread-1", run_id="run-1")

    with pytest.raises(TypeError, match="string, integer, or None"):
        encode_sse(event, event_id=True)


@pytest.mark.parametrize("event_id", ["line\rbreak", "line\nbreak", "nul\0byte"])
def test_encode_sse_rejects_event_ids_that_change_framing(event_id: str) -> None:
    event = RunStartedEvent(thread_id="thread-1", run_id="run-1")

    with pytest.raises(ValueError, match="CR, LF, or NUL"):
        encode_sse(event, event_id=event_id)
