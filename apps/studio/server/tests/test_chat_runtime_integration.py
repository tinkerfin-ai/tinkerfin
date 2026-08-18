import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from uuid import NAMESPACE_URL, uuid5

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.types import interrupt
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tinkerfin.coordination import InMemoryRunCoordinator
from tinkerfin.runtime import TinkerFin
from tinkerfin_messaging.agui import AgUiCodec
from tinkerfin_messaging.backend import MemoryBackend
from tinkerfin_messaging.errors import RunAlreadyActive
from tinkerfin_messaging.messaging import Messaging
from tinkerfin_studio.agent.factory import ConversationAgentFactory
from tinkerfin_studio.api.errors import BusinessException
from tinkerfin_studio.auth.types import UserContext
from tinkerfin_studio.conversation.coordinator import (
    ConversationProjectionCoordinator,
)
from tinkerfin_studio.conversation.models import (
    ConversationInterrupt,
    ConversationRun,
)
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.conversation.request import ChatRequest
from tinkerfin_studio.conversation.service import (
    ConversationChatService,
    ConversationCommandService,
)
from tinkerfin_studio.infrastructure.database import Database
from tinkerfin_studio.models.repository import AgentModelRepository
from tinkerfin_studio.models.schemas import AgentModelWrite
from tinkerfin_studio.models.service import AgentModelService
from tinkerfin_studio.resources import ApplicationResources


class ProjectionProbe:
    """记录 chat 是否注册后台投影"""

    def __init__(self) -> None:
        self.runs: list[str] = []

    def ensure(self, *, thread_pk: int, stream: str, run: str) -> None:
        del thread_pk, stream
        self.runs.append(run)

    async def reconcile(self, *, thread_pk: int, stream: str) -> int:
        del thread_pk, stream
        return 0


async def test_non_empty_thread_id_must_belong_to_the_current_user(
    session: AsyncSession,
) -> None:
    """非空 threadId 不得隐式创建或访问不存在的会话"""

    await AgentModelService(AgentModelRepository(session)).upsert(
        AgentModelWrite(
            model_id="main",
            display_name="Main",
            provider="openai",
            model_name="provider-main",
            base_url="https://models.example.test/v1",
            api_key=SecretStr("secret"),
            enabled=True,
            is_default=True,
        )
    )
    service = ConversationChatService(
        session,
        user=UserContext(
            user_id=7,
            username="alice",
            display_name="Alice",
            roles=(),
            disabled=False,
        ),
        resources=cast(ApplicationResources, SimpleNamespace()),
    )

    with pytest.raises(BusinessException) as caught:
        await service.start(
            ChatRequest.model_validate(
                {
                    "threadId": "thread-not-found",
                    "runId": "run-unknown-thread",
                    "state": {},
                    "messages": [{"role": "user", "content": "继续"}],
                    "tools": [],
                    "context": [],
                    "forwardedProps": {"model": "main", "mode": "default"},
                }
            ),
            last_event_id=None,
        )

    assert caught.value.error_code.http_status == 404
    assert caught.value.message == "会话不存在"
    assert (
        await ConversationRepository(session).get_thread(
            user_id=7,
            thread_id="thread-not-found",
        )
        is None
    )


async def test_chat_does_not_filter_optional_deep_agent_state_channels(
    session: AsyncSession,
    monkeypatch,
) -> None:
    """缺少 todos channel 的合法 graph 仍应完成标准 AG-UI 生命周期"""

    await AgentModelService(AgentModelRepository(session)).upsert(
        AgentModelWrite(
            model_id="main",
            display_name="Main",
            provider="openai",
            model_name="provider-main",
            base_url="https://models.example.test/v1",
            api_key=SecretStr("secret"),
            enabled=True,
            is_default=True,
        )
    )
    builder = StateGraph(MessagesState)

    async def answer(state: MessagesState) -> dict[str, object]:
        del state
        return {}

    builder.add_node("answer", answer)
    builder.add_edge(START, "answer")
    builder.add_edge("answer", END)
    graph = builder.compile()

    async def create_graph(
        _factory: ConversationAgentFactory,
        *,
        user_id: int,
        model_config,
    ):
        del user_id, model_config
        return graph

    monkeypatch.setattr(ConversationAgentFactory, "create", create_graph)
    backend = MemoryBackend()
    probe = ProjectionProbe()
    async with Messaging(backend=backend) as messaging:
        resources = cast(
            ApplicationResources,
            SimpleNamespace(
                settings=SimpleNamespace(tavily_api_key=None),
                agent_persistence=object(),
                sandbox_manager=object(),
                tinkerfin=TinkerFin(
                    run_coordinator=InMemoryRunCoordinator[str](
                        key_resolver=lambda principal: principal
                    )
                ),
                conversation_channel=messaging.channel(
                    name="studio-conversation-agui",
                    codec=AgUiCodec(),
                ),
                conversation_projector=probe,
            ),
        )
        service = ConversationChatService(
            session,
            user=UserContext(
                user_id=7,
                username="alice",
                display_name="Alice",
                roles=(),
                disabled=False,
            ),
            resources=resources,
        )
        chat_request = ChatRequest.model_validate(
            {
                "threadId": "",
                "runId": "run-1",
                "state": {},
                "messages": [{"role": "user", "content": "你好"}],
                "tools": [],
                "context": [],
                "forwardedProps": {"model": "main", "mode": "plan"},
            }
        )
        prepared = await service.start(
            chat_request,
            last_event_id=None,
        )
        stored_thread = await ConversationRepository(session).get_thread(
            user_id=7,
            thread_id=prepared.thread_id,
        )
        assert stored_thread is not None
        assert stored_thread.title == "你好"
        frames = [chunk async for chunk in prepared.body]
        assert set(probe.runs) == {"run-1"}
        assert len(probe.runs) == len(frames)
        retried = await service.start(chat_request, last_event_id="0")
        retried_frames = [chunk async for chunk in retried.body]
        assert len(probe.runs) == len(frames)
        stored_threads = await ConversationRepository(session).list_threads(
            user_id=7,
            page_size=10,
            cursor=None,
        )

    events = [json.loads(frame.split(b"data: ", 1)[1]) for frame in frames]
    event_types = [event["type"] for event in events]
    assert event_types[0] == "RUN_STARTED"
    assert event_types[-1] == "RUN_FINISHED"
    assert "RUN_ERROR" not in event_types
    assert retried.thread_id == prepared.thread_id
    assert retried_frames == frames
    assert [thread.thread_id for thread in stored_threads] == [prepared.thread_id]
    assert events[0]["threadId"] == prepared.thread_id
    assert events[0]["title"] == "你好"
    assert events[0]["input"]["threadId"] == prepared.thread_id
    assert len(events[0]["input"]["messages"]) == 1
    started_user_message = events[0]["input"]["messages"][0]
    assert set(started_user_message) == {"id", "role", "content"}
    assert started_user_message["role"] == "user"
    assert started_user_message["content"] == "你好"
    assert started_user_message["id"].startswith("message-")


