"""Distribution and import-boundary contracts for the messaging package."""

from __future__ import annotations

import inspect
import json
import subprocess
import sys
from dataclasses import FrozenInstanceError
from importlib.metadata import PackageNotFoundError, distribution
from importlib.resources import files
from pathlib import Path

import pytest

import tinkerfin_messaging
from tinkerfin_contracts import RunIdentity


@pytest.mark.parametrize(
    ("method_name", "documents_return"),
    [
        ("prepare", True),
        ("append", True),
        ("begin_settlement", True),
        ("finish", False),
        ("latest_seq", True),
        ("get_run_status", True),
        ("read", True),
        ("bind_follow", True),
        ("follow", True),
        ("request_cancel", True),
        ("wait_for_cancel", True),
        ("wait_finished", True),
        ("failure", True),
        ("renew", True),
        ("delete_stream", False),
    ],
)
def test_backend_protocol_documents_public_lifecycle_contracts(
    method_name: str,
    documents_return: bool,
) -> None:
    """Keep ownership, result, and failure guidance next to each Backend method."""

    method = getattr(tinkerfin_messaging.MessagingBackend, method_name)
    documentation = inspect.getdoc(method)

    assert documentation is not None
    assert "Args:" in documentation
    assert "Raises:" in documentation
    assert ("Returns:" in documentation) is documents_return


def test_public_namespace_exposes_the_default_tinkerfin_facade() -> None:
    required = {
        "CancelCallback",
        "AgUiCodec",
        "CancelContext",
        "CancellableMessageSource",
        "DeferredMessageSource",
        "MemoryBackend",
        "MessageChannel",
        "MessageCodec",
        "MessageCodecInputSource",
        "MessageEnvelope",
        "MessageSource",
        "MessageSourceBinding",
        "MessageSubscription",
        "Messaging",
        "MessagingBackend",
        "MessagingSettlementTimeout",
        "NativeStreamPart",
        "NativeStreamPartCodec",
        "ProfiledMessageSource",
        "ProfiledDeferredMessageSource",
        "RedisBackend",
        "SourceProfileMismatch",
        "SseRenderer",
    }

    assert required <= set(tinkerfin_messaging.__all__)
    assert all(hasattr(tinkerfin_messaging, name) for name in required)


def test_cancel_context_is_an_immutable_public_value() -> None:
    context = tinkerfin_messaging.CancelContext(
        channel="events",
        identity=RunIdentity(threadId="conversation-1", runId="run-1"),
    )

    assert (context.channel, context.identity) == (
        "events",
        RunIdentity(threadId="conversation-1", runId="run-1"),
    )
    with pytest.raises(FrozenInstanceError):
        context.__setattr__(
            "identity",
            RunIdentity(threadId="conversation-1", runId="replacement"),
        )


def test_base_import_does_not_load_optional_integrations() -> None:
    repository_root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json, sys, tinkerfin_messaging; "
                "print(json.dumps(sorted(name for name in sys.modules "
                "if any(name == prefix or name.startswith(prefix + '.') "
                "for prefix in ('ag_ui', 'deepagents', 'langchain', 'langgraph', "
                "'redis', 'tinkerfin', 'tinkerfin_native_stream')))))"
            ),
        ],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(result.stdout) == []


def test_distribution_declares_only_protocol_neutral_core_dependencies() -> None:
    try:
        metadata = distribution("tinkerfin-messaging")
    except PackageNotFoundError:
        pytest.fail("tinkerfin-messaging distribution is not installed")

    requirements = set(metadata.requires or ())
    assert "pydantic<3,>=2" in requirements
    assert "tinkerfin-contracts<0.9.0,>=0.1.0" in requirements
    assert not any(
        "extra ==" not in value and "ag-ui-protocol" in value for value in requirements
    )
    assert not any(
        "extra ==" not in value and value.startswith("tinkerfin<")
        for value in requirements
    )
    assert files("tinkerfin_messaging").joinpath("py.typed").is_file()


def test_distribution_declares_redis_agui_and_native_extras() -> None:
    requirements = set(distribution("tinkerfin-messaging").requires or ())

    assert 'redis<9,>=6; extra == "redis"' in requirements
    assert 'ag-ui-protocol==0.1.19; extra == "agui"' in requirements
    assert 'tinkerfin-native-stream<0.9.0,>=0.1.0; extra == "native"' in requirements
    assert not any(
        'extra == "native"' in value and value.startswith("tinkerfin<")
        for value in requirements
    )


@pytest.mark.parametrize(
    ("symbol", "blocked_packages", "extra"),
    (
        ("AgUiCodec", ("ag_ui",), "agui"),
        ("NativeStreamPartCodec", ("tinkerfin_native_stream",), "native"),
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
