"""Studio 版本来源测试"""

from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path

import tinkerfin_studio
from tinkerfin_studio.application import create_application

APP_ROOT = Path(__file__).resolve().parents[1]


def test_built_wheel_reads_version_module(tmp_path: Path) -> None:
    """wheel 元数据必须读取版本模块，避免 pyproject 重复维护版本号"""

    project_dir = tmp_path / "project"
    output_dir = tmp_path / "dist"
    shutil.copytree(APP_ROOT / "src", project_dir / "src")
    shutil.copy2(APP_ROOT / "pyproject.toml", project_dir / "pyproject.toml")
    shutil.copy2(APP_ROOT / "LICENSE", project_dir / "LICENSE")
    (project_dir / "src/tinkerfin_studio/version.py").write_text(
        '__version__ = "9.8.7"\n',
        encoding="utf-8",
    )

    subprocess.run(
        [
            "uv",
            "build",
            "--quiet",
            "--wheel",
            "--out-dir",
            str(output_dir),
            "--no-create-gitignore",
            str(project_dir),
        ],
        check=True,
    )

    wheel = next(output_dir.glob("tinkerfin_studio-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        metadata_path = next(
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        )
        metadata = archive.read(metadata_path).decode("utf-8")

    assert "\nVersion: 9.8.7\n" in f"\n{metadata}"


def test_application_uses_the_package_version() -> None:
    """OpenAPI 版本必须与 Studio 包版本保持一致"""

    application = create_application(lifespan=None)

    assert application.version == tinkerfin_studio.__version__
