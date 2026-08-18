"""Internal structural contracts used by the OpenSandbox lifecycle manager."""

from collections.abc import Mapping
from typing import Protocol

from ..backends.sdk import OpenSandboxBackend
from ..models import OpenSandboxConfig, OpenSandboxRuntimeInfo


class _SandboxClient(Protocol):
    """Create, reconnect, inspect, destroy, and close remote sandboxes."""

    config: OpenSandboxConfig

    async def create(
        self,
        *,
        metadata: Mapping[str, str] | None = None,
    ) -> OpenSandboxBackend: ...

    async def connect(self, sandbox_id: str) -> OpenSandboxBackend: ...

    async def inspect(self, sandbox_id: str) -> OpenSandboxRuntimeInfo: ...

    async def destroy(self, sandbox_id: str) -> None: ...

    async def aclose(self) -> None: ...