async def test_concurrent_empty_thread_retries_share_one_thread_and_run(
    database: Database,
    monkeypatch,
) -> None:
    """同一 run 的并发首发重试必须附着同一后端会话"""

    async with database.session() as setup_session:
        await AgentModelService(AgentModelRepository(setup_session)).upsert(
            AgentModelWrite(
                model_id="main",
                display_name="Main",
                provider="openai",
                model_name="provider-main",
                base_url="https://models.example.test/v1",
                api_key=SecretStr("secret"),
                enabled=True,
                is_default=True,
            )
        )
    builder = StateGraph(MessagesState)

    async def answer(state: MessagesState) -> dict[str, object]:
        del state
        return {}

    builder.add_node("answer", answer)
    builder.add_edge(START, "answer")
    builder.add_edge("answer", END)
    graph = builder.compile()

    async def create_graph(
        _factory: ConversationAgentFactory,
        *,
        user_id: int,
        model_config,
    ):
        del user_id, model_config
        return graph

    monkeypatch.setattr(ConversationAgentFactory, "create", create_graph)
    request = ChatRequest.model_validate(
        {
            "threadId": "",
            "runId": "run-concurrent",
            "state": {},
            "messages": [{"role": "user", "content": "并发重试"}],
            "tools": [],
            "context": [],
            "forwardedProps": {"model": "main", "mode": "default"},
        }
    )
    user = UserContext(
        user_id=7,
        username="alice",
        display_name="Alice",
        roles=(),
        disabled=False,
    )
    probe = ProjectionProbe()
    async with Messaging(backend=MemoryBackend()) as messaging:
        resources = cast(
            ApplicationResources,
            SimpleNamespace(
                settings=SimpleNamespace(tavily_api_key=None),
                agent_persistence=object(),
                sandbox_manager=object(),
                tinkerfin=TinkerFin(
                    run_coordinator=InMemoryRunCoordinator[str](
                        key_resolver=lambda principal: principal
                    )
                ),
                conversation_channel=messaging.channel(
                    name="studio-conversation-agui",
                    codec=AgUiCodec(),
                ),
                conversation_projector=probe,
            ),
        )
        async with (
            database.session() as first_session,
            database.session() as second_session,
        ):
            first, second = await asyncio.gather(
                ConversationChatService(
                    first_session,
                    user=user,
                    resources=resources,
                ).start(request, last_event_id="0"),
                ConversationChatService(
                    second_session,
                    user=user,
                    resources=resources,
                ).start(request, last_event_id="0"),
            )
            first_frames, second_frames = await asyncio.gather(
                _collect_frames(first.body),
                _collect_frames(second.body),
            )

    async with database.session() as verification_session:
        repository = ConversationRepository(verification_session)
        threads = await repository.list_threads(
            user_id=7,
            page_size=10,
            cursor=None,
        )
        run = await repository.get_run(
            thread_pk=threads[0].id,
            run_id="run-concurrent",
        )

    assert first.thread_id == second.thread_id
    assert first_frames == second_frames
    assert [thread.thread_id for thread in threads] == [first.thread_id]
    assert run is not None


