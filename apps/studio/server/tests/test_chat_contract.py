import pytest
from pydantic import ValidationError

from tinkerfin_studio.api.errors import BusinessException
from tinkerfin_studio.application import create_application
from tinkerfin_studio.conversation.request import ChatRequest
from tinkerfin_studio.conversation.service import parse_last_event_id


def test_chat_request_preserves_plan_mode_without_interpreting_it() -> None:
    """plan 模式应留在 forwardedProps，而不是改变请求结构"""

    request = ChatRequest.model_validate(
        {
            "threadId": "",
            "runId": "run-1",
            "state": {},
            "messages": [{"role": "user", "content": "执行任务"}],
            "tools": [],
            "context": [],
            "forwardedProps": {"model": "main", "mode": "plan", "trace": "x"},
        }
    )

    normalized = request.normalized(
        thread_id="thread-1",
        message_ids=("message-server-1",),
    )

    assert normalized["threadId"] == "thread-1"
    assert normalized["messages"] == [
        {
            "id": "message-server-1",
            "role": "user",
            "content": "执行任务",
        }
    ]
    assert normalized["forwardedProps"] == {
        "model": "main",
        "mode": "plan",
        "trace": "x",
    }


def test_chat_request_rejects_client_generated_message_id() -> None:
    """客户端不得提交由自身生成的协议消息 ID"""

    with pytest.raises(ValidationError):
        ChatRequest.model_validate(
            {
                "threadId": "",
                "runId": "run-1",
                "state": {},
                "messages": [
                    {"id": "client-message-1", "role": "user", "content": "执行任务"}
                ],
                "tools": [],
                "context": [],
                "forwardedProps": {"model": "main", "mode": "default"},
            }
        )


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
                "forwardedProps": {"model": "main", "mode": "default"},
            }
        )


@pytest.mark.parametrize("value", ["-1", "01", "1.0", " 1", "1 ", "一"])
def test_last_event_id_rejects_noncanonical_values(value: str) -> None:
    """非法游标必须在发送 SSE 响应头前失败"""

    with pytest.raises(BusinessException) as caught:
        parse_last_event_id(value)

    assert int(caught.value.error_code) == 1_001_004_019


def test_conversation_routes_are_registered_with_the_locked_paths() -> None:
    """应用 OpenAPI 应暴露旧接口和新增取消接口"""

    paths = create_application(lifespan=None).openapi()["paths"]

    assert "/api/conversation/chat" in paths
    assert "/api/conversation/history" in paths
    assert "/api/conversation/{thread_id}/history" in paths
    assert "/api/conversation/{thread_id}/events" in paths
    assert "/api/conversation/{thread_id}/runs/{run_id}/cancel" in paths
