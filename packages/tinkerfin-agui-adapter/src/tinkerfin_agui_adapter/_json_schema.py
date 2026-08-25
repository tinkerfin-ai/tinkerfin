"""Draft 2020-12 validation for declared interrupt response contracts."""

from __future__ import annotations

from typing import Protocol, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from pydantic import JsonValue


class _JsonSchemaInstanceValidator(Protocol):
    def validate(self, instance: object) -> None: ...


def require_valid_schema(schema: dict[str, JsonValue]) -> None:
    """Reject a malformed JSON Schema before it is published to a client."""

    Draft202012Validator.check_schema(schema)


def validate_json_schema_instance(
    value: JsonValue,
    schema: dict[str, JsonValue],
) -> None:
    """Validate one trusted JSON value against a predeclared response schema."""

    validator = cast(
        _JsonSchemaInstanceValidator,
        Draft202012Validator(schema),
    )
    validator.validate(value)


__all__ = [
    "SchemaError",
    "ValidationError",
    "require_valid_schema",
    "validate_json_schema_instance",
]
