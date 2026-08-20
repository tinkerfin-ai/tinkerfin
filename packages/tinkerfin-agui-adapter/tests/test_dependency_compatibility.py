"""Compatibility and dependency-boundary checks for the adapter package."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from ag_ui.core import RawEvent, StateSnapshotEvent, TextMessageContentEvent
from deepagents.middleware.subagents import TaskToolSchema
from langchain_core.messages import AIMessageChunk
from langgraph.types import Interrupt
from pydantic import ValidationError

from tinkerfin_agui_adapter import DeepAgentAgUiAdapter, Identity
from tinkerfin_agui_adapter.subagent import SubagentTaskInput


def test_subagent_task_input_matches_locked_deepagents_required_fields() -> None:
    """The runtime projection accepts the locked task tool's required input."""

    payload = {
        "description": "Inspect the repository and report the public API.",
        "subagent_type": "researcher",
    }

    native = TaskToolSchema.model_validate(payload)
    projected = SubagentTaskInput.model_validate(payload)

    assert set(TaskToolSchema.model_fields) == {"description", "subagent_type"}
    assert set(SubagentTaskInput.model_fields) == set(TaskToolSchema.model_fields)
    assert native.model_dump() == projected.model_dump() == payload


@pytest.mark.parametrize("missing_field", ["description", "subagent_type"])
def test_subagent_task_input_rejects_each_required_field_like_deepagents(
    missing_field: str,
) -> None:
    payload = {
        "description": "Inspect the repository and report the public API.",
        "subagent_type": "researcher",
    }
    del payload[missing_field]

    with pytest.raises(ValidationError):
        TaskToolSchema.model_validate(payload)
    with pytest.raises(ValidationError):
        SubagentTaskInput.model_validate(payload)


def test_adapter_accepts_locked_v2_messages_tasks_and_values_envelopes() -> None:
    """Exercise documented v2 envelope shapes using installed framework objects."""

    adapter = DeepAgentAgUiAdapter(
        identity=Identity(threadId="thread-1", runId="run-1")
    )
    task_input = TaskToolSchema(
        description="Inspect the repository and report the public API.",
        subagent_type="researcher",
    )
    messages = adapter.process(
        {
            "type": "messages",
            "ns": (),
            "data": (
                AIMessageChunk(id="message-1", content="Hello", chunk_position="last"),
                {"langgraph_node": "model", "lc_agent_name": None},
            ),
        }
    )
    tasks = adapter.process(
        {
            "type": "tasks",
            "ns": (),
            "data": {
                "id": "task-1",
                "name": "tools",
                "input": [
                    {
                        "name": "task",
                        "args": task_input.model_dump(),
                        "id": "tool-call-1",
                        "type": "tool_call",
                    }
                ],
                "triggers": ("branch:to:tools",),
            },
        }
    )
    values = adapter.process(
        {
            "type": "values",
            "ns": (),
            "data": {"todos": []},
            "interrupts": (Interrupt(value={"kind": "review"}, id="interrupt-1"),),
        }
    )

    assert any(isinstance(event, TextMessageContentEvent) for event in messages)
    assert len(tasks) == 1 and isinstance(tasks[0], RawEvent)
    assert any(isinstance(event, StateSnapshotEvent) for event in values)


def _imported_roots(source_root: Path) -> set[str]:
    imported: set[str] = set()
    for source_file in source_root.rglob("*.py"):
        tree = ast.parse(
            source_file.read_text(encoding="utf-8"), filename=str(source_file)
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".", 1)[0])
    return imported


def test_adapter_source_does_not_import_integration_or_framework_packages() -> None:
    source_root = Path(__file__).parents[1] / "src"
    forbidden_roots = frozenset(
        {"tinkerfin", "deepagents", "langgraph", "langchain", "langsmith"}
    )

    assert source_root.is_dir()
    assert not (_imported_roots(source_root) & forbidden_roots)
