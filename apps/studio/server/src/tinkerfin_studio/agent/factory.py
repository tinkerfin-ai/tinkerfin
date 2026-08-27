"""按 Messaging owner 延迟创建会话 Agent 事件源"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from pathlib import Path
from typing import cast

import yaml
from ag_ui.core import BaseEvent
from anyio import to_thread
from deepagents.backends.composite import CompositeBackend
from deepagents.backends.store import StoreBackend
from deepagents.middleware.subagents import SubAgent
from langchain.agents.middleware import InterruptOnConfig, TodoListMiddleware
from langchain.agents.middleware.types import InputAgentState
from langchain.chat_models.base import init_chat_model
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field, RootModel

from tinkerfin import (
    AgUiEventStream,
    AgUiResumeBinding,
    AgUiResumeCheckpointObserver,
    DeepAgentDefinition,
    TinkerFin,
)
from tinkerfin.plan import PlanReviewAction
from tinkerfin_messaging import (
    MessageSourceBinding,
    ProfiledDeferredMessageSource,
    map_source,
)
from tinkerfin_sandbox.lifecycle.manager import OpenSandboxManager
from tinkerfin_studio.agent.persistence import AgentPersistence
from tinkerfin_studio.agent.plan_clarification import StudioPlanClarificationForm
from tinkerfin_studio.agent.plan_content import StudioMarkdownPlanContent
from tinkerfin_studio.agent.tools import build_web_search_tool
from tinkerfin_studio.conversation.run_preparation import (
    PreparedRunRequest,
    decorate_main_event,
)
from tinkerfin_studio.models.schemas import AgentModelConfig

_SUBAGENTS_PATH = Path(__file__).with_name("subagents.yaml")
logger = logging.getLogger(__name__)
_SYSTEM_PROMPT = """你是 TinkerFin Studio 的主 Agent。

处理复杂任务时使用 write_todos 维护清单，
使用 task 委派适合的独立研究任务，
使用文件工具在用户 Sandbox 中读写结果。
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


def _create_model(
    config: AgentModelConfig,
    *,
    reasoning_enabled: bool | None = None,
) -> BaseChatModel:
    enable_reasoning = (
        config.reasoning_enabled if reasoning_enabled is None else reasoning_enabled
    )
    if config.provider == "deepseek":
        if enable_reasoning:
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
                extra_body={"thinking": {"type": "disabled"}},
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
    """借用全局资源，为 Messaging owner 创建请求级 AG-UI 事件源"""

    def __init__(
        self,
        *,
        persistence: AgentPersistence,
        sandbox_manager: OpenSandboxManager[str],
        tinkerfin: TinkerFin,
        tavily_api_key: str | None,
    ) -> None:
        self._persistence = persistence
        self._sandbox_manager = sandbox_manager
        self._tinkerfin = tinkerfin
        self._tavily_api_key = tavily_api_key

    def create_agui_events(
        self,
        *,
        user_id: int,
        model_config: AgentModelConfig,
        graph_input: InputAgentState | None,
        prepared: PreparedRunRequest,
        resume: AgUiResumeBinding | None,
        title: str,
        on_resume_checkpointed: AgUiResumeCheckpointObserver | None = None,
    ) -> ProfiledDeferredMessageSource[BaseEvent, BaseEvent]:
        """返回仅由 Messaging producer owner 打开的 AG-UI 事件源"""

        async def open_events() -> MessageSourceBinding[BaseEvent]:
            try:
                definition = await self._create_definition(
                    user_id=user_id,
                    model_config=model_config,
                )
                if resume is None:
                    if graph_input is None:
                        raise RuntimeError("普通运行缺少 Graph 输入")
                    ordinary_runtime = await to_thread.run_sync(
                        partial(
                            definition.new_agui,
                            identity=prepared.identity,
                            parent_run_id=prepared.parent_run_id,
                            mode=prepared.mode,
                            expose_reasoning_events=False,
                            expose_subagent_events=True,
                        )
                    )
                    agent_events = ordinary_runtime.astream(
                        graph_input,
                        config=prepared.graph_config,
                    )
                else:
                    resume_runtime = await to_thread.run_sync(
                        partial(
                            definition.new_agui,
                            identity=prepared.identity,
                            parent_run_id=prepared.parent_run_id,
                            mode=prepared.mode,
                            resume=resume,
                            on_resume_checkpointed=on_resume_checkpointed,
                            expose_reasoning_events=False,
                            expose_subagent_events=True,
                        )
                    )
                    agent_events = resume_runtime.astream(config=prepared.graph_config)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.exception("创建会话 Agent Runtime 失败")
                agent_events = AgUiEventStream.from_initialization_error(
                    error,
                    identity=prepared.identity,
                    parent_run_id=prepared.parent_run_id,
                )

            def attach_run_metadata(event: BaseEvent) -> BaseEvent:
                """发布服务端会话元数据"""

                return decorate_main_event(event, prepared=prepared, title=title)

            return MessageSourceBinding(
                source=map_source(agent_events, attach_run_metadata),
            )

        return ProfiledDeferredMessageSource(
            open_events,
            identity=prepared.identity,
            codec_profile="agui.event",
            source_type=BaseEvent,
            replay_type=BaseEvent,
            cancellable=True,
            cancel_after_first_item=True,
        )

    async def _create_definition(
        self,
        *,
        user_id: int,
        model_config: AgentModelConfig,
    ) -> DeepAgentDefinition[None]:
        """准备一次请求借用的模型、Sandbox 与 Deep Agent 建图参数"""

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
        plan_model = (
            _create_model(model_config, reasoning_enabled=False)
            if model_config.provider == "deepseek" and model_config.reasoning_enabled
            else model
        )
        web_search = build_web_search_tool(self._tavily_api_key)
        tool_registry: dict[str, BaseTool] = {web_search.name: web_search}
        definitions = await to_thread.run_sync(_read_subagents)
        subagents: list[SubAgent] = []
        for name, definition in definitions.root.items():
            subagents.append(
                cast(
                    SubAgent,
                    cast(
                        object,
                        {
                            "name": name,
                            "description": definition.description,
                            "system_prompt": definition.system_prompt,
                            "tools": [
                                tool_registry[tool_name]
                                for tool_name in definition.tools
                            ],
                            "middleware": list(
                                self._sandbox_manager.build_agent_middleware(backend)
                            ),
                        },
                    ),
                )
            )
        # 文件审批只提供拒绝与允许，恢复仍按框架原生决策提交
        interrupt = cast(
            InterruptOnConfig,
            cast(
                object,
                {
                    "allowed_decisions": ["approve", "reject"],
                    "description": "需要人工审批：Agent 正准备写入文件",
                },
            ),
        )
        # Studio 计划草稿只允许批准、反馈和拒绝，避免客户端与恢复 Schema 漂移
        return self._tinkerfin.plan(
            enabled=True,
            planner_model=plan_model,
            clarification_schema=StudioPlanClarificationForm,
            content_schema=StudioMarkdownPlanContent,
            allowed_review_actions=(
                PlanReviewAction.APPROVE,
                PlanReviewAction.RESPOND,
                PlanReviewAction.REJECT,
            ),
        ).create_deep_agent(
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
