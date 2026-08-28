"""Public finite-JSON normalization contracts."""

from __future__ import annotations

import inspect

import pytest

from tinkerfin_native_stream import NativeStreamContractError, to_json_value


def test_public_normalizer_hides_recursive_cycle_state() -> None:
    assert tuple(inspect.signature(to_json_value).parameters) == ("value",)

    recursive: list[object] = []
    recursive.append(recursive)

    with pytest.raises(NativeStreamContractError, match="must not contain cycles"):
        to_json_value(recursive)
