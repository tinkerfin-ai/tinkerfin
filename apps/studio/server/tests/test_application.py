from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient

from tinkerfin_studio.application import create_application


async def test_application_exposes_liveness_without_external_resources() -> None:
    """liveness 只表达进程存活，不触发外部连接。"""

    application = create_application(lifespan=None)

    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readiness_requires_initialized_resources() -> None:
    """资源尚未初始化时 readiness 必须返回 503。"""

    application = create_application(lifespan=None)

    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "components": {}}


async def test_readiness_reports_dependency_status_without_error_details() -> None:
    """readiness 只公开稳定组件状态，不泄漏底层异常。"""

    class FakeReadiness:
        async def check(self) -> dict[str, bool]:
            return {"mysql": True, "redis": True, "opensandbox": False}

    application = create_application(lifespan=None)
    application.state.resources = SimpleNamespace(readiness=FakeReadiness())

    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "components": {"mysql": True, "redis": True, "opensandbox": False},
    }


async def test_unknown_route_uses_the_shared_error_envelope() -> None:
    """未匹配路由不得泄漏 FastAPI 默认错误结构"""

    application = create_application(lifespan=None)

    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.get("/missing")

    assert response.status_code == 404
    assert response.json() == {
        "code": 404,
        "message": "请求未找到",
        "data": None,
    }
