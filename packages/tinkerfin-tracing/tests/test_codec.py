"""Canonical Trace payload round trips, digests, and corruption rejection."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tinkerfin_contracts import RunIdentity
from tinkerfin_tracing import CanonicalTracePayloadCodec, RunFact
from tinkerfin_tracing.errors import TraceStoreProtocolError


def _fact() -> RunFact:
    return RunFact(
        source_observation_id="codec-observation",
        identity=RunIdentity(
            namespace="test", thread_id="codec-thread", run_id="codec-run"
        ),
        occurred_at=datetime.now(UTC),
        monotonic_ns=1,
        phase="started",
        input_kind="ordinary",
    )


def test_fact_round_trip_is_deterministic_and_digest_covers_canonical_bytes() -> None:
    codec = CanonicalTracePayloadCodec()
    fact = _fact()

    first = codec.encode_fact(fact)
    second = codec.encode_fact(fact.model_copy(deep=True))

    assert first == second
    assert codec.digest(first.data) == first.digest
    assert codec.decode_fact(first.data) == fact


def test_json_key_order_does_not_change_canonical_bytes() -> None:
    codec = CanonicalTracePayloadCodec()

    first = codec.encode_json({"b": 2, "a": {"d": 4, "c": 3}})
    second = codec.encode_json({"a": {"c": 3, "d": 4}, "b": 2})

    assert first == second
    assert first.data == b'{"a":{"c":3,"d":4},"b":2}'


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"not-json",
        b'{"value":NaN}',
        b'{"kind":"unknown"}',
    ],
)
def test_invalid_or_corrupt_payloads_fail_closed(payload: bytes) -> None:
    codec = CanonicalTracePayloadCodec()

    with pytest.raises((TypeError, ValueError, TraceStoreProtocolError)):
        if b'"kind"' in payload:
            codec.decode_fact(payload)
        else:
            codec.decode_json(payload)


def test_non_finite_input_is_rejected_before_digest() -> None:
    codec = CanonicalTracePayloadCodec()

    with pytest.raises(TraceStoreProtocolError):
        codec.encode_json({"value": float("nan")})


@pytest.mark.parametrize(
    "payload",
    [
        b'{ "a":1}',
        b'{"b":2,"a":1}',
        b'{"text":"\\u4f60\\u597d"}',
    ],
)
def test_valid_but_noncanonical_json_is_rejected(payload: bytes) -> None:
    codec = CanonicalTracePayloadCodec()

    with pytest.raises(TraceStoreProtocolError, match="canonical"):
        codec.decode_json(payload)


def test_decoded_json_is_a_fresh_defensive_graph() -> None:
    codec = CanonicalTracePayloadCodec()
    encoded = codec.encode_json({"nested": {"value": "original"}})

    first = codec.decode_json(encoded.data)
    second = codec.decode_json(encoded.data)
    assert isinstance(first, dict) and isinstance(second, dict)
    nested = first["nested"]
    assert isinstance(nested, dict)
    nested["value"] = "changed"

    assert second == {"nested": {"value": "original"}}
