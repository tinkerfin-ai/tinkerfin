"""Strict semantic fact contracts accepted by custom Trace Store implementations."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from tinkerfin_contracts import RunIdentity
from tinkerfin_tracing import (
    AgentStepFact,
    CanonicalTracePayloadCodec,
    CapturedValue,
    ContextContributionFact,
    InteractionFact,
    ModelCallFact,
    RunFact,
    RuntimeTaskFact,
    ToolExecutionFact,
    ToolFact,
)


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


def test_failure_origin_serialization_is_stable_for_all_failure_fact_kinds() -> None:
    codec = CanonicalTracePayloadCodec()
    facts = (
        RunFact.model_validate(
            {
                **_common(),
                "phase": "terminal",
                "outcome": "failed",
                "errorType": "builtins.RuntimeError",
            }
        ),
        ToolFact.model_validate(
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
                "resultStatus": "error",
            }
        ),
        RuntimeTaskFact.model_validate(
            {
                **_common(),
                "phase": "failed",
                "taskId": "task:1",
                "sourceTaskId": "1",
                "taskName": "tools",
                "errorType": "builtins.RuntimeError",
            }
        ),
        AgentStepFact.model_validate(
            {
                **_common(),
                "phase": "failed",
                "callId": "call:1",
                "stepKind": "agent",
                "name": "agent",
                "errorType": "builtins.RuntimeError",
            }
        ),
        ModelCallFact.model_validate(
            {
                **_common(),
                "phase": "failed",
                "callId": "model:1",
                "errorType": "builtins.RuntimeError",
            }
        ),
        ToolExecutionFact.model_validate(
            {
                **_common(),
                "phase": "failed",
                "executionId": "tool-execution:1",
                "toolName": "search",
                "errorType": "builtins.RuntimeError",
            }
        ),
        ContextContributionFact.model_validate(
            {
                **_common(),
                "phase": "failed",
                "contributionId": "context:1",
                "contextKind": "retrieval",
                "name": "knowledge-base",
                "errorType": "builtins.RuntimeError",
            }
        ),
    )

    for fact in facts:
        origin = fact.model_copy(update={"failure_origin": True})
        fallback_payload = codec.encode_fact(fact).data
        origin_payload = codec.encode_fact(origin).data

        if isinstance(fact, AgentStepFact):
            assert b'"failureOrigin":false' in fallback_payload
        else:
            assert b'"failureOrigin"' not in fallback_payload
        assert b'"failureOrigin":true' in origin_payload
        assert codec.decode_fact(fallback_payload) == fact
        assert codec.decode_fact(origin_payload) == origin


def test_failure_origin_requires_a_failed_terminal_run() -> None:
    with pytest.raises(ValidationError, match="failed terminal"):
        RunFact.model_validate(
            {
                **_common(),
                "phase": "closed",
                "outcome": "failed",
                "errorType": "builtins.RuntimeError",
                "failureOrigin": True,
            }
        )
