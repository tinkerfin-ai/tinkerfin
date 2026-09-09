"""The native package remains independent from optional framework integrations."""

from __future__ import annotations

import subprocess
import sys


def test_minimal_import_does_not_load_optional_packages() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import tinkerfin_native_stream; "
                "blocked=('ag_ui', 'deepagents', 'tinkerfin_agui_adapter', "
                "'tinkerfin_messaging', 'tinkerfin_tracing', 'sqlalchemy', 'redis'); "
                "assert not any(name == prefix or name.startswith(prefix + '.') "
                "for name in sys.modules for prefix in blocked)"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
