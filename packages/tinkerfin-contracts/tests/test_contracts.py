"""Public contract validation and structural observer boundaries."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from tinkerfin_contracts import (
    RUNTIME_OBSERVATION_ADAPTER,
    ContextContributionObservation,
    ModelCallObservation,
    NativeMessageObservation,
    NativeMessageRecord,
    NativeReasoningObservation,
    ObservationBoundary,
    RunIdentity,
    RunInputObservation,
    RunObservationSession,
    RunResumeSummary,
    RunSourceContext,
    RuntimeObservation,
    RuntimeObserver,
    ToolExecutionObservation,
)


def _context() -> RunSourceContext:
    return RunSourceContext(
        identity=RunIdentity(threadId="thread-1", runId="run-1"),
        runtime_profile="deepagents-v2",
        input_kind="ordinary",
        input={"messages": []},
        config={"configurable": {"thread_id": "thread-1"}},
    )


def test_run_identity_is_strict_frozen_and_uses_current_aliases() -> None:
    identity = RunIdentity(threadId="thread-1", runId="run-1")

    assert identity.model_dump(by_alias=True) == {
        "threadId": "thread-1",
        "runId": "run-1",
    }
    with pytest.raises(ValidationError):
        RunIdentity(threadId=" thread-1", runId="run-1")
    with pytest.raises(ValidationError):
        RunIdentity.model_validate(
            {"threadId": "thread-1", "runId": "run-1", "version": 1}
        )
    with pytest.raises(ValidationError):
        identity.thread_id = "replacement"  # type: ignore[misc]
    assert RunIdentity(threadId="t" * 1024, runId="r" * 1024).run_id == "r" * 1024
    with pytest.raises(ValidationError):
        RunIdentity(threadId="t" * 1025, runId="run-1")
    with pytest.raises(ValidationError):
        RunIdentity(threadId="thread-1", runId="r" * 1025)


def test_observation_union_rejects_unknown_kinds_and_version_fields() -> None:
    observation = RunInputObservation(
        identity=_context().identity,
        source=_context(),
        observed_at=datetime.now(UTC),
        monotonic_ns=1,
    )

    restored = RUNTIME_OBSERVATION_ADAPTER.validate_python(
        observation.model_dump(mode="python", by_alias=True)
    )
    assert isinstance(restored, RunInputObservation)
    with pytest.raises(ValidationError):
        RUNTIME_OBSERVATION_ADAPTER.validate_python(
            {
                **observation.model_dump(mode="python", by_alias=True),
                "schemaVersion": 1,
            }
        )
    with pytest.raises(ValidationError):
        RUNTIME_OBSERVATION_ADAPTER.validate_python(
            {
                **observation.model_dump(mode="python", by_alias=True),
                "kind": "native.unknown",
            }
        )


def test_native_message_contract_preserves_structured_public_content() -> None:
    observation = NativeMessageObservation(
        identity=_context().identity,
        namespace=("tools:task-1",),
        message=NativeMessageRecord(
            message_type="assistant_chunk",
            id="message-1",
            content=[{"type": "text", "text": "visible"}],
        ),
        metadata={"langgraph_node": "model"},
        observed_at=datetime.now(UTC),
        monotonic_ns=2,
    )

    assert observation.namespace == ("tools:task-1",)
    assert observation.message.content == [{"type": "text", "text": "visible"}]


def test_reasoning_observation_is_explicit_and_strict() -> None:
    observation = NativeReasoningObservation(
        identity=_context().identity,
        namespace=(),
        message_id="message-1",
        extractor="deepseek.additional_kwargs.reasoning_content",
        content="private reasoning",
        snapshot=False,
        observed_at=datetime.now(UTC),
        monotonic_ns=3,
    )

    restored = RUNTIME_OBSERVATION_ADAPTER.validate_python(
        observation.model_dump(mode="python", by_alias=True)
    )

    assert isinstance(restored, NativeReasoningObservation)
    assert restored.content == "private reasoning"
    with pytest.raises(ValidationError):
        NativeReasoningObservation.model_validate(
            {
                **observation.model_dump(mode="python"),
                "schema_version": 1,
            }
        )


def test_call_failure_origin_is_exclusive_to_failed_phases() -> None:
    now = datetime.now(UTC)
    failures = (
        ModelCallObservation(
            identity=_context().identity,
            phase="failed",
            call_id="model-call",
            output_message_ids=(),
            error_type="builtins.RuntimeError",
            failure_origin=True,
            observed_at=now,
            monotonic_ns=8,
        ),
        ToolExecutionObservation(
            identity=_context().identity,
            phase="failed",
            execution_id="tool-call",
            tool_name="lookup",
            error_type="builtins.RuntimeError",
            failure_origin=True,
            observed_at=now,
            monotonic_ns=9,
        ),
        ContextContributionObservation(
            identity=_context().identity,
            phase="failed",
            contribution_id="retrieval-call",
            context_kind="retrieval",
            name="lookup",
            error_type="builtins.RuntimeError",
            failure_origin=True,
            observed_at=now,
            monotonic_ns=10,
        ),
    )

    for failure in failures:
        restored = RUNTIME_OBSERVATION_ADAPTER.validate_python(
            failure.model_dump(mode="python", by_alias=True)
        )
        assert isinstance(
            restored,
            (
                ModelCallObservation,
                ToolExecutionObservation,
                ContextContributionObservation,
            ),
        )
        assert restored.failure_origin is True
        invalid = failure.model_dump(mode="python")
        invalid["phase"] = "cancelled"
        invalid["error_type"] = None
        if "error_message" in invalid:
            invalid["error_message"] = None
        with pytest.raises(ValidationError, match="own a failure"):
            type(failure).model_validate(invalid)


def test_model_output_message_ids_are_phase_bound_and_canonical() -> None:
    now = datetime.now(UTC)
    first_output = ModelCallObservation(
        identity=_context().identity,
        phase="first_output",
        call_id="model-call",
        output_message_ids=("message-1",),
        observed_at=now,
        monotonic_ns=11,
    )
    completed = ModelCallObservation(
        identity=_context().identity,
        phase="completed",
        call_id="model-call",
        output_message_ids=("message-1", "message-2"),
        observed_at=now,
        monotonic_ns=12,
    )

    assert first_output.output_message_ids == ("message-1",)
    assert completed.output_message_ids == ("message-1", "message-2")
    with pytest.raises(ValidationError, match="first output or completed"):
        ModelCallObservation(
            identity=_context().identity,
            phase="started",
            call_id="model-call",
            messages=(NativeMessageRecord(message_type="human", content="hello"),),
            output_message_ids=("message-1",),
            observed_at=now,
            monotonic_ns=13,
        )
    with pytest.raises(ValidationError, match="canonical"):
        ModelCallObservation(
            identity=_context().identity,
            phase="completed",
            call_id="model-call",
            output_message_ids=(" message-1",),
            observed_at=now,
            monotonic_ns=14,
        )
    with pytest.raises(ValidationError, match="unique"):
        ModelCallObservation(
            identity=_context().identity,
            phase="completed",
            call_id="model-call",
            output_message_ids=("message-1", "message-1"),
            observed_at=now,
            monotonic_ns=15,
        )


def test_resume_and_run_input_contracts_reject_ambiguous_identity() -> None:
    with pytest.raises(ValidationError, match="cannot contain a decision"):
        RunResumeSummary(
            interrupt_id="interrupt-1",
            status="cancelled",
            decision="reject",
        )
    with pytest.raises(ValidationError, match="interrupt IDs must be unique"):
        RunSourceContext(
            identity=_context().identity,
            runtime_profile="deepagents-v2",
            input_kind="resume",
            input={},
            config={},
            resume=(
                RunResumeSummary(interrupt_id="interrupt-1", status="resolved"),
                RunResumeSummary(interrupt_id="interrupt-1", status="resolved"),
            ),
        )
    with pytest.raises(ValidationError, match="identity must match"):
        RunInputObservation(
            identity=RunIdentity(threadId="thread-1", runId="different-run"),
            source=_context(),
            observed_at=datetime.now(UTC),
            monotonic_ns=1,
        )


class _Session:
    async def observe(self, observation: RuntimeObservation) -> None:
        del observation

    async def force(self, boundary: ObservationBoundary) -> None:
        del boundary

    def failure_waiter(self) -> asyncio.Future[BaseException]:
        return asyncio.get_running_loop().create_future()

    async def aclose(self) -> None:
        return None


class _Observer:
    async def open_run(self, context: RunSourceContext) -> _Session:
        del context
        return _Session()


def test_observer_protocols_are_real_runtime_checkable_boundaries() -> None:
    observer = _Observer()
    session = _Session()

    assert isinstance(observer, RuntimeObserver)
    assert isinstance(session, RunObservationSession)
