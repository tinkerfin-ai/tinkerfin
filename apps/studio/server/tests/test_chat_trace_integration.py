from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from tinkerfin import AgUiResumeRequest, RunIdentity, TinkerFin
from tinkerfin_studio.api.errors import BusinessException, ConversationErrorCode
from tinkerfin_studio.auth.types import UserContext
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.conversation.request import ChatRequest
from tinkerfin_studio.conversation.run_preparation import (
    ResumeChatIntent,
    StartChatIntent,
    classify_intent,
    prepare_run_request,
)
from tinkerfin_studio.conversation.run_registration import ConversationRunPreparer
from tinkerfin_studio.conversation.service import ConversationChatService
from tinkerfin_studio.models.repository import AgentModelRepository
from tinkerfin_studio.models.schemas import AgentModelConfig, AgentModelSave
from tinkerfin_studio.models.service import AgentModelService
from tinkerfin_studio.resources import ApplicationResources


def _model(model_id: str = "model-main") -> AgentModelConfig:
    return AgentModelConfig(
        model_id=model_id,
        display_name="主模型",
        provider="deepseek",
        model_name="deepseek-chat",
        base_url="https://example.invalid/v1",
        api_key=SecretStr("secret"),
        reasoning_enabled=False,
    )


@pytest.fixture(autouse=True)
async def stored_model_configs(session):
    """登记测试使用的真实模型配置，运行登记校验当前配置未变化"""
    service = AgentModelService(AgentModelRepository(session, user_id=1))
    for model_id in ("model-main", "model-other"):
        await service.save_settings(
            AgentModelSave.model_validate(_model(model_id).model_dump())
        )


def _ordinary_request(
    *,
    thread_id: str = "",
    run_id: str = "run-1",
    model_id: str = "model-main",
    parent_run_id: str | None = None,
) -> ChatRequest:
    payload = {
        "threadId": thread_id,
        "runId": run_id,
        "state": {},
        "messages": [{"role": "user", "content": "完成任务"}],
        "tools": [],
        "context": [],
        "forwardedProps": {
            "model": model_id,
            "command": {"plan": "off"},
        },
    }
    if parent_run_id is not None:
        payload["parentRunId"] = parent_run_id
    return ChatRequest.model_validate(payload)


def _resume_request(
    *,
    thread_id: str,
    run_id: str,
    model_id: str = "model-main",
) -> ChatRequest:
    return ChatRequest.model_validate(
        {
            "threadId": thread_id,
            "runId": run_id,
            "state": {},
            "messages": [],
            "tools": [],
            "context": [],
            "forwardedProps": {
                "model": model_id,
                "command": {"plan": "off"},
            },
            "resume": [
                {
                    "interruptId": "interrupt-root#0",
                    "status": "resolved",
                    "payload": {"type": "approve"},
                }
            ],
        }
    )


async def test_run_registration_persists_model_and_input(session, attachments) -> None:
    request = _ordinary_request()
    intent = classify_intent(request)
    assert isinstance(intent, StartChatIntent)
    preparer = ConversationRunPreparer(session, user_id=1, attachments=attachments)
    resolved = await preparer.resolve_thread(request, intent=intent)
    prepared = prepare_run_request(
        request,
        user_id=1,
        thread_id=resolved.thread.thread_id,
    )

    execution = await preparer.register(
        intent=intent,
        prepared=prepared,
        model=_model(),
        thread=resolved.thread,
        thread_created=resolved.created,
    )

    repository = ConversationRepository(session)
    registration = await repository.get_run(
        thread_pk=execution.thread.id,
        run_id=request.run_id,
    )
    assert registration is not None
    assert registration.model_id == "model-main"
    assert registration.input_json == prepared.input_json
    assert execution.thread.last_run_id is None
    assert execution.thread.status == "idle"

    await preparer.activate_started(
        thread_pk=execution.thread.id,
        identity_run_id=request.run_id,
        registered=execution.registered,
    )

    assert execution.thread.last_run_id == request.run_id
    assert execution.thread.last_model == "model-main"
    assert execution.thread.status == "running"
    assert registration.status == "starting"


