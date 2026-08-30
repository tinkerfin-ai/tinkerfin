"""Internal canonical fact preparation shared by built-in Trace writers."""

from __future__ import annotations

from dataclasses import dataclass

from .codec import CanonicalTracePayloadCodec
from .facts import TraceSemanticFact


@dataclass(frozen=True, slots=True)
class PreparedTraceFact:
    """Keep one validated fact with the exact canonical bytes used for admission."""

    fact: TraceSemanticFact
    canonical_payload: bytes
    exact_byte_count: int
    payload_digest: str


def prepare_trace_facts(
    facts: tuple[TraceSemanticFact, ...],
    *,
    codec: CanonicalTracePayloadCodec,
) -> tuple[PreparedTraceFact, ...]:
    """Encode each fact once and retain its exact admission and Store evidence."""

    prepared: list[PreparedTraceFact] = []
    for fact in facts:
        encoded = codec.encode_fact(fact)
        prepared.append(
            PreparedTraceFact(
                fact=fact,
                canonical_payload=encoded.data,
                exact_byte_count=len(encoded.data),
                payload_digest=encoded.digest,
            )
        )
    return tuple(prepared)
