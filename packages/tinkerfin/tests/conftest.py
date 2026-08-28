"""Fixtures for exercising request Runtimes through the public Deep Agent façade."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tinkerfin import DeepAgentDefinition, TinkerFin


@pytest.fixture
def definition_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[..., DeepAgentDefinition[None]]:
    """Build a public Definition around one deterministic Graph test double."""

    def create(
        graph: object,
        *,
        tinkerfin: TinkerFin | None = None,
    ) -> DeepAgentDefinition[None]:
        def build(*_args: object, **_kwargs: object) -> object:
            return graph

        monkeypatch.setattr(
            "tinkerfin.runtime_profile._deepagents_graph.create_deep_agent",
            build,
        )
        return (tinkerfin or TinkerFin()).create_deep_agent(
            model="provider:model",
            tools=[],
        )

    return create
