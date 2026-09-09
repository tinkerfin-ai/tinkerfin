"""Finite JSON preserves the meaning of independently constructed observations."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel, ValidationError

from tinkerfin_contracts import (
    ContextContributionObservation,
    ModelCallObservation,
    NativeInterruptRecord,
    NativeMessageObservation,
    NativeMessageRecord,
    NativeReasoningObservation,
    NativeStateObservation,
    NativeTaskObservation,
    NativeToolCall,
    RunIdentity,
    RunSourceContext,
    ToolExecutionObservation,
)

_IDENTITY = RunIdentity(threadId="finite-thread", runId="finite-run")
_MESSAGE = NativeMessageRecord(message_type="human", content="hello")
_OBSERVED = {
    "identity": _IDENTITY,
    "observed_at": datetime(2026, 9, 5, tzinfo=UTC),
    "monotonic_ns": 1,
    "namespace": (),
}


def _observation(model: type[BaseModel], **values: object) -> BaseModel:
    return model.model_validate({**_OBSERVED, **values})


_CASES = (
    (
        RunSourceContext(
            identity=_IDENTITY,
            runtime_profile="deepagents",
            input_kind="ordinary",
            input={},
            config={},
        ),
        ("input", "config"),
    ),
    (NativeToolCall(id="tool", name="lookup", arguments={}), ("arguments",)),
    (_MESSAGE, ("content", "response_metadata", "usage_metadata")),
    (NativeInterruptRecord(id="interrupt", value={}), ("value",)),
    (
        _observation(
            ModelCallObservation,
            call_id="model",
            phase="started",
            messages=(_MESSAGE,),
        ),
        ("invocation", "options", "usage", "response_metadata"),
    ),
    (
        _observation(
            ToolExecutionObservation,
            execution_id="tool",
            tool_name="lookup",
            phase="started",
            input={},
        ),
        ("input",),
    ),
    (
        _observation(
            ToolExecutionObservation,
            execution_id="tool",
            tool_name="lookup",
            phase="completed",
            output={},
        ),
        ("output",),
    ),
    (
        _observation(
            ContextContributionObservation,
            contribution_id="context",
            context_kind="retrieval",
            name="lookup",
            phase="started",
        ),
        ("input",),
    ),
    (
        _observation(
            ContextContributionObservation,
            contribution_id="context",
            context_kind="retrieval",
            name="lookup",
            phase="completed",
        ),
        ("output",),
    ),
    (
        _observation(NativeMessageObservation, message=_MESSAGE),
        ("metadata",),
    ),
    (
        _observation(
            NativeReasoningObservation,
            message_id="message",
            extractor="provider",
            content="reasoning",
            snapshot=False,
        ),
        ("content",),
    ),
    (
        _observation(
            NativeTaskObservation, task_id="task", name="tools", phase="result"
        ),
        ("input", "result", "metadata"),
    ),
    (_observation(NativeStateObservation, state={}), ("state",)),
)
_FIELDS = [
    pytest.param(model, field, id=f"{type(model).__name__}-{field}")
    for model, fields in _CASES
    for field in fields
]


@pytest.mark.parametrize(("model", "field"), _FIELDS)
@pytest.mark.parametrize("number", (float("nan"), float("inf"), float("-inf")))
def test_non_finite_json_is_rejected_before_serialization(
    model: BaseModel, field: str, number: float
) -> None:
    payload = model.model_dump(mode="python")
    payload[field] = {"nested": [{"measurement": number}]}
    with pytest.raises(ValidationError, match="finite"):
        type(model).model_validate(payload)

    encoded = model.model_dump(mode="json")
    encoded[field] = payload[field]
    with pytest.raises(ValidationError, match="finite"):
        type(model).model_validate_json(json.dumps(encoded))


@pytest.mark.parametrize(("model", "field"), _FIELDS)
def test_finite_numbers_and_null_remain_distinct_after_round_trip(
    model: BaseModel, field: str
) -> None:
    payload = model.model_dump(mode="python")
    expected = {"values": [0, 1.25, -2.5, None, True]}
    payload[field] = expected
    validated = type(model).model_validate(payload)
    restored = type(model).model_validate_json(validated.model_dump_json())
    assert getattr(restored, field) == expected
