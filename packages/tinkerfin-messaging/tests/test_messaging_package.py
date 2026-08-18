"""Distribution and import-boundary contracts for the messaging package."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import FrozenInstanceError
from importlib.metadata import PackageNotFoundError, distribution
from importlib.resources import files
from pathlib import Path

import pytest

import tinkerfin_messaging


def test_public_namespace_exposes_the_protocol_neutral_facade() -> None:
    required = {
        "CancelCallback",
        "CancelContext",
        "MemoryBackend",
        "MessageChannel",
        "MessageCodec",
        "MessageEnvelope",
        "MessageSource",
        "MessageSubscription",
        "Messaging",
        "MessagingBackend",
        "MessagingSettlementTimeout",
        "ProfiledMessageSource",
        "SourceProfileMismatch",
        "SseRenderer",
    }

    assert required <= set(tinkerfin_messaging.__all__)
    assert all(hasattr(tinkerfin_messaging, name) for name in required)
    assert {
        "AgUiCodec",
        "NativeStreamPart",
        "NativeStreamPartCodec",
        "RedisBackend",
    }.isdisjoint(tinkerfin_messaging.__all__)


def test_cancel_context_is_an_immutable_public_value() -> None:
    context = tinkerfin_messaging.CancelContext(
        channel="events",
        stream="conversation-1",
        run="run-1",
    )

    assert (context.channel, context.stream, context.run) == (
        "events",
        "conversation-1",
        "run-1",
    )
    with pytest.raises(FrozenInstanceError):
        context.__setattr__("run", "replacement")


def test_base_import_does_not_load_optional_integrations() -> None:
    repository_root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json, sys, tinkerfin_messaging; "
                "print(json.dumps(sorted(name for name in sys.modules "
                "if name == 'tinkerfin' or name.startswith('tinkerfin.') "
                "or name == 'tinkerfin_agui_adapter' "
                "or name.startswith('tinkerfin_agui_adapter.') "
                "or name == 'ag_ui' or name.startswith('ag_ui.') "
                "or name == 'redis' or name.startswith('redis.'))))"
            ),
        ],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(result.stdout) == []


def test_distribution_declares_only_protocol_neutral_base_dependencies() -> None:
    try:
        metadata = distribution("tinkerfin-messaging")
    except PackageNotFoundError:
        pytest.fail("tinkerfin-messaging distribution is not installed")

    requirements = set(metadata.requires or ())
    assert "pydantic<3,>=2" in requirements
    assert not any(
        requirement.lower().startswith(
            ("tinkerfin ", "tinkerfin;", "tinkerfin-", "pydantic-core")
        )
        for requirement in requirements
    )
    assert files("tinkerfin_messaging").joinpath("py.typed").is_file()


def test_distribution_declares_independent_optional_integrations() -> None:
    requirements = set(distribution("tinkerfin-messaging").requires or ())

    assert 'ag-ui-protocol==0.1.19; extra == "agui"' in requirements
    assert 'tinkerfin<0.9.0,>=0.1.0; extra == "native"' in requirements
    assert 'redis<9,>=6; extra == "redis"' in requirements


@pytest.mark.parametrize(
    ("symbol", "blocked_packages", "extra"),
    (
        ("AgUiCodec", ("ag_ui",), "agui"),
        (
            "NativeStreamPartCodec",
            ("tinkerfin",),
            "native",
        ),
        ("RedisBackend", ("redis",), "redis"),
    ),
)
def test_missing_optional_dependency_reports_the_install_command(
    symbol: str,
    blocked_packages: tuple[str, ...],
    extra: str,
) -> None:
    repository_root = Path(__file__).resolve().parents[3]
    script = f"""
import importlib.abc
import sys

blocked = {blocked_packages!r}

class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + '.') for name in blocked):
            error = ModuleNotFoundError(f'blocked optional dependency: {{fullname}}')
            error.name = fullname
            raise error
        return None

sys.meta_path.insert(0, Blocker())
import tinkerfin_messaging

try:
    getattr(tinkerfin_messaging, {symbol!r})
except ImportError as error:
    print(str(error))
else:
    raise SystemExit('optional import unexpectedly succeeded')
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    )

    assert f'pip install "tinkerfin-messaging[{extra}]"' in result.stdout


def test_core_import_does_not_load_or_depend_on_messaging() -> None:
    repository_root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json, sys, tinkerfin; "
                "print(json.dumps('tinkerfin_messaging' in sys.modules))"
            ),
        ],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == "false"
    core_requirements = distribution("tinkerfin").requires or ()
    assert not any(
        requirement.startswith("tinkerfin-messaging")
        for requirement in core_requirements
    )
