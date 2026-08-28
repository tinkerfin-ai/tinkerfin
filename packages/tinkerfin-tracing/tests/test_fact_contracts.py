"""Strict semantic fact contracts accepted by custom Trace Store implementations."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from tinkerfin_contracts import RunIdentity
from tinkerfin_tracing import CapturedValue, InteractionFact, RunFact, ToolFact


def _common() -> dict[str, object]:
    return {
        "sourceObservationId": "observation-1",
        "identity": RunIdentity(threadId="thread-1", runId="run-1"),
        "occurredAt": datetime.now(UTC),
        "monotonicNs": 1,
    }


def test_run_fact_requires_phase_specific_lifecycle_evidence() -> None:
    with pytest.raises(ValidationError, match="require input_kind"):
        RunFact.model_validate({**_common(), "phase": "started"})
    with pytest.raises(ValidationError, match="require outcome"):
        RunFact.model_validate({**_common(), "phase": "terminal"})
    with pytest.raises(ValidationError, match="require interrupt_ids"):
        RunFact.model_validate({**_common(), "phase": "resume_checkpointed"})


def test_tool_result_and_interaction_phase_cannot_be_ambiguous() -> None:
    with pytest.raises(ValidationError, match="content and result_status"):
        ToolFact.model_validate(
            {
                **_common(),
                "phase": "result",
                "toolCallId": "tool:call-1",
                "sourceToolCallId": "call-1",
                "toolName": "search",
            }
        )
    with pytest.raises(ValidationError, match="must be pending"):
        InteractionFact.model_validate(
            {
                **_common(),
                "phase": "opened",
                "interactionId": "interaction:1",
                "sourceInteractionId": "1",
                "interactionKind": "approval",
                "status": "resolved",
            }
        )

    result = ToolFact.model_validate(
        {
            **_common(),
            "phase": "result",
            "toolCallId": "tool:call-1",
            "sourceToolCallId": "call-1",
            "toolName": "search",
            "content": CapturedValue(
                disposition="inline",
                safe_size_bytes=4,
                value=None,
            ),
            "resultStatus": "success",
        }
    )
    assert result.content is not None
    assert result.content.value is None
