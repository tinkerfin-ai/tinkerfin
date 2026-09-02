"""Definition metadata supplied to request-scoped Runtime observations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from langchain.agents.middleware.types import AgentMiddleware

from tinkerfin_contracts import MiddlewareDescriptor, SkillSourceDescriptor


@dataclass(frozen=True, slots=True)
class DefinitionTraceContext:
    """Carry immutable definition metadata into each request observation context."""

    middleware: tuple[MiddlewareDescriptor, ...] = ()
    skill_sources: tuple[SkillSourceDescriptor, ...] = ()


_HOOK_NAMES = (
    "before_agent",
    "abefore_agent",
    "before_model",
    "abefore_model",
    "wrap_model_call",
    "awrap_model_call",
    "wrap_tool_call",
    "awrap_tool_call",
    "after_model",
    "aafter_model",
    "after_agent",
    "aafter_agent",
)


def describe_middleware(
    values: object,
) -> tuple[
    tuple[AgentMiddleware[Any, Any, Any], ...], tuple[MiddlewareDescriptor, ...]
]:
    """Validate one middleware sequence and describe every original instance."""

    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError("middleware must be a sequence")
    middleware: list[AgentMiddleware[Any, Any, Any]] = []
    descriptors: list[MiddlewareDescriptor] = []
    for value in cast(Sequence[object], values):
        if not isinstance(value, AgentMiddleware):
            raise TypeError("middleware entries must be AgentMiddleware instances")
        resolved = cast(AgentMiddleware[Any, Any, Any], value)
        middleware.append(resolved)
        value_type = type(resolved)
        descriptors.append(
            MiddlewareDescriptor(
                name=resolved.name,
                class_name=f"{value_type.__module__}.{value_type.__qualname__}",
                hooks=tuple(
                    name
                    for name in _HOOK_NAMES
                    if getattr(value_type, name) is not getattr(AgentMiddleware, name)
                ),
            )
        )
    return tuple(middleware), tuple(descriptors)


def skill_sources(
    arguments: Mapping[str, object],
) -> tuple[SkillSourceDescriptor, ...]:
    """Collect configured root and declarative subagent Skill source paths."""

    descriptors: list[SkillSourceDescriptor] = []
    raw_root = arguments.get("skills")
    if isinstance(raw_root, Sequence) and not isinstance(raw_root, (str, bytes)):
        descriptors.extend(
            SkillSourceDescriptor(path=path)
            for path in cast(Sequence[object], raw_root)
            if isinstance(path, str)
        )
    raw_subagents = arguments.get("subagents")
    if isinstance(raw_subagents, Sequence) and not isinstance(
        raw_subagents, (str, bytes)
    ):
        for subagent in cast(Sequence[object], raw_subagents):
            if not isinstance(subagent, Mapping):
                continue
            resolved_subagent = cast(Mapping[str, object], subagent)
            agent_name = resolved_subagent.get("name")
            sources = resolved_subagent.get("skills")
            if (
                not isinstance(agent_name, str)
                or not isinstance(sources, Sequence)
                or isinstance(sources, (str, bytes))
            ):
                continue
            descriptors.extend(
                SkillSourceDescriptor(path=path, agent_name=agent_name)
                for path in cast(Sequence[object], sources)
                if isinstance(path, str)
            )
    unique: list[SkillSourceDescriptor] = []
    seen: set[tuple[str | None, str]] = set()
    for descriptor in descriptors:
        key = (descriptor.agent_name, descriptor.path)
        if key not in seen:
            seen.add(key)
            unique.append(descriptor)
    return tuple(unique)


__all__ = ["DefinitionTraceContext", "describe_middleware", "skill_sources"]
