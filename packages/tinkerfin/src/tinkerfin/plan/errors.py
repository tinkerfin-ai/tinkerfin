"""Public Plan Mode failures."""

from ..errors import TinkerFinError, TinkerFinErrorCode


class PlanModeConfigurationError(TinkerFinError, ValueError):
    """Plan Mode cannot be built from the supplied Deep Agent configuration."""

    code = TinkerFinErrorCode.PLAN_MODE_CONFIGURATION


class PlanStructuredOutputError(TinkerFinError, RuntimeError):
    """A model result cannot satisfy the configured Plan response contract."""

    code = TinkerFinErrorCode.PLAN_STRUCTURED_OUTPUT


__all__ = ["PlanModeConfigurationError", "PlanStructuredOutputError"]