async def test_resume_registration_stores_only_claim_identity(
    session, attachments
) -> None:
    repository = ConversationRepository(session)
    thread = await repository.create_thread(
        user_id=1,
        thread_id="thread-resume",
        title="恢复会话",
        model_id="model-main",
    )
    await repository.create_run_registration(
        thread_id=thread.id,
        run_id="run-interrupted",
        parent_run_id=None,
        model_id="model-main",
        input_json={"runId": "run-interrupted"},
    )
    thread.last_run_id = "run-interrupted"
    thread.status = "waiting_approval"
    thread.has_pending_interrupt = True
    thread.pending_interaction_kind = "tool_approval"
    await repository.commit()
    request = _resume_request(thread_id=thread.thread_id, run_id="run-resume")
    intent = classify_intent(request)
    assert isinstance(intent, ResumeChatIntent)
    prepared = prepare_run_request(
        request,
        user_id=1,
        thread_id=thread.thread_id,
    )

    execution = await ConversationRunPreparer(
        session, user_id=1, attachments=attachments
    ).register(
        intent=intent,
        prepared=prepared,
        model=_model(),
        thread=thread,
    )

    assert isinstance(execution.resume, AgUiResumeRequest)
    claims = await repository.list_claims_for_update(
        thread_pk=thread.id,
        interrupt_ids=frozenset({"interrupt-root#0"}),
    )
    assert len(claims) == 1
    assert claims[0].source_run_id == "run-interrupted"
    assert claims[0].claimed_run_id == "run-resume"
    assert not hasattr(claims[0], "request_json")
    assert not hasattr(claims[0], "resume_json")


@pytest.mark.parametrize("continuation", ["resume", "branch"])
async def test_continuation_rejects_a_model_different_from_the_source_run(
    session,
    continuation: str,
    attachments,
) -> None:
    repository = ConversationRepository(session)
    thread = await repository.create_thread(
        user_id=1,
        thread_id=f"thread-model-fence-{continuation}",
        title="模型继承",
        model_id="model-main",
    )
    await repository.create_run_registration(
        thread_id=thread.id,
        run_id="run-source",
        parent_run_id=None,
        model_id="model-main",
        input_json={"runId": "run-source"},
    )
    thread.last_run_id = "run-source"
    if continuation == "resume":
        thread.status = "waiting_approval"
        thread.has_pending_interrupt = True
        thread.pending_interaction_kind = "tool_approval"
        request = _resume_request(
            thread_id=thread.thread_id,
            run_id="run-continuation",
            model_id="model-other",
        )
    else:
        request = _ordinary_request(
            thread_id=thread.thread_id,
            run_id="run-continuation",
            model_id="model-other",
            parent_run_id="run-source",
        )
    thread_pk = thread.id
    await repository.commit()
    intent = classify_intent(request)
    prepared = prepare_run_request(
        request,
        user_id=1,
        thread_id=thread.thread_id,
    )

    with pytest.raises(BusinessException) as captured:
        await ConversationRunPreparer(
            session, user_id=1, attachments=attachments
        ).register(
            intent=intent,
            prepared=prepared,
            model=_model("model-other"),
            thread=thread,
        )

    assert captured.value.error_code is ConversationErrorCode.RUN_IDENTITY_CONFLICT
    assert (
        await repository.get_run(
            thread_pk=thread_pk,
            run_id="run-continuation",
        )
        is None
    )


async def test_same_run_rejects_a_changed_registered_model(
    session, attachments
) -> None:
    request = _ordinary_request(run_id="run-model")
    intent = classify_intent(request)
    assert isinstance(intent, StartChatIntent)
    preparer = ConversationRunPreparer(session, user_id=1, attachments=attachments)
    resolved = await preparer.resolve_thread(request, intent=intent)
    prepared = prepare_run_request(
        request,
        user_id=1,
        thread_id=resolved.thread.thread_id,
    )
    execution = await preparer.register(
        intent=intent,
        prepared=prepared,
        model=_model(),
        thread=resolved.thread,
        thread_created=resolved.created,
    )
    repository = ConversationRepository(session)
    registration = await repository.get_run(
        thread_pk=execution.thread.id,
        run_id=request.run_id,
    )
    assert registration is not None
    registration.model_id = "model-other"
    await repository.commit()

    with pytest.raises(BusinessException) as captured:
        await preparer.register(
            intent=intent,
            prepared=prepared,
            model=_model(),
            thread=execution.thread,
        )

    assert captured.value.error_code is ConversationErrorCode.RUN_IDENTITY_CONFLICT


