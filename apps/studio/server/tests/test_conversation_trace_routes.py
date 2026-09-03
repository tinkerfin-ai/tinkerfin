"""链路查询 HTTP 参数与错误包络"""

from httpx import ASGITransport, AsyncClient

from tinkerfin_studio.api.dependencies import get_conversation_history_service
from tinkerfin_studio.application import create_application


async def test_trace_graph_query_rejects_noncanonical_namespace() -> None:
    """非法图命名空间必须在进入业务查询前返回统一校验错误"""

    application = create_application(lifespan=None)
    application.dependency_overrides[get_conversation_history_service] = object

    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/api/conversation/thread-1/trace/graph",
            params={"namespace": "tools| invalid"},
        )

    assert response.status_code == 422
    assert response.json() == {
        "code": 422,
        "message": "请求参数校验失败",
        "data": None,
    }
