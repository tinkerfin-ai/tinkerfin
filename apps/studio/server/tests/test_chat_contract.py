import pytest
from ag_ui.core import RunAgentInput
from pydantic import ValidationError

from tinkerfin_studio.api.errors import BusinessException
from tinkerfin_studio.application import create_application
from tinkerfin_studio.conversation.request import ChatRequest
from tinkerfin_studio.conversation.run_preparation import (
    StartChatIntent,
    classify_intent,
    prepare_run_request,
)
from tinkerfin_studio.conversation.service import parse_last_event_id


def test_chat_request_preserves_command_extensions_and_derives_plan_mode() -> None:
    """command 扩展应完整保留，plan 状态只在运行准备边界解释"""

    request = ChatRequest.from_agui(
        RunAgentInput.model_validate(
            {
                "threadId": "",
                "runId": "run-1",
                "state": {},
                "messages": [
                    {"id": "client-request-1", "role": "user", "content": "执行任务"}
                ],
                "tools": [],
                "context": [],
                "forwardedProps": {
                    "model": "main",
                    "command": {"plan": "on", "compact": "保留这段命令输入"},
                    "trace": "x",
                },
            }
        )
    )

    normalized = request.normalized(
        thread_id="thread-1",
        message_ids=("message-server-1",),
    )

    payload = normalized.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert payload["threadId"] == "thread-1"
    assert payload["messages"] == [
        {
            "id": "message-server-1",
            "role": "user",
            "content": "执行任务",
        }
    ]
    assert payload["forwardedProps"] == {
        "model": "main",
        "command": {"plan": "on", "compact": "保留这段命令输入"},
        "trace": "x",
    }
    prepared = prepare_run_request(request, user_id=7, thread_id="thread-1")
    assert prepared.mode == "plan"


@pytest.mark.parametrize(
    "forwarded_props",
    (
        {"model": "main", "mode": "plan"},
        {"model": "main", "command": {"plan": "invalid"}},
        {"model": "main", "command": {}},
    ),
)
def test_chat_request_rejects_removed_or_invalid_plan_commands(
    forwarded_props: dict[str, object],
) -> None:
    """当前请求必须只使用精确的 command.plan 契约"""

    with pytest.raises(ValidationError):
        ChatRequest.from_agui(
            RunAgentInput.model_validate(
                {
                    "threadId": "",
                    "runId": "run-invalid-command",
                    "state": {},
                    "messages": [],
                    "tools": [],
                    "context": [],
                    "forwardedProps": forwarded_props,
                }
            )
        )


def test_chat_request_drops_the_protocol_message_id() -> None:
    """HTTP 要求客户端 ID，但业务快照只使用服务端权威 ID"""

    request = ChatRequest.from_agui(
        RunAgentInput.model_validate(
            {
                "threadId": "",
                "runId": "run-1",
                "state": {},
                "messages": [
                    {"id": "client-message-1", "role": "user", "content": "执行任务"}
                ],
                "tools": [],
                "context": [],
                "forwardedProps": {"model": "main", "command": {"plan": "off"}},
            }
        )
    )

    assert request.messages == [
        {"role": "user", "content": "执行任务", "name": None, "encryptedValue": None}
    ]
    normalized = request.normalized(
        thread_id="thread-1",
        message_ids=("message-server-1",),
    )
    assert normalized.messages[0].id == "message-server-1"


def test_from_agui_preserves_standard_roles_multimodal_content_and_extensions() -> None:
    protocol_input = RunAgentInput.model_validate(
        {
            "threadId": "thread-1",
            "runId": "run-roles",
            "parentRunId": "run-parent",
            "state": {"draft": True},
            "messages": [
                {"id": "developer-1", "role": "developer", "content": "规则"},
                {"id": "system-1", "role": "system", "content": "系统"},
                {
                    "id": "assistant-1",
                    "role": "assistant",
                    "content": "调用工具",
                    "toolCalls": [
                        {
                            "id": "call-1",
                            "function": {"name": "lookup", "arguments": "{}"},
                        }
                    ],
                },
                {
                    "id": "user-1",
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "分析附件"},
                        {
                            "type": "image",
                            "source": {
                                "type": "url",
                                "value": "https://example.test/chart.png",
                                "mimeType": "image/png",
                            },
                            "metadata": {"alt": "图表"},
                        },
                        {
                            "type": "document",
                            "source": {
                                "type": "data",
                                "value": "cGRm",
                                "mimeType": "application/pdf",
                            },
                        },
                    ],
                },
                {
                    "id": "tool-1",
                    "role": "tool",
                    "toolCallId": "call-1",
                    "content": "完成",
                },
                {
                    "id": "activity-1",
                    "role": "activity",
                    "activityType": "progress",
                    "content": {"percent": 50},
                },
                {"id": "reasoning-1", "role": "reasoning", "content": "思考"},
            ],
            "tools": [
                {
                    "name": "client_tool",
                    "description": "客户端声明",
                    "parameters": {"type": "object"},
                    "vendor": "kept",
                }
            ],
            "context": [{"description": "tenant", "value": "acme", "vendor": "kept"}],
            "forwardedProps": {
                "model": "main",
                "command": {"plan": "off"},
                "trace": {"sampled": True},
            },
        }
    )

    request = ChatRequest.from_agui(protocol_input)
    normalized = request.normalized(
        thread_id="thread-1",
        message_ids=tuple(f"server-{index}" for index in range(7)),
    )

    assert [message["role"] for message in request.messages] == [
        "developer",
        "system",
        "assistant",
        "user",
        "tool",
        "activity",
        "reasoning",
    ]
    assert all("id" not in message for message in request.messages)
    assert normalized.messages[3].content == protocol_input.messages[3].content
    assert normalized.forwarded_props["trace"] == {"sampled": True}
    assert normalized.tools[0].model_extra == {"vendor": "kept"}
    assert normalized.context[0].model_extra == {"vendor": "kept"}