async def test_concurrent_resume_claims_one_interrupt_for_exactly_one_run(
    database: Database,
    monkeypatch,
) -> None:
    """不同 runId 并发恢复时只能有一个 run 消费审批"""

    user = UserContext(
        user_id=7,
        username="alice",
        display_name="Alice",
        roles=(),
        disabled=False,
    )
    thread_id = "thread-concurrent-resume"
    stream = f"users/{user.user_id}/threads/{thread_id}"
    now = datetime.now(UTC).replace(tzinfo=None)
    async with database.session() as setup_session:
        await AgentModelService(AgentModelRepository(setup_session)).upsert(
            AgentModelWrite(
                model_id="main",
                display_name="Main",
                provider="openai",
                model_name="provider-main",
                base_url="https://models.example.test/v1",
                api_key=SecretStr("secret"),
                enabled=True,
                is_default=True,
            )
        )
        repository = ConversationRepository(setup_session)
        thread = await repository.create_thread(
            user_id=user.user_id,
            thread_id=thread_id,
            title="并发审批",
            model_id="main",
        )
        await repository.commit()

    executed_decisions: list[tuple[str, object]] = []
    builder = StateGraph(MessagesState)

    async def review_first_write(state: MessagesState) -> dict[str, object]:
        del state
        decision = interrupt(
            {
                "action_requests": [
                    {
                        "name": "write_file",
                        "args": {"file_path": "/audit.txt", "content": "approved"},
                        "description": "写入审批审计文件",
                    }
                ],
                "review_configs": [
                    {
                        "action_name": "write_file",
                        "allowed_decisions": ["approve", "reject"],
                    }
                ],
            }
        )
        executed_decisions.append(("first", decision))
        return {
            "messages": [
                ToolMessage(
                    content="写入完成",
                    name="write_file",
                    tool_call_id="call-write-audit",
                ),
                AIMessage(
                    content="",
                    id="assistant-review-second",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {
                                "file_path": "/second-audit.txt",
                                "content": "second",
                            },
                            "id": "call-write-second-audit",
                            "type": "tool_call",
                        }
                    ],
                ),
            ]
        }

    async def review_second_write(state: MessagesState) -> dict[str, object]:
        del state
        decision = interrupt(
            {
                "action_requests": [
                    {
                        "name": "write_file",
                        "args": {
                            "file_path": "/second-audit.txt",
                            "content": "second",
                        },
                        "description": "写入第二个审批审计文件",
                    }
                ],
                "review_configs": [
                    {
                        "action_name": "write_file",
                        "allowed_decisions": ["approve", "reject"],
                    }
                ],
            }
        )
        executed_decisions.append(("second", decision))
        return {
            "messages": [
                ToolMessage(
                    content="第二次写入完成",
                    name="write_file",
                    tool_call_id="call-write-second-audit",
                )
            ]
        }

    builder.add_node("review_first_write", review_first_write)
    builder.add_node("review_second_write", review_second_write)
    builder.add_edge(START, "review_first_write")
    builder.add_edge("review_first_write", "review_second_write")
    builder.add_edge("review_second_write", END)
    graph = builder.compile(checkpointer=MemorySaver())
    await graph.ainvoke(
        {
            "messages": [
                AIMessage(
                    content="",
                    id="assistant-review",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {
                                "file_path": "/audit.txt",
                                "content": "approved",
                            },
                            "id": "call-write-audit",
                            "type": "tool_call",
                        }
                    ],
                )
            ]
        },
        config={"configurable": {"thread_id": stream}},
    )
    checkpoint = await graph.aget_state(
        {"configurable": {"thread_id": stream}},
        subgraphs=True,
    )
    public_interrupt_id = checkpoint.interrupts[0].id
    request_json = {
        "id": public_interrupt_id,
        "reason": "tool_call",
        "message": "需要审批写入文件",
    }
    async with database.session() as setup_session:
        setup_session.add(
            ConversationInterrupt(
                conversation_thread_id=thread.id,
                run_id="run-interrupted",
                resolved_run_id=None,
                interrupt_id=public_interrupt_id,
                status="pending",
                reason="tool_call",
                message="需要审批写入文件",
                request_json=request_json,
                resume_json=None,
                created_at=now,
                resolved_at=None,
                updated_at=now,
            )
        )
        await setup_session.commit()

    checkpoint_reads = 0
    first_checkpoint_read = asyncio.Event()
    second_checkpoint_read = asyncio.Event()
    release_stale_checkpoint = asyncio.Event()
    original_get_state = graph.aget_state

    async def observe_get_state(config, *, subgraphs: bool = False):
        nonlocal checkpoint_reads
        checkpoint_reads += 1
        captured = await original_get_state(config, subgraphs=subgraphs)
        if checkpoint_reads == 1:
            first_checkpoint_read.set()
            try:
                await asyncio.wait_for(second_checkpoint_read.wait(), timeout=0.2)
            except TimeoutError:
                pass
        elif checkpoint_reads == 2:
            second_checkpoint_read.set()
            await release_stale_checkpoint.wait()
        return captured

    monkeypatch.setattr(graph, "aget_state", observe_get_state)

    async def create_graph(
        _factory: ConversationAgentFactory,
        *,
        user_id: int,
        model_config,
    ):
        del user_id, model_config
        return graph

    monkeypatch.setattr(ConversationAgentFactory, "create", create_graph)
    requests = tuple(
        ChatRequest.model_validate(
            {
                "threadId": thread_id,
                "runId": run_id,
                "state": {},
                "messages": [],
                "tools": [],
                "context": [],
                "forwardedProps": {"model": "main", "mode": "default"},
                "resume": [
                    {
                        "interruptId": public_interrupt_id,
                        "status": "resolved",
                        "payload": {"type": "approve"},
                    }
                ],
            }
        )
        for run_id in ("run-resume-a", "run-resume-b")
    )
    probe = ProjectionProbe()
    async with Messaging(backend=MemoryBackend()) as messaging:
        resources = cast(
            ApplicationResources,
            SimpleNamespace(
                settings=SimpleNamespace(tavily_api_key=None),
                agent_persistence=object(),
                sandbox_manager=object(),
                tinkerfin=TinkerFin(
                    run_coordinator=InMemoryRunCoordinator[str](
                        key_resolver=lambda principal: principal
                    )
                ),
                conversation_channel=messaging.channel(
                    name="studio-conversation-agui",
                    codec=AgUiCodec(),
                ),
                conversation_projector=probe,
            ),
        )
        async with (
            database.session() as first_session,
            database.session() as second_session,
        ):
            services = (
                ConversationChatService(first_session, user=user, resources=resources),
                ConversationChatService(second_session, user=user, resources=resources),
            )
            winner_task = asyncio.create_task(
                services[0].start(requests[0], last_event_id="0"),
                name="test-resume-claim-winner",
            )
            await asyncio.wait_for(first_checkpoint_read.wait(), timeout=2)
            loser_task = asyncio.create_task(
                services[1].start(requests[1], last_event_id="0"),
                name="test-resume-claim-loser",
            )
            prepared = await asyncio.wait_for(winner_task, timeout=2)
            frames = await _collect_frames(prepared.body)
            release_stale_checkpoint.set()
            loser_results = await asyncio.wait_for(
                asyncio.gather(loser_task, return_exceptions=True),
                timeout=2,
            )
            loser = loser_results[0]
            if not isinstance(loser, BaseException):
                await _collect_frames(loser.body)
            assert isinstance(loser, BusinessException)
            assert loser.error_code.http_status == 409
            assert loser.message == "该审批已被另一次恢复运行认领"
            retried = await services[0].start(
                requests[0],
                last_event_id="0",
            )
            retried_frames = await _collect_frames(retried.body)

    winner_run_id = requests[0].run_id
    loser_run_id = requests[1].run_id
    events = [json.loads(frame.split(b"data: ", 1)[1]) for frame in frames]
    assert [event["type"] for event in events].count("RUN_STARTED") == 1
    assert events[-1]["type"] == "RUN_FINISHED"
    assert events[-1]["outcome"]["type"] == "interrupt"
    assert events[-1]["outcome"]["interrupts"][0]["id"] != public_interrupt_id
    assert retried_frames == frames
    assert checkpoint_reads == 1
    assert [stage for stage, _decision in executed_decisions] == ["first"]
    async with database.session() as verification_session:
        stored_interrupt = await verification_session.scalar(
            select(ConversationInterrupt).where(
                ConversationInterrupt.conversation_thread_id == thread.id,
                ConversationInterrupt.interrupt_id == public_interrupt_id,
            )
        )
        stored_runs = list(
            await verification_session.scalars(
                select(ConversationRun).where(
                    ConversationRun.conversation_thread_id == thread.id
                )
            )
        )
    assert stored_interrupt is not None
    assert stored_interrupt.resolved_run_id == winner_run_id
    assert [run.run_id for run in stored_runs] == [winner_run_id]
    assert all(run.run_id != loser_run_id for run in stored_runs)


