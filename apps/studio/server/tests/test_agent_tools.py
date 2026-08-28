"""Studio 业务 Tool 的异步资源生命周期测试"""

from __future__ import annotations

import asyncio
from typing import ClassVar, Literal, Self, cast

import pytest
from langchain_core.tools import BaseTool

from tinkerfin_studio.agent import tools as tools_module
from tinkerfin_studio.agent.tools import build_web_search_tool


class _FakeTavilyClient:
    """记录每次搜索调用是否完整退出异步上下文"""

    behavior: ClassVar[Literal["success", "error", "cancel"]] = "success"
    instances: ClassVar[list[_FakeTavilyClient]] = []

    def __init__(self, *, api_key: str) -> None:
        assert api_key == "secret"
        self.closed = False
        self.instances.append(self)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object,
    ) -> None:
        del exc_type, exc_value, traceback
        self.closed = True

    async def search(self, **options: object) -> dict[str, object]:
        assert options == {
            "query": "current news",
            "max_results": 3,
            "topic": "news",
        }
        if self.behavior == "error":
            raise RuntimeError("provider failure")
        if self.behavior == "cancel":
            raise asyncio.CancelledError
        return {
            "answer": "ok",
            "results": [
                {
                    "title": "Result",
                    "url": "https://example.com/result",
                    "content": "Summary",
                    "score": 0.9,
                }
            ],
            "response_time": 0.1,
        }


async def _invoke(tool: BaseTool) -> object:
    return await tool.ainvoke(
        {"query": "current news", "max_results": 3, "topic": "news"}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("behavior", ["success", "error"])
async def test_web_search_closes_owned_client_after_completion(
    monkeypatch: pytest.MonkeyPatch,
    behavior: Literal["success", "error"],
) -> None:
    """成功与普通供应商失败都必须释放本次 Tool 的连接池"""

    _FakeTavilyClient.instances.clear()
    _FakeTavilyClient.behavior = behavior
    monkeypatch.setattr(
        tools_module,
        "AsyncTavilyClient",
        cast(object, _FakeTavilyClient),
    )

    await _invoke(build_web_search_tool("secret"))

    assert len(_FakeTavilyClient.instances) == 1
    assert _FakeTavilyClient.instances[0].closed is True


@pytest.mark.asyncio
async def test_web_search_closes_owned_client_and_preserves_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """调用取消必须关闭连接池并继续传播原取消语义"""

    _FakeTavilyClient.instances.clear()
    _FakeTavilyClient.behavior = "cancel"
    monkeypatch.setattr(
        tools_module,
        "AsyncTavilyClient",
        cast(object, _FakeTavilyClient),
    )

    with pytest.raises(asyncio.CancelledError):
        await _invoke(build_web_search_tool("secret"))

    assert len(_FakeTavilyClient.instances) == 1
    assert _FakeTavilyClient.instances[0].closed is True
