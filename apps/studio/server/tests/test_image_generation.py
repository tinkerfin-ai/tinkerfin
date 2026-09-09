"""兼容生图响应、公开网络边界和取消时的资源释放"""

import asyncio
import base64
import json
import socket

import httpx
import pytest
from pydantic import SecretStr

from tinkerfin_studio.attachments import generation
from tinkerfin_studio.models.schemas import AgentModelConfig
from tinkerfin_studio.models.transport import ModelTransport


def image_model() -> AgentModelConfig:
    return AgentModelConfig(
        model_id="my-image",
        display_name="My image",
        provider="openai",
        model_name="image-model",
        base_url="https://images.example/api",
        api_key=SecretStr("private-key"),
        reasoning_enabled=False,
        purpose="image",
        generation_options={"size": "1024x1024"},
    )


@pytest.mark.parametrize("response_kind", ["url", "b64_json"])
async def test_compatible_image_response_keeps_credentials_on_generation_only(
    monkeypatch, response_kind
):
    requests: list[httpx.Request] = []
    image = b"stored-image-bytes"

    async def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            payload = json.loads(request.content)
            assert payload == {
                "model": "image-model",
                "prompt": "a chart",
                "size": "1024x1024",
            }
            assert request.headers["Authorization"] == "Bearer private-key"
            result = (
                {"url": "https://cdn.example/image.png"}
                if response_kind == "url"
                else {"b64_json": base64.b64encode(image).decode()}
            )
            return httpx.Response(200, json={"data": [result]})
        assert request.url == "https://cdn.example/image.png"
        assert "authorization" not in request.headers
        return httpx.Response(200, content=image)

    monkeypatch.setattr(
        generation, "ModelTransport", lambda **kwargs: httpx.MockTransport(handle)
    )
    assert await generation.generate_image_bytes(image_model(), "a chart") == image
    assert len(requests) == (2 if response_kind == "url" else 1)


@pytest.mark.parametrize("status", [302, 429, 500])
async def test_generation_failure_is_not_retried_or_redirected(monkeypatch, status):
    requests = []

    async def handle(request):
        requests.append(request)
        return httpx.Response(
            status,
            headers={"location": "https://elsewhere.example"},
            json={"error": "failed"},
        )

    monkeypatch.setattr(
        generation, "ModelTransport", lambda **kwargs: httpx.MockTransport(handle)
    )
    with pytest.raises(ValueError):
        await generation.generate_image_bytes(image_model(), "a chart")
    assert len(requests) == 1


async def test_cancelled_generation_closes_transport_without_retry(monkeypatch):
    class CancelledTransport(httpx.AsyncBaseTransport):
        closed = False
        calls = 0

        async def handle_async_request(self, request):
            self.calls += 1
            raise asyncio.CancelledError()

        async def aclose(self):
            self.closed = True

    transport = CancelledTransport()
    monkeypatch.setattr(generation, "ModelTransport", lambda **kwargs: transport)
    with pytest.raises(asyncio.CancelledError):
        await generation.generate_image_bytes(image_model(), "a chart")
    assert transport.closed and transport.calls == 1


async def test_malformed_response_does_not_include_signed_url_in_public_error(
    monkeypatch,
):
    async def handle(request):
        return httpx.Response(
            200,
            json={
                "data": [
                    {"url": "https://cdn.example?secret=private"},
                    {"url": "https://cdn.example/extra"},
                ]
            },
        )

    monkeypatch.setattr(
        generation, "ModelTransport", lambda **kwargs: httpx.MockTransport(handle)
    )
    with pytest.raises(ValueError) as rejected:
        await generation.generate_image_bytes(image_model(), "a chart")
    assert "private" not in str(rejected.value)
    assert "格式不正确" in str(rejected.value)


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1"])
async def test_transport_rejects_private_resolution_before_connecting(
    monkeypatch, address
):
    async def resolve(*args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443))
        ]

    monkeypatch.setattr("tinkerfin_studio.models.transport.anyio.getaddrinfo", resolve)
    async with httpx.AsyncClient(transport=ModelTransport()) as client:
        with pytest.raises(ValueError, match="私有网络"):
            await client.get("https://images.example/image")


async def test_transport_pins_validated_ip_and_preserves_host_and_sni(monkeypatch):
    async def resolve(*args, **kwargs):
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("8.8.8.8", 443),
            )
        ]

    requests = []

    async def handle(request):
        requests.append(request)
        return httpx.Response(200)

    monkeypatch.setattr("tinkerfin_studio.models.transport.anyio.getaddrinfo", resolve)
    monkeypatch.setattr(
        httpx, "AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(handle)
    )
    async with httpx.AsyncClient(transport=ModelTransport()) as client:
        await client.get("https://images.example/image")
    assert requests[0].url.host == "8.8.8.8"
    assert requests[0].headers["host"] == "images.example"
    assert requests[0].extensions["sni_hostname"] == "images.example"


async def test_allowed_generation_cannot_download_from_unlisted_local_origin(
    monkeypatch,
):
    requests: list[httpx.Request] = []

    async def resolve(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 11434))]

    async def handle(request):
        requests.append(request)
        return httpx.Response(
            200, json={"data": [{"url": "http://127.0.0.1:11435/internal"}]}
        )

    monkeypatch.setattr("tinkerfin_studio.models.transport.anyio.getaddrinfo", resolve)
    monkeypatch.setattr(
        httpx, "AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(handle)
    )
    model = image_model().model_copy(update={"base_url": "http://127.0.0.1:11434/v1"})
    with pytest.raises(ValueError, match="MODEL_ALLOWED_ORIGINS"):
        await generation.generate_image_bytes(
            model, "a chart", allowed_origins=("http://127.0.0.1:11434",)
        )
    assert len(requests) == 1 and requests[0].method == "POST"