async def test_concurrent_same_run_resume_attaches_without_remapping_checkpoint(
    database: Database,
    monkeypatch,
) -> None:
    """同 runId 并发恢复必须只消费一次 checkpoint 并返回同一事件流"""

    user = UserContext(
        user_id=7,
        username="alice",
        display_name="Alice",
        roles=(),
        disabled=False,
    )
    thread_id = "thread-concurrent-same-resume"
    stream = f"users/{user.user_id}/threads/{thread_id}"
    async with database.session() as setup_session:
        await AgentModelService(AgentModelRepository(setup_session)).upsert(
            AgentModelWrite(
                model_id="main",
                display_name="Main",
                provider="openai",
                model_name="provider-main",
                base_url="https://models.example.test/v1",
                api_key=SecretStr("secret"),
                enabled=True,
                is_default=True,
            )
        )
        repository = ConversationRepository(setup_session)
        thread = await repository.create_thread(
            user_id=user.user_id,
            thread_id=thread_id,
            title="同 run 恢复",
            model_id="main",
        )
        await repository.commit()

    executed_decisions: list[object] = []
    builder = StateGraph(MessagesState)

    async def review_write(state: MessagesState) -> dict[str, object]:
        del state
        decision = interrupt(
            {
                "action_requests": [
                    {
                        "name": "write_file",
                        "args": {"file_path": "/same.txt", "content": "ok"},
                        "description": "写入同 run 文件",
                    }
                ],
                "review_configs": [
                    {
                        "action_name": "write_file",
                        "allowed_decisions": ["approve", "reject"],
                    }
                ],
            }
        )
        executed_decisions.append(decision)
        return {
            "messages": [
                ToolMessage(
                    content="写入完成",
                    name="write_file",
                    tool_call_id="call-same-resume",
                )
            ]
        }

    builder.add_node("review_write", review_write)
    builder.add_edge(START, "review_write")
    builder.add_edge("review_write", END)
    graph = builder.compile(checkpointer=MemorySaver())
    await graph.ainvoke(
        {
            "messages": [
                AIMessage(
                    content="",
                    id="assistant-same-resume",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {"file_path": "/same.txt", "content": "ok"},
                            "id": "call-same-resume",
                            "type": "tool_call",
                        }
                    ],
                )
            ]
        },
        config={"configurable": {"thread_id": stream}},
    )
    checkpoint = await graph.aget_state(
        {"configurable": {"thread_id": stream}},
        subgraphs=True,
    )
    interrupt_id = checkpoint.interrupts[0].id
    now = datetime.now(UTC).replace(tzinfo=None)
    async with database.session() as setup_session:
        setup_session.add(
            ConversationInterrupt(
                conversation_thread_id=thread.id,
                run_id="run-interrupted",
                resolved_run_id=None,
                interrupt_id=interrupt_id,
                status="pending",
                reason="tool_call",
                message="确认写入",
                request_json={"id": interrupt_id, "reason": "tool_call"},
                resume_json=None,
                created_at=now,
                resolved_at=None,
                updated_at=now,
            )
        )
        await setup_session.commit()

    checkpoint_reads = 0
    first_checkpoint_read = asyncio.Event()
    release_second_checkpoint = asyncio.Event()
    original_get_state = graph.aget_state
    initial_run_reads = 0
    both_initial_run_reads = asyncio.Event()
    original_get_run = ConversationRepository.get_run
    original_claim = ConversationRepository.claim_pending_interrupts

    async def observe_get_run(
        repository: ConversationRepository,
        *,
        thread_pk: int,
        run_id: str,
    ):
        nonlocal initial_run_reads
        result = await original_get_run(
            repository,
            thread_pk=thread_pk,
            run_id=run_id,
        )
        if initial_run_reads < 2:
            initial_run_reads += 1
            if initial_run_reads == 2:
                both_initial_run_reads.set()
        return result

    async def observe_claim(
        repository: ConversationRepository,
        *,
        thread_pk: int,
        run_id: str,
        interrupt_ids: frozenset[str],
    ):
        await both_initial_run_reads.wait()
        return await original_claim(
            repository,
            thread_pk=thread_pk,
            run_id=run_id,
            interrupt_ids=interrupt_ids,
        )

    async def observe_get_state(config, *, subgraphs: bool = False):
        nonlocal checkpoint_reads
        checkpoint_reads += 1
        if checkpoint_reads == 1:
            first_checkpoint_read.set()
        else:
            await release_second_checkpoint.wait()
        return await original_get_state(config, subgraphs=subgraphs)

    monkeypatch.setattr(graph, "aget_state", observe_get_state)
    monkeypatch.setattr(ConversationRepository, "get_run", observe_get_run)
    monkeypatch.setattr(
        ConversationRepository,
        "claim_pending_interrupts",
        observe_claim,
    )

    async def create_graph(
        _factory: ConversationAgentFactory,
        *,
        user_id: int,
        model_config,
    ):
        del user_id, model_config
        return graph

    monkeypatch.setattr(ConversationAgentFactory, "create", create_graph)
    request = ChatRequest.model_validate(
        {
            "threadId": thread_id,
            "runId": "run-same-resume",
            "state": {},
            "messages": [],
            "tools": [],
            "context": [],
            "forwardedProps": {"model": "main", "mode": "default"},
            "resume": [
                {
                    "interruptId": interrupt_id,
                    "status": "resolved",
                    "payload": {"type": "approve"},
                }
            ],
        }
    )
    probe = ProjectionProbe()
    async with Messaging(backend=MemoryBackend()) as messaging:
        resources = cast(
            ApplicationResources,
            SimpleNamespace(
                settings=SimpleNamespace(tavily_api_key=None),
                agent_persistence=object(),
                sandbox_manager=object(),
                tinkerfin=TinkerFin(
                    run_coordinator=InMemoryRunCoordinator[str](
                        key_resolver=lambda principal: principal
                    )
                ),
                conversation_channel=messaging.channel(
                    name="studio-conversation-agui",
                    codec=AgUiCodec(),
                ),
                conversation_projector=probe,
            ),
        )
        async with (
            database.session() as first_session,
            database.session() as second_session,
        ):
            services = (
                ConversationChatService(first_session, user=user, resources=resources),
                ConversationChatService(second_session, user=user, resources=resources),
            )
            tasks = (
                asyncio.create_task(
                    services[0].start(request, last_event_id="0"),
                    name="test-same-resume-first",
                ),
                asyncio.create_task(
                    services[1].start(request, last_event_id="0"),
                    name="test-same-resume-second",
                ),
            )
            await asyncio.wait_for(first_checkpoint_read.wait(), timeout=2)
            done, _pending = await asyncio.wait(
                tasks,
                timeout=2,
                return_when=asyncio.FIRST_COMPLETED,
            )
            assert done
            winner_task = next(iter(done))
            follower_task = tasks[1] if winner_task is tasks[0] else tasks[0]
            winner = await winner_task
            winner_frames = await _collect_frames(winner.body)
            release_second_checkpoint.set()
            follower_results = await asyncio.wait_for(
                asyncio.gather(follower_task, return_exceptions=True),
                timeout=2,
            )
            follower = follower_results[0]
            assert not isinstance(follower, BaseException)
            follower_frames = await _collect_frames(follower.body)

    assert follower_frames == winner_frames
    assert checkpoint_reads == 1
    assert len(executed_decisions) == 1
    async with database.session() as verification_session:
        stored_runs = list(
            await verification_session.scalars(
                select(ConversationRun).where(
                    ConversationRun.conversation_thread_id == thread.id,
                    ConversationRun.run_id == request.run_id,
                )
            )
        )
    assert len(stored_runs) == 1


