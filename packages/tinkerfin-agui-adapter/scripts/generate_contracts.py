"""Generate checked-in cross-language JSON contracts from public models."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tinkerfin_agui_adapter import (
    SUBAGENT_PROVENANCE_SCHEMA,
    TOOL_REVIEW_SCHEMA,
    Identity,
    ScopedIdCodec,
    SubagentProvenance,
    ToolReviewInterruptMetadata,
    create_subagent_provenance,
)
from tinkerfin_agui_adapter.models import JsonObject

_SCRIPT_PATH = Path(__file__).resolve()
_PACKAGE_ROOT = _SCRIPT_PATH.parents[1]
_REPOSITORY_ROOT = _SCRIPT_PATH.parents[3]
_CONTRACT_ROOT = _PACKAGE_ROOT / "src" / "tinkerfin_agui_adapter" / "contracts"
_WEB_CONTRACT_ROOT = (
    _REPOSITORY_ROOT
    / "apps"
    / "studio"
    / "web"
    / "src"
    / "features"
    / "conversation"
    / "agui"
    / "contracts"
)


def _serialized(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _artifacts() -> dict[Path, str]:
    tool_schema = ToolReviewInterruptMetadata.model_json_schema(by_alias=True)
    tool_schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    tool_schema["$id"] = (
        "https://github.com/tinkerfin-ai/tinkerfin/blob/main/"
        "packages/tinkerfin-agui-adapter/src/tinkerfin_agui_adapter/"
        "contracts/tool-review.schema.json"
    )
    tool_fixture = ToolReviewInterruptMetadata(
        schema=TOOL_REVIEW_SCHEMA,
        nativeInterruptId="fixture-native-interrupt",
        actionIndex=0,
        toolName="write_file",
        allowedDecisions=("approve", "edit", "reject", "respond"),
        originalArgs=JsonObject(
            {
                "file_path": "/workspace/report.md",
                "content": "fixture",
            }
        ),
    ).model_dump(mode="json", by_alias=True)
    tool_fixture_text = _serialized(tool_fixture)
    subagent_schema = SubagentProvenance.model_json_schema(by_alias=True)
    subagent_schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    subagent_schema["$id"] = (
        "https://github.com/tinkerfin-ai/tinkerfin/blob/main/"
        "packages/tinkerfin-agui-adapter/src/tinkerfin_agui_adapter/"
        "contracts/subagent-provenance.schema.json"
    )
    parent_namespace = ("tools:parent",)
    parent_tool_call_id = ScopedIdCodec().encode("tool", parent_namespace, "call-task")
    subagent_fixture = create_subagent_provenance(
        identity=Identity(threadId="thread-known", runId="run-known"),
        namespace=(*parent_namespace, "tools:graph-task"),
        parent_namespace=parent_namespace,
        graph_task_id="graph-task",
        agent_name="researcher",
        parent_tool_call_id=parent_tool_call_id,
        description="Research the requested topic",
    ).model_dump(mode="json", by_alias=True)
    assert subagent_fixture["schema"] == SUBAGENT_PROVENANCE_SCHEMA
    subagent_fixture_text = _serialized(subagent_fixture)
    return {
        _CONTRACT_ROOT / "tool-review.schema.json": _serialized(tool_schema),
        _CONTRACT_ROOT / "tool-review.fixture.json": tool_fixture_text,
        _WEB_CONTRACT_ROOT / "tool-review.fixture.json": tool_fixture_text,
        _CONTRACT_ROOT / "subagent-provenance.schema.json": _serialized(
            subagent_schema
        ),
        _CONTRACT_ROOT / "subagent-provenance.fixture.json": subagent_fixture_text,
        _WEB_CONTRACT_ROOT / "subagent-provenance.fixture.json": subagent_fixture_text,
    }


def _check(artifacts: dict[Path, str]) -> int:
    stale = [
        path
        for path, expected in artifacts.items()
        if not path.exists() or path.read_text(encoding="utf-8") != expected
    ]
    if stale:
        for path in stale:
            print(f"stale generated contract: {path.relative_to(_REPOSITORY_ROOT)}")
        return 1
    return 0


def _write(artifacts: dict[Path, str]) -> None:
    for path, content in artifacts.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail when checked-in contract artifacts differ from the public model.",
    )
    arguments = parser.parse_args()
    artifacts = _artifacts()
    if arguments.check:
        return _check(artifacts)
    _write(artifacts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
