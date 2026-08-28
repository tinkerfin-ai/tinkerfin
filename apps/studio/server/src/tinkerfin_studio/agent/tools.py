"""Studio 提供给 Deep Agents 的业务 Tool"""

from __future__ import annotations

import logging
from typing import Literal, cast

from langchain_core.tools import BaseTool, ToolException, tool
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, JsonValue
from tavily import AsyncTavilyClient

logger = logging.getLogger(__name__)


class WebSearchInput(BaseModel):
    """网页搜索 Tool 输入"""

    query: str = Field(
        min_length=2, max_length=500, description="具体、明确的搜索关键词"
    )
    max_results: int = Field(default=5, ge=1, le=10, description="最多返回的结果数量")
    topic: Literal["general", "news"] = Field(
        default="general", description="普通网页或新闻主题"
    )


class WebSearchItem(BaseModel):
    """单条标准化搜索结果"""

    model_config = ConfigDict(extra="ignore")

    title: str = Field(min_length=1, description="搜索结果标题")
    url: AnyHttpUrl = Field(description="搜索结果来源 URL")
    content: str = Field(default="", description="搜索结果摘要")
    score: float | None = Field(default=None, description="外部服务相关性分数")


class WebSearchPayload(BaseModel):
    """Tavily 外部响应校验模型"""

    model_config = ConfigDict(extra="ignore")

    answer: str | None = Field(default=None, description="外部服务生成的摘要答案")
    results: list[WebSearchItem] = Field(
        default_factory=list, description="标准化搜索结果"
    )
    response_time: float | None = Field(default=None, description="外部接口耗时秒数")


def build_web_search_tool(api_key: str | None) -> BaseTool:
    """创建只捕获当前应用配置的异步网页搜索 Tool"""

    @tool(
        "web_search",
        args_schema=WebSearchInput,
        response_format="content_and_artifact",
        parse_docstring=True,
        error_on_invalid_docstring=True,
    )
    async def web_search(
        query: str,
        max_results: int = 5,
        topic: Literal["general", "news"] = "general",
    ) -> tuple[str, dict[str, JsonValue]]:
        """搜索实时网页信息，返回标题、来源 URL 与内容摘要。

        Args:
            query: 具体、明确的搜索关键词
            max_results: 最多返回的结果数量
            topic: 搜索主题，支持普通网页或新闻

        Returns:
            可供模型读取的 JSON 内容和结构化搜索结果

        Raises:
            ToolException: 未配置搜索密钥或外部搜索调用失败
        """

        if api_key is None:
            raise ToolException(
                '{"status":"error","error":"Web search is not configured"}'
            )
        search_input = WebSearchInput(
            query=query,
            max_results=max_results,
            topic=topic,
        )
        try:
            # 每次 Tool 调用独占一个短生命周期连接池，取消和异常也由上下文完成关闭
            async with AsyncTavilyClient(api_key=api_key) as client:
                raw = await client.search(
                    query=search_input.query,
                    max_results=search_input.max_results,
                    topic=search_input.topic,
                )
            payload = WebSearchPayload.model_validate(raw)
        except ToolException:
            raise
        except Exception as error:
            logger.warning("Tavily 搜索失败: error_type=%s", type(error).__name__)
            raise ToolException(
                '{"status":"error","error":"Web search failed"}'
            ) from error
        artifact = cast(
            dict[str, JsonValue],
            payload.model_dump(mode="json", exclude_none=True),
        )
        content = payload.model_dump_json(exclude_none=True)
        return content, artifact

    web_search.handle_tool_error = True
    return web_search
