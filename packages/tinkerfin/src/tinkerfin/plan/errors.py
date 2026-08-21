"""Public Plan Mode failures."""

from ..errors import TinkerFinError, TinkerFinErrorCode


class PlanModeConfigurationError(TinkerFinError, ValueError):
    """Plan Mode cannot be built from the supplied Deep Agent configuration."""

    code = TinkerFinErrorCode.PLAN_MODE_CONFIGURATION


__all__ = ["PlanModeConfigurationError"]
