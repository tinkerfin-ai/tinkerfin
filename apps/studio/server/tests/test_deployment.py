"""Studio 后端部署入口测试"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import stat
import subprocess
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_DIR = APP_ROOT / "deploy"


def _release_project_paths() -> tuple[str, ...]:
    script = (DEPLOY_DIR / "deploy.sh").read_text(encoding="utf-8")
    match = re.search(
        r"readonly -a PROJECT_PATHS=\(\n(?P<body>.*?)\n\)",
        script,
        flags=re.DOTALL,
    )
    assert match is not None
    return tuple(shlex.split(match.group("body")))


def test_deploy_builds_the_complete_workspace_release_set() -> None:
    """生产镜像必须包含 Studio 导入链需要的全部本地发行包"""

    assert _release_project_paths() == (
        "packages/tinkerfin-contracts",
        "packages/tinkerfin-native-stream",
        "packages/tinkerfin-agui-adapter",
        "packages/tinkerfin",
        "packages/tinkerfin-messaging",
        "packages/tinkerfin-tracing",
        "packages/tinkerfin-sandbox",
        "packages/tinkerfin-langgraph-mysql",
        "apps/studio/server",
    )


def test_setup_generates_private_file_secrets_without_printing_values(
    tmp_path: Path,
) -> None:
    """初始化脚本必须生成私有文件且不把密钥写到输出"""

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
        "redis_control_password",
        "redis_runtime_password",
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
    """同一 Compose 必须支持完整后端栈和仅 Studio 两种模式"""

    secrets = tmp_path / "secrets"
    secrets.mkdir()
    for name in (
        "mysql_root_password",
        "mysql_password",
        "redis_control_password",
        "redis_runtime_password",
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
        "redis-control",
        "redis-runtime",
        "opensandbox",
        "database-init",
        "studio",
    }
    assert external == ["studio"]


def test_bundled_opensandbox_persists_runtime_expiration_metadata() -> None:
    """Server 重建后必须保留 Docker runtime 已续期的过期时间"""

    compose = (DEPLOY_DIR / "docker-compose.yaml").read_text(encoding="utf-8")

    assert "opensandbox-metadata:/root/.opensandbox/metadata" in compose
    assert re.search(r"(?m)^  opensandbox-metadata:\s*$", compose)
