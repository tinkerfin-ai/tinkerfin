"""Durable envelope and recovery value contracts."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from tinkerfin import Identity
from tinkerfin_agui_adapter import Identity as AdapterIdentity
from tinkerfin_messaging import (
    MessageEnvelope,
    RecoverableMessage,
    RecoveryCheckpoint,
)


def _envelope(**changes: object) -> MessageEnvelope:
    values: dict[str, object] = {
        "channel": "events",
        "identity": Identity(threadId="conversation-1", runId="run-1"),
        "seq": 1,
        "message_id": "run-1:1",
        "codec": "test.bytes.v1",
        "payload": b"payload",
        "created_at": datetime(2026, 8, 14, tzinfo=UTC),
    }
    values.update(changes)
    return MessageEnvelope.model_validate(values)


def test_envelope_preserves_protocol_neutral_bytes() -> None:
    envelope = _envelope(payload=b"\x00\xffpayload")

    assert envelope.payload == b"\x00\xffpayload"
    assert "schema_version" not in envelope.model_fields_set
    assert "schema_version" not in envelope.model_dump(mode="python")
    assert envelope.model_copy(deep=True) == envelope


def test_current_envelopes_reject_removed_version_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        _envelope(schema_version=2)
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RecoveryCheckpoint.model_validate(
            {"schema_version": 1, "position": b"2", "last_message_id": None}
        )


def test_messaging_uses_the_same_identity_type_as_the_framework() -> None:
    assert Identity is AdapterIdentity
    assert MessageEnvelope.model_fields["identity"].annotation is Identity


def test_envelope_rejects_zero_sequence() -> None:
    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        _envelope(seq=0)


@pytest.mark.parametrize(
    "field",
    ["channel", "message_id", "codec"],
)
def test_envelope_rejects_noncanonical_identifiers(field: str) -> None:
    with pytest.raises(ValidationError, match="non-blank"):
        _envelope(**{field: " value "})


def test_envelope_requires_an_aware_utc_timestamp() -> None:
    with pytest.raises(ValidationError, match="aware UTC"):
        _envelope(created_at=datetime(2026, 8, 14))


def test_recovery_checkpoint_rejects_a_blank_last_message_id() -> None:
    with pytest.raises(ValidationError, match="non-blank"):
        RecoveryCheckpoint(position=b"2", last_message_id=" ")


def test_recovery_checkpoint_rejects_an_overlong_last_message_id() -> None:
    with pytest.raises(ValidationError, match="at most 1024"):
        RecoveryCheckpoint(position=b"2", last_message_id="x" * 1025)


def test_public_schemas_publish_identifier_length_limits() -> None:
    envelope_properties = MessageEnvelope.model_json_schema()["properties"]
    assert "schema_version" not in envelope_properties
    for field in ("channel", "message_id", "codec"):
        assert envelope_properties[field]["maxLength"] == 1024

    checkpoint_properties = RecoveryCheckpoint.model_json_schema()["properties"]
    assert "schema_version" not in checkpoint_properties
    checkpoint_schema = checkpoint_properties["last_message_id"]
    assert checkpoint_schema["anyOf"][0]["maxLength"] == 1024


def test_recoverable_message_rejects_a_blank_stable_message_id() -> None:
    checkpoint = RecoveryCheckpoint(position=b"2", last_message_id="message-2")

    with pytest.raises(ValueError, match="non-blank"):
        RecoverableMessage(message_id=" ", data="value", checkpoint=checkpoint)


def test_recoverable_message_rejects_a_checkpoint_for_another_message() -> None:
    checkpoint = RecoveryCheckpoint(position=b"2", last_message_id="message-1")

    with pytest.raises(ValueError, match="last_message_id must match message_id"):
        RecoverableMessage(
            message_id="message-2",
            data="value",
            checkpoint=checkpoint,
        )


@pytest.mark.parametrize(
    "field",
    ["channel", "message_id", "codec"],
)
def test_envelope_rejects_identifiers_over_1024_characters(field: str) -> None:
    with pytest.raises(ValidationError, match="at most 1024"):
        _envelope(**{field: "x" * 1025})


def test_envelope_rejects_unbounded_or_extra_identity_fields() -> None:
    with pytest.raises(ValidationError, match="at most 1024"):
        _envelope(identity=Identity(threadId="x" * 1025, runId="run-1"))
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        _envelope(
            identity={
                "threadId": "conversation-1",
                "runId": "run-1",
                "parentRunId": "parent-1",
            }
        )
