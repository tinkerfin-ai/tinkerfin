"""请求级 Agent 事件源的延迟创建与公开行为"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from types import SimpleNamespace
from typing import cast

import pytest
from ag_ui.core import BaseEvent, RunAgentInput, RunErrorEvent, RunStartedEvent
from langchain.agents.middleware.types import InputAgentState
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from pydantic import SecretStr

from tinkerfin import AgUiEventStream, TinkerFin
from tinkerfin_agui_adapter import AgUiLifecycleEventFactory
from tinkerfin_messaging import FiniteMessageSource, MemoryBackend, Messaging
from tinkerfin_messaging.agui import AgUiCodec
from tinkerfin_sandbox.lifecycle.manager import OpenSandboxManager
from tinkerfin_studio.agent import factory as factory_module
from tinkerfin_studio.agent.factory import ConversationAgentFactory, _create_model
from tinkerfin_studio.agent.persistence import AgentPersistence
from tinkerfin_studio.conversation.request import ChatRequest
from tinkerfin_studio.conversation.run_preparation import prepare_run_request
from tinkerfin_studio.models.schemas import AgentModelConfig


class _AgentEvents(FiniteMessageSource[BaseEvent]):
    @property
    def messaging_cancel_callback(
        self,
    ) -> Callable[[], Awaitable[list[BaseEvent]]]:
        return self.abort

    async def abort(self) -> list[BaseEvent]:
        await self.aclose()
        return []


def _model_config() -> AgentModelConfig:
    return AgentModelConfig(
        model_id="main",
        display_name="Main",
        provider="openai",
        model_name="provider-main",
        base_url="https://models.example.test/v1",
        api_key=SecretStr("secret"),
        reasoning_enabled=False,
        updated_at="2026-08-19T00:00:00",
    )


def _run_input(*, mode: str = "default") -> RunAgentInput:
    return RunAgentInput.model_validate(
        {
            "threadId": "thread-1",
            "runId": "run-1",
            "state": {},
            "messages": [],
            "tools": [],
            "context": [],
            "forwardedProps": {"model": "main", "mode": mode},
        }
    )


def _prepared(*, mode: str = "default"):
    return prepare_run_request(
        ChatRequest.from_agui(_run_input(mode=mode)),
        user_id=7,
        thread_id="thread-1",
    )


def test_prepare_run_request_preserves_the_selected_agent_mode() -> None:
    assert _prepared(mode="plan").mode == "plan"


@pytest.mark.parametrize(
    ("reasoning_enabled", "thinking_type", "has_reasoning_effort"),
    ((True, "enabled", True), (False, "disabled", False)),
)
def test_create_deepseek_model_explicitly_controls_thinking(
    monkeypatch,
    reasoning_enabled: bool,
    thinking_type: str,
    has_reasoning_effort: bool,
) -> None:
    """DeepSeek 的 reasoning 开关必须转换为显式 provider 参数"""

    captured: dict[str, object] = {}
    model = FakeListChatModel(responses=["unused"])

    def init_model(model_name: str, **kwargs: object):
        captured["model_name"] = model_name
        captured.update(kwargs)
        return model

    monkeypatch.setattr(factory_module, "init_chat_model", init_model)
    config = _model_config().model_copy(
        update={
            "provider": "deepseek",
            "model_name": "deepseek-v4-pro",
            "reasoning_enabled": reasoning_enabled,
        }
    )

    assert _create_model(config) is model
    assert captured["extra_body"] == {"thinking": {"type": thinking_type}}
    assert ("reasoning_effort" in captured) is has_reasoning_effort


async def test_create_definition_uses_non_reasoning_models_for_plan(
    monkeypatch,
) -> None:
    """主 Agent 保留 reasoning，Plan Gate 与 Planner 使用可结构化输出的模型"""

    root_model = FakeListChatModel(responses=["root"])
    plan_model = FakeListChatModel(responses=["plan"])
    reasoning_overrides: list[bool | None] = []

    def create_model(
        config: AgentModelConfig,
        *,
        reasoning_enabled: bool | None = None,
    ):
        del config
        reasoning_overrides.append(reasoning_enabled)
        return root_model if reasoning_enabled is None else plan_model

    class SandboxManager:
        async def get(self, key: str) -> object:
            assert key == "users/7"
            return object()

        def build_agent_middleware(self, backend: object) -> tuple[()]:
            del backend
            return ()

    class RecordingTinkerFin:
        def __init__(self) -> None:
            self.plan_options: dict[str, object] = {}
            self.definition_options: dict[str, object] = {}

        def plan(self, **options: object):
            self.plan_options = options
            return self

        def create_deep_agent(self, **options: object):
            self.definition_options = options
            return SimpleNamespace()

    monkeypatch.setattr(factory_module, "_create_model", create_model)
    monkeypatch.setattr(
        factory_module,
        "CompositeBackend",
        lambda **_kwargs: SimpleNamespace(),
    )
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
    tinkerfin = RecordingTinkerFin()
    factory = ConversationAgentFactory(
        persistence=cast(
            AgentPersistence,
            SimpleNamespace(checkpointer=object(), store=object()),
        ),
        sandbox_manager=cast(OpenSandboxManager[str], SandboxManager()),
        tinkerfin=cast(TinkerFin, tinkerfin),
        tavily_api_key=None,
    )
    config = _model_config().model_copy(
        update={
            "provider": "deepseek",
            "model_name": "deepseek-v4-pro",
            "reasoning_enabled": True,
        }
    )

    await factory._create_definition(user_id=7, model_config=config)

    assert reasoning_overrides == [None, False]
    assert tinkerfin.plan_options == {
        "enabled": True,
        "planner_model": plan_model,
    }
    assert tinkerfin.definition_options["model"] is root_model


async def test_create_agui_events_defers_definition_and_enriches_main_start(
    monkeypatch,
) -> None:
    """未拉取时不得建图，owner 拉取后应交付完整主运行元数据"""

    definition_calls = 0
    runtime_calls = 0
    runtime_options: dict[str, object] = {}

    class Runtime:
        def astream(self, graph_input, config=None):
            nonlocal runtime_calls
            del graph_input, config
            runtime_calls += 1
            return _AgentEvents.from_events(
                (
                    AgUiLifecycleEventFactory().started(
                        identity=_prepared().identity,
                    ),
                )
            )

    class Definition:
        def new_agui(self, **kwargs):
            runtime_options.update(kwargs)
            return Runtime()

    async def create_definition(_factory, *, user_id, model_config):
        nonlocal definition_calls
        del user_id, model_config
        definition_calls += 1
        return Definition()

    monkeypatch.setattr(
        ConversationAgentFactory,
        "_create_definition",
        create_definition,
        raising=False,
    )
    factory = ConversationAgentFactory(
        persistence=cast(AgentPersistence, SimpleNamespace()),
        sandbox_manager=cast(OpenSandboxManager[str], SimpleNamespace()),
        tinkerfin=TinkerFin(),
        tavily_api_key=None,
    )
    model = _model_config()
    prepared = _prepared()
    events = factory.create_agui_events(
        user_id=7,
        model_config=model,
        graph_input=cast(InputAgentState, {"messages": []}),
        prepared=prepared,
        resume=None,
        title="会话标题",
    )

    assert definition_calls == 0
    assert runtime_calls == 0

    emitted = [event async for event in events]

    assert definition_calls == 1
    assert runtime_calls == 1
    assert runtime_options["mode"] == "default"
    assert len(emitted) == 1
    started = emitted[0]
    assert isinstance(started, RunStartedEvent)
    started_payload = started.model_dump(mode="python", by_alias=False)
    assert started_payload["input"] == prepared.protocol_input.model_dump(
        mode="python",
        by_alias=False,
    )
    assert started_payload["title"] == "会话标题"


async def test_create_agui_events_converts_owner_initialization_failure(
    monkeypatch,
) -> None:
    """owner 初始化失败也必须产生完整且唯一的 AG-UI 错误生命周期"""

    expected = RuntimeError("cannot initialize agent")

    async def fail_definition(_factory, *, user_id, model_config):
        del user_id, model_config
        raise expected

    monkeypatch.setattr(
        ConversationAgentFactory,
        "_create_definition",
        fail_definition,
    )
    factory = ConversationAgentFactory(
        persistence=cast(AgentPersistence, SimpleNamespace()),
        sandbox_manager=cast(OpenSandboxManager[str], SimpleNamespace()),
        tinkerfin=TinkerFin(),
        tavily_api_key=None,
    )
    prepared = _prepared()
    events = factory.create_agui_events(
        user_id=7,
        model_config=_model_config(),
        graph_input=cast(InputAgentState, {"messages": []}),
        prepared=prepared,
        resume=None,
        title="会话标题",
    )

    emitted = [event async for event in events]

    assert [type(event) for event in emitted] == [RunStartedEvent, RunErrorEvent]
    started = emitted[0]
    failed = emitted[1]
    assert isinstance(started, RunStartedEvent)
    assert isinstance(failed, RunErrorEvent)
    assert started.raw_event == {
        "threadId": "thread-1",
        "runId": "run-1",
        "initializationFailed": True,
    }
    assert failed.code == "runtime_initialization_error"
    assert failed.message == "Agent run failed"


async def test_remote_cancel_waits_for_deferred_agent_open_and_keeps_one_terminal(
    monkeypatch,
) -> None:
    """打开期间的远程取消必须等待同一 Runtime 并提交唯一取消终止"""

    opening = asyncio.Event()
    release_open = asyncio.Event()
    open_calls = 0

    class Runtime:
        def astream(self, graph_input, config=None) -> AgUiEventStream:
            del graph_input, config

            async def parts() -> AsyncIterator[Mapping[str, object]]:
                await asyncio.Event().wait()
                if False:  # pragma: no cover - 保持异步迭代器形状
                    yield {}

            return AgUiEventStream(
                parts=parts(),
                identity=_prepared().identity,
                expose_reasoning_events=False,
                expose_subagent_events=True,
                prior_tool_call_ids=frozenset(),
                timeout=None,
                settlement_timeout=None,
                on_event=None,
            )

    class Definition:
        def new_agui(self, **kwargs):
            del kwargs
            return Runtime()

    async def create_definition(_factory, *, user_id, model_config):
        nonlocal open_calls
        del user_id, model_config
        open_calls += 1
        opening.set()
        await release_open.wait()
        return Definition()

    monkeypatch.setattr(
        ConversationAgentFactory,
        "_create_definition",
        create_definition,
    )
    factory = ConversationAgentFactory(
        persistence=cast(AgentPersistence, SimpleNamespace()),
        sandbox_manager=cast(OpenSandboxManager[str], SimpleNamespace()),
        tinkerfin=TinkerFin(),
        tavily_api_key=None,
    )
    prepared = _prepared()
    events = factory.create_agui_events(
        user_id=7,
        model_config=_model_config(),
        graph_input=cast(InputAgentState, {"messages": []}),
        prepared=prepared,
        resume=None,
        title="会话标题",
    )

    async with Messaging(backend=MemoryBackend()) as messaging:
        channel = messaging.channel(name="events", codec=AgUiCodec())
        subscription = await channel.wrap(
            events,
            after=0,
        )
        await asyncio.wait_for(opening.wait(), timeout=1)
        cancelling = asyncio.create_task(channel.cancel(identity=prepared.identity))
        await asyncio.sleep(0)
        assert not cancelling.done()

        release_open.set()

        assert await asyncio.wait_for(cancelling, timeout=1) is True
        emitted = [message.data async for message in subscription]

    assert open_calls == 1
    assert [type(event) for event in emitted] == [RunStartedEvent, RunErrorEvent]
    failed = emitted[-1]
    assert isinstance(failed, RunErrorEvent)
    assert failed.code == "cancelled"
    assert failed.message == "聊天生成已取消"
