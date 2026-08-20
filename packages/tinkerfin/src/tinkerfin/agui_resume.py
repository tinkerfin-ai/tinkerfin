"""Validated binding between one run identity and LangGraph resume input."""

from __future__ import annotations

import copy
from dataclasses import dataclass

from langgraph.types import Command
from pydantic import JsonValue, TypeAdapter, ValidationError

from tinkerfin_agui_adapter import (
    Identity,
    ResumeTranslation,
    ScopedIdCodec,
)

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


@dataclass(frozen=True, slots=True, init=False)
class AgUiResumeBinding:
    """Bind one run identity to its native command and prior Tool IDs.

    The binding snapshots validated input and JSON resume data. It owns no Graph,
    checkpointer, or I/O resource, so a host can reconstruct it from trusted persisted
    `resume_data` and `prior_tool_call_ids` for an idempotent retry.
    """

    identity: Identity
    _resume_data: dict[str, JsonValue]
    prior_tool_call_ids: frozenset[str]

    def __init__(
        self,
        *,
        identity: Identity,
        command: Command,
        prior_tool_call_ids: frozenset[str] = frozenset(),
    ) -> None:
        if not isinstance(identity, Identity):
            raise TypeError("identity must be an Identity")
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
        object.__setattr__(self, "identity", identity)
        object.__setattr__(self, "_resume_data", copy.deepcopy(resume_data))
        object.__setattr__(self, "prior_tool_call_ids", prior_tool_call_ids)

    @property
    def command(self) -> Command:
        """Return a defensive copy of the bound pure resume Command."""

        return Command(resume=copy.deepcopy(self._resume_data))

    @classmethod
    def from_translation(
        cls,
        *,
        identity: Identity,
        translation: ResumeTranslation,
    ) -> AgUiResumeBinding:
        """Build a binding from one fully resolved stock Deep Agents translation."""

        if not isinstance(translation, ResumeTranslation):
            raise TypeError("translation must be a ResumeTranslation")
        if translation.mode != "command" or translation.resume_data is None:
            raise ValueError("translation must have mode='command'")
        return cls(
            identity=identity,
            command=Command(resume=translation.root),
            prior_tool_call_ids=frozenset(translation.prior_tool_call_ids),
        )

    def validate_identity(self, identity: Identity) -> None:
        """Require the exact thread and run identity captured by this binding."""

        if identity != self.identity:
            raise ValueError("resume binding belongs to a different identity")

    def validate_command(self, command: object) -> None:
        """Require the exact pure resume Command captured by this binding."""

        if command != Command(resume=self._resume_data):
            raise ValueError("graph input must equal the resume binding command")
