"""每个 chat 请求独立创建 Deep Agent graph"""

from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import cast

import yaml
from anyio import to_thread
from deepagents.backends.composite import CompositeBackend
from deepagents.backends.store import StoreBackend
from deepagents.graph import create_deep_agent
from deepagents.middleware.subagents import SubAgent
from langchain.agents.middleware import InterruptOnConfig, TodoListMiddleware
from langchain.chat_models.base import init_chat_model
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel, ConfigDict, Field, RootModel

from tinkerfin_sandbox.lifecycle.manager import OpenSandboxManager
from tinkerfin_studio.agent.persistence import AgentPersistence
from tinkerfin_studio.agent.tools import build_web_search_tool
from tinkerfin_studio.models.schemas import AgentModelConfig

_SUBAGENTS_PATH = Path(__file__).with_name("subagents.yaml")
_SYSTEM_PROMPT = """你是 TinkerFin Studio 的主 Agent。

处理复杂任务时使用 write_todos 维护清单，使用 task 委派适合的独立研究任务，
使用文件工具在用户 Sandbox 中读写结果。调用 write_todos 时，该模型消息只能包含
write_todos 一个 Tool 调用；等待 ToolMessage 返回后再调用其他 Tool。
"""


class _SubagentDefinition(BaseModel):
    """声明式子 Agent 配置"""

    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1)
    system_prompt: str = Field(min_length=1)
    tools: list[str] = Field(default_factory=list)


class _SubagentFile(RootModel[dict[str, _SubagentDefinition]]):
    """完整子 Agent 配置文件"""


def _read_subagents() -> _SubagentFile:
    payload = yaml.safe_load(_SUBAGENTS_PATH.read_text(encoding="utf-8"))
    return _SubagentFile.model_validate(payload)


def _create_model(config: AgentModelConfig) -> BaseChatModel:
    if config.provider == "deepseek" and config.reasoning_enabled:
        model = init_chat_model(
            config.model_name,
            model_provider=config.provider,
            api_key=config.api_key.get_secret_value(),
            base_url=config.base_url,
            streaming=True,
            timeout=600,
            max_tokens=25_000,
            extra_body={"thinking": {"type": "enabled"}},
            reasoning_effort="high",
        )
    else:
        model = init_chat_model(
            config.model_name,
            model_provider=config.provider,
            api_key=config.api_key.get_secret_value(),
            base_url=config.base_url,
            streaming=True,
            timeout=600,
            max_tokens=25_000,
        )
    if not isinstance(model, BaseChatModel):
        raise TypeError("模型配置未创建 BaseChatModel")
    return model


class ConversationAgentFactory:
    """借用全局 persistence 与 Sandbox，按请求编译一个新 graph"""

    def __init__(
        self,
        *,
        persistence: AgentPersistence,
        sandbox_manager: OpenSandboxManager[str],
        tavily_api_key: str | None,
    ) -> None:
        self._persistence = persistence
        self._sandbox_manager = sandbox_manager
        self._tavily_api_key = tavily_api_key

    async def create(
        self,
        *,
        user_id: int,
        model_config: AgentModelConfig,
    ) -> CompiledStateGraph:
        """为一次 chat HTTP 请求创建全新的会话 graph"""

        sandbox = await self._sandbox_manager.get(f"users/{user_id}")
        backend = CompositeBackend(
            default=sandbox,
            routes={
                "/memories/": StoreBackend(
                    namespace=lambda _runtime: (
                        "tinkerfin-studio",
                        "users",
                        str(user_id),
                        "memories",
                    )
                )
            },
        )
        model = _create_model(model_config)
        web_search = build_web_search_tool(self._tavily_api_key)
        tool_registry: dict[str, BaseTool] = {web_search.name: web_search}
        definitions = await to_thread.run_sync(_read_subagents)
        subagents: list[SubAgent] = []
        for name, definition in definitions.root.items():
            subagents.append(
                cast(
                    SubAgent,
                    {
                        "name": name,
                        "description": definition.description,
                        "system_prompt": definition.system_prompt,
                        "tools": [
                            tool_registry[tool_name] for tool_name in definition.tools
                        ],
                        "middleware": list(
                            self._sandbox_manager.build_agent_middleware(backend)
                        ),
                    },
                )
            )
        interrupt = cast(
            InterruptOnConfig,
            {
                "allowed_decisions": ["approve", "edit", "reject"],
                "description": "需要人工审批：Agent 正准备写入文件",
            },
        )
        create = partial(
            create_deep_agent,
            model=model,
            tools=[web_search],
            system_prompt=_SYSTEM_PROMPT,
            middleware=(
                *self._sandbox_manager.build_agent_middleware(backend),
                TodoListMiddleware(),
            ),
            subagents=subagents,
            backend=backend,
            interrupt_on={"write_file": interrupt},
            checkpointer=self._persistence.checkpointer,
            store=self._persistence.store,
        )
        return await to_thread.run_sync(create)
