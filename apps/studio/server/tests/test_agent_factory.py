"""Studio Agent 定义的模型、Plan、Tool 与资源装配合同"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import httpx
import pytest
from ag_ui.core import RunAgentInput
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from pydantic import SecretStr, ValidationError

from tinkerfin import TinkerFin
from tinkerfin.media import AttachmentSupport
from tinkerfin.plan import PlanReviewAction
from tinkerfin_sandbox import OpenSandboxBackendUnavailableError
from tinkerfin_sandbox.lifecycle.manager import OpenSandboxManager
from tinkerfin_studio.agent import factory as factory_module
from tinkerfin_studio.agent.factory import ConversationAgentFactory
from tinkerfin_studio.agent.persistence import AgentPersistence
from tinkerfin_studio.agent.plan_clarification import StudioPlanClarificationForm
from tinkerfin_studio.agent.plan_content import StudioMarkdownPlanContent
from tinkerfin_studio.conversation.request import ChatRequest
from tinkerfin_studio.conversation.run_preparation import prepare_run_request
from tinkerfin_studio.models import providers as providers_module
from tinkerfin_studio.models.chat import create_chat_model
from tinkerfin_studio.models.schemas import AgentModelConfig


@pytest.fixture
async def model_http_client():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200))
    ) as client:
        yield client


def _model_config() -> AgentModelConfig:
    return AgentModelConfig(
        model_id="main",
        display_name="Main",
        provider="openai",
        model_name="provider-main",
        base_url="https://models.example.test/v1",
        api_key=SecretStr("secret"),
        reasoning_enabled=False,
    )


def _run_input(*, mode: str = "default") -> RunAgentInput:
    return RunAgentInput.model_validate(
        {
            "threadId": "thread-1",
            "runId": "run-1",
            "state": {},
            "messages": [
                {"id": "client-message-1", "role": "user", "content": "执行任务"}
            ],
            "tools": [],
            "context": [],
            "forwardedProps": {
                "model": "main",
                "command": {"plan": "on" if mode == "plan" else "off"},
            },
        }
    )


def _prepared(*, mode: str = "default"):
    return prepare_run_request(
        ChatRequest.from_agui(_run_input(mode=mode)),
        user_id=7,
        thread_id="thread-1",
    )


def test_studio_plan_content_requires_dynamic_description_and_markdown() -> None:
    schema = StudioMarkdownPlanContent.model_json_schema(by_alias=True)

    assert set(schema["required"]) == {"description", "markdown"}
    assert schema["properties"]["description"]["maxLength"] == 80
    content = StudioMarkdownPlanContent(
        description="先澄清范围，再按步骤实现并验证",
        markdown="# Plan\n\n1. Clarify\n2. Implement",
    )
    assert content.description == "先澄清范围，再按步骤实现并验证"
    with pytest.raises(ValidationError):
        StudioMarkdownPlanContent(description="", markdown="# Plan")


@pytest.mark.parametrize(
    ("reasoning_enabled", "thinking_type", "has_reasoning_effort"),
    ((True, "enabled", True), (False, "disabled", False)),
)
def test_create_deepseek_model_explicitly_controls_thinking(
    monkeypatch: pytest.MonkeyPatch,
    reasoning_enabled: bool,
    thinking_type: str,
    has_reasoning_effort: bool,
) -> None:
    """把 Studio reasoning 开关转换为明确的模型参数"""

    captured: dict[str, object] = {}
    model = FakeListChatModel(responses=["unused"])

    def init_model(model_name: str, **kwargs: object):
        captured["model_name"] = model_name
        captured.update(kwargs)
        return model

    monkeypatch.setattr(providers_module, "init_chat_model", init_model)
    config = _model_config().model_copy(
        update={
            "provider": "deepseek",
            "model_name": "deepseek-v4-pro",
            "reasoning_enabled": reasoning_enabled,
        }
    )

    assert create_chat_model(config) is model
    assert captured["extra_body"] == {"thinking": {"type": thinking_type}}
    assert ("reasoning_effort" in captured) is has_reasoning_effort


async def test_create_agent_configures_plan_tools_and_borrowed_resources(
    monkeypatch: pytest.MonkeyPatch,
    attachments,
    model_http_client,
) -> None:
    """创建 Agent 时保留产品 Plan、审批和应用资源配置"""

    root_model = FakeListChatModel(responses=["root"])
    plan_model = FakeListChatModel(responses=["plan"])
    reasoning_overrides: list[bool | None] = []
    from unittest.mock import create_autospec

    from tinkerfin_sandbox import RootedOpenSandboxBackend

    sandbox = create_autospec(RootedOpenSandboxBackend, instance=True)
    composite_backend = object()
    composite_options: dict[str, object] = {}

    def create_model(
        config: AgentModelConfig,
        *,
        reasoning_enabled: bool | None = None,
        http_async_client=None,
    ):
        del config
        reasoning_overrides.append(reasoning_enabled)
        return root_model if reasoning_enabled is None else plan_model

    class SandboxManager:
        async def get(self, key: str) -> object:
            assert key == "users/7"
            return sandbox

        def build_agent_middleware(self, backend: object) -> tuple[()]:
            del backend
            return ()

    class RecordingTinkerFin:
        def __init__(self) -> None:
            self.plan_options: dict[str, object] = {}
            self.definition_options: dict[str, object] = {}
            self.attachment_support: AttachmentSupport | None = None

        def attachments(self, support: AttachmentSupport):
            self.attachment_support = support
            return self

        def plan(self, **options: object):
            self.plan_options = options
            return self

        def create_deep_agent(self, **options: object):
            self.definition_options = options
            return SimpleNamespace()

    monkeypatch.setattr(factory_module, "create_chat_model", create_model)

    def create_composite_backend(**options: object) -> object:
        composite_options.update(options)
        return composite_backend

    monkeypatch.setattr(factory_module, "CompositeBackend", create_composite_backend)
    monkeypatch.setattr(
        factory_module,
        "StoreBackend",
        lambda **_kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        factory_module,
        "_read_subagents",
        lambda: factory_module._SubagentFile.model_validate({}),
    )
    checkpointer = object()
    store = object()
    tinkerfin = RecordingTinkerFin()
    factory = ConversationAgentFactory(
        attachments=attachments,
        thread_id="thread",
        image_model=None,
        model_http_client=model_http_client,
        persistence=cast(
            AgentPersistence,
            SimpleNamespace(checkpointer=checkpointer, store=store),
        ),
        sandbox_manager=cast(OpenSandboxManager[str], SandboxManager()),
        tavily_api_key=None,
    )
    config = _model_config().model_copy(
        update={
            "provider": "deepseek",
            "model_name": "deepseek-v4-pro",
            "reasoning_enabled": True,
        }
    )

    await factory.create_agent(
        tinkerfin=cast(TinkerFin, tinkerfin),
        user_id=7,
        model_config=config,
    )

    assert isinstance(tinkerfin.attachment_support, AttachmentSupport)
    assert reasoning_overrides == [None, False]
    assert tinkerfin.plan_options == {
        "enabled": True,
        "planner_model": plan_model,
        "clarification_schema": StudioPlanClarificationForm,
        "content_schema": StudioMarkdownPlanContent,
        "allowed_review_actions": (
            PlanReviewAction.APPROVE,
            PlanReviewAction.REJECT,
            PlanReviewAction.CANCEL,
        ),
    }
    assert tinkerfin.definition_options["model"] is root_model
    assert composite_options["default"] is sandbox
    assert tinkerfin.definition_options["backend"] is composite_backend
    assert tinkerfin.definition_options["checkpointer"] is checkpointer
    assert tinkerfin.definition_options["store"] is store
    assert tinkerfin.definition_options["interrupt_on"] == {
        "write_file": {
            "allowed_decisions": ["approve", "reject"],
            "description": "需要人工审批：Agent 正准备写入文件",
        }
    }


async def test_create_agent_propagates_sandbox_creation_failure(
    attachments, model_http_client
) -> None:
    """Sandbox 恢复或创建失败必须让当前 Agent Run 明确失败"""

    expected = OpenSandboxBackendUnavailableError("Sandbox control plane unavailable")

    class FailingSandboxManager:
        async def get(self, key: str) -> object:
            assert key == "users/7"
            raise expected

    factory = ConversationAgentFactory(
        attachments=attachments,
        thread_id="thread",
        image_model=None,
        model_http_client=model_http_client,
        persistence=cast(
            AgentPersistence,
            SimpleNamespace(checkpointer=object(), store=object()),
        ),
        sandbox_manager=cast(OpenSandboxManager[str], FailingSandboxManager()),
        tavily_api_key=None,
    )

    with pytest.raises(OpenSandboxBackendUnavailableError) as captured:
        await factory.create_agent(
            tinkerfin=TinkerFin(),
            user_id=7,
            model_config=_model_config(),
        )

    assert captured.value is expected
