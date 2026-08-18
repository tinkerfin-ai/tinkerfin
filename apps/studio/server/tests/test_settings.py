from pathlib import Path

from tinkerfin_studio.config.settings import Settings, load_settings


def _clear_settings_environment(monkeypatch) -> None:
    for field_name in Settings.model_fields:
        monkeypatch.delenv(field_name.upper(), raising=False)


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
                "REDIS_HOST=redis.internal",
                "REDIS_PORT=6380",
                "REDIS_PASSWORD=redis-secret",
                "REDIS_DB=2",
                "REDIS_CHECKPOINT_DB=0",
                "OPEN_SANDBOX_DOMAIN=127.0.0.1:8091",
                "OPEN_SANDBOX_PROTOCOL=http",
                "OPEN_SANDBOX_WARM_POOL_SIZE=3",
                "TAVILY_API_KEY=tavily-secret",
            )
        ),
        encoding="utf-8",
    )

    settings = load_settings(env_file=env_file)

    assert settings.database.url == ("mysql+asyncmy://studio:secret@db:3306/studio")
    assert settings.redis.host == "redis.internal"
    assert settings.redis.port == 6380
    assert settings.redis.database == 2
    assert settings.redis.checkpoint_database == 0
    assert settings.redis.messaging_key_prefix == "tinkerfin:studio:messaging"
    assert settings.redis.run_key_prefix == "tinkerfin:studio:run"
    assert settings.sandbox.domain == "127.0.0.1:8091"
    assert settings.sandbox.warm_pool_size == 3
    assert settings.tavily_api_key is not None
    assert "redis-secret" not in repr(settings)
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
    """容器 Secrets 文件必须优先于普通环境变量且不进入配置 repr。"""

    _clear_settings_environment(monkeypatch)
    database_url_file = tmp_path / "database_url"
    redis_password_file = tmp_path / "redis_password"
    sandbox_key_file = tmp_path / "sandbox_key"
    tavily_key_file = tmp_path / "tavily_key"
    database_url_file.write_text(
        "mysql+asyncmy://studio:file-secret@mysql:3306/tinkerfin\n",
        encoding="utf-8",
    )
    redis_password_file.write_text("redis-file-secret\n", encoding="utf-8")
    sandbox_key_file.write_text("sandbox-file-secret\n", encoding="utf-8")
    tavily_key_file.write_text("tavily-file-secret\n", encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            (
                "DATABASE_URL=mysql+asyncmy://studio:plain@db:3306/tinkerfin",
                f"DATABASE_URL_FILE={database_url_file}",
                "REDIS_PASSWORD=plain-redis",
                f"REDIS_PASSWORD_FILE={redis_password_file}",
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
    assert settings.redis.password is not None
    assert settings.redis.password.get_secret_value() == "redis-file-secret"
    assert settings.sandbox.api_key is not None
    assert settings.sandbox.api_key.get_secret_value() == "sandbox-file-secret"
    assert settings.tavily_api_key is not None
    assert settings.tavily_api_key.get_secret_value() == "tavily-file-secret"
    assert "file-secret" not in repr(settings)


def test_secret_file_rejects_missing_or_blank_content(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """配置的 Secret 文件缺失或为空时必须拒绝启动。"""

    _clear_settings_environment(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            (
                "DATABASE_URL_FILE=" + str(tmp_path / "missing"),
                "REDIS_PASSWORD_FILE=" + str(tmp_path / "blank"),
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
