"""Validated binding between one AG-UI resume request and LangGraph input."""

from __future__ import annotations

import copy
from dataclasses import dataclass

from ag_ui.core import RunAgentInput
from langgraph.types import Command
from pydantic import JsonValue, TypeAdapter, ValidationError

from tinkerfin_agui_adapter import (
    AgUiLifecycleEventFactory,
    ResumeTranslation,
    ScopedIdCodec,
)

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


@dataclass(frozen=True, slots=True, init=False)
class AgUiResumeBinding:
    """Bind one AG-UI resume request to its native command and prior Tool IDs.

    The binding snapshots validated input and JSON resume data. It owns no Graph,
    checkpointer, or I/O resource, so a host can reconstruct it from trusted persisted
    `resume_data` and `prior_tool_call_ids` for an idempotent retry.
    """

    _run_input: RunAgentInput
    _resume_data: dict[str, JsonValue]
    prior_tool_call_ids: frozenset[str]

    def __init__(
        self,
        *,
        run_input: RunAgentInput,
        command: Command,
        prior_tool_call_ids: frozenset[str] = frozenset(),
    ) -> None:
        AgUiLifecycleEventFactory.validate_run_input(run_input)
        if not run_input.resume:
            raise ValueError("run_input.resume must be non-empty for a resume binding")
        if not isinstance(command, Command):
            raise TypeError("command must be a Command")
        if (
            command.resume is None
            or command.graph is not None
            or command.update is not None
            or command.goto != ()
        ):
            raise ValueError(
                "command must be a pure resume Command without graph, update, or goto"
            )
        try:
            resume_data = _JSON_OBJECT.validate_python(command.resume)
        except ValidationError as error:
            raise ValueError("command must contain JSON object resume data") from error
        if not isinstance(prior_tool_call_ids, frozenset):
            raise TypeError("prior_tool_call_ids must be a frozenset")
        codec = ScopedIdCodec()
        for tool_call_id in prior_tool_call_ids:
            try:
                kind, _namespace, _raw_id = codec.decode(tool_call_id)
            except (TypeError, ValueError) as error:
                raise ValueError(
                    "prior_tool_call_ids must contain complete scoped Tool IDs"
                ) from error
            if kind != "tool":
                raise ValueError(
                    "prior_tool_call_ids must contain complete scoped Tool IDs"
                )
        object.__setattr__(self, "_run_input", run_input.model_copy(deep=True))
        object.__setattr__(self, "_resume_data", copy.deepcopy(resume_data))
        object.__setattr__(self, "prior_tool_call_ids", prior_tool_call_ids)

    @property
    def run_input(self) -> RunAgentInput:
        """Return a defensive copy of the complete bound AG-UI input."""

        return self._run_input.model_copy(deep=True)

    @property
    def command(self) -> Command:
        """Return a defensive copy of the bound pure resume Command."""

        return Command(resume=copy.deepcopy(self._resume_data))

    @classmethod
    def from_translation(
        cls,
        *,
        run_input: RunAgentInput,
        translation: ResumeTranslation,
    ) -> AgUiResumeBinding:
        """Build a binding from one fully resolved stock Deep Agents translation."""

        if not isinstance(translation, ResumeTranslation):
            raise TypeError("translation must be a ResumeTranslation")
        if translation.mode != "command" or translation.resume_data is None:
            raise ValueError("translation must have mode='command'")
        return cls(
            run_input=run_input,
            command=Command(resume=translation.root),
            prior_tool_call_ids=frozenset(translation.prior_tool_call_ids),
        )

    def validate_run_input(self, run_input: RunAgentInput) -> None:
        """Require the exact complete AG-UI request captured by this binding."""

        if run_input != self._run_input:
            raise ValueError("resume binding belongs to a different run_input")

    def validate_command(self, command: object) -> None:
        """Require the exact pure resume Command captured by this binding."""

        if command != Command(resume=self._resume_data):
            raise ValueError("graph input must equal the resume binding command")
