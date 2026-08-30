from __future__ import annotations

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
from tinkerfin_studio.models.schemas import AgentModelConfig, AgentModelWrite
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
        runtime_profile="deepagents-v2",
        updated_at="2026-08-28T00:00:00",
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


async def test_run_registration_persists_model_and_runtime_profile(session) -> None:
    request = _ordinary_request()
    intent = classify_intent(request)
    assert isinstance(intent, StartChatIntent)
    preparer = ConversationRunPreparer(session, user_id=1)
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
    assert registration.runtime_profile == "deepagents-v2"
    assert registration.input_json == prepared.input_json


async def test_resume_registration_stores_only_claim_identity(session) -> None:
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
        runtime_profile="deepagents-v2",
        input_json={"runId": "run-interrupted"},
        config_json={"runtimeProfile": "deepagents-v2"},
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

    execution = await ConversationRunPreparer(session, user_id=1).register(
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
        runtime_profile="deepagents-v2",
        input_json={"runId": "run-source"},
        config_json={"runtimeProfile": "deepagents-v2"},
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
        await ConversationRunPreparer(session, user_id=1).register(
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


async def test_same_run_rejects_a_changed_registered_runtime_profile(session) -> None:
    request = _ordinary_request(run_id="run-profile")
    intent = classify_intent(request)
    assert isinstance(intent, StartChatIntent)
    preparer = ConversationRunPreparer(session, user_id=1)
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
    registration.runtime_profile = "unavailable-profile"
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
    def __init__(self) -> None:
        self.after: int | None = None
        self.body: _Body | None = None

    async def sse(
        self,
        _source,
        *,
        after: int | None = None,
        on_source_starting=None,
        on_delivery_not_started=None,
    ):
        del on_delivery_not_started
        self.after = after
        if on_source_starting is not None:
            await on_source_starting()

        self.body = _Body()
        return self.body


class _Body:
    def __init__(self) -> None:
        self.closed = False

    def __aiter__(self) -> _Body:
        return self

    async def __anext__(self) -> bytes:
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.closed = True


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
) -> None:
    await AgentModelService(AgentModelRepository(session)).upsert(
        AgentModelWrite(
            model_id="model-main",
            display_name="主模型",
            provider="deepseek",
            model_name="deepseek-chat",
            base_url="https://example.invalid/v1",
            api_key=SecretStr("secret"),
            runtime_profile="deepagents-v2",
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
            agent_persistence=object(),
            sandbox_manager=object(),
            tinkerfin_profiles={"deepagents-v2": TinkerFin()},
            settings=SimpleNamespace(tavily_api_key=None),
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
    assert [chunk async for chunk in prepared.body] == []


async def test_chat_service_closes_sse_body_when_trace_follow_cannot_start(
    database,
    session,
) -> None:
    """Trace follow 注册失败时立即释放尚未交给 HTTP 的 SSE 内容"""

    await AgentModelService(AgentModelRepository(session)).upsert(
        AgentModelWrite(
            model_id="model-main",
            display_name="主模型",
            provider="deepseek",
            model_name="deepseek-chat",
            base_url="https://example.invalid/v1",
            api_key=SecretStr("secret"),
            runtime_profile="deepagents-v2",
            enabled=True,
            is_default=True,
        )
    )
    channel = _Channel()
    trace = _FailingTraceCoordinator()
    resources = cast(
        ApplicationResources,
        SimpleNamespace(
            database=database,
            agent_persistence=object(),
            sandbox_manager=object(),
            tinkerfin_profiles={"deepagents-v2": TinkerFin()},
            settings=SimpleNamespace(tavily_api_key=None),
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

    with pytest.raises(RuntimeError, match="trace follow unavailable"):
        await service.start(_ordinary_request(), last_event_id=None)

    assert channel.body is not None
    assert channel.body.closed


async def test_previous_head_reconcile_releases_the_request_transaction(
    database,
    session,
) -> None:
    """共享 Trace 查询前必须归还业务 Session 的池连接"""

    await AgentModelService(AgentModelRepository(session)).upsert(
        AgentModelWrite(
            model_id="model-main",
            display_name="主模型",
            provider="deepseek",
            model_name="deepseek-chat",
            base_url="https://example.invalid/v1",
            api_key=SecretStr("secret"),
            runtime_profile="deepagents-v2",
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
        runtime_profile="deepagents-v2",
        input_json={"runId": "run-previous"},
        config_json={"runtimeProfile": "deepagents-v2"},
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
            agent_persistence=object(),
            sandbox_manager=object(),
            tinkerfin_profiles={"deepagents-v2": TinkerFin()},
            settings=SimpleNamespace(tavily_api_key=None),
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