async def test_resume_preflight_failure_releases_interrupt_claim(
    session: AsyncSession,
    monkeypatch,
) -> None:
    """Messaging 拒绝启动时不得遗留无主 run 的审批认领"""

    await AgentModelService(AgentModelRepository(session)).upsert(
        AgentModelWrite(
            model_id="main",
            display_name="Main",
            provider="openai",
            model_name="provider-main",
            base_url="https://models.example.test/v1",
            api_key=SecretStr("secret"),
            enabled=True,
            is_default=True,
        )
    )
    repository = ConversationRepository(session)
    thread = await repository.create_thread(
        user_id=7,
        thread_id="thread-preflight-failure",
        title="预握手失败",
        model_id="main",
    )
    now = datetime.now(UTC).replace(tzinfo=None)
    interrupt_id = "interrupt-preflight"
    session.add(
        ConversationInterrupt(
            conversation_thread_id=thread.id,
            run_id="run-interrupted",
            resolved_run_id=None,
            interrupt_id=interrupt_id,
            status="pending",
            reason="tool_call",
            message="确认写入",
            request_json={"id": interrupt_id, "reason": "tool_call"},
            resume_json=None,
            created_at=now,
            resolved_at=None,
            updated_at=now,
        )
    )
    await repository.commit()
    snapshot = SimpleNamespace(
        config={"configurable": {"checkpoint_ns": ""}},
        values={
            "messages": [
                AIMessage(
                    content="",
                    id="assistant-preflight",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {"file_path": "/audit.txt", "content": "ok"},
                            "id": "call-preflight",
                            "type": "tool_call",
                        }
                    ],
                )
            ]
        },
        tasks=(),
        interrupts=(
            SimpleNamespace(
                id=interrupt_id,
                value={
                    "action_requests": [
                        {
                            "name": "write_file",
                            "args": {"file_path": "/audit.txt", "content": "ok"},
                            "description": "写入审计文件",
                        }
                    ],
                    "review_configs": [
                        {
                            "action_name": "write_file",
                            "allowed_decisions": ["approve", "reject"],
                        }
                    ],
                },
            ),
        ),
    )

    async def get_state(config, *, subgraphs: bool = False):
        del config, subgraphs
        return snapshot

    async def stream_events(*args, **kwargs):
        del args, kwargs
        if False:
            yield None

    graph = SimpleNamespace(aget_state=get_state, astream=stream_events)

    async def create_graph(
        _factory: ConversationAgentFactory,
        *,
        user_id: int,
        model_config,
    ):
        del user_id, model_config
        return graph

    monkeypatch.setattr(ConversationAgentFactory, "create", create_graph)

    class RejectingChannel:
        async def sse(self, *args, **kwargs):
            del args, kwargs
            raise RunAlreadyActive(
                active_run="run-active",
                requested_run="run-preflight",
            )

    resources = cast(
        ApplicationResources,
        SimpleNamespace(
            settings=SimpleNamespace(tavily_api_key=None),
            agent_persistence=object(),
            sandbox_manager=object(),
            tinkerfin=TinkerFin(
                run_coordinator=InMemoryRunCoordinator[str](
                    key_resolver=lambda principal: principal
                )
            ),
            conversation_channel=RejectingChannel(),
            conversation_projector=ProjectionProbe(),
        ),
    )
    request = ChatRequest.model_validate(
        {
            "threadId": thread.thread_id,
            "runId": "run-preflight",
            "state": {},
            "messages": [],
            "tools": [],
            "context": [],
            "forwardedProps": {"model": "main", "mode": "default"},
            "resume": [
                {
                    "interruptId": interrupt_id,
                    "status": "resolved",
                    "payload": {"type": "approve"},
                }
            ],
        }
    )

    with pytest.raises(BusinessException) as caught:
        await ConversationChatService(
            session,
            user=UserContext(
                user_id=7,
                username="alice",
                display_name="Alice",
                roles=(),
                disabled=False,
            ),
            resources=resources,
        ).start(request, last_event_id="0")

    assert caught.value.message == "会话当前状态不允许启动新的运行"
    stored_interrupt = await session.scalar(
        select(ConversationInterrupt).where(
            ConversationInterrupt.conversation_thread_id == thread.id,
            ConversationInterrupt.interrupt_id == interrupt_id,
        )
    )
    stored_run = await repository.get_run(
        thread_pk=thread.id,
        run_id="run-preflight",
    )
    assert stored_interrupt is not None
    assert stored_interrupt.status == "pending"
    assert stored_interrupt.resolved_run_id is None
    assert stored_run is None


@pytest.mark.parametrize(
    ("sse_succeeds", "close_fails"),
    [(True, False), (True, True), (False, False)],
)
async def test_cancelled_preflight_waits_for_a_definitive_messaging_outcome(
    session: AsyncSession,
    monkeypatch,
    sse_succeeds: bool,
    close_fails: bool,
) -> None:
    """HTTP 取消不得中断 Messaging 所有权判定或误删已启动 run"""

    await AgentModelService(AgentModelRepository(session)).upsert(
        AgentModelWrite(
            model_id="main",
            display_name="Main",
            provider="openai",
            model_name="provider-main",
            base_url="https://models.example.test/v1",
            api_key=SecretStr("secret"),
            enabled=True,
            is_default=True,
        )
    )
    repository = ConversationRepository(session)
    thread = await repository.create_thread(
        user_id=7,
        thread_id=f"thread-cancelled-preflight-{sse_succeeds}",
        title="预握手取消",
        model_id="main",
    )
    now = datetime.now(UTC).replace(tzinfo=None)
    interrupt_id = f"interrupt-cancelled-preflight-{sse_succeeds}"
    session.add(
        ConversationInterrupt(
            conversation_thread_id=thread.id,
            run_id="run-interrupted",
            resolved_run_id=None,
            interrupt_id=interrupt_id,
            status="pending",
            reason="tool_call",
            message="确认写入",
            request_json={"id": interrupt_id, "reason": "tool_call"},
            resume_json=None,
            created_at=now,
            resolved_at=None,
            updated_at=now,
        )
    )
    await repository.commit()
    snapshot = SimpleNamespace(
        config={"configurable": {"checkpoint_ns": ""}},
        values={
            "messages": [
                AIMessage(
                    content="",
                    id="assistant-cancelled-preflight",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {"file_path": "/cancelled.txt", "content": "ok"},
                            "id": "call-cancelled-preflight",
                            "type": "tool_call",
                        }
                    ],
                )
            ]
        },
        tasks=(),
        interrupts=(
            SimpleNamespace(
                id=interrupt_id,
                value={
                    "action_requests": [
                        {
                            "name": "write_file",
                            "args": {
                                "file_path": "/cancelled.txt",
                                "content": "ok",
                            },
                            "description": "写入取消审计文件",
                        }
                    ],
                    "review_configs": [
                        {
                            "action_name": "write_file",
                            "allowed_decisions": ["approve", "reject"],
                        }
                    ],
                },
            ),
        ),
    )

    async def get_state(config, *, subgraphs: bool = False):
        del config, subgraphs
        return snapshot

    async def stream_events(*args, **kwargs):
        del args, kwargs
        if False:
            yield None

    graph = SimpleNamespace(aget_state=get_state, astream=stream_events)

    async def create_graph(
        _factory: ConversationAgentFactory,
        *,
        user_id: int,
        model_config,
    ):
        del user_id, model_config
        return graph

    monkeypatch.setattr(ConversationAgentFactory, "create", create_graph)
    entered = asyncio.Event()
    release = asyncio.Event()

    class ClosedBody:
        def __init__(self) -> None:
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self) -> bytes:
            raise StopAsyncIteration

        async def aclose(self) -> None:
            self.closed = True
            if close_fails:
                raise RuntimeError("subscription close failed")

    body = ClosedBody()

    class DelayedChannel:
        async def sse(self, *args, **kwargs):
            del args, kwargs
            entered.set()
            await release.wait()
            if not sse_succeeds:
                raise RunAlreadyActive(
                    active_run="run-active",
                    requested_run="run-cancelled-preflight",
                )
            return body

    resources = cast(
        ApplicationResources,
        SimpleNamespace(
            settings=SimpleNamespace(tavily_api_key=None),
            agent_persistence=object(),
            sandbox_manager=object(),
            tinkerfin=TinkerFin(
                run_coordinator=InMemoryRunCoordinator[str](
                    key_resolver=lambda principal: principal
                )
            ),
            conversation_channel=DelayedChannel(),
            conversation_projector=ProjectionProbe(),
        ),
    )
    request = ChatRequest.model_validate(
        {
            "threadId": thread.thread_id,
            "runId": "run-cancelled-preflight",
            "state": {},
            "messages": [],
            "tools": [],
            "context": [],
            "forwardedProps": {"model": "main", "mode": "default"},
            "resume": [
                {
                    "interruptId": interrupt_id,
                    "status": "resolved",
                    "payload": {"type": "approve"},
                }
            ],
        }
    )
    start_task = asyncio.create_task(
        ConversationChatService(
            session,
            user=UserContext(
                user_id=7,
                username="alice",
                display_name="Alice",
                roles=(),
                disabled=False,
            ),
            resources=resources,
        ).start(request, last_event_id="0"),
        name="test-cancelled-preflight",
    )
    await asyncio.wait_for(entered.wait(), timeout=2)

    start_task.cancel()
    await asyncio.sleep(0)
    assert not start_task.done()
    release.set()
    result = (await asyncio.gather(start_task, return_exceptions=True))[0]

    assert isinstance(result, asyncio.CancelledError)
    stored_interrupt = await session.scalar(
        select(ConversationInterrupt).where(
            ConversationInterrupt.conversation_thread_id == thread.id,
            ConversationInterrupt.interrupt_id == interrupt_id,
        )
    )
    stored_run = await repository.get_run(
        thread_pk=thread.id,
        run_id=request.run_id,
    )
    assert stored_interrupt is not None
    if sse_succeeds:
        assert body.closed is True
        assert stored_interrupt.resolved_run_id == request.run_id
        assert stored_run is not None
    else:
        assert body.closed is False
        assert stored_interrupt.resolved_run_id is None
        assert stored_run is None


