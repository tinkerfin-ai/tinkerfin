"""Public-safe capture and omission contracts."""

from __future__ import annotations

import pytest

from tinkerfin_tracing import (
    CapturePolicy,
    MiddlewareTraceCapture,
    ToolTraceCapture,
    TraceCaptureRejected,
)
from tinkerfin_tracing.capture import CapturedValue, ToolCaptureRule


class _MetricsMiddleware:
    pass


def test_public_safe_capture_removes_only_reserved_reasoning_and_credentials() -> None:
    captured = CapturePolicy.public_safe().capture(
        {
            "content": "visible",
            "reasoning_content": "business value",
            "additional_kwargs": {
                "reasoning_content": "private provider trace",
                "ordinary": "kept",
            },
            "nested": {"api_key": "secret", "value": "kept"},
        },
        max_bytes=4096,
    )

    assert captured.disposition == "inline"
    assert captured.value == {
        "content": "visible",
        "reasoning_content": "business value",
        "additional_kwargs": {"ordinary": "kept"},
        "nested": {"api_key": {"$type": "redacted"}, "value": "kept"},
    }


def test_public_safe_tool_content_is_metadata_only_without_an_allowlist() -> None:
    captured = CapturePolicy.public_safe().capture_tool(
        tool_name="search",
        value={"query": "private", "limit": 5},
        target="arguments",
        max_bytes=4096,
    )

    assert captured.disposition == "omitted"
    assert captured.value is None
    assert captured.reason == "tool_content_metadata_only"
    assert captured.safe_size_bytes > 0


def test_public_history_captures_every_tool_without_a_second_registry() -> None:
    policy = CapturePolicy.public_history()

    captured = policy.capture_tool(
        tool_name="new_business_tool",
        value={"query": "public", "api_key": "private"},
        target="arguments",
        max_bytes=4096,
    )

    assert captured.value == {
        "query": "public",
        "api_key": {"$type": "redacted"},
    }
    assert policy.traces_tool("new_business_tool") is True
    assert policy.captures_review_description("new_business_tool") is True


