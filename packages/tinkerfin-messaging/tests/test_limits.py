"""Durable payload and thread quota contracts."""

from __future__ import annotations

import pytest

from tinkerfin import RunIdentity
from tinkerfin_messaging import (
    MemoryBackend,
    MessagingLimits,
    MessagingQuotaExceeded,
    RecoveryCheckpoint,
)
from tinkerfin_messaging._messaging_ledger import _MessagingLedger


def _identity() -> RunIdentity:
    return RunIdentity(namespace="test", thread_id="quota-thread", run_id="quota-run")


def test_messaging_limits_reject_invalid_capacity_shapes() -> None:
    with pytest.raises(TypeError, match="max_thread_messages"):
        MessagingLimits(max_thread_messages=True)
    with pytest.raises(ValueError, match="greater than zero"):
        MessagingLimits(max_checkpoint_bytes=0)
    with pytest.raises(ValueError, match="must not be smaller"):
        MessagingLimits(
            max_message_payload_bytes=10,
            max_thread_payload_bytes=9,
        )


async def test_memory_backend_enforces_quota_before_mutation_and_keeps_retry_idempotent() -> (
    None
):
    limits = MessagingLimits(
        max_message_payload_bytes=4,
        max_checkpoint_bytes=2,
        max_thread_messages=2,
        max_thread_payload_bytes=6,
    )
    backend = _MessagingLedger(MemoryBackend(limits=limits))
    prepared = await backend.prepare(
        channel="events",
        identity=_identity(),
        codec="test.bytes.v1",
        after=0,
        cancellable=False,
        recoverable=True,
    )

    with pytest.raises(MessagingQuotaExceeded) as payload_error:
        await backend.append(
            prepared.handle,
            message_id="too-large",
            codec="test.bytes.v1",
            payload=b"12345",
        )
    assert payload_error.value.resource == "message_payload_bytes"
    assert await backend.latest_seq(channel="events", identity=_identity()) == 0

    with pytest.raises(MessagingQuotaExceeded) as checkpoint_error:
        await backend.append(
            prepared.handle,
            message_id="checkpoint-too-large",
            codec="test.bytes.v1",
            payload=b"1",
            checkpoint=RecoveryCheckpoint(
                position=b"123",
                last_message_id="checkpoint-too-large",
            ),
        )
    assert checkpoint_error.value.resource == "checkpoint_bytes"
    assert await backend.latest_seq(channel="events", identity=_identity()) == 0

    first = await backend.append(
        prepared.handle,
        message_id="first",
        codec="test.bytes.v1",
        payload=b"1234",
    )
    await backend.append(
        prepared.handle,
        message_id="second",
        codec="test.bytes.v1",
        payload=b"12",
    )
    assert (
        await backend.append(
            prepared.handle,
            message_id="first",
            codec="test.bytes.v1",
            payload=b"1234",
        )
        == first
    )

    with pytest.raises(MessagingQuotaExceeded) as thread_error:
        await backend.append(
            prepared.handle,
            message_id="third",
            codec="test.bytes.v1",
            payload=b"",
        )
    assert thread_error.value.resource == "thread_messages"
    assert await backend.latest_seq(channel="events", identity=_identity()) == 2
