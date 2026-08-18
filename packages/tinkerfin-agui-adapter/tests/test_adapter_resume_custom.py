from __future__ import annotations

import pytest
from ag_ui.core.types import ResumeEntry
from langchain_core.messages import AIMessage

from tinkerfin_agui_adapter import ResumeMapper, ResumeMappingError
from tinkerfin_agui_adapter.ids import ScopedIdCodec
from tinkerfin_agui_adapter.models import AgentRuntimeInterrupt


def _root_tool_id(raw_id: str) -> str:
    return ScopedIdCodec().encode("tool", (), raw_id)


def test_mixed_resolved_and_cancelled_resume_preserves_every_native_slot() -> None:
    interrupt = AgentRuntimeInterrupt(
        id="root",
        value={
            "action_requests": [
                {"name": "write_file", "args": {"path": "a"}},
                {"name": "write_file", "args": {"path": "b"}},
            ],
            "review_configs": [
                {"action_name": "write_file", "allowed_decisions": ["approve"]},
                {"action_name": "write_file", "allowed_decisions": ["approve"]},
            ],
        },
    )

    translation = ResumeMapper().map(
        entries=(
            ResumeEntry(interrupt_id="root#1", status="cancelled"),
            ResumeEntry(
                interrupt_id="root#0",
                status="resolved",
                payload={"type": "approve"},
            ),
        ),
        interrupts=(interrupt,),
        messages_by_namespace={
            (): (
                AIMessage(
                    id="message-root",
                    content="",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {"path": "a"},
                            "id": "call-a",
                            "type": "tool_call",
                        },
                        {
                            "name": "write_file",
                            "args": {"path": "b"},
                            "id": "call-b",
                            "type": "tool_call",
                        },
                    ],
                ),
            )
        },
    )

    assert translation.mode == "custom"
    assert translation.resume_data is None
    assert translation.decisions_by_interrupt == {"root": ({"type": "approve"}, None)}
    assert translation.cancelled_interrupt_ids == ("root#1",)
    assert translation.prior_tool_call_ids == (_root_tool_id("call-a"),)


def test_mixed_resume_uses_cancelled_actions_to_disambiguate_resolved_calls() -> None:
    interrupt = AgentRuntimeInterrupt(
        id="root",
        value={
            "action_requests": [
                {"name": "write_file", "args": {"path": "a"}},
                {"name": "write_file", "args": {"path": "b"}},
            ],
            "review_configs": [
                {"action_name": "write_file", "allowed_decisions": ["approve"]},
                {"action_name": "write_file", "allowed_decisions": ["approve"]},
            ],
        },
    )

    translation = ResumeMapper().map(
        entries=(
            ResumeEntry(interrupt_id="root#0", status="cancelled"),
            ResumeEntry(
                interrupt_id="root#1",
                status="resolved",
                payload={"type": "approve"},
            ),
        ),
        interrupts=(interrupt,),
        messages_by_namespace={
            (): (
                AIMessage(
                    id="message-root",
                    content="",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {"path": "b"},
                            "id": "call-b-unrelated",
                            "type": "tool_call",
                        },
                        {
                            "name": "write_file",
                            "args": {"path": "a"},
                            "id": "call-a-reviewed",
                            "type": "tool_call",
                        },
                        {
                            "name": "write_file",
                            "args": {"path": "b"},
                            "id": "call-b-reviewed",
                            "type": "tool_call",
                        },
                    ],
                ),
            )
        },
    )

    assert translation.mode == "custom"
    assert translation.cancelled_interrupt_ids == ("root#0",)
    assert translation.prior_tool_call_ids == (_root_tool_id("call-b-reviewed"),)


