"""Provider-reasoning extraction and public-payload privacy boundaries."""

from __future__ import annotations

import math

from pydantic import JsonValue, TypeAdapter
from pydantic_core import to_jsonable_python

_PRIVATE_PROVIDER_FIELD = "reasoning_content"
_PROVIDER_METADATA_CONTAINER = "additional_kwargs"
_JSON_VALUE_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


def normalize_operational_data(value: object) -> JsonValue:
    """Normalize Tool operational data without provider-reasoning filtering.

    Tool arguments and results must remain semantically identical to the proposal
    that is approved or executed. Unsupported opaque objects fail before event
    construction instead of being stringified implicitly.

    Args:
        value: Operational value from a typed or untyped upstream boundary.

    Returns:
        Finite JSON data preserving every non-private operational field.

    Raises:
        ValueError: The value is opaque, non-finite, or not JSON representable.
    """

    normalized = _JSON_VALUE_ADAPTER.validate_python(to_jsonable_python(value))
    _reject_non_finite_numbers(normalized)
    return normalized


def json_values_equal(left: JsonValue, right: JsonValue) -> bool:
    """Compare JSON values without conflating booleans and number types."""

    if type(left) is not type(right):
        return False
    if isinstance(left, dict) and isinstance(right, dict):
        if left.keys() != right.keys():
            return False
        return all(json_values_equal(left[key], right[key]) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            json_values_equal(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    return left == right


def _reject_non_finite_numbers(value: JsonValue) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite numbers are not valid operational JSON")
    if isinstance(value, list):
        for item in value:
            _reject_non_finite_numbers(item)
    elif isinstance(value, dict):
        for item in value.values():
            _reject_non_finite_numbers(item)


def sanitize_public_data(value: object) -> JsonValue:
    """Normalize a public graph and remove only verified provider metadata paths.

    The reserved path is `additional_kwargs.reasoning_content`. Same-named domain
    fields remain visible. Tool operational data must use
    `normalize_operational_data()` at a validated Tool or HITL boundary instead of
    gaining exemptions from field-name guesses in a general public graph.

    Args:
        value: Mapping, container, dataclass, Pydantic model, or JSON-compatible
            scalar that will become part of a public event.

    Returns:
        An auditable JSON value with verified provider reasoning removed.

    Raises:
        pydantic_core.PydanticSerializationError: The graph contains an unsupported
            opaque object.
        pydantic.ValidationError: Normalization did not produce a JSON value.
    """

    return _filter_provider_metadata(normalize_operational_data(value))


def _filter_provider_metadata(value: JsonValue) -> JsonValue:
    if isinstance(value, list):
        return [_filter_provider_metadata(item) for item in value]
    if not isinstance(value, dict):
        return value

    filtered: dict[str, JsonValue] = {}
    for key, child in value.items():
        if key == _PROVIDER_METADATA_CONTAINER and isinstance(child, dict):
            filtered[key] = {
                nested_key: _filter_provider_metadata(nested_value)
                for nested_key, nested_value in child.items()
                if nested_key != _PRIVATE_PROVIDER_FIELD
            }
            continue
        filtered[key] = _filter_provider_metadata(child)
    return filtered
