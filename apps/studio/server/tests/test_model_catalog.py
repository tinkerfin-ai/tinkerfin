import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from tinkerfin_studio.api.errors import ModelErrorCode, SystemException
from tinkerfin_studio.models.repository import AgentModelRepository
from tinkerfin_studio.models.schemas import AgentModelWrite
from tinkerfin_studio.models.service import AgentModelService


def test_model_write_requires_explicit_runtime_profile() -> None:
    """模型写入边界不得静默选择当前唯一 Profile"""

    with pytest.raises(ValidationError, match="runtime_profile"):
        AgentModelWrite.model_validate(
            {
                "model_id": "main",
                "display_name": "Main",
                "provider": "openai",
                "model_name": "provider-main",
                "base_url": "https://models.example.test/v1",
                "api_key": "secret",
            }
        )


async def test_model_catalog_returns_only_enabled_safe_fields(
    session: AsyncSession,
) -> None:
    """模型目录不得暴露 provider 连接和明文密钥"""

    service = AgentModelService(AgentModelRepository(session))
    await service.upsert(
        AgentModelWrite(
            model_id="deepseek-v4-pro",
            display_name="DeepSeek V4 Pro",
            provider="deepseek",
            model_name="deepseek-v4-pro",
            base_url="https://models.example.test/v1",
            api_key=SecretStr("database-plain-secret"),
            reasoning_enabled=True,
            runtime_profile="deepagents-v2",
            enabled=True,
            is_default=True,
            sort_order=20,
        )
    )
    await service.upsert(
        AgentModelWrite(
            model_id="disabled",
            display_name="Disabled",
            provider="openai",
            model_name="disabled-model",
            base_url="https://models.example.test/v1",
            api_key=SecretStr("disabled-secret"),
            runtime_profile="deepagents-v2",
            enabled=False,
            is_default=False,
            sort_order=10,
        )
    )

    catalog = await service.list_catalog()
    serialized = catalog.model_dump_json(by_alias=True)

    assert catalog.default_model_id == "deepseek-v4-pro"
    assert [item.model_id for item in catalog.items] == ["deepseek-v4-pro"]
    assert catalog.items[0].runtime_profile == "deepagents-v2"
    assert "database-plain-secret" not in serialized
    assert "models.example.test" not in serialized
    assert "provider" not in serialized


async def test_setting_a_new_default_clears_the_previous_default(
    session: AsyncSession,
) -> None:
    """同一事务中只能留下一个启用的默认模型"""

    service = AgentModelService(AgentModelRepository(session))
    for model_id, is_default in (("first", True), ("second", True)):
        await service.upsert(
            AgentModelWrite(
                model_id=model_id,
                display_name=model_id.title(),
                provider="openai",
                model_name=f"provider-{model_id}",
                base_url="https://models.example.test/v1",
                api_key=SecretStr(f"secret-{model_id}"),
                runtime_profile="deepagents-v2",
                enabled=True,
                is_default=is_default,
            )
        )

    catalog = await service.list_catalog()
    resolved = await service.resolve("second")

    assert catalog.default_model_id == "second"
    assert sum(item.is_default for item in catalog.items) == 1
    assert resolved.api_key.get_secret_value() == "secret-second"
    assert resolved.provider == "openai"


async def test_unknown_database_runtime_profile_fails_closed(
    session: AsyncSession,
) -> None:
    """未知 Profile 不得进入模型目录或 Agent 建图"""

    repository = AgentModelRepository(session)
    service = AgentModelService(repository)
    await service.upsert(
        AgentModelWrite(
            model_id="profile-invalid",
            display_name="Invalid Profile",
            provider="openai",
            model_name="provider-invalid",
            base_url="https://models.example.test/v1",
            api_key=SecretStr("secret"),
            runtime_profile="deepagents-v2",
            enabled=True,
            is_default=False,
        )
    )
    entity = await repository.get("profile-invalid")
    assert entity is not None
    entity.runtime_profile = "unavailable-profile"
    await repository.commit()

    with pytest.raises(SystemException) as catalog_error:
        await service.list_catalog()
    with pytest.raises(SystemException) as resolve_error:
        await service.resolve("profile-invalid")

    assert catalog_error.value.error_code is ModelErrorCode.CATALOG_UNAVAILABLE
    assert resolve_error.value.error_code is ModelErrorCode.CATALOG_UNAVAILABLE
