from typing import cast

from ag_ui.core import BaseEvent, Event
from pydantic import TypeAdapter

from tinkerfin_studio.conversation.subagent_events import SubagentRunEventEnricher

_EVENT_ADAPTER = TypeAdapter(Event)


def _event(value: dict[str, object]) -> BaseEvent:
    return cast(BaseEvent, _EVENT_ADAPTER.validate_python(value))


def test_enricher_assigns_server_run_ids_from_complete_subagent_namespaces() -> None:
    """并行子 Agent 必须按完整 namespace 获得不同的服务端 run ID"""

    enricher = SubagentRunEventEnricher(
        user_id=7,
        thread_id="thread-1",
        main_run_id="run-main",
    )
    started = enricher(
        _event(
            {
                "type": "RAW",
                "source": "langgraph.tasks",
                "rawEvent": {"type": "tasks", "phase": "start", "ns": []},
                "event": {
                    "data": {"id": "graph-task", "name": "tools"},
                    "provenance": {
                        "kind": "root",
                        "namespace": [],
                        "agentType": "main",
                        "agentName": "main",
                        "subagents": [
                            {
                                "namespace": ["tools:graph-task:0"],
                                "graphTaskId": "graph-task",
                                "agentName": "researcher",
                                "parentToolCallId": "tool-task-a",
                                "description": "研究 A",
                            },
                            {
                                "namespace": ["tools:graph-task:1"],
                                "graphTaskId": "graph-task",
                                "agentName": "researcher",
                                "parentToolCallId": "tool-task-b",
                                "description": "研究 B",
                            },
                        ],
                    },
                },
            }
        )
    )

    public_event = cast(
        dict[str, object], started.model_dump(mode="json", by_alias=True)
    )
    wrapped = cast(dict[str, object], public_event["event"])
    provenance = cast(dict[str, object], wrapped["provenance"])
    subagents = cast(list[dict[str, object]], provenance["subagents"])
    run_ids = [str(value["runId"]) for value in subagents]
    assert len(set(run_ids)) == 2
    assert all(run_id.startswith("subrun-") for run_id in run_ids)
    assert all(value["parentAgentRunId"] == "run-main" for value in subagents)

    child = enricher(
        _event(
            {
                "type": "TOOL_CALL_START",
                "toolCallId": "tool-search-a",
                "toolCallName": "web_search",
                "rawEvent": {
                    "streamMode": "messages",
                    "runId": "run-main",
                    "source": {
                        "kind": "deep_agent_subagent",
                        "namespace": ["tools:graph-task:0"],
                        "agentType": "subagent",
                        "agentName": "researcher",
                        "graphTaskId": "graph-task",
                        "parentToolCallId": "tool-task-a",
                        "subagentInput": "研究 A",
                    },
                },
            }
        )
    )
    child_raw = cast(
        dict[str, object], child.model_dump(mode="json", by_alias=True)["rawEvent"]
    )
    assert child_raw["runId"] == run_ids[0]
    assert child_raw["parentAgentRunId"] == "run-main"

    task_result = enricher(
        _event(
            {
                "type": "TOOL_CALL_RESULT",
                "toolCallId": "tool-task-a",
                "messageId": "message-task-a",
                "content": "研究完成",
                "role": "tool",
                "rawEvent": {
                    "streamMode": "messages",
                    "runId": "run-main",
                    "relatedNamespace": ["tools:graph-task:0"],
                    "toolResultStatus": "success",
                    "source": {
                        "kind": "root",
                        "namespace": [],
                        "agentType": "main",
                        "agentName": "main",
                    },
                },
            }
        )
    )
    result_raw = cast(
        dict[str, object],
        task_result.model_dump(mode="json", by_alias=True)["rawEvent"],
    )
    assert result_raw["relatedRunId"] == run_ids[0]

    repeated = SubagentRunEventEnricher(
        user_id=7,
        thread_id="thread-1",
        main_run_id="run-resume",
    )(
        _event(
            {
                "type": "RAW",
                "source": "langgraph.tasks",
                "rawEvent": {"type": "tasks", "phase": "start", "ns": []},
                "event": wrapped,
            }
        )
    )
    repeated_event = cast(
        dict[str, object], repeated.model_dump(mode="json", by_alias=True)["event"]
    )
    repeated_provenance = cast(dict[str, object], repeated_event["provenance"])
    repeated_subagents = cast(list[dict[str, object]], repeated_provenance["subagents"])
    assert [value["runId"] for value in repeated_subagents] == run_ids
