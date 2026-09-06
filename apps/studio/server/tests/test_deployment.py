"""Studio 后端部署入口测试"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from scripts.build_wheels import PROJECT_PATHS

APP_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_DIR = APP_ROOT / "deploy"


def test_deploy_builds_the_complete_workspace_release_set() -> None:
    """生产镜像必须包含 Studio 导入链需要的全部本地发行包"""

    assert PROJECT_PATHS == (
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


def test_deploy_calls_shared_build_without_removing_working_tree_artifacts(
    tmp_path: Path,
) -> None:
    """部署通过统一构建入口生成 wheel，并保留工作树中的构建文件"""

    root = tmp_path / "workspace"
    deploy = root / "apps/studio/server/deploy"
    deploy.mkdir(parents=True)
    shutil.copy2(DEPLOY_DIR / "deploy.sh", deploy / "deploy.sh")
    for filename in (
        "pyproject.toml",
        "uv.lock",
        "apps/studio/server/Dockerfile",
        "apps/studio/server/database/mysql/schema.sql",
        "apps/studio/server/deploy/docker-compose.yaml",
        "apps/studio/server/deploy/.env",
        "apps/studio/server/deploy/secrets/database_url",
        "apps/studio/server/deploy/secrets/mysql_password",
        "apps/studio/server/deploy/secrets/mysql_root_password",
        "apps/studio/server/deploy/secrets/redis_control_password",
        "apps/studio/server/deploy/secrets/redis_runtime_password",
        "apps/studio/server/deploy/secrets/opensandbox_api_key",
    ):
        target = root / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch()
    artifacts = (
        root / "packages/tinkerfin/build/lib/retained.py",
        root / "packages/tinkerfin/src/tinkerfin.egg-info/retained.txt",
    )
    for artifact in artifacts:
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("caller-owned\n", encoding="utf-8")
    builder = root / "scripts/build_wheels.py"
    builder.parent.mkdir()
    builder.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "output = Path(sys.argv[sys.argv.index('--out-dir') + 1])\n"
        "(output / 'tinkerfin_studio-0.1.0-py3-none-any.whl').touch()\n"
        "Path('build-invoked.txt').write_text(' '.join(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    commands = root / "bin"
    commands.mkdir()
    uv = commands / "uv"
    uv.write_text(
        '#!/usr/bin/env bash\nset -euo pipefail\ncase "$1" in\n'
        "  lock|export) exit 0 ;;\n"
        "  run)\n"
        "    shift\n"
        '    while [[ "$1" != python ]]; do shift; done\n'
        "    shift\n"
        '    exec "$BUILD_TEST_PYTHON" "$@" ;;\n'
        "  *) exit 71 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    docker = commands / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$*" == "compose version --short" ]]; then echo 2.24.0; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    for command in (uv, docker):
        command.chmod(0o700)
    environment = dict(os.environ)
    environment["PATH"] = str(commands) + os.pathsep + environment["PATH"]
    environment["BUILD_TEST_PYTHON"] = sys.executable

    result = subprocess.run(
        ["bash", str(deploy / "deploy.sh")],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (root / "build-invoked.txt").read_text() == f"--out-dir {root / 'dist'}"
    assert all(artifact.read_text() == "caller-owned\n" for artifact in artifacts)


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
        "studio",
    }
    assert external == ["studio"]


def test_bundled_mysql_uses_its_official_one_time_schema_bootstrap() -> None:
    """内置 MySQL 仅在新数据卷首次启动时导入 Studio 业务 SQL"""

    compose = (DEPLOY_DIR / "docker-compose.yaml").read_text(encoding="utf-8")
    deploy = (DEPLOY_DIR / "deploy.sh").read_text(encoding="utf-8")

    assert (
        "../database/mysql/schema.sql:"
        "/docker-entrypoint-initdb.d/10-studio-business.sql:ro"
    ) in compose
    assert "database-init:" not in compose
    assert "init-database.sh" not in compose
    assert "prepare_external_database" not in deploy


def test_bundled_opensandbox_persists_runtime_expiration_metadata() -> None:
    """Server 重建后必须保留 Docker runtime 已续期的过期时间"""

    compose = (DEPLOY_DIR / "docker-compose.yaml").read_text(encoding="utf-8")

    assert "opensandbox-metadata:/root/.opensandbox/metadata" in compose
    assert re.search(r"(?m)^  opensandbox-metadata:\s*$", compose)
