"""Lazy workspace borrowing for namespace-bound agent execution."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Generic, TypeVar

from deepagents.backends.composite import CompositeBackend
from deepagents.backends.protocol import BackendProtocol

from tinkerfin_contracts import PreparedWorkspace, RunIdentity

from ..backends.rooted import RootedOpenSandboxBackend
from ..middleware.filesystem import (
    ROOTED_EXECUTE_TOOL_DESCRIPTION,
    ROOTED_FILESYSTEM_SYSTEM_PROMPT,
)

if TYPE_CHECKING:
    from .manager import OpenSandboxManager

KeyT = TypeVar("KeyT")


class SandboxWorkspace(Generic[KeyT]):
    """Borrow a stable rooted Sandbox without taking its remote lifecycle."""

    def __init__(
        self,
        manager: OpenSandboxManager[KeyT],
        key: KeyT,
        routes: Mapping[str, BackendProtocol],
    ) -> None:
        self._manager = manager
        self._key = key
        self._routes = dict(routes)

    @asynccontextmanager
    async def prepare(
        self, identity: RunIdentity
    ) -> AsyncGenerator[
        PreparedWorkspace[RootedOpenSandboxBackend, BackendProtocol], None
    ]:
        """Keep this Run's manager borrow open until all Graph activity stops."""

        if not isinstance(identity, RunIdentity):
            raise TypeError("workspace preparation requires a RunIdentity")
        # The manager may serve other runs and owners concurrently. Holding one
        # operation delays manager close, while get releases its owner claim and
        # each tool independently leases the stable handle's current connection.
        async with self._manager._operation():
            workspace = await self._manager.get(self._key, namespace=identity.namespace)
            if not isinstance(workspace, RootedOpenSandboxBackend):
                raise TypeError(
                    "workspace preparation requires a configured workspace_root"
                )
            backend = (
                CompositeBackend(default=workspace, routes=dict(self._routes))
                if self._routes
                else workspace
            )
            yield PreparedWorkspace(
                workspace=workspace,
                backend=backend,
                filesystem_instructions=ROOTED_FILESYSTEM_SYSTEM_PROMPT,
                tool_descriptions={"execute": ROOTED_EXECUTE_TOOL_DESCRIPTION},
            )
