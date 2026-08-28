"""Public-safe payload capture with explicit omission semantics."""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import Field, JsonValue, field_validator, model_validator

from ._models import TraceModel
from .errors import TraceCaptureRejected

_CREDENTIAL_KEYS = frozenset(
    {
        "access_token",
        "auth_token",
        "api_key",
        "apikey",
        "authorization",
        "client_secret",
        "cookie",
        "id_token",
        "password",
        "private_key",
        "proxy_authorization",
        "refresh_token",
        "secret",
        "secret_key",
        "session_token",
        "set_cookie",
        "token",
        "x_api_key",
    }
)
_REDACTED: dict[str, JsonValue] = {"$type": "redacted"}


class CapturedValue(TraceModel):
    """Store either one safe JSON value or an explicit omission reason."""

    disposition: Literal["inline", "omitted"]
    safe_size_bytes: int = Field(ge=0)
    value: JsonValue | None = None
    reason: str | None = Field(default=None, min_length=1, max_length=1024)

    @model_validator(mode="after")
    def value_matches_disposition(self) -> CapturedValue:
        """Keep inline and omitted representations mutually exclusive."""

        if self.disposition == "omitted" and self.value is not None:
            raise ValueError("omitted capture cannot retain a value")
        if self.disposition == "omitted" and self.reason is None:
            raise ValueError("omitted capture requires a reason")
        if self.disposition == "inline" and self.reason is not None:
            raise ValueError("inline capture cannot have an omission reason")
        if self.disposition == "inline" and self.safe_size_bytes != len(
            _encode(self.value)
        ):
            raise ValueError("inline safe_size_bytes must match canonical JSON bytes")
        return self


class ToolCaptureRule(TraceModel):
    """Allow selected JSON Pointer paths for one Tool's public content."""

    tool_name: str = Field(min_length=1, max_length=1024)
    argument_paths: tuple[str, ...] = ()
    result_paths: tuple[str, ...] = ()

    @field_validator("argument_paths", "result_paths")
    @classmethod
    def paths_are_canonical_json_pointers(
        cls, paths: tuple[str, ...]
    ) -> tuple[str, ...]:
        """Require unique RFC 6901 pointer syntax without retaining whole payloads."""

        if any(not _is_canonical_pointer(path) for path in paths):
            raise ValueError("Tool capture paths must be canonical JSON Pointers")
        if len(set(paths)) != len(paths):
            raise ValueError("Tool capture paths must be unique")
        return paths


class ReasoningCapturePolicy(TraceModel):
    """Control provider reasoning retention independently from public messages."""

    mode: Literal["omit", "content"] = "omit"

    @classmethod
    def omitted(cls) -> ReasoningCapturePolicy:
        """Return the safe default that records no reasoning content."""

        return cls(mode="omit")

    @classmethod
    def content(cls) -> ReasoningCapturePolicy:
        """Return an explicit policy that retains bounded extracted content."""

        return cls(mode="content")

    def capture(self, value: JsonValue, *, max_bytes: int) -> CapturedValue:
        """Capture an extracted value only when content retention is authorized."""

        _validate_max_bytes(max_bytes)
        safe = _sanitize(value)
        encoded = _encode(safe)
        if self.mode == "omit":
            return CapturedValue(
                disposition="omitted",
                safe_size_bytes=len(encoded),
                reason="reasoning_capture_disabled",
            )
        if len(encoded) > max_bytes:
            return CapturedValue(
                disposition="omitted",
                safe_size_bytes=len(encoded),
                reason="payload_too_large",
            )
        return CapturedValue(
            disposition="inline",
            safe_size_bytes=len(encoded),
            value=safe,
        )


