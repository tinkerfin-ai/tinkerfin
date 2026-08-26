"""Public contracts for stable identity-free AG-UI resume bindings."""

from __future__ import annotations

import pytest
from ag_ui.core.types import Interrupt, ResumeEntry
from pydantic import ValidationError

from tinkerfin import AgUiResumeBinding, AgUiResumeBindingError, Identity
from tinkerfin_agui_adapter import ResumeMappingError, ResumeTranslation, ScopedIdCodec


def _identity(*, run_id: str = "run-resume") -> Identity:
    return Identity(threadId="thread-1", runId=run_id)


def _interrupts() -> tuple[Interrupt, ...]:
    native_value = {
        "action_requests": [
            {"name": "write_file", "args": {"path": "a"}},
            {"name": "write_file", "args": {"path": "b"}},
        ],
        "review_configs": [
            {"action_name": "write_file", "allowed_decisions": ["approve"]},
            {"action_name": "write_file", "allowed_decisions": ["approve"]},
        ],
    }
    codec = ScopedIdCodec()
    return tuple(
        Interrupt(
            id=f"interrupt-1#{index}",
            reason="tool_call",
            tool_call_id=codec.encode("tool", (), f"call-{index}"),
            metadata={
                "langgraphValue": native_value,
                "deepagents": {
                    "schema": "tinkerfin.deepagents.tool-review.v1",
                    "nativeInterruptId": "interrupt-1",
                    "actionIndex": index,
                    "toolName": "write_file",
                    "allowedDecisions": ["approve"],
                    "originalArgs": {"path": "a" if index == 0 else "b"},
                },
            },
        )
        for index in range(2)
    )


def _entry(
    interrupt_id: str,
    *,
    status: str = "resolved",
) -> ResumeEntry:
    return ResumeEntry.model_validate(
        {
            "interruptId": interrupt_id,
            "status": status,
            **({"payload": {"type": "approve"}} if status == "resolved" else {}),
        }
    )


def _resolved_binding() -> AgUiResumeBinding:
    return AgUiResumeBinding.from_agui(
        entries=(_entry("interrupt-1#0"), _entry("interrupt-1#1")),
        interrupts=_interrupts(),
    )


def test_from_agui_builds_one_identity_free_resume_binding() -> None:
    binding = _resolved_binding()

    assert binding.mode == "resume"
    assert binding.resume_data == {
        "decisions": [{"type": "approve"}, {"type": "approve"}]
    }
    assert binding.native_interrupt_ids == ("interrupt-1",)
    assert binding.prior_tool_call_ids == (
        ScopedIdCodec().encode("tool", (), "call-0"),
        ScopedIdCodec().encode("tool", (), "call-1"),
    )
    assert binding.contains_cancellations is False
    assert not hasattr(binding, "identity")
    assert not hasattr(binding, "command")


def test_from_agui_converts_adapter_failures_to_the_core_error_family() -> None:
    with pytest.raises(AgUiResumeBindingError) as raised:
        AgUiResumeBinding.from_agui(
            entries=(_entry("unknown"),),
            interrupts=_interrupts(),
        )

    assert isinstance(raised.value.cause, ResumeMappingError)
    adapter_code = raised.value.context["adapter_code"]
    assert isinstance(adapter_code, str)
    assert adapter_code.startswith("agui.resume.")


def test_binding_has_a_strict_stable_json_round_trip() -> None:
    binding = _resolved_binding()
    payload = binding.model_dump(mode="json", by_alias=True, exclude_none=True)

    assert payload == {
        "schemaVersion": 1,
        "mode": "resume",
        "resumeData": {"decisions": [{"type": "approve"}, {"type": "approve"}]},
        "nativeInterruptIds": ["interrupt-1"],
        "priorToolCallIds": [
            ScopedIdCodec().encode("tool", (), "call-0"),
            ScopedIdCodec().encode("tool", (), "call-1"),
        ],
        "sourceAgentNames": [],
        "unidentifiedExternalSource": False,
    }
    assert AgUiResumeBinding.model_validate(payload) == binding