def test_mixed_resume_collapses_ambiguity_confined_to_cancelled_calls() -> None:
    interrupt = AgentRuntimeInterrupt(
        id="root",
        value={
            "action_requests": [
                {"name": "write_file", "args": {"path": "a"}},
                {"name": "write_file", "args": {"path": "b"}},
            ],
            "review_configs": [
                {"action_name": "write_file", "allowed_decisions": ["approve"]},
                {"action_name": "write_file", "allowed_decisions": ["approve"]},
            ],
        },
    )

    translation = ResumeMapper().map(
        entries=(
            ResumeEntry(interrupt_id="root#0", status="cancelled"),
            ResumeEntry(
                interrupt_id="root#1",
                status="resolved",
                payload={"type": "approve"},
            ),
        ),
        interrupts=(interrupt,),
        messages_by_namespace={
            (): (
                AIMessage(
                    id="message-root",
                    content="",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {"path": "a"},
                            "id": "call-a-first",
                            "type": "tool_call",
                        },
                        {
                            "name": "write_file",
                            "args": {"path": "a"},
                            "id": "call-a-second",
                            "type": "tool_call",
                        },
                        {
                            "name": "write_file",
                            "args": {"path": "b"},
                            "id": "call-b-reviewed",
                            "type": "tool_call",
                        },
                    ],
                ),
            )
        },
    )

    assert translation.mode == "custom"
    assert translation.cancelled_interrupt_ids == ("root#0",)
    assert translation.prior_tool_call_ids == (_root_tool_id("call-b-reviewed"),)


def test_mixed_resume_rejects_distinct_resolved_call_projections() -> None:
    interrupt = AgentRuntimeInterrupt(
        id="root",
        value={
            "action_requests": [
                {"name": "write_file", "args": {"path": "b"}},
                {"name": "write_file", "args": {"path": "a"}},
            ],
            "review_configs": [
                {"action_name": "write_file", "allowed_decisions": ["approve"]},
                {"action_name": "write_file", "allowed_decisions": ["approve"]},
            ],
        },
    )

    with pytest.raises(ResumeMappingError, match="cannot be correlated"):
        ResumeMapper().map(
            entries=(
                ResumeEntry(
                    interrupt_id="root#0",
                    status="resolved",
                    payload={"type": "approve"},
                ),
                ResumeEntry(interrupt_id="root#1", status="cancelled"),
            ),
            interrupts=(interrupt,),
            messages_by_namespace={
                (): (
                    AIMessage(
                        id="message-root",
                        content="",
                        tool_calls=[
                            {
                                "name": "write_file",
                                "args": {"path": "b"},
                                "id": "call-b-first",
                                "type": "tool_call",
                            },
                            {
                                "name": "write_file",
                                "args": {"path": "b"},
                                "id": "call-b-second",
                                "type": "tool_call",
                            },
                            {
                                "name": "write_file",
                                "args": {"path": "a"},
                                "id": "call-a-first",
                                "type": "tool_call",
                            },
                            {
                                "name": "write_file",
                                "args": {"path": "a"},
                                "id": "call-a-second",
                                "type": "tool_call",
                            },
                        ],
                    ),
                )
            },
        )


def test_multi_group_resume_bounds_dead_end_correlation_search() -> None:
    repeated_action_count = 18
    repeated_candidate_count = 17
    repeated_interrupt = AgentRuntimeInterrupt(
        id="repeated",
        value={
            "action_requests": [
                {"name": "write_file", "args": {"path": "same"}}
                for _ in range(repeated_action_count)
            ],
            "review_configs": [
                {"action_name": "write_file", "allowed_decisions": ["approve"]}
                for _ in range(repeated_action_count)
            ],
        },
    )
    second_interrupt = AgentRuntimeInterrupt(
        id="second",
        value={
            "action_requests": [{"name": "read_file", "args": {"path": "b"}}],
            "review_configs": [
                {"action_name": "read_file", "allowed_decisions": ["approve"]}
            ],
        },
    )
    entries = tuple(
        ResumeEntry(
            interrupt_id=f"repeated#{index}",
            status="resolved",
            payload={"type": "approve"},
        )
        for index in range(repeated_action_count)
    ) + (
        ResumeEntry(
            interrupt_id="second",
            status="resolved",
            payload={"type": "approve"},
        ),
    )
    messages = (
        AIMessage(
            id="message-root",
            content="",
            tool_calls=[
                {
                    "name": "write_file",
                    "args": {"path": "same"},
                    "id": f"call-write-{index}",
                    "type": "tool_call",
                }
                for index in range(repeated_candidate_count)
            ]
            + [
                {
                    "name": "read_file",
                    "args": {"path": "b"},
                    "id": "call-read",
                    "type": "tool_call",
                }
            ],
        ),
    )

    with pytest.raises(ResumeMappingError) as raised:
        ResumeMapper().map(
            entries=entries,
            interrupts=(repeated_interrupt, second_interrupt),
            messages_by_namespace={(): messages},
        )

    assert raised.value.__cause__ is not None
    assert "safe search budget" in str(raised.value.__cause__)