async def test_delete_rejects_a_committed_run_before_messaging_preflight(
    database: Database,
    monkeypatch,
) -> None:
    """DELETE 不得利用 run commit 与首事件投影之间的 thread 状态窗口"""

    user = UserContext(
        user_id=7,
        username="alice",
        display_name="Alice",
        roles=(),
        disabled=False,
    )
    thread_id = "thread-delete-preflight-race"
    async with database.session() as setup_session:
        await AgentModelService(AgentModelRepository(setup_session)).upsert(
            AgentModelWrite(
                model_id="main",
                display_name="Main",
                provider="openai",
                model_name="provider-main",
                base_url="https://models.example.test/v1",
                api_key=SecretStr("secret"),
                enabled=True,
                is_default=True,
            )
        )
        repository = ConversationRepository(setup_session)
        thread = await repository.create_thread(
            user_id=user.user_id,
            thread_id=thread_id,
            title="删除竞态",
            model_id="main",
        )
        await repository.commit()

    async def stream_events(*args, **kwargs):
        del args, kwargs
        if False:
            yield None

    graph = SimpleNamespace(astream=stream_events)

    async def create_graph(
        _factory: ConversationAgentFactory,
        *,
        user_id: int,
        model_config,
    ):
        del user_id, model_config
        return graph

    monkeypatch.setattr(ConversationAgentFactory, "create", create_graph)
    entered = asyncio.Event()
    release = asyncio.Event()
    delete_stream_calls = 0
    delete_checkpoint_calls = 0

    class Body:
        def __aiter__(self):
            return self

        async def __anext__(self) -> bytes:
            raise StopAsyncIteration

        async def aclose(self) -> None:
            return None

    class DelayedChannel:
        async def sse(self, *args, **kwargs):
            del args, kwargs
            entered.set()
            await release.wait()
            return Body()

        async def delete_stream(self, *, stream: str) -> None:
            nonlocal delete_stream_calls
            del stream
            delete_stream_calls += 1

    class Checkpointer:
        async def adelete_thread(self, thread_id: str) -> None:
            nonlocal delete_checkpoint_calls
            del thread_id
            delete_checkpoint_calls += 1

    resources = cast(
        ApplicationResources,
        SimpleNamespace(
            settings=SimpleNamespace(tavily_api_key=None),
            agent_persistence=SimpleNamespace(checkpointer=Checkpointer()),
            sandbox_manager=object(),
            tinkerfin=TinkerFin(
                run_coordinator=InMemoryRunCoordinator[str](
                    key_resolver=lambda principal: principal
                )
            ),
            conversation_channel=DelayedChannel(),
            conversation_projector=ProjectionProbe(),
        ),
    )
    request = ChatRequest.model_validate(
        {
            "threadId": thread_id,
            "runId": "run-delete-preflight-race",
            "state": {},
            "messages": [{"role": "user", "content": "等待预握手"}],
            "tools": [],
            "context": [],
            "forwardedProps": {"model": "main", "mode": "default"},
        }
    )
    async with database.session() as chat_session, database.session() as delete_session:
        start_task = asyncio.create_task(
            ConversationChatService(
                chat_session,
                user=user,
                resources=resources,
            ).start(request, last_event_id="0"),
            name="test-delete-preflight-chat",
        )
        await asyncio.wait_for(entered.wait(), timeout=2)
        async with database.session() as verification_session:
            running = await ConversationRepository(verification_session).get_run(
                thread_pk=thread.id,
                run_id=request.run_id,
            )
        assert running is not None and running.status == "running"

        try:
            with pytest.raises(BusinessException) as caught:
                await ConversationCommandService(
                    ConversationRepository(delete_session),
                    user_id=user.user_id,
                    resources=resources,
                ).delete(thread_id=thread_id)
            assert caught.value.error_code.http_status == 409
        finally:
            release.set()
            prepared = await start_task
            assert await _collect_frames(prepared.body) == []

    assert delete_stream_calls == 0
    assert delete_checkpoint_calls == 0
    async with database.session() as verification_session:
        stored_thread = await ConversationRepository(verification_session).get_thread(
            user_id=user.user_id,
            thread_id=thread_id,
        )
        stored_run = await ConversationRepository(verification_session).get_run(
            thread_pk=thread.id,
            run_id=request.run_id,
        )
    assert stored_thread is not None
    assert stored_run is not None


