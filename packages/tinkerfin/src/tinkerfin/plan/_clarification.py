"""Definition-bound clarification schema validation and JSON persistence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import NoneType
from typing import Annotated, Any, cast, get_args, get_origin

from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, JsonValue, TypeAdapter, create_model

from ._contracts import PlannerOutcomeBase
from .clarification import (
    ClarificationFormBase,
    ClarificationModel,
    ClarificationOptionBase,
    ClarificationQuestionBase,
)
from .errors import PlanModeConfigurationError, PlanStructuredOutputError

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])
_LANGGRAPH_DURABILITY_CONFIG_KEY = "__pregel_durability"


@dataclass(frozen=True, slots=True)
class ClarificationQuestionCount:
    """Model-visible question count bounds derived from one host form schema."""

    minimum: int
    maximum: int | None


@dataclass(frozen=True, slots=True)
class ClarificationSchemaBinding:
    """Concrete form and structured response types frozen for one Definition."""

    form_schema: type[ClarificationFormBase]
    fingerprint: str
    planner_response_type: type[PlannerOutcomeBase]
    question_count: ClarificationQuestionCount


def stateless_child_config(config: RunnableConfig) -> RunnableConfig:
    """Keep parent runtime resources without inheriting synchronous durability."""

    child = cast(RunnableConfig, dict(config))
    configurable = config.get("configurable")
    if isinstance(configurable, Mapping):
        child_configurable = dict(configurable)
        child_configurable.pop(_LANGGRAPH_DURABILITY_CONFIG_KEY, None)
        child["configurable"] = child_configurable
    return child


def _annotation_leaves(annotation: object) -> tuple[object, ...]:
    origin = get_origin(annotation)
    if origin is Annotated:
        arguments = get_args(annotation)
        return _annotation_leaves(arguments[0]) if arguments else ()
    arguments = get_args(annotation)
    if arguments:
        leaves: list[object] = []
        for argument in arguments:
            if argument is Ellipsis:
                continue
            leaves.extend(_annotation_leaves(argument))
        return tuple(leaves)
    return (annotation,)


def _require_concrete_model(model: type[BaseModel], *, source: str) -> None:
    parameters = getattr(model, "__parameters__", ())
    if parameters:
        raise PlanModeConfigurationError(f"{source} must not contain unbound generics")


def _require_inherited_core_fields(
    model: type[BaseModel],
    *,
    base: type[BaseModel],
    field_names: frozenset[str],
    source: str,
) -> None:
    """Prevent host models from weakening framework-owned field contracts."""

    for parent in model.__mro__:
        if parent is base:
            return
        annotations = vars(parent).get("__annotations__", {})
        overridden = field_names.intersection(annotations)
        if overridden:
            fields = ", ".join(repr(name) for name in sorted(overridden))
            raise PlanModeConfigurationError(
                f"{source} must inherit framework core fields unchanged: {fields}"
            )
    raise PlanModeConfigurationError(f"{source} must inherit from {base.__name__}")


def _field_model_types(
    model: type[BaseModel],
    field_name: str,
    *,
    expected: type[BaseModel],
    source: str,
    optional: bool = False,
    variadic_tuple: bool = False,
) -> tuple[type[BaseModel], ...]:
    field = model.model_fields.get(field_name)
    if field is None:
        if optional:
            return ()
        raise PlanModeConfigurationError(f"{source} must define {field_name!r}")
    if variadic_tuple:
        arguments = get_args(field.annotation)
        if (
            get_origin(field.annotation) is not tuple
            or len(arguments) != 2
            or arguments[1] is not Ellipsis
        ):
            raise PlanModeConfigurationError(
                f"{source}.{field_name} must be a variadic tuple"
            )
    found: list[type[BaseModel]] = []
    for leaf in _annotation_leaves(field.annotation):
        if optional and leaf is NoneType:
            continue
        if leaf is Any or not isinstance(leaf, type) or not issubclass(leaf, expected):
            raise PlanModeConfigurationError(
                f"{source}.{field_name} must contain concrete {expected.__name__} types"
            )
        _require_concrete_model(leaf, source=f"{source}.{field_name}")
        found.append(leaf)
    if not found and not optional:
        raise PlanModeConfigurationError(
            f"{source}.{field_name} must contain at least one {expected.__name__}"
        )
    return tuple(dict.fromkeys(found))


def _validate_attributes(model: type[BaseModel], *, source: str) -> None:
    _field_model_types(
        model,
        "attributes",
        expected=ClarificationModel,
        source=source,
        optional=True,
    )


def _validate_questions_json_schema(
    model: type[BaseModel], *, source: str
) -> ClarificationQuestionCount:
    """Require model-visible, satisfiable clarification cardinality constraints."""

    field = model.model_fields["questions"]
    if not field.is_required():
        raise PlanModeConfigurationError(f"{source}.questions must be required")
    try:
        schema = _JSON_OBJECT.validate_python(model.model_json_schema(by_alias=True))
    except Exception as error:
        raise PlanModeConfigurationError(
            f"{source} could not produce a JSON Schema",
            cause=error,
        ) from error
    properties_value = schema.get("properties")
    properties = (
        cast(Mapping[str, JsonValue], properties_value)
        if isinstance(properties_value, Mapping)
        else None
    )
    questions_value = properties.get("questions") if properties is not None else None
    if not isinstance(questions_value, Mapping):
        raise PlanModeConfigurationError(
            f"{source}.questions must expose the stable JSON array field 'questions'"
        )
    questions = cast(Mapping[str, JsonValue], questions_value)
    if questions.get("type") != "array":
        raise PlanModeConfigurationError(
            f"{source}.questions must expose the stable JSON array field 'questions'"
        )
    minimum = questions.get("minItems")
    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 1:
        raise PlanModeConfigurationError(
            f"{source}.questions JSON Schema must declare minItems >= 1"
        )
    maximum = questions.get("maxItems")
    if maximum is not None and (
        isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < minimum
    ):
        raise PlanModeConfigurationError(
            f"{source}.questions JSON Schema maxItems must be >= minItems"
        )
    return ClarificationQuestionCount(minimum=minimum, maximum=maximum)


def _validate_clarification_schema(
    value: object,
) -> tuple[type[ClarificationFormBase], ClarificationQuestionCount]:
    """Validate one fully concrete host form using public typing metadata."""

    if not isinstance(value, type) or not issubclass(value, ClarificationFormBase):
        raise PlanModeConfigurationError(
            "clarification_schema must be a ClarificationFormBase subclass"
        )
    _require_concrete_model(value, source="clarification_schema")
    _require_inherited_core_fields(
        value,
        base=ClarificationFormBase,
        field_names=frozenset({"schema_version"}),
        source="clarification_schema",
    )
    question_types = _field_model_types(
        value,
        "questions",
        expected=ClarificationQuestionBase,
        source="clarification_schema",
        variadic_tuple=True,
    )
    question_count = _validate_questions_json_schema(
        value,
        source="clarification_schema",
    )
    for question_type in question_types:
        _require_inherited_core_fields(
            question_type,
            base=ClarificationQuestionBase,
            field_names=frozenset({"allow_free_text", "id", "prompt"}),
            source=question_type.__name__,
        )
        _validate_attributes(question_type, source=question_type.__name__)
        option_types = _field_model_types(
            question_type,
            "options",
            expected=ClarificationOptionBase,
            source=question_type.__name__,
            variadic_tuple=True,
        )
        for option_type in option_types:
            _require_inherited_core_fields(
                option_type,
                base=ClarificationOptionBase,
                field_names=frozenset({"description", "id", "label"}),
                source=option_type.__name__,
            )
            _validate_attributes(option_type, source=option_type.__name__)
    return value, question_count


def _schema_fingerprint(schema: type[ClarificationFormBase]) -> str:
    canonical = json.dumps(
        schema.model_json_schema(by_alias=True),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def create_clarification_binding(
    schema: object,
) -> ClarificationSchemaBinding:
    """Create immutable concrete response types for one validated form schema."""

    form_schema, question_count = _validate_clarification_schema(schema)
    fingerprint = _schema_fingerprint(form_schema)
    planner_type = create_model(
        "PlannerOutcome",
        __base__=PlannerOutcomeBase,
        clarification=(form_schema | None, None),
    )
    return ClarificationSchemaBinding(
        form_schema=form_schema,
        fingerprint=fingerprint,
        planner_response_type=planner_type,
        question_count=question_count,
    )


def serialize_form(
    binding: ClarificationSchemaBinding,
    value: object,
) -> tuple[ClarificationFormBase, dict[str, JsonValue]]:
    """Validate and round-trip a model result before durable persistence."""

    if not isinstance(value, binding.form_schema):
        raise PlanStructuredOutputError(
            "structured clarification did not use the configured form schema"
        )
    try:
        payload = _JSON_OBJECT.validate_python(
            value.model_dump(mode="json", by_alias=True, exclude_none=False)
        )
        restored = binding.form_schema.model_validate(payload)
    except Exception as error:
        raise PlanStructuredOutputError(
            "structured clarification failed its JSON checkpoint round-trip",
            cause=error,
        ) from error
    return restored, payload


def restore_form(
    binding: ClarificationSchemaBinding,
    payload: object,
) -> ClarificationFormBase:
    """Restore one checkpoint form with the current Definition schema."""

    try:
        return binding.form_schema.model_validate(payload)
    except Exception as error:
        raise PlanModeConfigurationError(
            "checkpoint clarification form is invalid for this Definition",
            cause=error,
        ) from error


__all__ = [
    "ClarificationSchemaBinding",
    "create_clarification_binding",
    "restore_form",
    "serialize_form",
    "stateless_child_config",
]
