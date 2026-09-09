"""用户模型配置的隔离、密钥保留和默认选择"""

from typing import Literal

import pytest
from pydantic import SecretStr

from tinkerfin_studio.api.errors import BusinessException, ModelErrorCode
from tinkerfin_studio.models.repository import AgentModelRepository
from tinkerfin_studio.models.schemas import AgentModelSave
from tinkerfin_studio.models.service import AgentModelService


def config(key: str, *, purpose: Literal["chat", "image"] = "chat", default=False):
    return AgentModelSave(
        model_id="same-id",
        display_name="My model",
        provider="openai",
        model_name="provider-model",
        base_url="https://api.openai.com/v1",
        api_key=SecretStr(key),
        purpose=purpose,
        is_default=default,
    )


async def test_model_settings_are_owned_by_user_and_do_not_expose_keys(session):
    first = AgentModelService(AgentModelRepository(session, user_id=1))
    second = AgentModelService(AgentModelRepository(session, user_id=2))
    await first.save_settings(config("owner-one"))
    await second.save_settings(config("owner-two"))
    assert (await first.resolve("same-id")).api_key.get_secret_value() == "owner-one"
    assert (await second.resolve("same-id")).api_key.get_secret_value() == "owner-two"
    assert "owner-one" not in (await first.settings())[0].model_dump_json()
    await second.delete_settings("same-id")
    assert len(await second.settings()) == 0
    assert len(await first.settings()) == 1


async def test_blank_key_is_retained_only_for_same_owner_and_endpoint(session):
    first = AgentModelService(AgentModelRepository(session, user_id=1))
    await first.save_settings(config("private"))
    await first.save_settings(config(""))
    assert (await first.resolve("same-id")).api_key.get_secret_value() == "private"
    second = AgentModelService(AgentModelRepository(session, user_id=2))
    with pytest.raises(BusinessException):
        await second.save_settings(config(""))
    with pytest.raises(BusinessException):
        await first.save_settings(
            config("").model_copy(update={"base_url": "https://other.example/v1"})
        )


async def test_image_service_does_not_appear_in_chat_catalog(session):
    service = AgentModelService(AgentModelRepository(session, user_id=1))
    await service.save_settings(config("image-secret", purpose="image", default=True))
    assert (await service.list_catalog()).items == []
    image = await service.resolve_image_model()
    assert image is not None and image.purpose == "image"


@pytest.mark.parametrize("status", ["preparing", "starting", "running", "waiting"])
async def test_unfinished_run_protects_only_its_owners_model(session, status):
    """运行和审批期间固定所用配置，其他用户的同名模型仍可编辑"""
    from datetime import UTC, datetime

    from tinkerfin_studio.conversation.models import (
        ConversationRunRegistration,
        ConversationThread,
    )

    first = AgentModelService(AgentModelRepository(session, user_id=1))
    second = AgentModelService(AgentModelRepository(session, user_id=2))
    await first.save_settings(config("one"))
    await second.save_settings(config("two"))
    now = datetime.now(UTC).replace(tzinfo=None)
    thread = ConversationThread(
        user_id=1, thread_id="active", title="进行中", created_at=now, updated_at=now
    )
    session.add(thread)
    await session.flush()
    session.add(
        ConversationRunRegistration(
            conversation_thread_id=thread.id,
            run_id="run",
            model_id="same-id",
            status=status,
            input_json={},
            started_at=now,
            created_at=now,
            updated_at=now,
        )
    )
    await session.commit()
    for operation in (
        first.save_settings(config("changed")),
        first.delete_settings("same-id"),
    ):
        with pytest.raises(BusinessException) as rejected:
            await operation
        assert rejected.value.error_code.http_status == 409
    await second.save_settings(config("updated"))
    assert (await second.resolve("same-id")).api_key.get_secret_value() == "updated"


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:11434/v1",
        "http://127.0.0.1:11434/v1",
        "http://ollama:11434/v1",
        "http://192.168.1.20:11434/v1",
        "https://models.internal/v1",
    ],
)
async def test_personal_model_can_save_and_resolve_local_service(session, base_url):
    service = AgentModelService(AgentModelRepository(session, user_id=1))
    value = config("ollama").model_copy(update={"base_url": base_url})
    await service.save_settings(value)
    saved = await service.resolve(value.model_id)
    assert saved.base_url == base_url
    assert saved.api_key.get_secret_value() == "ollama"


@pytest.mark.parametrize(
    "base_url",
    ["http://user:secret@localhost:11434/v1", "https://user:secret@models.internal/v1"],
)
async def test_model_endpoint_rejects_embedded_credentials(session, base_url):
    service = AgentModelService(AgentModelRepository(session, user_id=1))
    with pytest.raises(BusinessException) as rejected:
        await service.save_settings(
            config("ollama").model_copy(update={"base_url": base_url})
        )
    assert rejected.value.error_code.http_status == 422
    assert await service.settings() == []


async def test_no_default_image_model_does_not_select_another_service(session):
    service = AgentModelService(AgentModelRepository(session, user_id=1))
    await service.save_settings(config("image-secret", purpose="image"))
    assert await service.resolve_image_model() is None


async def test_rejected_save_finishes_its_transaction(session):
    service = AgentModelService(AgentModelRepository(session, user_id=1))
    with pytest.raises(BusinessException) as rejected:
        await service.save_settings(config(""))
    assert rejected.value.error_code == ModelErrorCode.KEY_REQUIRED
    assert not session.in_transaction()


async def test_model_purpose_mismatch_is_not_reported_as_disabled(session):
    service = AgentModelService(AgentModelRepository(session, user_id=1))
    await service.save_settings(config("image-secret", purpose="image"))
    with pytest.raises(BusinessException) as rejected:
        await service.resolve("same-id")
    assert rejected.value.error_code == ModelErrorCode.PURPOSE_MISMATCH


async def test_model_rejection_reaches_http_with_actionable_message(session):
    from starlette.requests import Request

    from tinkerfin_studio.api.errors import application_exception_handler

    service = AgentModelService(AgentModelRepository(session, user_id=1))
    with pytest.raises(BusinessException) as rejected:
        await service.save_settings(config(""))
    request = Request(
        {
            "type": "http",
            "method": "PUT",
            "path": "/models/configurations/same-id",
            "headers": [],
        }
    )
    response = await application_exception_handler(request, rejected.value)
    assert response.status_code == 422
    assert "新增模型需要填写 API 密钥" in bytes(response.body).decode()


async def test_failed_model_commit_rolls_back_default_change(session, monkeypatch):
    repository = AgentModelRepository(session, user_id=1)
    service = AgentModelService(repository)
    await service.save_settings(config("first", default=True))

    async def fail_commit():
        raise RuntimeError("commit unavailable")

    monkeypatch.setattr(repository, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="commit unavailable"):
        await service.save_settings(
            config("second", default=True).model_copy(update={"model_id": "second"})
        )
    assert not session.in_transaction()
    assert (await service.list_catalog()).default_model_id == "same-id"
    assert len(await service.settings()) == 1


async def test_cancelled_model_save_rolls_back_and_propagates(session, monkeypatch):
    import asyncio

    repository = AgentModelRepository(session, user_id=1)
    service = AgentModelService(repository)
    await service.save_settings(config("first", default=True))

    async def cancel_commit():
        raise asyncio.CancelledError()

    monkeypatch.setattr(repository, "commit", cancel_commit)
    with pytest.raises(asyncio.CancelledError):
        await service.save_settings(config("second"))
    assert not session.in_transaction()
    assert (await service.resolve("same-id")).api_key.get_secret_value() == "first"
