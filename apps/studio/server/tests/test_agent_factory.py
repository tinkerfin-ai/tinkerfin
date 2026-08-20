"""请求级 Agent 事件源的延迟创建与公开行为"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from types import SimpleNamespace
from typing import cast

from ag_ui.core import BaseEvent, RunAgentInput, RunErrorEvent, RunStartedEvent
from langchain.agents.middleware.types import InputAgentState
from pydantic import SecretStr

from tinkerfin import AgUiEventStream, TinkerFin
from tinkerfin_agui_adapter import AgUiLifecycleEventFactory
from tinkerfin_messaging import FiniteMessageSource, MemoryBackend, Messaging
from tinkerfin_messaging.agui import AgUiCodec
from tinkerfin_sandbox.lifecycle.manager import OpenSandboxManager
from tinkerfin_studio.agent.factory import ConversationAgentFactory
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


def _run_input() -> RunAgentInput:
    return RunAgentInput.model_validate(
        {
            "threadId": "thread-1",
            "runId": "run-1",
            "state": {},
            "messages": [],
            "tools": [],
            "context": [],
            "forwardedProps": {"model": "main", "mode": "default"},
        }
    )


def _prepared():
    return prepare_run_request(
        ChatRequest.from_agui(_run_input()),
        user_id=7,
        thread_id="thread-1",
    )


async def test_create_agui_events_defers_definition_and_enriches_main_start(
    monkeypatch,
) -> None:
    """未拉取时不得建图，owner 拉取后应交付完整主运行元数据"""

    definition_calls = 0
    runtime_calls = 0

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
            del kwargs
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
