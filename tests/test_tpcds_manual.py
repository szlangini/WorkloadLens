from __future__ import annotations

import sqlglot

from pathlib import Path

import sqlglot

from workloadlens.coverage.types_ast import collect_top_level_order_types, summarize_ast_operator_types
from workloadlens.utils.schema import parse_schema_types
from .tpcds_utils import resolve_query_path


SCHEMA_PATH = Path(__file__).resolve().parent / "data" / "tpcds_schema.sql"


def _load_schema():
    schema_sql = SCHEMA_PATH.read_text()
    return parse_schema_types(schema_sql)


def _assert_operator_counts(query_identifier: int, expected: dict[str, dict[str, int]], expected_order: dict[str, int] | None = None) -> None:
    schema = _load_schema()
    sql_path = resolve_query_path(query_identifier)
    sql_text = sql_path.read_text()
    ast = sqlglot.parse_one(sql_text, read="tsql")
    query_name = sql_path.name

    type_counts = summarize_ast_operator_types(ast, schema)
    for operator, type_map in expected.items():
        actual = type_counts.get(operator)
        normalized_actual = dict((key, actual[key]) for key in sorted(actual)) if actual else {}
        assert actual is not None, f"{query_name}: expected operator '{operator}' to be present"
        assert dict(sorted(actual.items())) == dict(sorted(type_map.items())), f"{query_name} operator {operator}: expected {type_map}, got {normalized_actual}"

    if expected_order is not None:
        order_counts = collect_top_level_order_types(ast, schema)
        assert dict(sorted(order_counts.items())) == dict(sorted(expected_order.items())), f"{query_name} order types mismatch"


def test_query1_type_counts() -> None:
    _assert_operator_counts(
        1,
        expected={
            "join": {"INT": 14},
            "filter": {"DECIMAL": 2, "INT": 2, "TEXT": 1},
            "aggregate": {"DECIMAL": 4},
            "project": {"DECIMAL": 2, "INT": 2, "TEXT": 1},
            "sort": {"TEXT": 1},
        },
        expected_order={"TEXT": 1},
    )


def test_query14_type_counts() -> None:
    _assert_operator_counts(
        14,
        expected={
            "join": {"INT": 188},
            "filter": {"DECIMAL": 10, "INT": 126},
            "aggregate": {"DECIMAL": 24, "INT": 25},
            "project": {"DECIMAL": 13, "INT": 72, "TEXT": 3},
            "sort": {"INT": 6, "TEXT": 2},
        },
        expected_order={"INT": 3, "TEXT": 1},
    )


def test_query27_type_counts() -> None:
    _assert_operator_counts(
        27,
        expected={
            "join": {"INT": 8},
            "filter": {"INT": 1, "TEXT": 4},
            "aggregate": {"DECIMAL": 3, "INT": 1},
            "project": {"DECIMAL": 3, "INT": 1, "TEXT": 3},
            "sort": {"TEXT": 2},
        },
        expected_order={"TEXT": 2},
    )