class _Channel:
    def __init__(self, *, close_error: BaseException | None = None) -> None:
        self.after: int | None = None
        self.body: _Body | None = None
        self._close_error = close_error

    async def sse(
        self,
        _source,
        *,
        after: int | None = None,
        on_source_ready=None,
        on_committed=None,
        on_delivery_not_started=None,
    ):
        del on_delivery_not_started, on_committed
        self.after = after
        if on_source_ready is not None:
            await on_source_ready()

        self.body = _Body(close_error=self._close_error)
        return self.body


class _Body:
    def __init__(self, *, close_error: BaseException | None = None) -> None:
        self.closed = False
        self._close_error = close_error

    def __aiter__(self) -> _Body:
        return self

    async def __anext__(self) -> bytes:
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.closed = True
        if self._close_error is not None:
            raise self._close_error


class _TraceCoordinator:
    def __init__(self, session: AsyncSession | None = None) -> None:
        self.ensured: list[RunIdentity] = []
        self._session = session
        self.reconcile_transaction_states: list[bool] = []

    async def recover_preparing(self, *, thread_pk: int | None = None):
        del thread_pk
        return frozenset()

    async def reconcile(self, *, thread_pk: int, identity: RunIdentity) -> int:
        del thread_pk, identity
        if self._session is not None:
            self.reconcile_transaction_states.append(self._session.in_transaction())
        return 1

    def ensure(self, *, thread_pk: int, identity: RunIdentity) -> None:
        del thread_pk
        self.ensured.append(identity)


class _FailingTraceCoordinator(_TraceCoordinator):
    def ensure(self, *, thread_pk: int, identity: RunIdentity) -> None:
        super().ensure(thread_pk=thread_pk, identity=identity)
        raise RuntimeError("trace follow unavailable")


async def test_chat_service_uses_messaging_only_for_delivery(
    database,
    session,
    attachments,
) -> None:
    await AgentModelService(AgentModelRepository(session, user_id=1)).save_settings(
        AgentModelSave(
            model_id="model-main",
            display_name="主模型",
            provider="deepseek",
            model_name="deepseek-chat",
            base_url="https://example.invalid/v1",
            api_key=SecretStr("secret"),
            enabled=True,
            is_default=True,
        )
    )
    channel = _Channel()
    trace = _TraceCoordinator()
    resources = cast(
        ApplicationResources,
        SimpleNamespace(
            database=database,
            attachments=attachments,
            model_http_client=None,
            agent_persistence=object(),
            sandbox_manager=object(),
            tinkerfin=TinkerFin(),
            settings=SimpleNamespace(tavily_api_key=None, model_allowed_origins=()),
            conversation_channel=channel,
            conversation_trace=trace,
        ),
    )
    service = ConversationChatService(
        session,
        user=UserContext(
            user_id=1,
            username="user",
            display_name="用户",
            roles=(),
            disabled=False,
        ),
        resources=resources,
    )

    prepared = await service.start(_ordinary_request(), last_event_id=None)

    assert channel.after is None
    assert len(trace.ensured) == 1
    assert trace.ensured[0].run_id == "run-1"
    repository = ConversationRepository(session)
    thread = await repository.get_thread(user_id=1, thread_id=prepared.thread_id)
    assert thread is not None
    assert thread.last_run_id == "run-1"
    assert thread.status == "running"
    assert [chunk async for chunk in prepared.body] == []


