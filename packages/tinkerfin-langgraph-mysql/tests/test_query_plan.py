"""Deterministic query-plan and driver-value regression contracts."""

from __future__ import annotations

from datetime import UTC, datetime

import orjson
import pytest
from langgraph.store.base import ListNamespacesOp, MatchCondition, SearchOp

from langgraph.store.mysql.base import BaseMySQLStore, Row, row_to_item


def _row(value: object) -> Row:
    timestamp = datetime(2026, 8, 28, tzinfo=UTC)
    return Row(
        key="preferences",
        value=value,
        prefix="users.42",
        created_at=timestamp,
        updated_at=timestamp,
    )


def test_locked_orjson_fragment_decodes_without_private_attributes() -> None:
    fragment = orjson.Fragment(b'{"theme":"dark"}')
    assert not hasattr(fragment, "buf")
    assert not hasattr(fragment, "contents")

    item = row_to_item(("users", "42"), _row(fragment))

    assert item.value == {"theme": "dark"}


def test_fragment_must_decode_to_a_store_object() -> None:
    with pytest.raises(TypeError, match="must decode to an object"):
        row_to_item(("users", "42"), _row(orjson.Fragment(b"[1,2]")))


def test_search_namespace_prefix_uses_complete_literal_labels() -> None:
    store = BaseMySQLStore()
    query, params = store._prepare_batch_search_queries(
        [
            (
                0,
                SearchOp(
                    namespace_prefix=("user%_",),
                    filter=None,
                    limit=10,
                    offset=0,
                ),
            )
        ]
    )[0]

    assert " LIKE " not in query
    assert "CHAR_LENGTH(prefix)" in query
    assert "SUBSTRING_INDEX" in query
    assert params == [1, 1, "user%_", 10, 0]


def test_search_treats_star_as_a_literal_namespace_label() -> None:
    store = BaseMySQLStore()
    query, params = store._prepare_batch_search_queries(
        [
            (
                0,
                SearchOp(
                    namespace_prefix=("*",),
                    filter=None,
                    limit=10,
                    offset=0,
                ),
            )
        ]
    )[0]

    assert "SUBSTRING_INDEX" in query
    assert params == [1, 1, "*", 10, 0]


def test_namespace_listing_matches_prefix_suffix_and_wildcard_by_label() -> None:
    store = BaseMySQLStore()
    query, params = store._get_batch_list_namespaces_queries(
        [
            (
                0,
                ListNamespacesOp(
                    match_conditions=(
                        MatchCondition(match_type="prefix", path=("users", "*")),
                        MatchCondition(match_type="suffix", path=("tail%_", "leaf")),
                    ),
                    max_depth=3,
                    limit=20,
                    offset=4,
                ),
            )
        ]
    )[0]

    assert " LIKE " not in query
    assert params == (
        3,
        3,
        2,
        1,
        "users",
        2,
        -2,
        "tail%_",
        -1,
        "leaf",
        20,
        4,
    )


@pytest.mark.parametrize(
    ("operator", "sql_operator"),
    [("$gt", ">"), ("$gte", ">="), ("$lt", "<"), ("$lte", "<=")],
)
def test_numeric_filter_query_never_falls_back_to_text_ordering(
    operator: str,
    sql_operator: str,
) -> None:
    store = BaseMySQLStore()

    condition, params = store._get_filter_condition("score", operator, 2.5)

    assert "AS CHAR" not in condition
    assert "AS DOUBLE" in condition
    assert "UNSIGNED INTEGER" in condition
    assert f" {sql_operator} %s" in condition
    assert params == ["score", "score", 2.5]


@pytest.mark.parametrize("operand", [2**53 - 1, 2**53, 2**53 + 1, 2**63 - 1, -(2**63)])
def test_integer_filter_matches_locked_in_memory_float_coercion(operand: int) -> None:
    store = BaseMySQLStore()

    condition, params = store._get_filter_condition("score", "$gt", operand)

    assert "AS DOUBLE" in condition
    assert params == ["score", "score", float(operand)]


@pytest.mark.parametrize("operand", ["2", True, None, float("inf")])
def test_numeric_filter_rejects_non_numeric_or_non_finite_operands(
    operand: object,
) -> None:
    store = BaseMySQLStore()

    with pytest.raises((TypeError, ValueError), match="operands"):
        store._get_filter_condition("score", "$gt", operand)
