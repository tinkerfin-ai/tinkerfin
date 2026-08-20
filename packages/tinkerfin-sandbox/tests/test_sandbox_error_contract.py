"""Public OpenSandbox error-family contracts."""

from __future__ import annotations

from tinkerfin_sandbox import (
    OpenSandboxBackendTimeoutError,
    OpenSandboxError,
    OpenSandboxErrorCode,
    OpenSandboxSettlementTimeoutError,
    OpenSandboxStateUnavailableError,
)


def test_error_codes_are_unique_and_namespaced() -> None:
    values = [code.value for code in OpenSandboxErrorCode]

    assert len(values) == len(set(values))
    assert all(value.startswith("sandbox.") for value in values)


def test_error_contexts_are_separated_read_only_and_copied() -> None:
    cause = ConnectionError("database internals")
    context = {"retryable": True}
    diagnostic_context = {
        "implementation": "sqlalchemy",
        "dialect": "mysql",
        "operation": "read_binding",
    }
    error = OpenSandboxStateUnavailableError(
        "State is unavailable",
        context=context,
        diagnostic_context=diagnostic_context,
        cause=cause,
    )
    context["retryable"] = False
    diagnostic_context["operation"] = "mutated"

    assert isinstance(error, OpenSandboxError)
    assert error.code is OpenSandboxErrorCode.STATE_UNAVAILABLE
    assert error.cause is cause
    assert error.__cause__ is cause
    assert str(error) == "State is unavailable"
    assert dict(error.context) == {"retryable": True}
    assert dict(error.diagnostic_context) == {
        "implementation": "sqlalchemy",
        "dialect": "mysql",
        "operation": "read_binding",
    }
    assert not hasattr(error.context, "__setitem__")
    assert not hasattr(error.diagnostic_context, "__setitem__")


def test_timeout_errors_remain_machine_classifiable() -> None:
    state_timeout = OpenSandboxBackendTimeoutError("command timed out")
    settlement_timeout = OpenSandboxSettlementTimeoutError(timeout=1)

    assert state_timeout.code is OpenSandboxErrorCode.BACKEND_TIMEOUT
    assert isinstance(settlement_timeout, TimeoutError)
    assert settlement_timeout.code is OpenSandboxErrorCode.SETTLEMENT_TIMEOUT
