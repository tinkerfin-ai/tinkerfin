"""Target public contract for the simplified AG-UI Runtime boundary."""

from __future__ import annotations

import inspect

from pydantic import BaseModel

import tinkerfin
import tinkerfin_agui_adapter
from tinkerfin import (
    AgUiResumeBinding,
    AgUiResumeCheckpoint,
    DeepAgentAgUiResumeRuntime,
    DeepAgentDefinition,
)


def test_target_public_surface_removes_context_and_exposes_checkpoint_semantics() -> (
    None
):
    """Expose one canonical identity and checkpoint-specific resume types."""

    assert "AgUiRunContext" not in tinkerfin_agui_adapter.__all__
    assert not hasattr(tinkerfin_agui_adapter, "AgUiRunContext")
    assert "AgUiResumeSettlement" not in tinkerfin.__all__
    assert "AgUiResumeSettlementObserver" not in tinkerfin.__all__
    assert "AgUiResumeCheckpoint" in tinkerfin.__all__
    assert "AgUiResumeCheckpointObserver" in tinkerfin.__all__
    assert "AgUiResumeInitializationFailureObserver" in tinkerfin.__all__
    assert "DeepAgentAgUiResumeRuntime" in tinkerfin.__all__
    assert AgUiResumeCheckpoint is not None
    assert DeepAgentAgUiResumeRuntime is not None


def test_target_new_agui_signature_has_no_protocol_input_or_settlement_callback() -> (
    None
):
    """Keep transport DTOs and ambiguous settlement wording out of the Runtime API."""

    parameters = inspect.signature(DeepAgentDefinition.new_agui).parameters

    assert "identity" in parameters
    assert "parent_run_id" in parameters
    assert "resume" in parameters
    assert "on_resume_checkpointed" in parameters
    assert "on_resume_initialization_failed" in parameters
    assert "run_input" not in parameters
    assert "on_resume_settled" not in parameters


def test_resume_binding_owns_a_strict_stable_persistence_round_trip() -> None:
    """Round-trip resume facts without identity, parent, or a public Command."""

    assert issubclass(AgUiResumeBinding, BaseModel)
    binding = AgUiResumeBinding.model_validate(
        {
            "mode": "resume",
            "resumeData": {"decisions": [{"type": "approve"}]},
            "nativeInterruptIds": ["interrupt-1"],
            "priorToolCallIds": [],
            "sourceAgentNames": [],
            "unidentifiedExternalSource": False,
        }
    )
    persisted = binding.model_dump(mode="json", by_alias=True, exclude_none=True)

    assert AgUiResumeBinding.model_validate(persisted) == binding
    assert not hasattr(binding, "identity")
    assert not hasattr(binding, "command")
    assert binding.contains_cancellations is False


def test_resume_binding_round_trips_all_cancelled_abandonment() -> None:
    """Represent all-cancelled input without a fabricated native rejection."""

    binding = AgUiResumeBinding.model_validate(
        {
            "mode": "abandon",
            "nativeInterruptIds": ["interrupt-1"],
            "priorToolCallIds": [],
            "sourceAgentNames": [],
            "unidentifiedExternalSource": False,
        }
    )

    assert binding.mode == "abandon"
    assert binding.contains_cancellations is True
    assert "resumeData" not in binding.model_dump(
        mode="json", by_alias=True, exclude_none=True
    )
