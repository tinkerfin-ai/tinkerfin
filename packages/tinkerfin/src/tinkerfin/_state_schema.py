"""TypedDict state composition shared by ordinary and Plan Deep Agents."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, NotRequired, Required, cast, get_origin, get_type_hints

from deepagents.graph import DeepAgentState
from pydantic import JsonValue
from typing_extensions import TypedDict, is_typeddict

from ._agui_lineage_state import LINEAGE_STATE_KEY, RESUME_MARKER_STATE_KEY


class TinkerFinRuntimeState(DeepAgentState, total=False):
    """Deep Agent state with framework-owned durable lifecycle evidence."""

    _tinkerfin_lineage: NotRequired[dict[str, JsonValue]]
    _tinkerfin_resume: NotRequired[dict[str, JsonValue]]


@dataclass(frozen=True, slots=True)
class StateSchemaSource:
    """One named schema contribution and fields inherited from its base contract."""

    name: str
    schema: type
    inherited_fields: frozenset[str] = frozenset()


class StateSchemaCompositionError(ValueError):
    """Raised when public state schemas cannot form one unambiguous contract."""


def _schema_hints(schema: type, *, source: str) -> dict[str, Any]:
    """Resolve one TypedDict while preserving explicit field requiredness.

    ``get_type_hints`` can erase the distinction between totality inherited from a
    base and per-field ``Required``/``NotRequired`` wrappers. Reapplying the runtime
    key sets keeps composition from silently weakening checkpoint state contracts.
    """

    if not is_typeddict(schema):
        raise StateSchemaCompositionError(f"{source} must be a TypedDict state schema")
    try:
        hints = get_type_hints(schema, include_extras=True)
    except (NameError, TypeError) as error:
        raise StateSchemaCompositionError(
            f"could not resolve state schema from {source}"
        ) from error
    required = cast(
        frozenset[str],
        getattr(schema, "__required_keys__", frozenset[str]()),
    )
    optional = cast(
        frozenset[str],
        getattr(schema, "__optional_keys__", frozenset[str]()),
    )
    normalized: dict[str, Any] = {}
    for field_name, field_type in hints.items():
        if get_origin(field_type) in (Required, NotRequired):
            normalized[field_name] = field_type
        elif field_name in required:
            normalized[field_name] = Required[field_type]
        elif field_name in optional:
            normalized[field_name] = NotRequired[field_type]
        else:
            normalized[field_name] = field_type
    return normalized


def state_schema_field_names(schema: type, *, source: str) -> frozenset[str]:
    """Return resolved field names for excluding inherited middleware state."""

    return frozenset(_schema_hints(schema, source=source))


def validate_state_schema(schema: type | None, *, source: str) -> None:
    """Validate one optional TypedDict before storing it on a shared factory."""

    if schema is not None:
        _schema_hints(schema, source=source)


def compose_state_schema(
    sources: Sequence[StateSchemaSource],
    *,
    name: str,
) -> type[DeepAgentState]:
    """Compose named state contributions without silent field precedence."""

    fields: dict[str, Any] = {}
    owners: dict[str, str] = {}
    for source in sources:
        hints = _schema_hints(source.schema, source=source.name)
        for field_name, field_type in hints.items():
            if field_name in source.inherited_fields:
                continue
            existing = fields.get(field_name)
            if existing is None:
                fields[field_name] = field_type
                owners[field_name] = source.name
                continue
            if existing == field_type:
                continue
            raise StateSchemaCompositionError(
                f"state field {field_name!r} conflicts between "
                f"{owners[field_name]} and {source.name}"
            )
    combined = TypedDict(
        name,  # pyright: ignore[reportArgumentType]
        fields,
        total=False,
    )
    return cast(type[DeepAgentState], combined)


def compose_deep_agent_base_schema(
    global_schema: type[DeepAgentState] | None,
    definition_schema: type[DeepAgentState] | None,
) -> type[DeepAgentState] | None:
    """Merge framework, global, and Definition state without field precedence."""

    if RESUME_MARKER_STATE_KEY not in TinkerFinRuntimeState.__annotations__:
        raise RuntimeError("TinkerFin Runtime state lost its reserved resume channel")
    if LINEAGE_STATE_KEY not in TinkerFinRuntimeState.__annotations__:
        raise RuntimeError("TinkerFin Runtime state lost its reserved lineage channel")
    sources = [
        StateSchemaSource("DeepAgentState", DeepAgentState),
        StateSchemaSource("TinkerFin Runtime state", TinkerFinRuntimeState),
    ]
    if global_schema is not None:
        sources.append(StateSchemaSource("TinkerFin state_schema", global_schema))
    if definition_schema is not None:
        sources.append(
            StateSchemaSource("create_deep_agent state_schema", definition_schema)
        )
    return compose_state_schema(sources, name="TinkerFinDeepAgentState")


def middleware_state_sources(
    middleware: Iterable[object],
    *,
    inherited_schema: type,
) -> tuple[StateSchemaSource, ...]:
    """Return public middleware additions without inherited AgentState scaffolding."""

    inherited = state_schema_field_names(
        inherited_schema,
        source=f"{inherited_schema.__name__} inherited state",
    )
    sources: list[StateSchemaSource] = []
    for item in middleware:
        schema = getattr(item, "state_schema", None)
        if isinstance(schema, type):
            sources.append(
                StateSchemaSource(
                    f"middleware {getattr(item, 'name', type(item).__name__)} state_schema",
                    schema,
                    inherited,
                )
            )
    return tuple(sources)


__all__ = [
    "StateSchemaCompositionError",
    "StateSchemaSource",
    "TinkerFinRuntimeState",
    "compose_deep_agent_base_schema",
    "compose_state_schema",
    "middleware_state_sources",
    "state_schema_field_names",
    "validate_state_schema",
]