async def test_delete_waits_for_completed_run_attachment_preflight(
    database: Database,
    monkeypatch,
) -> None:
    """已完成 run 的同 ID 重放必须先完成 prepare 才能被删除"""

    user = UserContext(
        user_id=7,
        username="alice",
        display_name="Alice",
        roles=(),
        disabled=False,
    )
    thread_id = "thread-delete-completed-attach"
    request = ChatRequest.model_validate(
        {
            "threadId": thread_id,
            "runId": "run-completed-attach",
            "state": {},
            "messages": [{"role": "user", "content": "重放完成 run"}],
            "tools": [],
            "context": [],
            "forwardedProps": {"model": "main", "mode": "default"},
        }
    )
    async with database.session() as setup_session:
        await AgentModelService(AgentModelRepository(setup_session)).upsert(
            AgentModelWrite(
                model_id="main",
                display_name="Main",
                provider="openai",
                model_name="provider-main",
                base_url="https://models.example.test/v1",
                api_key=SecretStr("secret"),
                enabled=True,
                is_default=True,
            )
        )
        repository = ConversationRepository(setup_session)
        thread = await repository.create_thread(
            user_id=user.user_id,
            thread_id=thread_id,
            title="完成 run 重放",
            model_id="main",
        )
        message_id = (
            "message-"
            f"{uuid5(NAMESPACE_URL, f'tinkerfin-studio:{user.user_id}:{thread_id}:{request.run_id}:0')}"
        )
        run = await repository.create_main_run(
            thread_id=thread.id,
            run_id=request.run_id,
            model_id="main",
            input_json=cast(
                dict[str, object],
                request.normalized(
                    thread_id=thread_id,
                    message_ids=(message_id,),
                ),
            ),
            config_json={"thread_id": f"users/{user.user_id}/threads/{thread_id}"},
        )
        run.status = "success"
        run.finished_at = datetime.now(UTC).replace(tzinfo=None)
        await repository.commit()

    thread_guard = asyncio.Lock()
    guard_holders: set[int] = set()
    original_lock_thread = ConversationRepository.lock_thread
    original_commit = ConversationRepository.commit
    original_rollback = ConversationRepository.rollback

    async def guarded_lock_thread(
        repository: ConversationRepository,
        thread_pk: int,
    ):
        await thread_guard.acquire()
        try:
            value = await original_lock_thread(repository, thread_pk)
        except BaseException:
            thread_guard.release()
            raise
        if value is None:
            thread_guard.release()
        else:
            guard_holders.add(id(repository))
        return value

    async def guarded_commit(repository: ConversationRepository) -> None:
        try:
            await original_commit(repository)
        finally:
            if id(repository) in guard_holders:
                guard_holders.remove(id(repository))
                thread_guard.release()

    async def guarded_rollback(repository: ConversationRepository) -> None:
        try:
            await original_rollback(repository)
        finally:
            if id(repository) in guard_holders:
                guard_holders.remove(id(repository))
                thread_guard.release()

    monkeypatch.setattr(ConversationRepository, "lock_thread", guarded_lock_thread)
    monkeypatch.setattr(ConversationRepository, "commit", guarded_commit)
    monkeypatch.setattr(ConversationRepository, "rollback", guarded_rollback)

    async def stream_events(*args, **kwargs):
        del args, kwargs
        if False:
            yield None

    graph = SimpleNamespace(astream=stream_events)

    async def create_graph(
        _factory: ConversationAgentFactory,
        *,
        user_id: int,
        model_config,
    ):
        del user_id, model_config
        return graph

    monkeypatch.setattr(ConversationAgentFactory, "create", create_graph)
    entered = asyncio.Event()
    release = asyncio.Event()
    delete_stream_calls = 0
    delete_checkpoint_calls = 0

    class Body:
        def __aiter__(self):
            return self

        async def __anext__(self) -> bytes:
            raise StopAsyncIteration

    class DelayedChannel:
        async def sse(self, *args, **kwargs):
            del args, kwargs
            entered.set()
            await release.wait()
            return Body()

        async def delete_stream(self, *, stream: str) -> None:
            nonlocal delete_stream_calls
            del stream
            delete_stream_calls += 1

    class Checkpointer:
        async def adelete_thread(self, thread_id: str) -> None:
            nonlocal delete_checkpoint_calls
            del thread_id
            delete_checkpoint_calls += 1

    resources = cast(
        ApplicationResources,
        SimpleNamespace(
            settings=SimpleNamespace(tavily_api_key=None),
            agent_persistence=SimpleNamespace(checkpointer=Checkpointer()),
            sandbox_manager=object(),
            tinkerfin=TinkerFin(
                run_coordinator=InMemoryRunCoordinator[str](
                    key_resolver=lambda principal: principal
                )
            ),
            conversation_channel=DelayedChannel(),
            conversation_projector=ProjectionProbe(),
        ),
    )
    async with database.session() as chat_session, database.session() as delete_session:
        start_task = asyncio.create_task(
            ConversationChatService(
                chat_session,
                user=user,
                resources=resources,
            ).start(request, last_event_id="0"),
            name="test-completed-attach-chat",
        )
        await asyncio.wait_for(entered.wait(), timeout=2)
        delete_task = asyncio.create_task(
            ConversationCommandService(
                ConversationRepository(delete_session),
                user_id=user.user_id,
                resources=resources,
            ).delete(thread_id=thread_id),
            name="test-completed-attach-delete",
        )
        await asyncio.sleep(0.05)

        try:
            assert not delete_task.done()
        finally:
            release.set()
        prepared = await start_task
        assert await _collect_frames(prepared.body) == []
        await delete_task

    assert delete_stream_calls == 1
    assert delete_checkpoint_calls == 1
    async with database.session() as verification_session:
        assert (
            await ConversationRepository(verification_session).get_thread(
                user_id=user.user_id,
                thread_id=thread_id,
            )
            is None
        )
        assert (
            await ConversationRepository(verification_session).get_run(
                thread_pk=thread.id,
                run_id=request.run_id,
            )
            is None
        )


