from __future__ import annotations

import json
import os
from pathlib import Path
from collections import Counter

import pytest
import sqlglot
from sqlglot import exp
from typer.testing import CliRunner

from workloadlens.cli import app
from workloadlens.coverage.operators import summarize_ast_operators
from workloadlens.core.normalize import extract_functions
from .tpcds_utils import resolve_query_path
from tests.utils_substrait import has_substrait_support


@pytest.mark.skipif(
    not has_substrait_support(),
    reason="Substrait extension not available (set SQLCOV_SUBSTRAIT_PATH or allow INSTALL substrait FROM community)",
)
def test_cli_tpcds_join_heavy_queries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Smoke test that runs three TPC-DS queries with multiple joins end-to-end through the CLI.

    The selected queries (47, 63, 89) each involve the classic store_sales / item / date_dim / store
    join topology and, in the case of q47, an additional self-join over a derived relation. We verify
    that the Substrait summary reflects the expected volume of joins and reads.
    """
    runner = CliRunner()

    # Respect existing environment configuration for local Substrait builds.
    ext_path = os.environ.get("SQLCOV_SUBSTRAIT_PATH", "")
    monkeypatch.setenv("SQLCOV_SUBSTRAIT_PATH", ext_path)

    schema_sql = """
    CREATE TABLE item (
        i_item_sk INTEGER,
        i_category VARCHAR,
        i_brand VARCHAR,
        i_manager_id INTEGER,
        i_class VARCHAR
    );

    CREATE TABLE store_sales (
        ss_item_sk INTEGER,
        ss_sold_date_sk INTEGER,
        ss_store_sk INTEGER,
        ss_sales_price DECIMAL(12, 2)
    );

    CREATE TABLE store (
        s_store_sk INTEGER,
        s_store_name VARCHAR,
        s_company_name VARCHAR
    );

    CREATE TABLE date_dim (
        d_date_sk INTEGER,
        d_year INTEGER,
        d_moy INTEGER,
        d_month_seq INTEGER
    );
    """.strip()

    schema_path = tmp_path / "tpcds_schema.sql"
    schema_path.write_text(schema_sql)

    # Copy the canonical TPC-DS query text into the temp workspace
    query_numbers = [47, 63, 89]
    query_sources = [(number, resolve_query_path(number)) for number in query_numbers]

    queries_dir = tmp_path / "queries"
    queries_dir.mkdir()
    for _, src in query_sources:
        (queries_dir / src.name).write_text(src.read_text())

    out_path = tmp_path / "coverage.jsonl"

    result = runner.invoke(
        app,
        [
            "run",
            "--schema",
            str(schema_path),
            "--queries",
            str(queries_dir / "*.sql"),
            "--json",
            str(out_path),
            "--require-substrait",
            "--disable-optimizer",
            "--debug",
        ],
        env=os.environ.copy(),
    )

    assert result.exit_code == 0, f"CLI invocation failed: {result.output}"
    assert out_path.exists(), "CLI did not emit coverage output"

    records = [json.loads(line) for line in out_path.read_text().splitlines() if line.strip()]
    by_id = {rec["id"]: rec for rec in records}

    def table_count(sql: str) -> int:
        """Count table references in normalized SQL."""
        node = sqlglot.parse_one(sql)
        return sum(1 for _ in node.find_all(exp.Table))

    expectation_templates = {
        47: {
                "tables": {"item", "store_sales", "date_dim", "store"},
                "min_table_refs": 4,
                "required_funcs": {"SUM", "AVG", "RANK"},
                "plan_checks": {"min_scans": 4, "min_joins": 5, "min_aggregates": 1},
            },
        63: {
                "tables": {"item", "store_sales", "date_dim", "store"},
                "min_table_refs": 4,
                "required_funcs": {"SUM", "AVG"},
                "plan_checks": {"min_scans": 4, "min_joins": 3, "min_aggregates": 1},
            },
        89: {
                "tables": {"item", "store_sales", "date_dim", "store"},
                "min_table_refs": 4,
                "required_funcs": {"SUM", "AVG"},
                "plan_checks": {"min_scans": 4, "min_joins": 3, "min_aggregates": 1},
            },
        }
    expectations = {
        f"{src.name}#1": expectation_templates[number] for number, src in query_sources
    }

    saw_plan = 0
    for qid, cfg in expectations.items():
        assert qid in by_id, f"Missing coverage entry for {qid}"
        entry = by_id[qid]

        norm_sql = entry["normalized_sql"]
        assert table_count(norm_sql) >= cfg["min_table_refs"], f"{qid}: expected >= {cfg['min_table_refs']} tables"

        # Check that all expected base tables appear in the normalized SQL.
        for table in cfg["tables"]:
            assert table in norm_sql.lower(), f"{qid}: missing table {table} in normalized SQL"

        funcs = entry.get("functions") or {}
        all_funcs = set(funcs.get("all") or [])
        for func in cfg["required_funcs"]:
            assert func in all_funcs, f"{qid}: expected function {func} in function list {all_funcs}"

        unsupported = set(entry.get("unsupported_features") or [])
        if entry["mode"] == "plan_typed":
            saw_plan += 1
            ops = entry.get("operators") or {}
            joins = int(ops.get("join", 0))
            scans = int(ops.get("scan", 0))
            aggs = int(ops.get("aggregate", 0))

            plan_checks = cfg["plan_checks"]
            assert scans >= plan_checks["min_scans"], f"{qid}: expected >= {plan_checks['min_scans']} scans"
            assert joins >= plan_checks["min_joins"], f"{qid}: expected >= {plan_checks['min_joins']} joins"
            assert aggs >= plan_checks["min_aggregates"], f"{qid}: expected >= {plan_checks['min_aggregates']} aggregates"

            join_types = entry.get("join_types") or {}
            inner_joins = int(join_types.get("JOIN_TYPE_INNER", 0))
            assert (
                inner_joins + int(join_types.get("JOIN_TYPE_LEFT", 0)) >= plan_checks["min_joins"]
            ), f"{qid}: expected join type counts to cover logical joins {join_types}"
        else:
            plan_checks = cfg["plan_checks"]
            ops = entry.get("operators") or {}
            joins = int(ops.get("join", 0))
            scans = int(ops.get("scan", 0))
            aggs = int(ops.get("aggregate", 0))
            assert scans >= plan_checks["min_scans"], f"{qid}: expected >= {plan_checks['min_scans']} scans (AST summary)"
            assert joins >= plan_checks["min_joins"], f"{qid}: expected >= {plan_checks['min_joins']} joins (AST summary)"
            assert aggs >= plan_checks["min_aggregates"], f"{qid}: expected >= {plan_checks['min_aggregates']} aggregates (AST summary)"

            join_types = entry.get("join_types") or {}
            inner_joins = int(join_types.get("JOIN_TYPE_INNER", 0))
            assert inner_joins >= plan_checks["min_joins"], f"{qid}: expected AST join types to reflect comma joins {join_types}"

            warnings = entry.get("warnings") or []
            errors = entry.get("errors") or []
            assert warnings or errors, f"{qid}: expected Substrait fallback to record a warning"
            all_msgs = warnings + errors
            window_hint = any("Not implemented" in msg or "NotImplementedException" in msg for msg in all_msgs)
            assert window_hint or ("window" in unsupported), f"{qid}: unexpected fallback diagnostics {all_msgs}"

    if saw_plan == 0:
        # All queries currently contain window functions which the Substrait exporter lacks.
        assert all(
            "window" in set((by_id[qid].get("unsupported_features") or []))
            or any(
                "Not implemented" in msg or "NotImplementedException" in msg
                for msg in ((by_id[qid].get("warnings") or []) + (by_id[qid].get("errors") or []))
            )
            for qid in expectations
        ), "Expected window-only unsupported cases when no typed plans are present"


@pytest.mark.parametrize(
    "query_number, expected_ops, expected_joins, expected_expr, expected_aggs, expected_windows",
    [
        (
            4,
            {
                "scan": 15,
                "join": 11,
                "filter": 4,
                "aggregate": 4,
                "sort": 1,
                "project": 34,
                "limit": 1,
                "set": 2,
                "cte": 1,
                "top_k": 1,
                "union_all": 2,
            },
            {"JOIN_TYPE_INNER": 11},
            Counter({"DIVIDE": 7, "ADD": 6, "SUB": 6}),
            Counter({"SUM": 3}),
            Counter(),
        ),
        (
            21,
            {
                "scan": 4,
                "join": 3,
                "filter": 2,
                "aggregate": 2,
                "sort": 1,
                "project": 5,
                "limit": 1,
                "top_k": 1,
            },
            {"JOIN_TYPE_INNER": 3},
            Counter({"DIVIDE": 3, "ADD": 1, "SUB": 1}),
            Counter({"SUM": 2}),
            Counter(),
        ),
        (
            72,
            {
                "scan": 11,
                "join": 10,
                "filter": 1,
                "aggregate": 1,
                "sort": 1,
                "project": 6,
                "limit": 1,
                "top_k": 1,
            },
            {"JOIN_TYPE_INNER": 8, "JOIN_TYPE_LEFT": 2},
            Counter({"ADD": 1}),
            Counter({"SUM": 2, "COUNT": 1}),
            Counter(),
        ),
    ],
)
def test_tpcds_handcount_validation(
    query_number: int,
    expected_ops: dict[str, int],
    expected_joins: dict[str, int],
    expected_expr: Counter,
    expected_aggs: Counter,
    expected_windows: Counter,
) -> None:
    """
    Validate operator and function extraction on additional TPC-DS queries by comparing against
    manually curated counts. We parse using the T-SQL dialect to accommodate TOP/N syntax.
    """
    query_path = resolve_query_path(query_number)
    query_name = query_path.name

    sql_text = query_path.read_text()
    ast = sqlglot.parse_one(sql_text, read="tsql")

    ops, joins = summarize_ast_operators(ast)
    assert ops == expected_ops, f"{query_name}: operator counts mismatch"
    assert joins == expected_joins, f"{query_name}: join type counts mismatch"

    funcs = extract_functions(ast)
    expr_counts = Counter(funcs["expression_ops"])
    agg_counts = Counter(funcs["agg_funcs"])
    window_counts = Counter(funcs["window_funcs"])

    assert expr_counts == expected_expr, f"{query_name}: expression operator counts mismatch"
    assert agg_counts == expected_aggs, f"{query_name}: aggregate function counts mismatch"
    assert window_counts == expected_windows, f"{query_name}: window function counts mismatch"
