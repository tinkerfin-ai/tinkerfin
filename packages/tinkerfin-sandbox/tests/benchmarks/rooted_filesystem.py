"""Reproducible local observations for Rooted filesystem operations."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile
import time
import tracemalloc
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, TypeVar

from deepagents.backends import LocalShellBackend

from tinkerfin_sandbox.backends._rooted_protocol import (
    _build_rooted_command,
    _build_rooted_transfer_command,
    _JsonValue,
    _parse_rooted_response,
    _parse_rooted_transfer_handshake,
    _RootedOperation,
    _RootedResponse,
    _RootedTransferHandshake,
)

_ResultT = TypeVar("_ResultT")


@dataclass(frozen=True, slots=True)
class Observation:
    """One measured operation with explicit work and transfer counts."""

    name: str
    commands: int
    wall_seconds: float
    peak_python_bytes: int
    transferred_bytes: int


def _measure(
    name: str,
    *,
    commands: int,
    transferred_bytes: int,
    operation: Callable[[], _ResultT],
) -> tuple[Observation, _ResultT]:
    tracemalloc.start()
    started = time.perf_counter()
    try:
        result = operation()
        wall_seconds = time.perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return (
        Observation(
            name=name,
            commands=commands,
            wall_seconds=wall_seconds,
            peak_python_bytes=peak,
            transferred_bytes=transferred_bytes,
        ),
        result,
    )


def _execute(
    backend: LocalShellBackend,
    *,
    root: Path,
    operation: _RootedOperation,
    arguments: Mapping[str, _JsonValue],
) -> _RootedResponse:
    request = _build_rooted_command(
        root=str(root),
        operation=operation,
        arguments=arguments,
    )
    return _parse_rooted_response(
        backend.execute(request.command),
        request=request,
    )


def _descriptor_transfer(
    *,
    root: Path,
    path: str,
    mode: Literal["upload", "download"],
    content: bytes | None = None,
) -> bytes:
    request = _build_rooted_transfer_command(
        root=str(root),
        path=path,
        mode=mode,
        token=f"benchmark-{mode}",
        hold_seconds=30,
    )
    process = subprocess.Popen(
        shlex.split(request.command),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        record = _parse_rooted_transfer_handshake(
            process.stdout.readline(),
            request=request,
        )
        if not isinstance(record, _RootedTransferHandshake):
            raise TypeError(f"descriptor helper failed: {record}")
        descriptor_path = Path(f"/proc/{record.pid}/fd/{record.fd}")
        if mode == "upload":
            if content is None:
                raise ValueError("upload content is required")
            descriptor_path.write_bytes(content)
            return b""
        return descriptor_path.read_bytes()
    finally:
        process.terminate()
        process.wait(timeout=5)


def main() -> None:
    """Run the fixed benchmark matrix and emit machine-readable observations."""
    iterations = 1_000
    entry_count = 10_000
    observations: list[Observation] = []
    with tempfile.TemporaryDirectory(prefix="tinkerfin-rooted-benchmark-") as raw:
        base = Path(raw).resolve()
        root = base / "workspace"
        root.mkdir()
        safe_file = root / "safe.txt"
        safe_file.write_text("safe\n", encoding="utf-8")
        outside = base / "outside.txt"
        outside.write_text("outside sentinel\n", encoding="utf-8")
        (root / "outside-link").symlink_to(outside)
        tree = root / "tree"
        tree.mkdir()
        for index in range(entry_count):
            (tree / f"entry-{index:05d}.txt").write_text(
                f"needle {index}\n",
                encoding="utf-8",
            )
        backend = LocalShellBackend(
            root_dir="/",
            virtual_mode=False,
            max_output_bytes=8 * 1024 * 1024,
            env={
                "PYTHONNOUSERSITE": "1",
                "PYTHONPATH": "",
                "PYTHONSAFEPATH": "1",
            },
            inherit_env=True,
        )

        observation, _ = _measure(
            "safe_reads",
            commands=iterations,
            transferred_bytes=iterations * len("safe"),
            operation=lambda: [
                _execute(
                    backend,
                    root=root,
                    operation="read",
                    arguments={
                        "path": "/safe.txt",
                        "offset": 0,
                        "limit": 1,
                        "binary": False,
                    },
                )
                for _ in range(iterations)
            ],
        )
        observations.append(observation)

        observation, _ = _measure(
            "rejected_external_links",
            commands=iterations,
            transferred_bytes=0,
            operation=lambda: [
                _execute(
                    backend,
                    root=root,
                    operation="read",
                    arguments={
                        "path": "/outside-link",
                        "offset": 0,
                        "limit": 1,
                        "binary": False,
                    },
                )
                for _ in range(iterations)
            ],
        )
        observations.append(observation)

        observation, _ = _measure(
            "glob_10000_entries",
            commands=1,
            transferred_bytes=0,
            operation=lambda: _execute(
                backend,
                root=root,
                operation="glob",
                arguments={"path": "/tree", "pattern": "*.txt"},
            ),
        )
        observations.append(observation)

        observation, _ = _measure(
            "grep_10000_files",
            commands=1,
            transferred_bytes=0,
            operation=lambda: _execute(
                backend,
                root=root,
                operation="grep",
                arguments={
                    "path": "/tree",
                    "pattern": "needle",
                    "glob": "*.txt",
                    "max_count": 100,
                },
            ),
        )
        observations.append(observation)

        if Path("/proc/self/fd").is_dir():
            content = os.urandom(16 * 1024 * 1024)
            observation, _ = _measure(
                "descriptor_upload_16mib",
                commands=1,
                transferred_bytes=len(content),
                operation=lambda: _descriptor_transfer(
                    root=root,
                    path="/large.bin",
                    mode="upload",
                    content=content,
                ),
            )
            observations.append(observation)
            observation, downloaded = _measure(
                "descriptor_download_16mib",
                commands=1,
                transferred_bytes=len(content),
                operation=lambda: _descriptor_transfer(
                    root=root,
                    path="/large.bin",
                    mode="download",
                ),
            )
            if downloaded != content:
                raise AssertionError("descriptor download content mismatch")
            observations.append(observation)

    print(json.dumps([asdict(item) for item in observations], indent=2))


if __name__ == "__main__":
    main()
