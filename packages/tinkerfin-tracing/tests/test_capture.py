"""Public Trace retention-policy and captured-value contracts."""

from __future__ import annotations

import pytest

from tinkerfin_tracing import (
    CapturePolicy,
    MiddlewareTraceCapture,
    ReasoningCapturePolicy,
    ToolCaptureRule,
    ToolTraceCapture,
)
from tinkerfin_tracing.capture import CapturedValue


class _MetricsMiddleware:
    pass


def test_public_history_covers_new_tools_and_exact_overrides() -> None:
    policy = CapturePolicy.public_history(
        tool_overrides={
            "metadata": ToolTraceCapture.metadata_only(),
            "selected": ToolTraceCapture.selected_content(
                argument_paths=("/query",),
                result_paths=("/answer",),
            ),
            "disabled": ToolTraceCapture.disabled(),
        }
    )

    assert policy.tool_capture("unconfigured").mode == "full_content"
    assert policy.tool_capture("metadata").mode == "metadata_only"
    assert policy.tool_capture("selected").argument_paths == ("/query",)
    assert policy.traces_tool("disabled") is False
    assert policy.captures_review_description("unconfigured") is True


def test_public_safe_resolves_selected_paths_without_a_second_registry() -> None:
    policy = CapturePolicy.public_safe(
        tool_rules=(
            ToolCaptureRule(
                tool_name="search",
                argument_paths=("/query", "/filters/0/name"),
                result_paths=("",),
            ),
        )
    )

    selected = policy.tool_capture("search")
    assert selected.mode == "selected_content"
    assert selected.argument_paths == ("/query", "/filters/0/name")
    assert selected.result_paths == ("",)
    assert policy.tool_capture("unknown").mode == "metadata_only"


def test_public_history_requires_typed_override_names_and_values() -> None:
    with pytest.raises(TypeError, match="mapping"):
        CapturePolicy.public_history(
            tool_overrides=()  # pyright: ignore[reportArgumentType]
        )
    with pytest.raises(TypeError, match="name strings"):
        CapturePolicy.public_history(
            tool_overrides={  # pyright: ignore[reportArgumentType]
                1: ToolTraceCapture.metadata_only()
            }
        )
    with pytest.raises(TypeError, match="ToolTraceCapture"):
        CapturePolicy.public_history(
            tool_overrides={  # pyright: ignore[reportArgumentType]
                "search": object()
            }
        )


def test_middleware_capture_uses_exact_name_before_implementation_type() -> None:
    policy = CapturePolicy.public_history(
        include_error_messages=True,
        middleware_overrides={
            _MetricsMiddleware: MiddlewareTraceCapture.disabled(),
            "customer-metrics": MiddlewareTraceCapture.visible(),
        },
    )
    class_name = f"{_MetricsMiddleware.__module__}.{_MetricsMiddleware.__qualname__}"

    assert policy.include_error_messages is True
    assert (
        policy.middleware_capture(name="internal-metrics", class_name=class_name).mode
        == "disabled"
    )
    assert (
        policy.middleware_capture(
            name="customer-metrics",
            class_name=class_name,
        ).mode
        == "visible"
    )
    assert policy.middleware_capture(name="other", class_name=None).mode == "visible"


def test_middleware_overrides_require_stable_typed_selectors() -> None:
    with pytest.raises(TypeError, match="mapping"):
        CapturePolicy.public_history(
            middleware_overrides=()  # pyright: ignore[reportArgumentType]
        )
    with pytest.raises(TypeError, match="names or types"):
        CapturePolicy.public_history(
            middleware_overrides={  # pyright: ignore[reportArgumentType]
                object(): MiddlewareTraceCapture.disabled()
            }
        )

    class LocalMiddleware:
        pass

    with pytest.raises(ValueError, match="importable"):
        CapturePolicy.public_history(
            middleware_overrides={
                LocalMiddleware: MiddlewareTraceCapture.visible(),
            }
        )


def test_tool_paths_and_modes_reject_ambiguous_configuration() -> None:
    with pytest.raises(ValueError, match="root Tool capture path"):
        ToolCaptureRule(
            tool_name="read_file",
            result_paths=("", "/content"),
        )
    with pytest.raises(ValueError, match="canonical JSON Pointers"):
        ToolCaptureRule(tool_name="search", argument_paths=("/filters/~2name",))
    with pytest.raises(ValueError, match="only selected_content"):
        ToolTraceCapture(mode="full_content", argument_paths=("/query",))


def test_reasoning_policy_is_authorization_only() -> None:
    assert ReasoningCapturePolicy.omitted().mode == "omit"
    assert ReasoningCapturePolicy.content().mode == "content"


def test_captured_value_distinguishes_json_null_from_omission() -> None:
    value = CapturedValue(disposition="inline", safe_size_bytes=4, value=None)
    assert value.value is None
    assert value.reason is None

    with pytest.raises(ValueError, match="canonical JSON bytes"):
        CapturedValue(disposition="inline", safe_size_bytes=3, value=None)
    with pytest.raises(ValueError, match="omitted capture requires a reason"):
        CapturedValue(disposition="omitted", safe_size_bytes=0)
