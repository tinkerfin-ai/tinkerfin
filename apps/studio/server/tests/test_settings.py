from pathlib import Path

import pytest

from tinkerfin_studio.config.settings import Settings, load_settings


def _clear_settings_environment(monkeypatch) -> None:
    for field_name in Settings.model_fields:
        monkeypatch.delenv(field_name.upper(), raising=False)


@pytest.mark.parametrize("configured", [".data/attachments", "../files", None])
def test_attachment_directory_is_stable_across_working_directories(
    tmp_path: Path, monkeypatch, configured: str | None
) -> None:
    """同一配置在不同工作目录启动时必须读取同一份附件"""
    _clear_settings_environment(monkeypatch)
    config_directory = tmp_path / "config"
    config_directory.mkdir()
    env_file = config_directory / ".env"
    content = "DATABASE_URL=mysql+asyncmy://studio:secret@db:3306/studio\n"
    if configured is not None:
        content += f"ATTACHMENT_DIRECTORY={configured}\n"
    env_file.write_text(content, encoding="utf-8")
    expected = (config_directory / (configured or ".data/attachments")).resolve()
    for cwd in [tmp_path, config_directory]:
        monkeypatch.chdir(cwd)
        assert load_settings(env_file=env_file).attachment_directory == expected


def test_absolute_attachment_directory_overrides_file_without_changing_location(
    tmp_path: Path, monkeypatch
) -> None:
    """环境变量中的绝对数据卷路径不受配置位置和工作目录影响"""
    _clear_settings_environment(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DATABASE_URL=mysql+asyncmy://studio:secret@db:3306/studio\n"
        "ATTACHMENT_DIRECTORY=local-files\n",
        encoding="utf-8",
    )
    expected = tmp_path / "volume"
    monkeypatch.setenv("ATTACHMENT_DIRECTORY", str(expected))
    assert load_settings(env_file=env_file).attachment_directory == expected
    assert load_settings(env_file=None).attachment_directory == expected


def test_default_attachment_directory_is_independent_of_working_directory(
    tmp_path: Path, monkeypatch
) -> None:
    """直接构建和默认配置读取都采用固定的应用配置目录"""
    _clear_settings_environment(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "mysql+asyncmy://studio:secret@db:3306/studio")
    before = load_settings(env_file=None).attachment_directory
    monkeypatch.chdir(tmp_path)
    assert load_settings(env_file=None).attachment_directory == before
    assert (
        Settings(
            database_url="mysql+asyncmy://studio:secret@db:3306/studio",
            attachment_directory=Path(".data/attachments"),
        ).attachment_directory
        == before
    )
    assert before.is_absolute()


def test_load_settings_groups_external_resource_configuration(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """配置文件应生成可直接交给资源层的精确设置"""

    _clear_settings_environment(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            (
                "DATABASE_URL=mysql+asyncmy://studio:secret@db:3306/studio",
                "REDIS_CONTROL_HOST=redis-control.internal",
                "REDIS_CONTROL_PORT=6380",
                "REDIS_CONTROL_PASSWORD=control-secret",
                "REDIS_CONTROL_DB=2",
                "REDIS_RUNTIME_HOST=redis-runtime.internal",
                "REDIS_RUNTIME_PORT=6381",
                "REDIS_RUNTIME_PASSWORD=runtime-secret",
                "REDIS_RUNTIME_DB=3",
                "REDIS_RUNTIME_CHECKPOINT_DB=0",
                "OPEN_SANDBOX_DOMAIN=127.0.0.1:8091",
                "OPEN_SANDBOX_PROTOCOL=http",
                "OPEN_SANDBOX_WARM_POOL_SIZE=3",
                "AUTH_TOKEN_EXPIRE_SECONDS=86400",
                "TAVILY_API_KEY=tavily-secret",
            )
        ),
        encoding="utf-8",
    )

    settings = load_settings(env_file=env_file)

    assert settings.database.url == ("mysql+asyncmy://studio:secret@db:3306/studio")
    assert settings.redis_control.host == "redis-control.internal"
    assert settings.redis_control.port == 6380
    assert settings.redis_control.database == 2
    assert settings.redis_control.run_key_prefix == "tinkerfin:studio:run"
    assert settings.redis_runtime.host == "redis-runtime.internal"
    assert settings.redis_runtime.port == 6381
    assert settings.redis_runtime.database == 3
    assert settings.redis_runtime.checkpoint_database == 0
    assert settings.redis_runtime.messaging_key_prefix == "tinkerfin:studio:messaging"
    assert settings.sandbox.domain == "127.0.0.1:8091"
    assert settings.sandbox.warm_pool_size == 3
    assert settings.auth_token_expire_seconds == 86400
    assert settings.messaging_retention_seconds == 86400
    assert settings.database.connection_budget == 21
    assert settings.database.management_connection_reserve == 10
    assert settings.tavily_api_key is not None
    assert "control-secret" not in repr(settings)
    assert "runtime-secret" not in repr(settings)
    assert "tavily-secret" not in repr(settings)


