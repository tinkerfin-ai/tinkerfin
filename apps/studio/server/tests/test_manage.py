"""Studio 管理 CLI 参数契约测试。"""

from tinkerfin_studio.manage import parse_args


def test_management_parser_keeps_secrets_out_of_required_arguments() -> None:
    """用户密码和模型密钥缺省必须由隐藏输入读取。"""

    user = parse_args(
        [
            "user",
            "create",
            "--username",
            "alice",
            "--display-name",
            "Alice",
        ]
    )
    model = parse_args(
        [
            "model",
            "upsert",
            "--model-id",
            "main",
            "--display-name",
            "Main",
            "--provider",
            "openai",
            "--model-name",
            "provider-main",
            "--base-url",
            "https://models.example.test/v1",
            "--default",
        ]
    )

    assert user.password is None
    assert model.api_key is None
    assert model.is_default is True


def test_forwarded_command_migration_is_dry_run_by_default() -> None:
    """数据迁移必须显式传入 apply 才允许写入"""

    dry_run = parse_args(["data", "migrate-forwarded-commands"])
    apply = parse_args(["data", "migrate-forwarded-commands", "--apply"])

    assert dry_run.apply is False
    assert apply.apply is True
