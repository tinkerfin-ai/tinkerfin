from __future__ import annotations

import inspect
import re
from collections.abc import AsyncIterable, AsyncIterator
from typing import get_type_hints

from ag_ui.core import BaseEvent, RunAgentInput

import tinkerfin_agui_adapter
from tinkerfin_agui_adapter import (
    DeepAgentAgUiAdapter,
    ResumeMapper,
    ResumeMappingError,
    astream_events,
)

_PUBLIC_EXPORTS = {
    "AgUiLifecycleEventFactory",
    "AgentRunOutcome",
    "AgentRuntimeInterrupt",
    "DeepAgentAgUiAdapter",
    "HitlActionRequest",
    "HitlCorrelationError",
    "HitlRequest",
    "HitlReviewConfig",
    "InterruptCorrelationError",
    "ResumeMapper",
    "ResumeMappingError",
    "ResumeMappingFailure",
    "ResumeTranslation",
    "ScopedIdCodec",
    "SseEventId",
    "astream_events",
    "encode_sse",
    "micro_batch",
}


def test_high_level_stream_has_the_locked_public_contract() -> None:
    signature = inspect.signature(astream_events)
    hints = get_type_hints(astream_events)

    assert list(signature.parameters) == [
        "parts",
        "run_input",
        "expose_reasoning_events",
        "expose_subagent_events",
        "prior_tool_call_ids",
    ]
    assert signature.parameters["parts"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for name, parameter in signature.parameters.items()
        if name != "parts"
    )
    assert hints["parts"] == AsyncIterable[object]
    assert hints["run_input"] is RunAgentInput
    assert hints["expose_reasoning_events"] is bool
    assert signature.parameters["expose_reasoning_events"].default is False
    assert hints["expose_subagent_events"] is bool
    assert signature.parameters["expose_subagent_events"].default is True
    assert hints["prior_tool_call_ids"] == frozenset[str]
    assert signature.parameters["prior_tool_call_ids"].default == frozenset()
    assert hints["return"] == AsyncIterator[BaseEvent]

    process_hints = get_type_hints(DeepAgentAgUiAdapter.process)
    assert process_hints["part"] is object
    assert process_hints["return"] == list[BaseEvent]


def test_adapter_exports_exactly_the_documented_public_surface() -> None:
    assert set(tinkerfin_agui_adapter.__all__) == _PUBLIC_EXPORTS
    assert len(tinkerfin_agui_adapter.__all__) == len(_PUBLIC_EXPORTS)
    assert all(hasattr(tinkerfin_agui_adapter, name) for name in _PUBLIC_EXPORTS)


def test_public_adapter_schema_and_resume_errors_are_english() -> None:
    schema_text = str(
        {
            name: getattr(tinkerfin_agui_adapter, name).model_json_schema()
            for name in (
                "AgentRunOutcome",
                "AgentRuntimeInterrupt",
                "HitlActionRequest",
                "HitlRequest",
                "HitlReviewConfig",
            )
        }
    )
    assert re.search(r"[\u4e00-\u9fff]", schema_text) is None

    try:
        ResumeMapper().map(entries=(), interrupts=())
    except ResumeMappingError as error:
        assert re.search(r"[\u4e00-\u9fff]", str(error)) is None
    else:
        raise AssertionError("empty pending interrupts must fail")
