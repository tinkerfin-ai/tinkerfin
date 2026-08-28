"""按 Messaging owner 延迟创建会话 Agent 事件源"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
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
    AgUiResumeCheckpointObserver,
    AgUiResumeInitializationFailureObserver,
    AgUiResumeRequest,
    DeepAgentDefinition,
    TinkerFin,
    join_task,
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
        tinkerfin_profiles: Mapping[str, TinkerFin],
        tavily_api_key: str | None,
    ) -> None:
        self._persistence = persistence
        self._sandbox_manager = sandbox_manager
        self._tinkerfin_profiles = dict(tinkerfin_profiles)
        self._tavily_api_key = tavily_api_key

    def create_agui_events(
        self,
        *,
        user_id: int,
        model_config: AgentModelConfig,
        graph_input: InputAgentState | None,
        prepared: PreparedRunRequest,
        resume: AgUiResumeRequest | None,
        title: str,
        on_producer_opened: Callable[[], Awaitable[None]],
        on_resume_checkpointed: AgUiResumeCheckpointObserver | None = None,
        on_resume_initialization_failed: (
            AgUiResumeInitializationFailureObserver | None
        ) = None,
    ) -> ProfiledDeferredMessageSource[BaseEvent, BaseEvent]:
        """返回仅由 Messaging producer owner 打开的 AG-UI 事件源

        恢复请求已经在短事务中认领公开 ID，但 checkpointer 解析仍由延迟 owner 执行
        初始化失败或取消时必须先可靠释放未 checkpoint 的认领，避免无效客户端输入
        永久占用后续恢复机会

        Args:
            user_id: 已通过认证与会话归属校验的用户 ID
            model_config: 已解密且绑定当前 Runtime Profile 的模型配置
            graph_input: 普通运行的业务 Graph 输入；恢复运行为空
            prepared: 已固定身份、谱系、mode 与 Graph 配置的请求
            resume: 只含客户端决定的恢复请求；普通运行为空
            title: 注入 Agent 的当前会话标题
            on_producer_opened: Messaging owner 建立后、业务源打开前的激活回调
            on_resume_checkpointed: marker 可读后结算业务认领的幂等回调
            on_resume_initialization_failed: marker 前失败时释放业务认领的幂等回调

        Returns:
            只会被真正 Messaging producer owner 打开一次的延迟事件源

        Raises:
            RuntimeError: 模型绑定的 Runtime Profile 不在当前 Worker 目录
        """

        tinkerfin = self._tinkerfin_profiles.get(model_config.runtime_profile)
        if tinkerfin is None:
            raise RuntimeError("模型绑定的 Runtime Profile 在当前 Worker 不可用")

        async def open_events() -> MessageSourceBinding[BaseEvent]:
            resolved_resume = None

            async def release_resume_claims() -> None:
                callback = on_resume_initialization_failed
                if resume is None or callback is None:
                    return

                async def release() -> None:
                    await callback()

                cleanup = asyncio.create_task(
                    release(),
                    name=f"studio-resume-claim-release:{prepared.identity.run_id}",
                )
                await join_task(cleanup)

            async def release_preserving(primary: BaseException) -> None:
                """释放 marker 前认领，同时保持主失败或取消语义"""

                try:
                    await release_resume_claims()
                except BaseException as cleanup_error:  # 保留初始化与清理的主次顺序
                    if not isinstance(primary, Exception):
                        primary.add_note(
                            f"恢复认领释放同时失败：{type(cleanup_error).__name__}"
                        )
                        return
                    if not isinstance(cleanup_error, Exception):
                        cleanup_error.add_note(
                            f"Agent Runtime 初始化同时失败：{type(primary).__name__}"
                        )
                        raise
                    primary.add_note(
                        f"恢复认领释放同时失败：{type(cleanup_error).__name__}"
                    )

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
                    resolved_resume = await definition.prepare_agui_resume(
                        identity=prepared.identity,
                        parent_run_id=prepared.parent_run_id,
                        mode=prepared.mode,
                        request=resume,
                    )
                    resume_runtime = await to_thread.run_sync(
                        partial(
                            definition.new_agui,
                            identity=prepared.identity,
                            parent_run_id=prepared.parent_run_id,
                            mode=prepared.mode,
                            resume=resolved_resume,
                            on_resume_checkpointed=on_resume_checkpointed,
                            on_resume_initialization_failed=(
                                on_resume_initialization_failed
                            ),
                            expose_reasoning_events=False,
                            expose_subagent_events=True,
                        )
                    )
                    agent_events = resume_runtime.astream(config=prepared.graph_config)
            except asyncio.CancelledError as cancellation:
                await release_preserving(cancellation)
                raise
            except Exception as error:
                await release_preserving(error)
                logger.exception("创建会话 Agent Runtime 失败")
                agent_events = tinkerfin.failed_agui_run(
                    error,
                    identity=prepared.identity,
                    parent_run_id=prepared.parent_run_id,
                    mode=prepared.mode,
                    input=graph_input,
                    config=prepared.graph_config,
                    resume=resolved_resume,
                    resume_request=(resume if resolved_resume is None else None),
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
            on_owner_preflight=on_producer_opened,
        )

    async def _create_definition(
        self,
        *,
        user_id: int,
        model_config: AgentModelConfig,
    ) -> DeepAgentDefinition[None]:
        """准备一次请求借用的模型、Sandbox 与 Deep Agent 建图参数"""

        tinkerfin = self._tinkerfin_profiles.get(model_config.runtime_profile)
        if tinkerfin is None:
            raise RuntimeError("模型绑定的 Runtime Profile 在当前 Worker 不可用")
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
        return tinkerfin.plan(
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