def test_binding_does_not_expose_mutable_native_resume_data() -> None:
    binding = _resolved_binding()
    exposed = binding.resume_data
    assert isinstance(exposed, dict)
    exposed["decisions"] = []

    assert binding.resume_data == {
        "decisions": [{"type": "approve"}, {"type": "approve"}]
    }


def test_mixed_resume_preserves_cancelled_slots_without_fabricating_reject() -> None:
    binding = AgUiResumeBinding.from_agui(
        entries=(
            _entry("interrupt-1#1", status="cancelled"),
            _entry("interrupt-1#0"),
        ),
        interrupts=_interrupts(),
    )

    assert binding.mode == "resume"
    assert binding.contains_cancellations is True
    assert binding.resume_data == {
        "decisions": [
            {"type": "approve"},
            {"type": "tinkerfin_cancel"},
        ]
    }


def test_all_cancelled_resume_is_a_round_trippable_abandonment() -> None:
    binding = AgUiResumeBinding.from_agui(
        entries=(
            _entry("interrupt-1#0", status="cancelled"),
            _entry("interrupt-1#1", status="cancelled"),
        ),
        interrupts=_interrupts(),
    )
    payload = binding.model_dump(mode="json", by_alias=True, exclude_none=True)

    assert binding.mode == "abandon"
    assert binding.contains_cancellations is True
    assert binding.resume_data is None
    assert "resumeData" not in payload
    assert AgUiResumeBinding.model_validate(payload) == binding


def test_binding_rejects_mixed_generic_runtime_cancellation() -> None:
    translation = ResumeTranslation(
        mode="custom",
        kind="runtime",
        resume_data=None,
        cancelled_interrupt_ids=("runtime-2",),
        decisions_by_interrupt={
            "runtime-1": ({"answer": "yes"},),
            "runtime-2": (None,),
        },
    )

    with pytest.raises(ValueError, match="runtime interrupts"):
        AgUiResumeBinding._from_translation(translation)


def test_binding_rejects_invalid_persisted_shapes() -> None:
    with pytest.raises(ValidationError, match="requires resumeData"):
        AgUiResumeBinding.model_validate(
            {
                "schemaVersion": 1,
                "mode": "resume",
                "nativeInterruptIds": ["interrupt-1"],
            }
        )
    with pytest.raises(ValidationError, match="cannot include resumeData"):
        AgUiResumeBinding.model_validate(
            {
                "schemaVersion": 1,
                "mode": "abandon",
                "resumeData": {},
                "nativeInterruptIds": ["interrupt-1"],
            }
        )
    with pytest.raises(ValidationError, match="complete scoped Tool IDs"):
        AgUiResumeBinding.model_validate(
            {
                "schemaVersion": 1,
                "mode": "resume",
                "resumeData": {"decisions": []},
                "nativeInterruptIds": ["interrupt-1"],
                "priorToolCallIds": ["call-1"],
            }
        )


def test_marker_combines_binding_with_identity_parent_and_tool_correlation() -> None:
    identity = _identity()
    first = _resolved_binding()
    second = first.model_copy(
        update={
            "prior_tool_call_ids": (
                ScopedIdCodec().encode("tool", (), "different-call"),
            )
        }
    )

    first_checkpoint = first._checkpoint(
        identity=identity,
        parent_run_id="run-parent",
    )
    second_checkpoint = second._checkpoint(
        identity=identity,
        parent_run_id="run-parent",
    )

    assert first_checkpoint.identity is identity
    assert first_checkpoint.parent_run_id == "run-parent"
    assert first_checkpoint.marker_id != second_checkpoint.marker_id
    assert (
        first_checkpoint.marker_id
        != first._checkpoint(
            identity=_identity(run_id="run-other"),
            parent_run_id="run-parent",
        ).marker_id
    )
