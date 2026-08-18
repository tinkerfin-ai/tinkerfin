"""Reversible namespace-scoped IDs shared by events and snapshots."""

from __future__ import annotations

import base64
import binascii
import json
from typing import Literal, TypeAlias, cast

ScopedIdKind: TypeAlias = Literal[
    "message",
    "tool",
    "reasoning",
    "reasoning-message",
]

_KINDS = frozenset({"message", "tool", "reasoning", "reasoning-message"})
_PREFIX = "tf"
_VERSION = 1


class ScopedIdCodec:
    """Encode complete graph namespaces and native IDs without collisions."""

    def encode(
        self,
        kind: ScopedIdKind,
        namespace: tuple[str, ...],
        raw_id: str,
    ) -> str:
        """Return an event ID safe from delimiters and cross-scope reuse."""

        self._validate(kind, namespace, raw_id)
        payload = json.dumps(
            [_VERSION, list(namespace), raw_id],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        token = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
        return f"{_PREFIX}:{kind}:{token}"

    def decode(self, value: str) -> tuple[ScopedIdKind, tuple[str, ...], str]:
        """Decode and validate an ID produced by this codec version."""

        if not isinstance(value, str):
            raise TypeError("scoped ID must be a string")
        try:
            prefix, raw_kind, token = value.split(":", 2)
        except ValueError as error:
            raise ValueError("invalid scoped ID") from error
        if prefix != _PREFIX or raw_kind not in _KINDS or not token:
            raise ValueError("invalid scoped ID")
        padding = "=" * (-len(token) % 4)
        try:
            decoded = base64.b64decode(
                token + padding,
                altchars=b"-_",
                validate=True,
            )
            payload = json.loads(decoded.decode("utf-8"))
        except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("invalid scoped ID") from error
        if (
            not isinstance(payload, list)
            or len(payload) != 3
            or payload[0] != _VERSION
            or not isinstance(payload[1], list)
            or not isinstance(payload[2], str)
        ):
            raise ValueError("invalid scoped ID")
        namespace = tuple(payload[1])
        kind = cast(ScopedIdKind, raw_kind)
        self._validate(kind, namespace, payload[2])
        return kind, namespace, payload[2]

    @staticmethod
    def _validate(
        kind: ScopedIdKind,
        namespace: tuple[str, ...],
        raw_id: str,
    ) -> None:
        if kind not in _KINDS:
            raise ValueError("unsupported scoped ID kind")
        if not isinstance(namespace, tuple) or any(
            not isinstance(segment, str) or not segment for segment in namespace
        ):
            raise ValueError("namespace must contain only non-empty strings")
        if not isinstance(raw_id, str) or not raw_id:
            raise ValueError("raw ID must be a non-empty string")
