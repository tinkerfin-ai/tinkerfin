"""Current MySQL Store Schema and deterministic LangGraph query construction."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime
from typing import (
    Any,
    NotRequired,
    TypeAlias,
    cast,
)

import orjson
from langgraph.store.base import (
    GetOp,
    InvalidNamespaceError,
    Item,
    ListNamespacesOp,
    Op,
    PutOp,
    SearchItem,
    SearchOp,
)
from typing_extensions import TypedDict

_STORE_TABLE_COMMENT = "Deep Agents long-term memory Store"
_STORE_SCHEMA_STATEMENT = """
CREATE TABLE IF NOT EXISTS store (
    prefix VARCHAR(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin
        NOT NULL COMMENT 'Store document namespace',
    `key` VARCHAR(150) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin
        NOT NULL COMMENT 'Document key within the namespace',
    value JSON NOT NULL COMMENT 'Document JSON value',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP COMMENT 'Document creation time',
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP COMMENT 'Document update time',
    CONSTRAINT pk_store PRIMARY KEY (prefix, `key`),
    KEY store_prefix_idx (prefix)
) COMMENT='Deep Agents long-term memory Store'
"""

_STORE_COLUMN_CONTRACTS: tuple[tuple[str, str, str, str | None, str], ...] = (
    ("prefix", "varchar(500)", "NO", None, "Store document namespace"),
    ("key", "varchar(150)", "NO", None, "Document key within the namespace"),
    ("value", "json", "NO", None, "Document JSON value"),
    ("created_at", "timestamp", "YES", "current_timestamp", "Document creation time"),
    ("updated_at", "timestamp", "YES", "current_timestamp", "Document update time"),
)
_STORE_INDEX_CONTRACTS: tuple[tuple[str, int, int, str, str], ...] = (
    ("PRIMARY", 0, 1, "prefix", "BTREE"),
    ("PRIMARY", 0, 2, "key", "BTREE"),
    ("store_prefix_idx", 1, 1, "prefix", "BTREE"),
)


SqlParameter: TypeAlias = str | int | float | bool | bytes | None
SqlQuery: TypeAlias = tuple[str, Sequence[SqlParameter]]
GroupedOps: TypeAlias = dict[type[Op], list[tuple[int, Op]]]
GetQuery: TypeAlias = tuple[
    str,
    tuple[SqlParameter, ...],
    tuple[str, ...],
    list[tuple[int, str]],
]


class BaseMySQLStore:
    """Build deterministic MySQL queries shared by the async Store runtime."""

    _deserializer: Callable[[bytes | orjson.Fragment], dict[str, Any]] | None

    def _get_batch_GET_ops_queries(
        self,
        get_ops: Sequence[tuple[int, GetOp]],
    ) -> list[GetQuery]:
        namespace_groups: defaultdict[tuple[str, ...], list[tuple[int, str]]] = (
            defaultdict(list)
        )
        for idx, op in get_ops:
            namespace_groups[op.namespace].append((idx, op.key))
        results: list[GetQuery] = []
        for namespace, items in namespace_groups.items():
            _, keys = zip(*items)
            keys_to_query = ",".join(["%s"] * len(keys))
            query = f"""
                SELECT `key`, value, created_at, updated_at
                FROM store
                WHERE prefix = %s AND `key` IN ({keys_to_query})
            """
            params = (_namespace_to_text(namespace), *keys)
            results.append((query, params, namespace, items))
        return results

    def _prepare_batch_PUT_queries(
        self,
        put_ops: Sequence[tuple[int, PutOp]],
    ) -> list[SqlQuery]:
        # Last-write wins
        dedupped_ops: dict[tuple[tuple[str, ...], str], PutOp] = {}
        for _, op in put_ops:
            dedupped_ops[(op.namespace, op.key)] = op

        inserts: list[PutOp] = []
        deletes: list[PutOp] = []
        for op in dedupped_ops.values():
            if op.value is None:
                deletes.append(op)
            else:
                inserts.append(op)

        queries: list[SqlQuery] = []

        if deletes:
            namespace_groups: dict[tuple[str, ...], list[str]] = defaultdict(list)
            for op in deletes:
                namespace_groups[op.namespace].append(op.key)
            for namespace, keys in namespace_groups.items():
                placeholders = ",".join(["%s"] * len(keys))
                query = (
                    f"DELETE FROM store WHERE prefix = %s AND `key` IN ({placeholders})"
                )
                params = (_namespace_to_text(namespace), *keys)
                queries.append((query, params))
        if inserts:
            values: list[str] = []
            insertion_params: list[SqlParameter] = []
            for op in inserts:
                values.append("(%s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")
                insertion_params.extend(
                    [
                        _namespace_to_text(op.namespace),
                        op.key,
                        json.dumps(op.value),
                    ]
                )
            values_str = ",".join(values)
            query = f"""
                INSERT INTO store (prefix, `key`, value, created_at, updated_at)
                VALUES {values_str} AS new
                ON DUPLICATE KEY UPDATE
                    value = new.value,
                    updated_at = CURRENT_TIMESTAMP
            """
            queries.append((query, insertion_params))

        return queries

    def _prepare_batch_search_queries(
        self,
        search_ops: Sequence[tuple[int, SearchOp]],
    ) -> list[SqlQuery]:
        queries: list[SqlQuery] = []
        for _, op in search_ops:
            # Build filter conditions first
            filter_params: list[SqlParameter] = []
            filter_conditions: list[str] = []
            if op.filter:
                for key, value in op.filter.items():
                    if isinstance(value, dict):
                        operators = cast(dict[object, Any], value)
                        for raw_op_name, val in operators.items():
                            if not isinstance(raw_op_name, str):
                                raise TypeError(
                                    "Store filter operators must be strings"
                                )
                            condition, filter_params_ = self._get_filter_condition(
                                key, raw_op_name, val
                            )
                            filter_conditions.append(condition)
                            filter_params.extend(filter_params_)
                    else:
                        filter_conditions.append(
                            "json_extract(value, concat('$.', %s)) = CAST(%s AS JSON)"
                        )
                        filter_params.extend([key, json.dumps(value)])

            base_query = """
                SELECT prefix, `key`, value, created_at, updated_at
                FROM store
            """
            namespace_conditions, namespace_params = _namespace_match_conditions(
                op.namespace_prefix,
                match_type="prefix",
                allow_wildcard=False,
            )
            conditions = [*namespace_conditions, *filter_conditions]
            params: list[SqlParameter] = [*namespace_params, *filter_params]
            if conditions:
                base_query += " WHERE " + " AND ".join(conditions)

            base_query += " ORDER BY updated_at DESC"
            base_query += " LIMIT %s OFFSET %s"
            params.extend([op.limit, op.offset])

            queries.append((base_query, params))

        return queries

    def _get_batch_list_namespaces_queries(
        self,
        list_ops: Sequence[tuple[int, ListNamespacesOp]],
    ) -> list[SqlQuery]:
        queries: list[SqlQuery] = []
        for _, op in list_ops:
            query = """
                SELECT truncated_prefix, MIN(prefix)
                FROM (
                    SELECT
                        prefix,
                        CASE
                            WHEN %s IS NOT NULL THEN
                                SUBSTRING_INDEX(prefix, '.', %s)
                            ELSE prefix
                        END AS truncated_prefix
                    FROM store
            """
            params: list[Any] = [op.max_depth, op.max_depth]

            conditions: list[str] = []
            if op.match_conditions:
                for condition in op.match_conditions:
                    condition_sql, condition_params = _namespace_match_conditions(
                        condition.path,
                        match_type=condition.match_type,
                        allow_wildcard=True,
                    )
                    conditions.extend(condition_sql)
                    params.extend(condition_params)

            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            query += ") AS subquery "

            query += " GROUP BY truncated_prefix"
            query += " ORDER BY truncated_prefix LIMIT %s OFFSET %s"
            params.extend([op.limit, op.offset])
            queries.append((query, tuple(params)))

        return queries

    def _get_filter_condition(
        self,
        key: str,
        op: str,
        value: Any,
    ) -> tuple[str, list[SqlParameter]]:
        """Helper to generate filter conditions."""
        if op == "$eq":
            return "json_extract(value, concat('$.', %s)) = CAST(%s AS JSON)", [
                key,
                json.dumps(value),
            ]
        elif op in {"$gt", "$gte", "$lt", "$lte"}:
            operator = {"$gt": ">", "$gte": ">=", "$lt": "<", "$lte": "<="}[op]
            operand = _numeric_filter_operand(value)
            extracted = "json_extract(value, concat('$.', %s))"
            return (
                f"(JSON_TYPE({extracted}) IN "
                "('INTEGER', 'UNSIGNED INTEGER', 'DOUBLE', 'DECIMAL') "
                f"AND CAST(JSON_UNQUOTE({extracted}) AS DOUBLE) {operator} %s)"
            ), [key, key, operand]
        elif op == "$ne":
            return "json_extract(value, concat('$.', %s)) != CAST(%s AS JSON)", [
                key,
                json.dumps(value),
            ]
        else:
            raise ValueError(f"Unsupported operator: {op}")


class Row(TypedDict):
    """Describe one document row returned by a Store query."""

    key: str
    value: object
    prefix: str
    created_at: datetime
    updated_at: datetime
    score: NotRequired[str | bytes | int | float | None]


class NamespaceRow(TypedDict):
    """Describe one namespace projection returned by the Store query."""

    truncated_prefix: str | bytes | list[str]


def _namespace_to_text(namespace: tuple[str, ...]) -> str:
    """Convert namespace tuple to text string."""
    return ".".join(namespace)


def _namespace_match_conditions(
    path: tuple[str, ...],
    *,
    match_type: str,
    allow_wildcard: bool,
) -> tuple[list[str], list[SqlParameter]]:
    """Match complete namespace labels without SQL wildcard interpretation.

    LangGraph rejects dots inside namespace labels, so the persisted dot separator is
    an exact hierarchy boundary. ``SUBSTRING_INDEX`` compares each label as a bound
    parameter; percent and underscore therefore remain ordinary label characters.
    ``*`` is a wildcard only for ListNamespaces match conditions. Search treats it as
    the same literal namespace label as InMemoryStore.

    Args:
        path: Namespace labels selected by Search or ListNamespaces.
        match_type: ``prefix`` or ``suffix`` label direction.
        allow_wildcard: Whether ``*`` omits one ListNamespaces label predicate.

    Returns:
        Ordered SQL predicates and their parameter values.

    Raises:
        ValueError: ``match_type`` is unsupported.
    """

    if match_type not in {"prefix", "suffix"}:
        raise ValueError(f"Unsupported namespace match type: {match_type}")
    if not path:
        return [], []
    depth = "(1 + CHAR_LENGTH(prefix) - CHAR_LENGTH(REPLACE(prefix, '.', '')))"
    conditions = [f"{depth} >= %s"]
    params: list[SqlParameter] = [len(path)]
    for index, label in enumerate(path, start=1):
        if allow_wildcard and label == "*":
            continue
        if match_type == "prefix":
            expression = "SUBSTRING_INDEX(SUBSTRING_INDEX(prefix, '.', %s), '.', -1)"
            position = index
        else:
            expression = "SUBSTRING_INDEX(SUBSTRING_INDEX(prefix, '.', %s), '.', 1)"
            position = -(len(path) - index + 1)
        conditions.append(f"{expression} = %s")
        params.extend([position, label])
    return conditions, params


def _numeric_filter_operand(value: object) -> float:
    """Match the locked InMemoryStore finite-float ordering contract."""

    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError("Store ordering filter operands must be numbers")
    operand = float(value)
    if not math.isfinite(operand):
        raise ValueError("Store ordering filter operands must be finite")
    return operand


def row_to_item(
    namespace: tuple[str, ...],
    row: Row,
    *,
    loader: Callable[[bytes | orjson.Fragment], dict[str, Any]] | None = None,
) -> Item:
    """Convert a row from the database into an Item."""
    loader = loader or _json_loads
    return Item(
        value=_stored_value(row["value"], loader=loader),
        key=row["key"],
        namespace=namespace,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def row_to_search_item(
    namespace: tuple[str, ...],
    row: Row,
    *,
    loader: Callable[[bytes | orjson.Fragment], dict[str, Any]] | None = None,
) -> SearchItem:
    """Convert a database row into a scored search result."""
    loader = loader or _json_loads
    score = row.get("score")
    if score is not None:
        try:
            score = float(score)
        except (TypeError, ValueError):
            score = None
    return SearchItem(
        value=_stored_value(row["value"], loader=loader),
        key=row["key"],
        namespace=namespace,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        score=score,
    )


def group_ops(ops: Iterable[Op]) -> tuple[GroupedOps, int]:
    """Validate document operations before grouping a single batch for execution.

    LangGraph BaseStore validates convenience writes, but explicit PutOp and GetOp
    values also enter here. Reject ambiguous document paths and unsupported TTLs
    before any database work. Search and listing paths retain their separate matching
    semantics, including empty prefixes and listing wildcards.

    Args:
        ops: Document and namespace operations in their original result order.

    Returns:
        Operations grouped by concrete type and the expected result count.

    Raises:
        InvalidNamespaceError: A document path violates LangGraph's namespace rules.
        NotImplementedError: A document operation requests an unsupported TTL.
    """

    grouped_ops: GroupedOps = defaultdict(list)
    tot = 0
    for idx, op in enumerate(ops):
        if isinstance(op, GetOp | PutOp):
            _validate_document_namespace(op.namespace)
        if isinstance(op, PutOp) and op.ttl is not None:
            raise NotImplementedError("TTL is not supported by the MySQL Store")
        grouped_ops[type(op)].append((idx, op))
        tot += 1
    return grouped_ops, tot


def _validate_document_namespace(namespace: tuple[str, ...]) -> None:
    """Keep document identities within the locked BaseStore namespace contract."""
    if not namespace:
        raise InvalidNamespaceError("Namespace cannot be empty")
    for label in namespace:
        if not isinstance(label, str) or not label or "." in label:
            raise InvalidNamespaceError(
                "Namespace labels must be non-empty strings without periods"
            )
    if namespace[0] == "langgraph":
        raise InvalidNamespaceError("The langgraph root namespace is reserved")


def _json_loads(content: bytes | orjson.Fragment) -> dict[str, Any]:
    if isinstance(content, orjson.Fragment):
        # Fragment intentionally exposes no raw-buffer attribute in the locked orjson
        # 3.11.9 API. Serializing the Fragment alone is the supported way to recover
        # its exact JSON bytes before applying the Store's object-only validation.
        content = orjson.dumps(content)
    decoded = orjson.loads(content)
    if not isinstance(decoded, dict):
        raise TypeError("stored LangGraph values must decode to an object")
    return cast(dict[str, Any], decoded)


def decode_namespace(namespace: str | bytes | list[str]) -> tuple[str, ...]:
    """Decode one driver namespace representation into its tuple contract."""

    if isinstance(namespace, list):
        return tuple(namespace)
    if isinstance(namespace, bytes):
        namespace = namespace.decode()[1:]
    return tuple(namespace.split("."))


def _stored_value(
    value: object,
    *,
    loader: Callable[[bytes | orjson.Fragment], dict[str, Any]],
) -> dict[str, Any]:
    """Validate a decoded driver value or deserialize its encoded representation."""

    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, item in cast(dict[object, object], value).items():
            if not isinstance(key, str):
                raise TypeError("stored LangGraph value keys must be strings")
            normalized[key] = item
        return normalized
    if isinstance(value, str):
        return loader(value.encode())
    if isinstance(value, bytes | orjson.Fragment):
        return loader(value)
    raise TypeError("stored LangGraph values must be JSON objects or encoded JSON")
