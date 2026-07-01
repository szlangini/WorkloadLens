from __future__ import annotations

from typing import Dict, Any
import json

import pytest

from workloadlens.report.aggregate import aggregate_records
from workloadlens.core.normalize import extract_functions
import sqlglot
from workloadlens.report.aggregate import aggregate_jsonl_paths
from workloadlens.coverage.types_ast import (
    collect_top_level_order_types,
    collect_group_by_key_counts,
    collect_union_all_fan_in,
    compute_group_by_key_count,
)
from .tpcds_utils import resolve_query_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _query_record(
    operators: Dict[str, int],
    *,
    operator_types_origin: Dict[str, Dict[str, int]] | None = None,
    limit: Dict[str, Any] | None = None,
    union_total: int | None = None,
    features: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    ops = dict(operators)
    if union_total is not None:
        ops["union_all"] = union_total
    record = {
        "id": "q.sql#1",
        "mode": "plan_typed",
        "operators": ops,
        "join_types": {},
        "functions": {"projection": [], "aggregate": [], "window": [], "expression": [], "all": []},
        "statement_type": "Select",
        "operator_types_origin": operator_types_origin or {},
        "operator_types": {},
        "features": features or {},
        "sql_length_bytes": 10,
        "operator_total": sum(ops.values()),
        "is_query": True,
        "limit": limit or {"has_limit": False, "value": None, "has_order_by": False},
    }
    return record


def _schema_record() -> Dict[str, Any]:
    return {
        "id": "schema.sql#non_query1",
        "mode": "schema",
        "operators": {},
        "join_types": {},
        "functions": {"projection": [], "aggregate": [], "window": [], "expression": [], "all": []},
        "statement_type": "Create",
        "sql_length_bytes": 42,
        "operator_total": 0,
        "is_query": False,
    }


def test_schema_included_in_all_scope_not_queries() -> None:
    records = [
        _schema_record(),
        _query_record({"scan": 1}),
    ]

    stats_all = aggregate_records(records)
    stats_queries = aggregate_records(records, predicate=lambda rec: bool(rec.get("is_query")))

    assert stats_all.schema_statements == 1
    assert stats_all.statement_types["Create"] == 1
    assert stats_queries.schema_statements == 0
    assert stats_queries.statement_types["Select"] == 1


def test_origin_types_capture_text_join_even_with_casts() -> None:
    record = _query_record(
        {"scan": 2, "join": 1},
        operator_types_origin={"join": {"TEXT": 2}},
    )
    stats_queries = aggregate_records([record], predicate=lambda rec: bool(rec.get("is_query")))
    origin_counts = stats_queries.origin_operator_type_counts_dict()
    assert origin_counts["join"]["TEXT"] == 2


def test_origin_types_capture_filter_and_order_types() -> None:
    record = _query_record(
        {"scan": 1, "filter": 1, "sort": 1},
        operator_types_origin={
            "filter": {"DATE": 1},
            "sort": {"TIMESTAMP": 1},
        },
    )
    stats_queries = aggregate_records([record], predicate=lambda rec: bool(rec.get("is_query")))
    origin_counts = stats_queries.origin_operator_type_counts_dict()
    assert origin_counts["filter"]["DATE"] == 1
    assert origin_counts["sort"]["TIMESTAMP"] == 1


def test_limit_distributions_split_by_order() -> None:
    records = [
        _query_record(
            {"scan": 1, "limit": 1},
            limit={"has_limit": True, "value": 100, "has_order_by": False},
        ),
        _query_record(
            {"scan": 1, "sort": 1, "limit": 1},
            limit={"has_limit": True, "value": 0, "has_order_by": True},
        ),
    ]
    stats_queries = aggregate_records(records, predicate=lambda rec: bool(rec.get("is_query")))
    assert stats_queries.limit_without_order == [100]
    assert stats_queries.limit_with_order == [0]


def test_conditional_join_and_union_lists() -> None:
    records = [
        _query_record({"scan": 1}, features={"has_union": False}),  # no join / union
        _query_record({"scan": 2, "join": 4}, union_total=3, features={"has_union": True}),
    ]
    stats_queries = aggregate_records(records, predicate=lambda rec: bool(rec.get("is_query")))
    assert 4 in stats_queries.joins_per_query
    assert stats_queries.union_all_totals.count(3) == 1
    assert stats_queries.union_all_totals.count(0) == 1
    assert stats_queries.feature_counts.get("has_union") == 1
    assert stats_queries.total_queries == 2


def test_union_all_fan_in_aggregation() -> None:
    records = [
        {
            **_query_record({"scan": 1}),
            "union_all_fan_in_values": [3, 4],
        },
        {
            **_query_record({"scan": 1}),
            "union_all_fan_in_values": [2],
        },
    ]
    stats_queries = aggregate_records(records, predicate=lambda rec: bool(rec.get("is_query")))
    assert sorted(stats_queries.union_all_fan_in_values) == [2, 3, 4]


def test_extract_functions_detects_anyvalue() -> None:
    sql = "SELECT ANYVALUE(c) FROM t GROUP BY k"
    ast = sqlglot.parse_one(sql)
    funcs = extract_functions(ast)
    assert "ANYVALUE" in funcs["agg_funcs"]


def test_summary_counts_balances_totals() -> None:
    records = [
        _query_record({"scan": 1}, features={}),
        {
            "id": "q2.sql#1",
            "mode": "ast_only",
            "operators": {"scan": 1},
            "join_types": {},
            "functions": {"projection": [], "aggregate": [], "window": [], "expression": [], "all": []},
            "statement_type": "Select",
            "operator_types_origin": {},
            "operator_types": {},
            "features": {},
            "sql_length_bytes": 5,
            "operator_total": 1,
            "is_query": True,
            "limit": {"has_limit": False, "value": None, "has_order_by": False},
        },
        {
            "id": "bad.sql#1",
            "mode": "error",
            "operators": {},
            "join_types": {},
            "functions": {"projection": [], "aggregate": [], "window": [], "expression": [], "all": []},
            "statement_type": "Select",
            "sql_length_bytes": 0,
            "operator_total": 0,
            "is_query": False,
        },
    ]
    stats_all = aggregate_records(records)
    from workloadlens.report.pdf import _summary_counts

    summary = _summary_counts(stats_all)
    assert summary["total"] == 3
    assert summary["substrait"] == 1
    assert summary["parsed_ast"] == 1  # ast-only only
    assert summary["failed"] == 1


def test_aggregate_jsonl_paths_operator_source(tmp_path) -> None:
    json_path = tmp_path / "coverage.jsonl"
    records = [
        {
            "id": "q1#1",
            "mode": "plan_typed",
            "operators": {"scan": 1},
            "join_types": {},
            "functions": {"projection": [], "aggregate": [], "window": [], "expression": [], "all": []},
            "statement_type": "Select",
            "operator_total": 1,
            "operator_source": "substrait",
            "is_query": True,
        },
        {
            "id": "schema#1",
            "mode": "schema",
            "operators": {},
            "join_types": {},
            "functions": {"projection": [], "aggregate": [], "window": [], "expression": [], "all": []},
            "statement_type": "Create",
            "operator_total": 0,
            "operator_source": "ast",
            "is_query": False,
        },
    ]
    json_path.write_text("\n".join(json.dumps(rec) for rec in records), encoding="utf-8")
    bundle = aggregate_jsonl_paths([json_path])
    assert bundle.metadata.get("operator_source") == "Substrait"


def test_group_by_key_count_basic() -> None:
    ast = sqlglot.parse_one(
        "SELECT sum(v) FROM t GROUP BY region, category",
        read="duckdb",
    )
    assert compute_group_by_key_count(ast) == 2


def test_union_all_fan_in_detection() -> None:
    ast = sqlglot.parse_one(
        "SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3",
        read="duckdb",
    )
    assert collect_union_all_fan_in(ast) == [3]

    ast_distinct = sqlglot.parse_one(
        "SELECT 1 UNION SELECT 2 UNION ALL SELECT 3",
        read="duckdb",
    )
    assert collect_union_all_fan_in(ast_distinct) == [2]


def test_group_by_key_count_rollup_combines_axes() -> None:
    ast = sqlglot.parse_one(
        "SELECT sum(v) FROM t GROUP BY region, ROLLUP(category, subcategory)",
        read="duckdb",
    )
    assert compute_group_by_key_count(ast) == 3


def test_group_by_key_count_rollup_matches_tpcds_query() -> None:
    sql_path = resolve_query_path(27)
    sql_text = sql_path.read_text()
    ast = sqlglot.parse_one(sql_text, read="tsql")
    assert compute_group_by_key_count(ast) == 2


def test_group_by_key_count_distinct_keyword() -> None:
    ast = sqlglot.parse_one("SELECT DISTINCT a, b FROM t", read="duckdb")
    assert compute_group_by_key_count(ast) == 2


def test_group_by_key_count_distinct_function() -> None:
    ast = sqlglot.parse_one("SELECT DISTINCT(a) FROM t", read="tsql")
    assert compute_group_by_key_count(ast) == 1


def test_group_by_key_count_count_distinct() -> None:
    ast = sqlglot.parse_one(
        "SELECT COUNT(DISTINCT order_id), SUM(total) FROM sales",
        read="duckdb",
    )
    assert compute_group_by_key_count(ast) == 1


def test_collect_group_by_key_counts_all_selects() -> None:
    ast = sqlglot.parse_one(
        "WITH a AS (SELECT x, y FROM t GROUP BY x, y) "
        "SELECT x FROM a GROUP BY x",
        read="duckdb",
    )
    counts = collect_group_by_key_counts(ast)
    assert sorted(counts) == [1, 2]


def test_collect_group_by_key_counts_empty_when_no_grouping() -> None:
    ast = sqlglot.parse_one("SELECT * FROM t", read="duckdb")
    assert collect_group_by_key_counts(ast) == []


def test_collect_top_level_order_types_basic() -> None:
    schema = {"sales": {"order_id": "INT", "label": "TEXT"}}
    ast = sqlglot.parse_one("SELECT order_id FROM sales ORDER BY label", read="duckdb")
    counts = collect_top_level_order_types(ast, schema)
    assert counts["TEXT"] == 1
    assert "INT" not in counts


def test_collect_top_level_order_types_ignores_inner_sort() -> None:
    schema = {"sales": {"order_id": "INT", "label": "TEXT"}}
    ast = sqlglot.parse_one(
        "SELECT * FROM (SELECT * FROM sales ORDER BY label) t ORDER BY order_id",
        read="duckdb",
    )
    counts = collect_top_level_order_types(ast, schema)
    assert counts["INT"] == 1
    assert "TEXT" not in counts


def test_aggregate_records_tracks_top_level_order_types() -> None:
    record = _query_record({"scan": 1})
    record["order_by_top_types"] = {"TEXT": 2, "INT": 1}
    record["order_by_top_types_unique"] = ["TEXT", "INT", "TEXT"]
    stats = aggregate_records([record])
    assert stats.top_level_order_types["TEXT"] == 2
    assert stats.top_level_order_types["INT"] == 1
    assert stats.top_level_order_types_unique["TEXT"] == 1
    assert stats.top_level_order_types_unique["INT"] == 1


def test_aggregate_records_order_unique_fallback() -> None:
    record = _query_record({"scan": 1})
    record["operator_types"] = {"sort": {"INT": 1, "DECIMAL": 1}}
    stats = aggregate_records([record])
    assert stats.top_level_order_types_unique["INT"] == 1
    assert stats.top_level_order_types_unique["DECIMAL"] == 1


def test_aggregate_records_limit_with_order_handles_missing_limit() -> None:
    record = _query_record({"scan": 1, "limit": 1})
    record["limit"] = {"has_limit": False, "value": None, "has_order_by": True}
    stats = aggregate_records([record], predicate=lambda rec: True)
    assert stats.limit_with_order == [0]


def test_aggregate_records_limit_lists_override_top_level() -> None:
    record = _query_record({"scan": 1, "limit": 1})
    record["limit_with_order_values"] = [5, 0]
    record["limit_without_order_values"] = [20]
    record["operators"]["sort"] = 2
    stats = aggregate_records([record], predicate=lambda rec: True)
    assert stats.limit_with_order == [5, 0]
    assert stats.limit_without_order == [20]


def test_aggregate_records_sort_excess_counts_as_zero() -> None:
    record = _query_record({"scan": 1, "limit": 1})
    record["limit_with_order_values"] = [100]
    record["operators"]["sort"] = 3
    stats = aggregate_records([record], predicate=lambda rec: True)
    assert stats.limit_with_order.count(0) == 2
    assert stats.limit_with_order.count(100) == 1