class CapturePolicy(TraceModel):
    """Define the only public-safe retention behavior used by the Tracer."""

    tool_rules: tuple[ToolCaptureRule, ...] = ()

    @classmethod
    def public_safe(
        cls,
        *,
        tool_rules: tuple[ToolCaptureRule, ...] = (),
    ) -> CapturePolicy:
        """Return the default user-visible capture policy."""

        return cls(tool_rules=tool_rules)

    @field_validator("tool_rules")
    @classmethod
    def tool_names_are_unique(
        cls, rules: tuple[ToolCaptureRule, ...]
    ) -> tuple[ToolCaptureRule, ...]:
        """Ensure one deterministic capture rule owns each Tool name."""

        names = [rule.tool_name for rule in rules]
        if len(set(names)) != len(names):
            raise ValueError("Tool capture rules must use unique Tool names")
        return rules

    def capture(self, value: JsonValue, *, max_bytes: int) -> CapturedValue:
        """Sanitize one JSON graph and omit it when the encoded value is too large."""

        _validate_max_bytes(max_bytes)
        safe = _sanitize(value)
        encoded = _encode(safe)
        if len(encoded) > max_bytes:
            return CapturedValue(
                disposition="omitted",
                safe_size_bytes=len(encoded),
                reason="payload_too_large",
            )
        return CapturedValue(
            disposition="inline",
            safe_size_bytes=len(encoded),
            value=safe,
        )

    def sanitize(self, value: JsonValue) -> JsonValue:
        """Return a detached public-safe graph without applying retention limits."""

        return _sanitize(value)

    def capture_tool(
        self,
        *,
        tool_name: str,
        value: JsonValue,
        target: Literal["arguments", "result"],
        max_bytes: int,
    ) -> CapturedValue:
        """Capture only explicitly allowed Tool paths, otherwise retain metadata."""

        _validate_max_bytes(max_bytes)
        safe = _sanitize(value)
        encoded_size = len(_encode(safe))
        rule = next(
            (
                candidate
                for candidate in self.tool_rules
                if candidate.tool_name == tool_name
            ),
            None,
        )
        if rule is None:
            return CapturedValue(
                disposition="omitted",
                safe_size_bytes=encoded_size,
                reason="tool_content_not_allowlisted",
            )
        paths = rule.argument_paths if target == "arguments" else rule.result_paths
        selected: dict[str, JsonValue] = {}
        for path in paths:
            found, selected_value = _resolve_pointer(safe, path)
            if found:
                selected[path] = selected_value
        return self.capture(selected, max_bytes=max_bytes)

    def capture_metadata_only(
        self,
        value: JsonValue,
        *,
        reason: str,
    ) -> CapturedValue:
        """Measure sanitized content while retaining no value bytes."""

        if not isinstance(reason, str) or not reason or len(reason) > 1024:
            raise ValueError("metadata-only capture requires a bounded reason")
        safe = _sanitize(value)
        return CapturedValue(
            disposition="omitted",
            safe_size_bytes=len(_encode(safe)),
            reason=reason,
        )

    def capture_structure(
        self,
        value: JsonValue,
        *,
        max_bytes: int,
    ) -> CapturedValue:
        """Retain bounded structural metadata without retaining source values."""

        _validate_max_bytes(max_bytes)
        safe = _sanitize(value)
        summary: dict[str, JsonValue] = {
            "$type": "structural_metadata",
            "dataType": _json_type(safe),
            "sourceSafeSizeBytes": len(_encode(safe)),
        }
        if isinstance(safe, dict):
            summary["topLevelKeys"] = list[JsonValue](sorted(safe))
        elif isinstance(safe, list):
            summary["itemCount"] = len(safe)
        return self.capture(summary, max_bytes=max_bytes)


def _encode(value: JsonValue) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    except (TypeError, ValueError) as error:
        raise TraceCaptureRejected(
            "Trace capture requires a finite JSON value",
            diagnostic_context={"error_type": type(error).__name__},
            cause=error,
        ) from error


def _validate_max_bytes(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("max_bytes must be an integer")
    if value < 1:
        raise ValueError("max_bytes must be positive")


def _is_canonical_pointer(path: str) -> bool:
    if not path.startswith("/"):
        return False
    index = 0
    while index < len(path):
        if path[index] != "~":
            index += 1
            continue
        if index + 1 >= len(path) or path[index + 1] not in {"0", "1"}:
            return False
        index += 2
    return True


def _credential_key(value: str) -> str:
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", value)
    return re.sub(r"[^a-z0-9]+", "_", separated.casefold()).strip("_")


def _is_credential_key(value: str) -> bool:
    """Recognize exact or vendor-prefixed credential field names."""

    normalized = _credential_key(value)
    return any(
        normalized == credential or normalized.endswith(f"_{credential}")
        for credential in _CREDENTIAL_KEYS
    )


def _json_type(value: JsonValue) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _sanitize(value: JsonValue) -> JsonValue:
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if not isinstance(value, dict):
        return value
    sanitized: dict[str, JsonValue] = {}
    for key, item in value.items():
        if _is_credential_key(key):
            sanitized[key] = dict(_REDACTED)
            continue
        if key == "additional_kwargs" and isinstance(item, dict):
            metadata = {
                metadata_key: metadata_value
                for metadata_key, metadata_value in item.items()
                if metadata_key != "reasoning_content"
            }
            sanitized[key] = _sanitize(metadata)
            continue
        sanitized[key] = _sanitize(item)
    return sanitized


def _resolve_pointer(value: JsonValue, path: str) -> tuple[bool, JsonValue]:
    current = value
    for raw_component in path.split("/")[1:]:
        component = raw_component.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if component not in current:
                return False, None
            current = current[component]
            continue
        valid_index = component == "0" or (
            bool(component) and component[0] in "123456789" and component.isdecimal()
        )
        if isinstance(current, list) and valid_index:
            index = int(component)
            if index >= len(current):
                return False, None
            current = current[index]
            continue
        return False, None
    return True, current


__all__ = [
    "CapturePolicy",
    "CapturedValue",
    "ReasoningCapturePolicy",
    "ToolCaptureRule",
]
