"""Public AG-UI adapter error-family contracts."""

from __future__ import annotations

import pytest

from tinkerfin_agui_adapter import (
    AgUiAdapterError,
    AgUiAdapterErrorCode,
    AgUiStreamContractError,
    HitlCorrelationError,
    ResumeMappingError,
)


def test_error_codes_are_unique_and_namespaced() -> None:
    values = [code.value for code in AgUiAdapterErrorCode]

    assert len(values) == len(set(values))
    assert all(value.startswith("agui.") for value in values)


def test_stream_contract_error_preserves_safe_context_and_cause() -> None:
    cause = ValueError("native detail")
    context = {"mode": "messages"}
    diagnostic_context = {"stage": "validation"}
    error = AgUiStreamContractError(
        "invalid messages part",
        context=context,
        diagnostic_context=diagnostic_context,
        cause=cause,
    )
    context["mode"] = "mutated"
    diagnostic_context["stage"] = "mutated"

    assert isinstance(error, AgUiAdapterError)
    assert isinstance(error, ValueError)
    assert error.code is AgUiAdapterErrorCode.STREAM_CONTRACT_INVALID
    assert error.cause is cause
    assert error.__cause__ is cause
    assert str(error) == "invalid messages part"
    assert dict(error.context) == {"mode": "messages"}
    assert dict(error.diagnostic_context) == {"stage": "validation"}
    assert not hasattr(error.context, "__setitem__")
    assert not hasattr(error.diagnostic_context, "__setitem__")


def test_resume_code_must_belong_to_resume_family() -> None:
    error = ResumeMappingError(
        AgUiAdapterErrorCode.RESUME_INCOMPLETE,
        "resume is incomplete",
    )

    assert error.code is AgUiAdapterErrorCode.RESUME_INCOMPLETE
    assert isinstance(error, ValueError)
    assert issubclass(HitlCorrelationError, AgUiAdapterError)
    with pytest.raises(ValueError, match="resume failure"):
        ResumeMappingError(
            AgUiAdapterErrorCode.CONVERSION_FAILED,
            "wrong category",
        )
