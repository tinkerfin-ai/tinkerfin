"""Public-safe payload capture with explicit omission semantics."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Literal, Self

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
    include_review_description: bool = False

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
        if "" in paths and len(paths) != 1:
            raise ValueError("The root Tool capture path must be used alone")
        return paths


class ToolTraceCapture(TraceModel):
    """Define how one Tool contributes content and lifecycle facts to Trace."""

    mode: Literal[
        "full_content",
        "metadata_only",
        "selected_content",
        "disabled",
    ]
    argument_paths: tuple[str, ...] = ()
    result_paths: tuple[str, ...] = ()
    include_review_description: bool = False

    @classmethod
    def full_content(cls) -> Self:
        """Trace lifecycle plus complete sanitized arguments, results, and review text."""

        return cls(mode="full_content", include_review_description=True)

    @classmethod
    def metadata_only(cls) -> Self:
        """Trace Tool lifecycle while retaining no argument or result content."""

        return cls(mode="metadata_only")

    @classmethod
    def selected_content(
        cls,
        *,
        argument_paths: tuple[str, ...] = (),
        result_paths: tuple[str, ...] = (),
        include_review_description: bool = False,
    ) -> Self:
        """Trace lifecycle plus explicitly selected RFC 6901 content paths.

        Args:
            argument_paths: Canonical JSON Pointers selected from Tool arguments.
            result_paths: Canonical JSON Pointers selected from Tool results.
            include_review_description: Whether a public review description may be
                retained when selected arguments are available.

        Returns:
            An immutable selected-content setting for one Tool.
        """

        return cls(
            mode="selected_content",
            argument_paths=argument_paths,
            result_paths=result_paths,
            include_review_description=include_review_description,
        )

    @classmethod
    def disabled(cls) -> Self:
        """Suppress Tool lifecycle and content facts from Trace."""

        return cls(mode="disabled")

    @field_validator("argument_paths", "result_paths")
    @classmethod
    def paths_are_canonical_json_pointers(
        cls, paths: tuple[str, ...]
    ) -> tuple[str, ...]:
        """Require unique canonical pointers and one unambiguous root selection."""

        if any(not _is_canonical_pointer(path) for path in paths):
            raise ValueError("Tool capture paths must be canonical JSON Pointers")
        if len(set(paths)) != len(paths):
            raise ValueError("Tool capture paths must be unique")
        if "" in paths and len(paths) != 1:
            raise ValueError("The root Tool capture path must be used alone")
        return paths

    @model_validator(mode="after")
    def paths_match_mode(self) -> ToolTraceCapture:
        """Keep path and review settings exclusive to content-bearing modes."""

        has_paths = bool(self.argument_paths or self.result_paths)
        if self.mode != "selected_content" and has_paths:
            raise ValueError("only selected_content accepts Tool capture paths")
        if self.mode in {"metadata_only", "disabled"} and (
            self.include_review_description
        ):
            raise ValueError(
                "metadata-only or disabled Tool capture cannot retain review text"
            )
        return self


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
    """Define public-safe content retention for messages, state, and Tools."""

    default_tool_capture: ToolTraceCapture = Field(
        default_factory=ToolTraceCapture.metadata_only
    )
    tool_overrides: tuple[tuple[str, ToolTraceCapture], ...] = ()
    tool_rules: tuple[ToolCaptureRule, ...] = ()

    @classmethod
    def public_safe(
        cls,
        *,
        tool_rules: tuple[ToolCaptureRule, ...] = (),
    ) -> CapturePolicy:
        """Return a metadata-only Tool policy with optional selected paths."""

        return cls(
            default_tool_capture=ToolTraceCapture.metadata_only(),
            tool_rules=tool_rules,
        )

    @classmethod
    def public_history(
        cls,
        *,
        tool_overrides: Mapping[str, ToolTraceCapture] | None = None,
    ) -> CapturePolicy:
        """Capture every Tool's sanitized public content with exact-name overrides.

        Args:
            tool_overrides: Optional settings for Tools whose content or lifecycle
                retention differs from the full-content default.

        Returns:
            A policy that automatically covers newly observed Tools.

        Raises:
            TypeError: An override key or value has the wrong type.
        """

        if tool_overrides is None:
            overrides: tuple[tuple[str, ToolTraceCapture], ...] = ()
        else:
            if not isinstance(tool_overrides, Mapping):
                raise TypeError("tool_overrides must be a mapping or None")
            normalized: list[tuple[str, ToolTraceCapture]] = []
            for tool_name, capture in tool_overrides.items():
                if not isinstance(tool_name, str):
                    raise TypeError("tool_overrides keys must be Tool name strings")
                if not isinstance(capture, ToolTraceCapture):
                    raise TypeError(
                        "tool_overrides values must be ToolTraceCapture values"
                    )
                normalized.append((tool_name, capture))
            overrides = tuple(normalized)
        return cls(
            default_tool_capture=ToolTraceCapture.full_content(),
            tool_overrides=overrides,
        )

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

    @field_validator("tool_overrides")
    @classmethod
    def override_names_are_unique(
        cls, overrides: tuple[tuple[str, ToolTraceCapture], ...]
    ) -> tuple[tuple[str, ToolTraceCapture], ...]:
        """Reject ambiguous duplicate per-Tool settings."""

        names = [tool_name for tool_name, _capture in overrides]
        if any(not tool_name or len(tool_name) > 1024 for tool_name in names):
            raise ValueError("Tool trace override names must be non-empty and bounded")
        if len(set(names)) != len(names):
            raise ValueError("Tool trace overrides must use unique Tool names")
        return overrides

    @model_validator(mode="after")
    def legacy_rules_do_not_conflict_with_overrides(self) -> CapturePolicy:
        """Keep low-level selected paths and high-level overrides unambiguous."""

        rule_names = {rule.tool_name for rule in self.tool_rules}
        override_names = {tool_name for tool_name, _capture in self.tool_overrides}
        if rule_names.intersection(override_names):
            raise ValueError(
                "a Tool cannot use both a capture rule and a trace override"
            )
        return self

    def tool_capture(self, tool_name: str) -> ToolTraceCapture:
        """Resolve one Tool's effective capture setting by exact stable name."""

        override = next(
            (
                candidate
                for candidate_name, candidate in self.tool_overrides
                if candidate_name == tool_name
            ),
            None,
        )
        if override is not None:
            return override
        rule = next(
            (
                candidate
                for candidate in self.tool_rules
                if candidate.tool_name == tool_name
            ),
            None,
        )
        if rule is not None:
            return ToolTraceCapture.selected_content(
                argument_paths=rule.argument_paths,
                result_paths=rule.result_paths,
                include_review_description=rule.include_review_description,
            )
        return self.default_tool_capture

    def traces_tool(self, tool_name: str) -> bool:
        """Return whether one Tool contributes lifecycle facts to Trace."""

        return self.tool_capture(tool_name).mode != "disabled"

    def captures_review_description(self, tool_name: str) -> bool:
        """Return whether one Tool may retain its public review description."""

        return self.tool_capture(tool_name).include_review_description

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
        """Capture Tool content according to its resolved high- or low-level setting."""

        _validate_max_bytes(max_bytes)
        safe = _sanitize(value)
        encoded_size = len(_encode(safe))
        capture = self.tool_capture(tool_name)
        if capture.mode == "full_content":
            return self.capture(safe, max_bytes=max_bytes)
        if capture.mode == "metadata_only":
            return CapturedValue(
                disposition="omitted",
                safe_size_bytes=encoded_size,
                reason="tool_content_metadata_only",
            )
        if capture.mode == "disabled":
            return CapturedValue(
                disposition="omitted",
                safe_size_bytes=encoded_size,
                reason="tool_tracing_disabled",
            )
        paths = (
            capture.argument_paths if target == "arguments" else capture.result_paths
        )
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
    if path == "":
        return True
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
    "ToolTraceCapture",
]
