"""创建 Studio 会话使用的 Deep Agent 定义"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import httpx
import yaml
from anyio import to_thread
from deepagents.backends.composite import CompositeBackend
from deepagents.backends.store import StoreBackend
from deepagents.middleware.subagents import SubAgent
from langchain.agents.middleware import InterruptOnConfig, TodoListMiddleware
from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field, RootModel

from tinkerfin import DeepAgentDefinition, TinkerFin
from tinkerfin.media import AttachmentSupport
from tinkerfin.plan import PlanReviewAction
from tinkerfin_sandbox import RootedOpenSandboxBackend
from tinkerfin_sandbox.lifecycle.manager import OpenSandboxManager
from tinkerfin_studio.agent.persistence import AgentPersistence
from tinkerfin_studio.agent.plan_clarification import StudioPlanClarificationForm
from tinkerfin_studio.agent.plan_content import StudioMarkdownPlanContent
from tinkerfin_studio.agent.tools import build_web_search_tool
from tinkerfin_studio.attachments.sandbox_tools import build_sandbox_attachment_tools
from tinkerfin_studio.attachments.service import AttachmentService
from tinkerfin_studio.attachments.tools import build_attachment_tools
from tinkerfin_studio.models.chat import create_chat_model
from tinkerfin_studio.models.schemas import AgentModelConfig

_SUBAGENTS_PATH = Path(__file__).with_name("subagents.yaml")
_SYSTEM_PROMPT = """你是 TinkerFin Studio 的主 Agent。

处理复杂任务时使用 write_todos 维护清单，
使用 task 委派适合的独立研究任务，
使用文件工具在用户 Sandbox 中读写结果。
附件引用中的 ID 可用于 read_attachment 或 view_image。
历史内容压缩后，先用 list_attachments 重新查找附件，不要假装已读原文件。
使用 create_file 或 generate_image 交付可下载结果，工具成功才表示文件存在。
工作区内已有文件用 deliver_file 交付；网页截图用 capture_browser。
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


class ConversationAgentFactory:
    """借用应用资源创建一次会话所需的 Agent 定义"""

    def __init__(
        self,
        *,
        persistence: AgentPersistence,
        sandbox_manager: OpenSandboxManager[str],
        tavily_api_key: str | None,
        attachments: AttachmentService,
        thread_id: str,
        image_model: AgentModelConfig | None,
        model_http_client: httpx.AsyncClient,
        model_allowed_origins: tuple[str, ...] = (),
    ) -> None:
        self._attachments = attachments
        self._thread_id = thread_id
        self._model_http_client = model_http_client
        self._model_allowed_origins = model_allowed_origins
        self._image_model = image_model
        self._persistence = persistence
        self._sandbox_manager = sandbox_manager
        self._tavily_api_key = tavily_api_key

    async def create_agent(
        self,
        *,
        tinkerfin: TinkerFin,
        user_id: int,
        model_config: AgentModelConfig,
    ) -> DeepAgentDefinition[None]:
        """准备模型、Sandbox、Tool 和 Subagent 后创建 Agent 定义

        Args:
            tinkerfin: 已挂载 Trace 的应用级框架门面
            user_id: 已通过认证的 Studio 用户 ID
            model_config: 已解密的模型连接配置

        Returns:
            借用应用级 checkpointer、Store 和 Sandbox 的 Agent 定义

        Raises:
            TypeError: 模型配置没有创建受支持的聊天模型
            BaseException: Sandbox、模型、Tool 或子 Agent 准备失败
        """

        sandbox = await self._sandbox_manager.get(f"users/{user_id}")
        if not isinstance(sandbox, RootedOpenSandboxBackend):
            raise TypeError("Studio 需要具有用户工作区边界的 Sandbox")
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
        model = create_chat_model(
            model_config, http_async_client=self._model_http_client
        )
        plan_model = (
            create_chat_model(
                model_config,
                reasoning_enabled=False,
                http_async_client=self._model_http_client,
            )
            if model_config.provider == "deepseek" and model_config.reasoning_enabled
            else model
        )
        web_search = build_web_search_tool(self._tavily_api_key)
        attachment_tools = build_attachment_tools(
            service=self._attachments,
            processor=self._attachments.documents,
            user_id=user_id,
            thread_id=self._thread_id,
            image_model=self._image_model,
            supports_images=model_config.image_support == "supported",
            model_allowed_origins=self._model_allowed_origins,
        )
        attachment_tools.extend(
            build_sandbox_attachment_tools(
                sandbox=sandbox,
                service=self._attachments,
                user_id=user_id,
                thread_id=self._thread_id,
            )
        )
        # 附件读取按当前用户与会话授权，图片能力只授予当前明确配置的模型
        attachment_support = AttachmentSupport(
            read_image=lambda attachment: self._attachments.read_image(
                attachment, user_id=user_id, thread_id=self._thread_id
            ),
            supports_images=lambda candidate: (
                (candidate is model or candidate is plan_model)
                and model_config.image_support == "supported"
            ),
        )
        tool_registry: dict[str, BaseTool] = {web_search.name: web_search}
        # YAML 解析依赖同步文件接口；AnyIO 默认 worker limiter 提供容量边界，
        # 非 abandon 模式会在调用方取消后等待本次短读取完成，不留下后台任务
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
                                *attachment_tools,
                                *[
                                    tool_registry[tool_name]
                                    for tool_name in definition.tools
                                ],
                            ],
                            "middleware": [
                                *self._sandbox_manager.build_agent_middleware(backend),
                            ],
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
        # Studio 只呈现批准、拒绝和取消；拒绝与取消继续留在 Plan 模式
        return (
            tinkerfin.attachments(attachment_support)
            .plan(
                enabled=True,
                planner_model=plan_model,
                clarification_schema=StudioPlanClarificationForm,
                content_schema=StudioMarkdownPlanContent,
                allowed_review_actions=(
                    PlanReviewAction.APPROVE,
                    PlanReviewAction.REJECT,
                    PlanReviewAction.CANCEL,
                ),
            )
            .create_deep_agent(
                model=model,
                tools=[web_search, *attachment_tools],
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
        )


__all__ = ["ConversationAgentFactory"]