@pytest.mark.parametrize(
    ("close_error", "expected_error"),
    (
        pytest.param(None, RuntimeError, id="close-succeeds"),
        pytest.param(ValueError("body close failed"), RuntimeError, id="close-fails"),
        pytest.param(
            asyncio.CancelledError("body close cancelled"),
            asyncio.CancelledError,
            id="close-cancelled",
        ),
    ),
)
async def test_chat_service_closes_sse_body_when_trace_follow_cannot_start(
    database,
    session,
    attachments,
    close_error: BaseException | None,
    expected_error: type[BaseException],
) -> None:
    """Trace follow 注册失败时立即释放尚未交给 HTTP 的 SSE 内容"""

    await AgentModelService(AgentModelRepository(session, user_id=1)).save_settings(
        AgentModelSave(
            model_id="model-main",
            display_name="主模型",
            provider="deepseek",
            model_name="deepseek-chat",
            base_url="https://example.invalid/v1",
            api_key=SecretStr("secret"),
            enabled=True,
            is_default=True,
        )
    )
    channel = _Channel(close_error=close_error)
    trace = _FailingTraceCoordinator()
    resources = cast(
        ApplicationResources,
        SimpleNamespace(
            database=database,
            attachments=attachments,
            model_http_client=None,
            agent_persistence=object(),
            sandbox_manager=object(),
            tinkerfin=TinkerFin(),
            settings=SimpleNamespace(tavily_api_key=None, model_allowed_origins=()),
            conversation_channel=channel,
            conversation_trace=trace,
        ),
    )
    service = ConversationChatService(
        session,
        user=UserContext(
            user_id=1,
            username="user",
            display_name="用户",
            roles=(),
            disabled=False,
        ),
        resources=resources,
    )

    with pytest.raises(expected_error) as caught:
        await service.start(_ordinary_request(), last_event_id=None)

    assert channel.body is not None
    assert channel.body.closed
    if isinstance(close_error, Exception):
        assert isinstance(caught.value, RuntimeError)
        assert "body close failed" in " ".join(getattr(caught.value, "__notes__", ()))
        assert caught.value.__cause__ is close_error
    elif isinstance(close_error, asyncio.CancelledError):
        assert isinstance(caught.value.__cause__, RuntimeError)
        assert "trace follow unavailable" in str(caught.value.__cause__)


async def test_previous_head_reconcile_releases_the_request_transaction(
    database,
    session,
    attachments,
) -> None:
    """共享 Trace 查询前必须归还业务 Session 的池连接"""

    await AgentModelService(AgentModelRepository(session, user_id=1)).save_settings(
        AgentModelSave(
            model_id="model-main",
            display_name="主模型",
            provider="deepseek",
            model_name="deepseek-chat",
            base_url="https://example.invalid/v1",
            api_key=SecretStr("secret"),
            enabled=True,
            is_default=True,
        )
    )
    repository = ConversationRepository(session)
    thread = await repository.create_thread(
        user_id=1,
        thread_id="thread-existing",
        title="已有会话",
        model_id="model-main",
    )
    previous = await repository.create_run_registration(
        thread_id=thread.id,
        run_id="run-previous",
        parent_run_id=None,
        model_id="model-main",
        input_json={"runId": "run-previous"},
    )
    previous.status = "succeeded"
    thread.last_run_id = previous.run_id
    thread.status = "idle"
    await repository.commit()
    channel = _Channel()
    trace = _TraceCoordinator(session)
    resources = cast(
        ApplicationResources,
        SimpleNamespace(
            database=database,
            attachments=attachments,
            model_http_client=None,
            agent_persistence=object(),
            sandbox_manager=object(),
            tinkerfin=TinkerFin(),
            settings=SimpleNamespace(tavily_api_key=None, model_allowed_origins=()),
            conversation_channel=channel,
            conversation_trace=trace,
        ),
    )
    service = ConversationChatService(
        session,
        user=UserContext(
            user_id=1,
            username="user",
            display_name="用户",
            roles=(),
            disabled=False,
        ),
        resources=resources,
    )

    prepared = await service.start(
        _ordinary_request(thread_id=thread.thread_id, run_id="run-next"),
        last_event_id=None,
    )

    assert trace.reconcile_transaction_states == [False]
    assert [chunk async for chunk in prepared.body] == []


