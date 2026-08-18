"""Public exceptions for foundational OpenSandbox capabilities.

These exceptions expose only stable semantics that callers can act on. Underlying SDK
or storage errors remain available through ``__cause__`` for logging and diagnosis
without entering the public exception hierarchy.
"""


class OpenSandboxStateError(RuntimeError):
    """OpenSandbox allocation state could not complete an atomic operation."""


class OpenSandboxStateOwnershipError(OpenSandboxStateError):
    """An expired or superseded State claim attempted to mutate ownership."""


class OpenSandboxStateConfigurationError(OpenSandboxStateError):
    """Active workers disagree about one shared State configuration."""


class OpenSandboxDestroyError(RuntimeError):
    """Explicit destruction was not confirmed, so the target remains retryable."""


class OpenSandboxResetError(RuntimeError):
    """Workspace reset failed safely without changing remote identity or binding."""


class OpenSandboxHandleOwnershipError(RuntimeError):
    """A caller tried to close a stable handle outside its lifecycle manager."""


class OpenSandboxManagerClosedError(RuntimeError):
    """A lifecycle operation was requested after manager shutdown began."""


class OpenSandboxSettlementTimeoutError(TimeoutError):
    """A caller stopped waiting before manager settlement completed."""

    def __init__(self, *, timeout: float) -> None:
        self.timeout = timeout
        super().__init__(f"OpenSandbox settlement timed out after {timeout:g} seconds")
