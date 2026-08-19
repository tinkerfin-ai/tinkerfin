"""Public contracts for binding AG-UI resume requests to LangGraph commands."""

from __future__ import annotations

import pytest
from ag_ui.core import RunAgentInput
from ag_ui.core.types import Interrupt
from langgraph.types import Command

from tinkerfin import AgUiResumeBinding
from tinkerfin_agui_adapter import ResumeMapper, ResumeTranslation, ScopedIdCodec


def _run_input() -> RunAgentInput:
    return RunAgentInput.model_validate(
        {
            "threadId": "thread-1",
            "runId": "run-resume",
            "parentRunId": "run-interrupted",
            "state": {"pending": True},
            "messages": [],
            "tools": [],
            "context": [],
            "forwardedProps": {"model": "test-model"},
            "resume": [
                {
                    "interruptId": "interrupt-1",
                    "status": "resolved",
                    "payload": {"type": "approve"},
                }
            ],
        }
    )


def _translation() -> ResumeTranslation:
    tool_id = ScopedIdCodec().encode("tool", (), "call-1")
    interrupt = Interrupt(
        id="interrupt-1",
        reason="tool_call",
        tool_call_id=tool_id,
        metadata={
            "langgraphValue": {
                "action_requests": [{"name": "write_file", "args": {"path": "a"}}],
                "review_configs": [
                    {
                        "action_name": "write_file",
                        "allowed_decisions": ["approve"],
                    }
                ],
            },
            "deepagents": {
                "nativeInterruptId": "interrupt-1",
                "actionIndex": 0,
                "toolName": "write_file",
                "allowedDecisions": ["approve"],
                "originalArgs": {"path": "a"},
            },
        },
    )
    run_input = _run_input()
    return ResumeMapper().map_agui(
        entries=run_input.resume or (),
        interrupts=(interrupt,),
    )


def test_resume_binding_builds_one_pure_command_from_translation() -> None:
    run_input = _run_input()

    binding = AgUiResumeBinding.from_translation(
        run_input=run_input,
        translation=_translation(),
    )

    assert binding.run_input == run_input
    assert binding.command == Command(resume={"decisions": [{"type": "approve"}]})
    assert binding.prior_tool_call_ids == frozenset(
        {ScopedIdCodec().encode("tool", (), "call-1")}
    )


@pytest.mark.parametrize(
    "translation",
    [
        ResumeTranslation(mode="abandon", resume_data=None),
        ResumeTranslation(mode="custom", resume_data=None),
    ],
)
def test_resume_binding_rejects_non_command_translation(
    translation: ResumeTranslation,
) -> None:
    with pytest.raises(ValueError, match="mode='command'"):
        AgUiResumeBinding.from_translation(
            run_input=_run_input(),
            translation=translation,
        )


@pytest.mark.parametrize(
    "command",
    [
        Command(),
        Command(resume={"decisions": []}, graph="__parent__"),
        Command(resume={"decisions": []}, update={}),
        Command(resume={"decisions": []}, goto="model"),
    ],
)
def test_resume_binding_rejects_non_resume_command(command: Command) -> None:
    with pytest.raises(ValueError, match="pure resume Command"):
        AgUiResumeBinding(run_input=_run_input(), command=command)


def test_resume_binding_rejects_unscoped_tool_id() -> None:
    with pytest.raises(ValueError, match="complete scoped Tool IDs"):
        AgUiResumeBinding(
            run_input=_run_input(),
            command=Command(resume={"decisions": [{"type": "approve"}]}),
            prior_tool_call_ids=frozenset({"call-1"}),
        )


def test_resume_binding_does_not_expose_mutable_internal_snapshots() -> None:
    binding = AgUiResumeBinding.from_translation(
        run_input=_run_input(),
        translation=_translation(),
    )

    exposed_input = binding.run_input
    exposed_input.run_id = "tampered"
    exposed_command = binding.command
    assert isinstance(exposed_command.resume, dict)
    exposed_command.resume["decisions"] = []

    assert binding.run_input.run_id == "run-resume"
    assert binding.command == Command(resume={"decisions": [{"type": "approve"}]})