@pytest.mark.parametrize("ending", ["title", "finish", "disconnect"])
async def test_title_notification_shares_chat_stream_and_response_lifetime(
    database,
    session,
    attachments,
    monkeypatch,
    ending,
):
    """真实Messaging与模型HTTP边界覆盖同流标题、结束不等待及断连清理"""
    import json
    from collections.abc import AsyncGenerator

    import httpx
    from ag_ui.core import BaseEvent, RunFinishedEvent, RunStartedEvent

    from tinkerfin_messaging import Messaging

    main_release, model_requested, model_release = (asyncio.Event() for _ in range(3))
    model_calls = 0
    source_opens = 0

    class Source:
        messaging_cancel_waits_for_first_item = True

        def __init__(self, identity: RunIdentity):
            self.messaging_identity = identity
            self.iterator = self.events()

        async def events(self) -> AsyncGenerator[BaseEvent, None]:
            identity = self.messaging_identity
            yield RunStartedEvent(thread_id=identity.thread_id, run_id=identity.run_id)
            await main_release.wait()
            yield RunFinishedEvent(thread_id=identity.thread_id, run_id=identity.run_id)

        def __aiter__(self):
            return self.iterator

        async def aclose(self):
            await self.iterator.aclose()

        async def messaging_cancel_callback(self, context):
            del context
            identity = self.messaging_identity
            return [
                RunFinishedEvent(thread_id=identity.thread_id, run_id=identity.run_id)
            ]

    async def open_run(self, identity, **kwargs):
        nonlocal source_opens
        del self, kwargs
        source_opens += 1
        return Source(identity)

    async def model_http(request):
        nonlocal model_calls
        del request
        model_calls += 1
        model_requested.set()
        await model_release.wait()
        payload = {
            "id": "title",
            "object": "chat.completion.chunk",
            "model": "deepseek-chat",
            "choices": [
                {"index": 0, "delta": {"content": "任务标题"}, "finish_reason": "stop"}
            ],
        }
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content="data: " + json.dumps(payload) + "\n\ndata: [DONE]\n\n",
        )

    monkeypatch.setattr(TinkerFin, "open_agui_run", open_run)
    async with (
        Messaging() as messaging,
        httpx.AsyncClient(transport=httpx.MockTransport(model_http)) as client,
    ):
        resources = cast(
            ApplicationResources,
            SimpleNamespace(
                database=database,
                attachments=attachments,
                model_http_client=client,
                agent_persistence=object(),
                sandbox_manager=object(),
                tinkerfin=TinkerFin(),
                settings=SimpleNamespace(tavily_api_key=None, model_allowed_origins=()),
                conversation_channel=messaging.channel(name="conversation"),
                conversation_trace=_TraceCoordinator(),
            ),
        )
        service = ConversationChatService(
            session,
            user=UserContext(
                user_id=1,
                username="user",
                display_name="用户",
                roles=(),
                disabled=False,
            ),
            resources=resources,
        )
        prepared = await service.start(_ordinary_request(), last_event_id=None)
        try:
            first = await anext(prepared.body)
            assert b'"type":"RUN_STARTED"' in first
            await asyncio.wait_for(model_requested.wait(), 2)
            # 同Run重连只附着原源；新的响应不拥有标题生成任务
            attached = await service.start(
                _ordinary_request(thread_id=prepared.thread_id), last_event_id="1"
            )
            await attached.body.aclose()
            assert source_opens == 1 and model_calls == 1
            if ending == "title":
                model_release.set()
                frame = await asyncio.wait_for(anext(prepared.body), 2)
                assert b'"name":"studio.conversation.title.updated"' in frame
                assert frame.startswith(b"id: 2\n")
                value = json.loads(frame.split(b"data: ", 1)[1])["value"]
                assert value["title"] == "任务标题" and value["titleSeq"] == 2
                main_release.set()
                tail = [frame async for frame in prepared.body]
                assert len(tail) == 1 and b'"type":"RUN_FINISHED"' in tail[0]
            elif ending == "finish":
                main_release.set()
                tail = await asyncio.wait_for(_collect_body(prepared.body), 2)
                assert len(tail) == 1 and b'"type":"RUN_FINISHED"' in tail[0]
            else:
                await asyncio.wait_for(prepared.body.aclose(), 2)
                assert (
                    await resources.conversation_channel.get_run_status(
                        identity=RunIdentity(threadId=prepared.thread_id, runId="run-1")
                    )
                    == "running"
                )
            async with database.session() as check:
                thread = await ConversationRepository(check).get_thread(
                    user_id=1, thread_id=prepared.thread_id
                )
                assert thread is not None
                assert thread.title_generation_status == (
                    "succeeded" if ending == "title" else "failed"
                )
        finally:
            main_release.set()
            model_release.set()
            await prepared.body.aclose()


async def _collect_body(body):
    return [chunk async for chunk in body]
