"""Studio 后端部署入口测试。"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_DIR = APP_ROOT / "deploy"


def test_setup_generates_private_file_secrets_without_printing_values(
    tmp_path: Path,
) -> None:
    """初始化脚本必须生成私有文件且不把密钥写到输出。"""

    target = tmp_path / "deploy"
    target.mkdir()
    shutil.copy2(DEPLOY_DIR / "setup.sh", target / "setup.sh")
    shutil.copy2(DEPLOY_DIR / ".env.example", target / ".env.example")

    result = subprocess.run(
        ["sh", str(target / "setup.sh")],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    secret_files = {
        "mysql_root_password",
        "mysql_password",
        "redis_password",
        "opensandbox_api_key",
        "database_url",
    }
    assert {path.name for path in (target / "secrets").iterdir()} == secret_files
    for path in (target / "secrets").iterdir():
        assert path.read_text(encoding="utf-8").strip()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert path.read_text(encoding="utf-8").strip() not in result.stdout
    assert stat.S_IMODE((target / "secrets").stat().st_mode) == 0o700


def test_compose_supports_bundled_and_external_service_sets(tmp_path: Path) -> None:
    """同一 Compose 必须支持完整后端栈和仅 Studio 两种模式。"""

    secrets = tmp_path / "secrets"
    secrets.mkdir()
    for name in (
        "mysql_root_password",
        "mysql_password",
        "redis_password",
        "opensandbox_api_key",
        "database_url",
    ):
        (secrets / name).write_text("test-secret\n", encoding="utf-8")
    env = os.environ.copy()
    env["SECRETS_DIR"] = str(secrets)
    env["STUDIO_ENV_FILE"] = str(DEPLOY_DIR / ".env.example")
    base = [
        "docker",
        "compose",
        "--env-file",
        str(DEPLOY_DIR / ".env.example"),
        "-f",
        str(DEPLOY_DIR / "docker-compose.yaml"),
    ]

    bundled = subprocess.run(
        [*base, "--profile", "bundled", "config", "--services"],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    external = subprocess.run(
        [*base, "config", "--services"],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()

    assert set(bundled) == {
        "mysql",
        "redis",
        "opensandbox",
        "database-init",
        "studio",
    }
    assert external == ["studio"]
