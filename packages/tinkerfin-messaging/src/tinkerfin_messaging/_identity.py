"""Canonical caller-defined run identity without runtime-specific fields."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import cast

from pydantic import BaseModel

JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
AttachIdentity = JsonValue | BaseModel


def required_canonical_text(name: str, value: str) -> str:
    """Validate non-blank text without changing its persisted identity."""

    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value.strip() or value != value.strip():
        raise ValueError(f"{name} must be non-blank without surrounding whitespace")
    return value


def required_identifier(name: str, value: str) -> str:
    """Validate one bounded public identifier without normalizing its identity."""

    required_canonical_text(name, value)
    if len(value) > 1024:
        raise ValueError(f"{name} must contain at most 1024 characters")
    return value


def _copy_json(value: object, *, active: set[int] | None = None) -> JsonValue:
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        raise ValueError("attach_identity must contain finite JSON values")
    if isinstance(value, bytes | bytearray) or not isinstance(
        value, Mapping | Sequence
    ):
        raise TypeError("attach_identity must contain JSON values only")

    containers = set() if active is None else active
    identity = id(value)
    if identity in containers:
        raise ValueError("attach_identity must not contain cycles")
    containers.add(identity)
    try:
        if isinstance(value, Mapping):
            copied: dict[str, JsonValue] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise TypeError("attach_identity object keys must be strings")
                copied[key] = _copy_json(item, active=containers)
            return copied
        return [
            _copy_json(item, active=containers)
            for item in cast(Sequence[object], value)
        ]
    finally:
        containers.remove(identity)


def identity_digest(identity: AttachIdentity | None) -> str:
    """Hash canonical finite JSON while preserving JSON scalar types."""

    if isinstance(identity, BaseModel):
        value = identity.model_dump(mode="json", by_alias=True, exclude_none=False)
    else:
        value = identity
    copied = _copy_json(value)
    encoded = json.dumps(
        copied,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(
        b"tinkerfin-messaging:attach-identity:v1\0" + encoded
    ).hexdigest()