def test_public_history_applies_exact_per_tool_capture_overrides() -> None:
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

    metadata = policy.capture_tool(
        tool_name="metadata",
        value={"query": "private"},
        target="arguments",
        max_bytes=4096,
    )
    selected = policy.capture_tool(
        tool_name="selected",
        value={"query": "public", "other": "private"},
        target="arguments",
        max_bytes=4096,
    )
    disabled = policy.capture_tool(
        tool_name="disabled",
        value={"query": "private"},
        target="arguments",
        max_bytes=4096,
    )

    assert metadata.reason == "tool_content_metadata_only"
    assert selected.value == {"/query": "public"}
    assert disabled.reason == "tool_tracing_disabled"
    assert policy.traces_tool("disabled") is False
    assert policy.traces_tool("unconfigured") is True


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
            "customer-metrics": MiddlewareTraceCapture.configuration_only(),
        },
    )
    class_name = f"{_MetricsMiddleware.__module__}.{_MetricsMiddleware.__qualname__}"

    assert policy.include_error_messages is True
    assert (
        policy.middleware_capture(
            name="internal-metrics",
            class_name=class_name,
        ).mode
        == "disabled"
    )
    assert (
        policy.middleware_capture(
            name="_MetricsMiddleware",
            class_name=None,
        ).mode
        == "disabled"
    )
    assert (
        policy.middleware_capture(
            name="customer-metrics",
            class_name=class_name,
        ).mode
        == "configuration_only"
    )
    assert (
        policy.middleware_capture(
            name="unconfigured",
            class_name=None,
        ).mode
        == "visible"
    )


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
    with pytest.raises(TypeError, match="MiddlewareTraceCapture"):
        CapturePolicy.public_history(
            middleware_overrides={  # pyright: ignore[reportArgumentType]
                _MetricsMiddleware: object()
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


def test_tool_allowlist_preserves_only_selected_json_pointer_paths() -> None:
    policy = CapturePolicy.public_safe(
        tool_rules=(
            ToolCaptureRule(
                tool_name="search",
                argument_paths=("/query", "/filters/0/name"),
            ),
        )
    )

    captured = policy.capture_tool(
        tool_name="search",
        value={
            "query": "public",
            "filters": [{"name": "docs", "token": "private"}],
            "unselected": "private",
        },
        target="arguments",
        max_bytes=4096,
    )

    assert captured.value == {
        "/query": "public",
        "/filters/0/name": "docs",
    }


def test_tool_allowlist_can_explicitly_select_the_sanitized_root_value() -> None:
    policy = CapturePolicy.public_safe(
        tool_rules=(ToolCaptureRule(tool_name="read_file", result_paths=("",)),)
    )

    captured = policy.capture_tool(
        tool_name="read_file",
        value={"content": "public", "api_key": "private"},
        target="result",
        max_bytes=4096,
    )

    assert captured.value == {
        "": {
            "content": "public",
            "api_key": {"$type": "redacted"},
        }
    }


def test_root_tool_capture_path_cannot_be_combined_with_nested_paths() -> None:
    with pytest.raises(ValueError, match="root Tool capture path"):
        ToolCaptureRule(
            tool_name="read_file",
            result_paths=("", "/content"),
        )


def test_oversized_safe_value_is_explicitly_omitted() -> None:
    captured = CapturePolicy.public_safe().capture("x" * 100, max_bytes=16)

    assert captured.disposition == "omitted"
    assert captured.reason == "payload_too_large"
    assert captured.safe_size_bytes > 16


def test_json_null_is_a_valid_inline_capture() -> None:
    captured = CapturePolicy.public_safe().capture(None, max_bytes=4096)

    assert captured.disposition == "inline"
    assert captured.value is None
    assert captured.reason is None

    with pytest.raises(ValueError, match="canonical JSON bytes"):
        CapturedValue(disposition="inline", safe_size_bytes=3, value=None)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_capture_rejects_non_finite_numbers_before_serialization(value: float) -> None:
    with pytest.raises(TraceCaptureRejected, match="finite JSON"):
        CapturePolicy.public_safe().capture(value, max_bytes=4096)


def test_credential_redaction_covers_common_authorization_spellings() -> None:
    captured = CapturePolicy.public_safe().capture(
        {
            "client-secret": "private",
            "private_key": "private",
            "proxyAuthorization": "private",
            "session_token": "private",
            "ordinary": "public",
        },
        max_bytes=4096,
    )

    assert captured.value == {
        "client-secret": {"$type": "redacted"},
        "private_key": {"$type": "redacted"},
        "proxyAuthorization": {"$type": "redacted"},
        "session_token": {"$type": "redacted"},
        "ordinary": "public",
    }


def test_credential_redaction_covers_vendor_and_token_key_spellings() -> None:
    captured = CapturePolicy.public_safe().capture(
        {
            "OPENAI_API_KEY": "sk-live-secret",
            "auth_token": "bearer-secret",
            "id_token": "jwt-secret",
            "secret_key": "signing-secret",
            "GITHUB_TOKEN": "github-secret",
            "token": "generic-secret",
            "ordinary_key": "public",
        },
        max_bytes=4096,
    )

    assert captured.value == {
        "OPENAI_API_KEY": {"$type": "redacted"},
        "auth_token": {"$type": "redacted"},
        "id_token": {"$type": "redacted"},
        "secret_key": {"$type": "redacted"},
        "GITHUB_TOKEN": {"$type": "redacted"},
        "token": {"$type": "redacted"},
        "ordinary_key": "public",
    }


def test_tool_capture_rejects_noncanonical_json_pointer_escape() -> None:
    with pytest.raises(ValueError, match="canonical JSON Pointers"):
        ToolCaptureRule(tool_name="search", argument_paths=("/filters/~2name",))


def test_tool_capture_does_not_treat_leading_zero_token_as_array_index() -> None:
    policy = CapturePolicy.public_safe(
        tool_rules=(ToolCaptureRule(tool_name="search", result_paths=("/items/01",)),)
    )

    captured = policy.capture_tool(
        tool_name="search",
        value={"items": ["zero", "one"]},
        target="result",
        max_bytes=4096,
    )

    assert captured.value == {}


def test_structural_capture_retains_shape_without_source_values() -> None:
    captured = CapturePolicy.public_safe().capture_structure(
        {"messages": ["private content"], "password": "credential"},
        max_bytes=4096,
    )

    assert captured.disposition == "inline"
    assert captured.value == {
        "$type": "structural_metadata",
        "dataType": "object",
        "sourceSafeSizeBytes": 64,
        "topLevelKeys": ["messages", "password"],
    }
    assert "private content" not in captured.model_dump_json(by_alias=True)
    assert "credential" not in captured.model_dump_json(by_alias=True)
