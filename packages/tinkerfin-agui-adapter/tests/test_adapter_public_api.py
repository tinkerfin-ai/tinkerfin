from __future__ import annotations

import inspect
import re
from collections.abc import AsyncIterable, AsyncIterator, Callable
from typing import get_type_hints

import pytest
from ag_ui.core import BaseEvent

import tinkerfin_agui_adapter
from tinkerfin_agui_adapter import (
    DeepAgentAgUiAdapter,
    Identity,
    ResumeMapper,
    ResumeMappingError,
    astream_events,
)

_PUBLIC_EXPORTS = {
    "AgUiAdapterError",
    "AgUiAdapterErrorCode",
    "AgUiConversionError",
    "AgUiLifecycleError",
    "AgUiLifecycleEventFactory",
    "AgUiSerializationError",
    "AgUiStreamContractError",
    "AgentRunOutcome",
    "AgentRuntimeInterrupt",
    "DeepAgentAgUiAdapter",
    "HitlActionRequest",
    "HitlCorrelationError",
    "HitlNoMatchError",
    "HitlRequest",
    "HitlReviewConfig",
    "Identity",
    "ResumeMapper",
    "ResumeMappingError",
    "ResumeTranslation",
    "RuntimeInterruptEnvelope",
    "SUBAGENT_PROVENANCE_SCHEMA",
    "ScopedIdCodec",
    "SseEventId",
    "SubagentProvenance",
    "TOOL_REVIEW_SCHEMA",
    "ToolReviewContractError",
    "ToolReviewDecision",
    "ToolReviewInterruptMetadata",
    "astream_events",
    "create_subagent_provenance",
    "encode_sse",
    "micro_batch",
    "parse_tool_review_interrupt",
    "subagent_invocation_id",
}


def test_high_level_stream_has_the_locked_public_contract() -> None:
    signature = inspect.signature(astream_events)
    hints = get_type_hints(astream_events)

    assert list(signature.parameters) == [
        "parts",
        "identity",
        "expose_reasoning_events",
        "expose_subagent_events",
        "prior_tool_call_ids",
        "private_state_keys",
    ]
    assert signature.parameters["parts"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for name, parameter in signature.parameters.items()
        if name != "parts"
    )
    assert hints["parts"] == AsyncIterable[object]
    assert hints["identity"] is Identity
    assert hints["expose_reasoning_events"] is bool
    assert signature.parameters["expose_reasoning_events"].default is False
    assert hints["expose_subagent_events"] is bool
    assert signature.parameters["expose_subagent_events"].default is True
    assert hints["prior_tool_call_ids"] == frozenset[str]
    assert signature.parameters["prior_tool_call_ids"].default == frozenset()
    assert hints["private_state_keys"] == frozenset[str]
    assert signature.parameters["private_state_keys"].default == frozenset()
    assert hints["return"] == AsyncIterator[BaseEvent]

    process_hints = get_type_hints(DeepAgentAgUiAdapter.process)
    assert process_hints["part"] is object
    assert process_hints["return"] == list[BaseEvent]

    adapter_signature = inspect.signature(DeepAgentAgUiAdapter)
    adapter_hints = get_type_hints(DeepAgentAgUiAdapter.__init__)
    assert list(adapter_signature.parameters) == [
        "identity",
        "prior_tool_call_ids",
        "expose_reasoning_events",
        "expose_subagent_events",
        "private_state_keys",
    ]
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in adapter_signature.parameters.values()
    )
    assert adapter_hints["identity"] is Identity
    assert adapter_hints["private_state_keys"] == frozenset[str]
    assert adapter_signature.parameters["private_state_keys"].default == frozenset()
    assert adapter_signature.parameters["identity"].default is inspect.Parameter.empty


def test_adapter_exports_exactly_the_documented_public_surface() -> None:
    assert set(tinkerfin_agui_adapter.__all__) == _PUBLIC_EXPORTS
    assert len(tinkerfin_agui_adapter.__all__) == len(_PUBLIC_EXPORTS)
    assert all(hasattr(tinkerfin_agui_adapter, name) for name in _PUBLIC_EXPORTS)


def test_private_state_policy_requires_an_immutable_canonical_key_set() -> None:
    constructor: Callable[..., DeepAgentAgUiAdapter] = DeepAgentAgUiAdapter
    with pytest.raises(TypeError, match="frozenset"):
        constructor(
            identity=Identity(threadId="thread-1", runId="run-1"),
            private_state_keys={"private"},  # pyright: ignore[reportArgumentType]
        )
    for value in ("", " private"):
        with pytest.raises(ValueError, match="canonical"):
            DeepAgentAgUiAdapter(
                identity=Identity(threadId="thread-1", runId="run-1"),
                private_state_keys=frozenset({value}),
            )


def test_public_adapter_schema_and_resume_errors_are_english() -> None:
    schema_text = str(
        {
            name: getattr(tinkerfin_agui_adapter, name).model_json_schema()
            for name in (
                "AgentRunOutcome",
                "Identity",
                "AgentRuntimeInterrupt",
                "RuntimeInterruptEnvelope",
                "SubagentProvenance",
                "ToolReviewInterruptMetadata",
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
