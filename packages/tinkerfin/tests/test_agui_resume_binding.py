"""Public contracts for binding AG-UI resume requests to LangGraph commands."""

from __future__ import annotations

import pytest
from ag_ui.core.types import Interrupt, ResumeEntry
from langgraph.types import Command

from tinkerfin import AgUiResumeBinding, Identity
from tinkerfin_agui_adapter import ResumeMapper, ResumeTranslation, ScopedIdCodec


def _identity(*, run_id: str = "run-resume") -> Identity:
    return Identity(threadId="thread-1", runId=run_id)


def _entries() -> tuple[ResumeEntry, ...]:
    return (
        ResumeEntry.model_validate(
            {
                "interruptId": "interrupt-1",
                "status": "resolved",
                "payload": {"type": "approve"},
            }
        ),
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
                "schema": "tinkerfin.deepagents.tool-review.v1",
                "nativeInterruptId": "interrupt-1",
                "actionIndex": 0,
                "toolName": "write_file",
                "allowedDecisions": ["approve"],
                "originalArgs": {"path": "a"},
            },
        },
    )
    return ResumeMapper().map_agui(
        entries=_entries(),
        interrupts=(interrupt,),
    )


def test_resume_binding_builds_one_pure_command_from_translation() -> None:
    identity = _identity()

    binding = AgUiResumeBinding.from_translation(
        identity=identity,
        translation=_translation(),
    )

    assert binding.identity is identity
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
            identity=_identity(),
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
        AgUiResumeBinding(identity=_identity(), command=command)


def test_resume_binding_rejects_unscoped_tool_id() -> None:
    with pytest.raises(ValueError, match="complete scoped Tool IDs"):
        AgUiResumeBinding(
            identity=_identity(),
            command=Command(resume={"decisions": [{"type": "approve"}]}),
            prior_tool_call_ids=frozenset({"call-1"}),
        )


def test_resume_binding_does_not_expose_mutable_internal_snapshots() -> None:
    binding = AgUiResumeBinding.from_translation(
        identity=_identity(),
        translation=_translation(),
    )

    exposed_command = binding.command
    assert isinstance(exposed_command.resume, dict)
    exposed_command.resume["decisions"] = []

    assert binding.identity == _identity()
    assert binding.command == Command(resume={"decisions": [{"type": "approve"}]})


def test_resume_binding_rejects_a_different_identity_or_command() -> None:
    binding = AgUiResumeBinding.from_translation(
        identity=_identity(),
        translation=_translation(),
    )

    with pytest.raises(ValueError, match="different identity"):
        binding.validate_identity(_identity(run_id="run-other"))
    with pytest.raises(ValueError, match="graph input"):
        binding.validate_command(Command(resume={"decisions": [{"type": "reject"}]}))
