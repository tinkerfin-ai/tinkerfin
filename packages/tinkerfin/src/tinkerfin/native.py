"""Versioned finite JSON projection for verified LangGraph v2 stream parts."""

from __future__ import annotations

import base64
import math
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from datetime import date, datetime, time
from enum import Enum
from typing import Literal
from uuid import UUID

from langchain_core.messages import BaseMessage, message_to_dict
from langgraph.types import Interrupt
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from .errors import TinkerFinStreamProtocolError

NativeMode = Literal[
    "messages",
    "tasks",
    "values",
    "updates",
    "checkpoints",
    "debug",
    "custom",
]
_NATIVE_MODES: frozenset[str] = frozenset(
    {
        "messages",
        "tasks",
        "values",
        "updates",
        "checkpoints",
        "debug",
        "custom",
    }
)


class NativeStreamPart(BaseModel):
    """Finite replay and direct-SSE representation of one v2 stream part."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_version: Literal[1] = Field(
        default=1,
        alias="schemaVersion",
        description="Version of the finite native stream schema.",
    )
    mode: NativeMode = Field(
        alias="type",
        description="Verified LangGraph v2 stream mode.",
    )
    namespace: tuple[str, ...] = Field(
        alias="ns",
        description="Complete root or subgraph namespace.",
    )
    data: JsonValue = Field(
        description="Tagged finite JSON representation of the native payload.",
    )
    interrupts: tuple[JsonValue, ...] = Field(
        default=(),
        description="Native values-mode interrupts carried by the envelope.",
    )


def _qualified_name(value: object) -> str:
    value_type = type(value)
    return f"{value_type.__module__}.{value_type.__qualname__}"


def _normalize(value: object, *, active: set[int] | None = None) -> JsonValue:
    """Convert supported native values to finite tagged JSON without `repr`."""

    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TinkerFinStreamProtocolError(
                "native stream parts must contain finite numbers"
            )
        return value
    if isinstance(value, bytes | bytearray):
        return {
            "$type": "bytes",
            "base64": base64.b64encode(bytes(value)).decode("ascii"),
        }
    if isinstance(value, UUID):
        return {"$type": "uuid", "value": str(value)}
    if isinstance(value, datetime):
        return {"$type": "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {"$type": "date", "value": value.isoformat()}
    if isinstance(value, time):
        return {"$type": "time", "value": value.isoformat()}
    if isinstance(value, Enum):
        return {
            "$type": "enum",
            "class": _qualified_name(value),
            "value": _normalize(value.value, active=active),
        }
    if isinstance(value, BaseException):
        return {
            "$type": "exception",
            "class": _qualified_name(value),
            "message": str(value),
        }

    containers = set() if active is None else active
    identity = id(value)
    if identity in containers:
        raise TinkerFinStreamProtocolError(
            "native stream parts must not contain cycles"
        )
    containers.add(identity)
    try:
        if isinstance(value, BaseMessage):
            normalized = _normalize(message_to_dict(value), active=containers)
            return {"$type": "langchain.message", "value": normalized}
        if isinstance(value, Interrupt):
            return {
                "$type": "langgraph.interrupt",
                "id": value.id,
                "value": _normalize(value.value, active=containers),
            }
        if isinstance(value, BaseModel):
            normalized = _normalize(
                value.model_dump(mode="python", by_alias=True),
                active=containers,
            )
            return {
                "$type": "pydantic",
                "class": _qualified_name(value),
                "value": normalized,
            }
        if is_dataclass(value) and not isinstance(value, type):
            normalized_fields = {
                field.name: _normalize(
                    getattr(value, field.name),
                    active=containers,
                )
                for field in fields(value)
            }
            return {
                "$type": "dataclass",
                "class": _qualified_name(value),
                "value": normalized_fields,
            }
        if isinstance(value, Mapping):
            normalized_mapping: dict[str, JsonValue] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise TypeError("native mapping keys must be strings")
                normalized_mapping[key] = _normalize(item, active=containers)
            return normalized_mapping
        if isinstance(value, tuple):
            return {
                "$type": "tuple",
                "items": [_normalize(item, active=containers) for item in value],
            }
        if isinstance(value, Sequence) and not isinstance(value, str):
            return [_normalize(item, active=containers) for item in value]
        raise TypeError(
            f"unsupported native stream value type: {_qualified_name(value)}"
        )
    finally:
        containers.remove(identity)


def normalize_native_stream_part(item: object) -> NativeStreamPart:
    """Validate and normalize one live LangGraph v2 stream envelope."""

    if not isinstance(item, Mapping):
        raise TypeError("native stream part must be a mapping")
    unknown = set(item) - {"type", "ns", "data", "interrupts"}
    if unknown:
        raise TinkerFinStreamProtocolError(
            f"native stream part contains unknown fields: {unknown!r}"
        )
    mode = item.get("type")
    if mode not in _NATIVE_MODES:
        raise TinkerFinStreamProtocolError(
            "native stream part type must be messages, tasks, values, updates, "
            "checkpoints, debug, or custom"
        )
    namespace = item.get("ns")
    if not isinstance(namespace, tuple) or not all(
        isinstance(component, str) for component in namespace
    ):
        raise TypeError("native stream part ns must be a tuple of strings")
    if "data" not in item:
        raise TinkerFinStreamProtocolError("native stream part must contain data")
    raw_interrupts = item.get("interrupts", ())
    if not isinstance(raw_interrupts, tuple):
        raise TypeError("native stream part interrupts must be a tuple")
    return NativeStreamPart(
        type=mode,
        ns=namespace,
        data=_normalize(item["data"]),
        interrupts=tuple(_normalize(value) for value in raw_interrupts),
    )


__all__ = ["NativeMode", "NativeStreamPart", "normalize_native_stream_part"]