def test_load_settings_rejects_non_async_mysql_url(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """请求链数据库必须使用 asyncmy，避免异步接口落入阻塞驱动"""

    _clear_settings_environment(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DATABASE_URL=mysql+pymysql://studio:secret@db:3306/studio\n",
        encoding="utf-8",
    )

    try:
        load_settings(env_file=env_file)
    except ValueError as error:
        assert "mysql+asyncmy" in str(error)
    else:  # pragma: no cover - 失败分支用于给断言提供清晰原因
        raise AssertionError("同步 MySQL URL 不应通过配置校验")


def test_secret_files_override_plain_environment_values(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """容器 Secrets 文件必须优先于普通环境变量且不进入配置 repr"""

    _clear_settings_environment(monkeypatch)
    database_url_file = tmp_path / "database_url"
    redis_control_password_file = tmp_path / "redis_control_password"
    redis_runtime_password_file = tmp_path / "redis_runtime_password"
    sandbox_key_file = tmp_path / "sandbox_key"
    tavily_key_file = tmp_path / "tavily_key"
    database_url_file.write_text(
        "mysql+asyncmy://studio:file-secret@mysql:3306/tinkerfin\n",
        encoding="utf-8",
    )
    redis_control_password_file.write_text("control-file-secret\n", encoding="utf-8")
    redis_runtime_password_file.write_text("runtime-file-secret\n", encoding="utf-8")
    sandbox_key_file.write_text("sandbox-file-secret\n", encoding="utf-8")
    tavily_key_file.write_text("tavily-file-secret\n", encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            (
                "DATABASE_URL=mysql+asyncmy://studio:plain@db:3306/tinkerfin",
                f"DATABASE_URL_FILE={database_url_file}",
                "REDIS_CONTROL_PASSWORD=plain-control",
                f"REDIS_CONTROL_PASSWORD_FILE={redis_control_password_file}",
                "REDIS_RUNTIME_PASSWORD=plain-runtime",
                f"REDIS_RUNTIME_PASSWORD_FILE={redis_runtime_password_file}",
                "OPEN_SANDBOX_API_KEY=plain-sandbox",
                f"OPEN_SANDBOX_API_KEY_FILE={sandbox_key_file}",
                "TAVILY_API_KEY=plain-tavily",
                f"TAVILY_API_KEY_FILE={tavily_key_file}",
            )
        ),
        encoding="utf-8",
    )

    settings = load_settings(env_file=env_file)

    assert settings.database.url == (
        "mysql+asyncmy://studio:file-secret@mysql:3306/tinkerfin"
    )
    assert settings.redis_control.password is not None
    assert settings.redis_control.password.get_secret_value() == "control-file-secret"
    assert settings.redis_runtime.password is not None
    assert settings.redis_runtime.password.get_secret_value() == "runtime-file-secret"
    assert settings.sandbox.api_key is not None
    assert settings.sandbox.api_key.get_secret_value() == "sandbox-file-secret"
    assert settings.tavily_api_key is not None
    assert settings.tavily_api_key.get_secret_value() == "tavily-file-secret"
    assert "file-secret" not in repr(settings)


def test_secret_file_rejects_missing_or_blank_content(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """配置的 Secret 文件缺失或为空时必须拒绝启动"""

    _clear_settings_environment(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            (
                "DATABASE_URL_FILE=" + str(tmp_path / "missing"),
                "REDIS_CONTROL_PASSWORD_FILE=" + str(tmp_path / "blank"),
            )
        ),
        encoding="utf-8",
    )
    (tmp_path / "blank").write_text("\n", encoding="utf-8")

    try:
        load_settings(env_file=env_file)
    except ValueError as error:
        assert "DATABASE_URL_FILE" in str(error)
    else:  # pragma: no cover - 失败分支用于提供清晰原因
        raise AssertionError("缺失的 Secret 文件不应通过配置校验")


def test_database_budget_must_cover_shared_pool_and_agent_store(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """总连接预算必须覆盖共享 SQLAlchemy 池和一条 Agent Store 连接"""

    _clear_settings_environment(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            (
                "DATABASE_URL=mysql+asyncmy://studio:secret@db:3306/studio",
                "DATABASE_POOL_SIZE=10",
                "DATABASE_MAX_OVERFLOW=10",
                "DATABASE_CONNECTION_BUDGET=20",
            )
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"pool_size \+ max_overflow"):
        load_settings(env_file=env_file)


def test_redis_control_and_runtime_must_use_distinct_services(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """两个逻辑域不得通过不同前缀伪装成物理故障域拆分"""

    _clear_settings_environment(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            (
                "DATABASE_URL=mysql+asyncmy://studio:secret@db:3306/studio",
                "REDIS_CONTROL_HOST=redis.internal",
                "REDIS_CONTROL_PORT=6379",
                "REDIS_RUNTIME_HOST=redis.internal",
                "REDIS_RUNTIME_PORT=6379",
            )
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="不同物理服务地址"):
        load_settings(env_file=env_file)
