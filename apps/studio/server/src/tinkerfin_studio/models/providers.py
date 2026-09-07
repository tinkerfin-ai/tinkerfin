"""当前两种聊天接口的独立模型适配器"""

from dataclasses import dataclass
from typing import Literal, TypedDict

import httpx
from langchain.chat_models.base import init_chat_model
from langchain_core.language_models import BaseChatModel

from tinkerfin_studio.models.schemas import AgentModelConfig


@dataclass(frozen=True, slots=True)
class ChatModelOptions:
    """一次模型构建所需的配置；客户端归调用方所有，适配器仅借用"""

    config: AgentModelConfig
    reasoning_enabled: bool
    http_async_client: httpx.AsyncClient | None
    timeout: float
    max_tokens: int
    max_retries: int


def create_openai_model(options: ChatModelOptions) -> BaseChatModel:
    """构建 OpenAI 兼容接口，服务商差异通过模型名称和地址配置"""
    model = init_chat_model(
        options.config.model_name,
        model_provider="openai",
        api_key=options.config.api_key.get_secret_value(),
        base_url=options.config.base_url,
        streaming=True,
        http_async_client=options.http_async_client,
        timeout=options.timeout,
        max_tokens=options.max_tokens,
        max_retries=options.max_retries,
    )
    if not isinstance(model, BaseChatModel):
        raise TypeError("模型配置未创建 BaseChatModel")
    return model


class _DeepSeekReasoning(TypedDict, total=False):
    reasoning_effort: Literal["high"]


def create_deepseek_model(options: ChatModelOptions) -> BaseChatModel:
    """把 Studio 推理开关转换为 DeepSeek 支持的请求参数"""
    reasoning_options: _DeepSeekReasoning = (
        {"reasoning_effort": "high"} if options.reasoning_enabled else {}
    )
    model = init_chat_model(
        options.config.model_name,
        model_provider="deepseek",
        api_key=options.config.api_key.get_secret_value(),
        base_url=options.config.base_url,
        streaming=True,
        http_async_client=options.http_async_client,
        timeout=options.timeout,
        max_tokens=options.max_tokens,
        max_retries=options.max_retries,
        extra_body={
            "thinking": {"type": "enabled" if options.reasoning_enabled else "disabled"}
        },
        **reasoning_options,
    )
    if not isinstance(model, BaseChatModel):
        raise TypeError("模型配置未创建 BaseChatModel")
    return model