async def test_immediate_resume_reconciles_committed_interrupt_before_claim(
    database: Database,
    monkeypatch,
) -> None:
    """客户端立即恢复时必须先把已提交 interrupt 追赶到 MySQL"""

    user = UserContext(
        user_id=7,
        username="alice",
        display_name="Alice",
        roles=(),
        disabled=False,
    )
    async with database.session() as setup_session:
        await AgentModelService(AgentModelRepository(setup_session)).upsert(
            AgentModelWrite(
                model_id="main",
                display_name="Main",
                provider="openai",
                model_name="provider-main",
                base_url="https://models.example.test/v1",
                api_key=SecretStr("secret"),
                enabled=True,
                is_default=True,
            )
        )
    builder = StateGraph(MessagesState)

    async def propose_write(state: MessagesState) -> dict[str, object]:
        del state
        return {
            "messages": [
                AIMessage(
                    content="",
                    id="assistant-immediate-resume",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {"file_path": "/immediate.txt", "content": "ok"},
                            "id": "call-immediate-resume",
                            "type": "tool_call",
                        }
                    ],
                )
            ]
        }

    async def review_write(state: MessagesState) -> dict[str, object]:
        del state
        decision = interrupt(
            {
                "action_requests": [
                    {
                        "name": "write_file",
                        "args": {"file_path": "/immediate.txt", "content": "ok"},
                        "description": "写入即时恢复文件",
                    }
                ],
                "review_configs": [
                    {
                        "action_name": "write_file",
                        "allowed_decisions": ["approve", "reject"],
                    }
                ],
            }
        )
        return {
            "messages": [
                ToolMessage(
                    content=str(decision),
                    name="write_file",
                    tool_call_id="call-immediate-resume",
                )
            ]
        }

    builder.add_node("propose_write", propose_write)
    builder.add_node("review_write", review_write)
    builder.add_edge(START, "propose_write")
    builder.add_edge("propose_write", "review_write")
    builder.add_edge("review_write", END)
    graph = builder.compile(checkpointer=MemorySaver())

    async def create_graph(
        _factory: ConversationAgentFactory,
        *,
        user_id: int,
        model_config,
    ):
        del user_id, model_config
        return graph

    monkeypatch.setattr(ConversationAgentFactory, "create", create_graph)
    backend = MemoryBackend()
    probe = ProjectionProbe()
    async with Messaging(backend=backend) as messaging:
        channel = messaging.channel(
            name="studio-conversation-agui",
            codec=AgUiCodec(),
        )
        initial_resources = cast(
            ApplicationResources,
            SimpleNamespace(
                settings=SimpleNamespace(tavily_api_key=None),
                agent_persistence=object(),
                sandbox_manager=object(),
                tinkerfin=TinkerFin(
                    run_coordinator=InMemoryRunCoordinator[str](
                        key_resolver=lambda principal: principal
                    )
                ),
                conversation_channel=channel,
                conversation_projector=probe,
            ),
        )
        async with database.session() as initial_session:
            initial = await ConversationChatService(
                initial_session,
                user=user,
                resources=initial_resources,
            ).start(
                ChatRequest.model_validate(
                    {
                        "threadId": "",
                        "runId": "run-immediate-interrupt",
                        "state": {},
                        "messages": [{"role": "user", "content": "写入文件"}],
                        "tools": [],
                        "context": [],
                        "forwardedProps": {"model": "main", "mode": "default"},
                    }
                ),
                last_event_id="0",
            )
            initial_frames = await _collect_frames(initial.body)
            thread = await ConversationRepository(initial_session).get_thread(
                user_id=user.user_id,
                thread_id=initial.thread_id,
            )
            assert thread is not None
            thread_pk = thread.id
        initial_events = [
            json.loads(frame.split(b"data: ", 1)[1]) for frame in initial_frames
        ]
        terminal = initial_events[-1]
        assert terminal["type"] == "RUN_FINISHED"
        assert terminal["outcome"]["type"] == "interrupt"
        interrupt_id = terminal["outcome"]["interrupts"][0]["id"]
        async with database.session() as verification_session:
            pending_rows = await verification_session.scalar(
                select(func.count(ConversationInterrupt.id)).where(
                    ConversationInterrupt.conversation_thread_id == thread_pk
                )
            )
        assert pending_rows == 0

        projector = ConversationProjectionCoordinator(
            database=database,
            backend=backend,
            channel="studio-conversation-agui",
        )
        resume_resources = cast(
            ApplicationResources,
            SimpleNamespace(
                settings=SimpleNamespace(tavily_api_key=None),
                agent_persistence=object(),
                sandbox_manager=object(),
                tinkerfin=TinkerFin(
                    run_coordinator=InMemoryRunCoordinator[str](
                        key_resolver=lambda principal: principal
                    )
                ),
                conversation_channel=channel,
                conversation_projector=projector,
            ),
        )
        try:
            async with database.session() as resume_session:
                resumed = await ConversationChatService(
                    resume_session,
                    user=user,
                    resources=resume_resources,
                ).start(
                    ChatRequest.model_validate(
                        {
                            "threadId": initial.thread_id,
                            "runId": "run-immediate-resume",
                            "state": {},
                            "messages": [],
                            "tools": [],
                            "context": [],
                            "forwardedProps": {
                                "model": "main",
                                "mode": "default",
                            },
                            "resume": [
                                {
                                    "interruptId": interrupt_id,
                                    "status": "resolved",
                                    "payload": {"type": "approve"},
                                }
                            ],
                        }
                    ),
                    last_event_id="0",
                )
                resumed_frames = await _collect_frames(resumed.body)
            projected_interrupt = None
            for _ in range(100):
                async with database.session() as verification_session:
                    projected_interrupt = await verification_session.scalar(
                        select(ConversationInterrupt).where(
                            ConversationInterrupt.conversation_thread_id == thread_pk,
                            ConversationInterrupt.interrupt_id == interrupt_id,
                        )
                    )
                if projected_interrupt is not None and (
                    projected_interrupt.status == "resolved"
                ):
                    break
                await asyncio.sleep(0.01)
        finally:
            await projector.aclose()

    resumed_events = [
        json.loads(frame.split(b"data: ", 1)[1]) for frame in resumed_frames
    ]
    assert resumed_events[0]["type"] == "RUN_STARTED"
    assert resumed_events[-1]["type"] == "RUN_FINISHED"
    assert projected_interrupt is not None
    assert projected_interrupt.status == "resolved"
    assert projected_interrupt.resolved_run_id == "run-immediate-resume"


async def test_cancel_waits_for_the_durable_cancelled_terminal(
    session: AsyncSession,
    monkeypatch,
) -> None:
    """取消接口返回前必须提交唯一 cancelled RUN_ERROR"""

    await AgentModelService(AgentModelRepository(session)).upsert(
        AgentModelWrite(
            model_id="main",
            display_name="Main",
            provider="openai",
            model_name="provider-main",
            base_url="https://models.example.test/v1",
            api_key=SecretStr("secret"),
            enabled=True,
            is_default=True,
        )
    )
    started = asyncio.Event()
    release = asyncio.Event()
    builder = StateGraph(MessagesState)

    async def block(state: MessagesState) -> dict[str, object]:
        del state
        started.set()
        await release.wait()
        return {}

    builder.add_node("block", block)
    builder.add_edge(START, "block")
    builder.add_edge("block", END)
    graph = builder.compile()

    async def create_graph(
        _factory: ConversationAgentFactory,
        *,
        user_id: int,
        model_config,
    ):
        del user_id, model_config
        return graph

    monkeypatch.setattr(ConversationAgentFactory, "create", create_graph)
    backend = MemoryBackend()
    probe = ProjectionProbe()
    user = UserContext(
        user_id=7,
        username="alice",
        display_name="Alice",
        roles=(),
        disabled=False,
    )
    async with Messaging(backend=backend) as messaging:
        resources = cast(
            ApplicationResources,
            SimpleNamespace(
                settings=SimpleNamespace(tavily_api_key=None),
                agent_persistence=object(),
                sandbox_manager=object(),
                tinkerfin=TinkerFin(
                    run_coordinator=InMemoryRunCoordinator[str](
                        key_resolver=lambda principal: principal
                    )
                ),
                conversation_channel=messaging.channel(
                    name="studio-conversation-agui",
                    codec=AgUiCodec(),
                ),
                conversation_projector=probe,
            ),
        )
        service = ConversationChatService(session, user=user, resources=resources)
        prepared = await service.start(
            ChatRequest.model_validate(
                {
                    "threadId": "",
                    "runId": "run-cancel",
                    "state": {},
                    "messages": [{"role": "user", "content": "等待"}],
                    "tools": [],
                    "context": [],
                    "forwardedProps": {"model": "main", "mode": "default"},
                }
            ),
            last_event_id=None,
        )
        delivery = asyncio.create_task(
            _collect_frames(prepared.body),
            name="test-chat-delivery",
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        result = await service.cancel(
            thread_id=prepared.thread_id,
            run_id="run-cancel",
        )
        frames = await asyncio.wait_for(delivery, timeout=2)

    event_types = [json.loads(frame.split(b"data: ", 1)[1]) for frame in frames]
    assert result.cancelled is True
    assert [event["type"] for event in event_types].count("RUN_ERROR") == 1
    assert event_types[-1]["code"] == "cancelled"
    assert event_types[-1]["message"] == "聊天生成已取消"


async def _collect_frames(body) -> list[bytes]:
    return [frame async for frame in body]