def test_multimodal_start_maps_only_the_selected_user_input_to_graph() -> None:
    request = ChatRequest.from_agui(
        RunAgentInput.model_validate(
            {
                "threadId": "",
                "runId": "run-multimodal",
                "state": {},
                "messages": [
                    {"id": "system-1", "role": "system", "content": "不注入"},
                    {
                        "id": "user-1",
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "分析附件"},
                            {
                                "type": "image",
                                "source": {
                                    "type": "url",
                                    "value": "https://example.test/chart.png",
                                    "mimeType": "image/png",
                                },
                            },
                        ],
                    },
                ],
                "tools": [],
                "context": [],
                "forwardedProps": {"model": "main", "command": {"plan": "off"}},
            }
        )
    )

    intent = classify_intent(request)

    assert isinstance(intent, StartChatIntent)
    assert intent.message_index == 1
    assert intent.title == "分析附件"
    assert intent.graph_message.content == [
        {"type": "text", "text": "分析附件"},
        {
            "type": "image",
            "url": "https://example.test/chart.png",
            "mime_type": "image/png",
        },
    ]


def test_client_message_id_does_not_change_the_canonical_business_snapshot() -> None:
    def request(client_id: str) -> ChatRequest:
        return ChatRequest.from_agui(
            RunAgentInput.model_validate(
                {
                    "threadId": "thread-1",
                    "runId": "run-1",
                    "state": {},
                    "messages": [
                        {"id": client_id, "role": "user", "content": "同一请求"}
                    ],
                    "tools": [],
                    "context": [],
                    "forwardedProps": {"model": "main", "command": {"plan": "off"}},
                }
            )
        )

    first = prepare_run_request(
        request("client-a"),
        user_id=7,
        thread_id="thread-1",
    )
    second = prepare_run_request(
        request("client-b"),
        user_id=7,
        thread_id="thread-1",
    )

    assert first.input_json == second.input_json
    assert first.message_ids == second.message_ids


@pytest.mark.parametrize("run_id", [" run-1", "run-1 ", "   "])
def test_chat_request_rejects_noncanonical_run_id(run_id: str) -> None:
    """runId 必须在任何持久化或事件源创建前拒绝首尾空白"""

    with pytest.raises(ValidationError):
        ChatRequest.model_validate(
            {
                "threadId": "",
                "runId": run_id,
                "state": {},
                "messages": [{"role": "user", "content": "执行任务"}],
                "tools": [],
                "context": [],
                "forwardedProps": {"model": "main", "command": {"plan": "off"}},
            }
        )


@pytest.mark.parametrize("value", ["-1", "01", "1.0", " 1", "1 ", "一"])
def test_last_event_id_rejects_noncanonical_values(value: str) -> None:
    """非法游标必须在发送 SSE 响应头前失败"""

    with pytest.raises(BusinessException) as caught:
        parse_last_event_id(value)

    assert int(caught.value.error_code) == 1_001_004_019


def test_conversation_routes_are_registered_with_the_locked_paths() -> None:
    """应用 OpenAPI 应暴露固定的会话查询与运行接口"""

    paths = create_application(lifespan=None).openapi()["paths"]

    assert "/api/conversation/chat" in paths
    assert "/api/conversation/history" in paths
    assert "/api/conversation/config" in paths
    assert "/api/conversation/{thread_id}/history" in paths
    assert "/api/conversation/{thread_id}/events" in paths
    assert "/api/conversation/{thread_id}/runs/{run_id}/cancel" in paths
    history_parameters = paths["/api/conversation/history"]["get"]["parameters"]
    assert {
        parameter["name"]
        for parameter in history_parameters
        if parameter["in"] == "query"
    } == {
        "pageSize",
        "cursor",
        "query",
    }
