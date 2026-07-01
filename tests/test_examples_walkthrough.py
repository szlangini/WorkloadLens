from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from workloadlens.cli import app
from tests.utils_substrait import has_substrait_support


def test_end_to_end_walkthrough(tmp_path: Path) -> None:
    """Readable walkthrough that exercises key reporting fields on a small workload."""

    if not has_substrait_support():
        pytest.skip("Substrait extension not available")

    schema_sql = """
    CREATE TABLE sales (
        order_id INTEGER,
        customer_id INTEGER,
        store_id INTEGER,
        sales_date DATE,
        amount DECIMAL(12, 2)
    );

    CREATE TABLE stores (
        store_id INTEGER,
        region TEXT
    );

    CREATE TABLE customers (
        customer_id INTEGER,
        vip BOOLEAN,
        state TEXT
    );
    """

    queries = {
        "q1_simple.sql": "SELECT amount FROM sales WHERE amount > 100;",
        "q2_aggregate.sql": """
            SELECT sales_date, SUM(amount) AS total
            FROM sales
            GROUP BY sales_date
            HAVING SUM(amount) > 500;
        """,
        "q3_join.sql": """
            SELECT s.region, SUM(sales.amount) AS regional_total
            FROM sales
            JOIN stores s ON sales.store_id = s.store_id
            GROUP BY s.region
            ORDER BY regional_total DESC
            LIMIT 10;
        """,
        "q4_window_union.sql": """
            WITH vip_sales AS (
                SELECT customer_id, amount
                FROM sales
                JOIN customers c ON sales.customer_id = c.customer_id
                WHERE c.vip
            )
            SELECT customer_id, amount, RANK() OVER (ORDER BY amount DESC) AS rnk
            FROM vip_sales
            UNION ALL
            SELECT customer_id, amount, 0 AS rnk
            FROM sales
            WHERE amount > 1000;
        """,
        "q5_complex.sql": """
            WITH large_orders AS (
                SELECT order_id
                FROM sales
                WHERE amount > 750
            )
            SELECT COUNT(*)
            FROM customers c
            WHERE EXISTS (
                SELECT 1
                FROM large_orders lo
                JOIN sales s ON lo.order_id = s.order_id
                WHERE s.customer_id = c.customer_id
            );
        """,
    }

    runner = CliRunner()
    schema_path = tmp_path / "schema.sql"
    schema_path.write_text(schema_sql)

    queries_dir = tmp_path / "queries"
    queries_dir.mkdir()
    for name, sql in queries.items():
        (queries_dir / name).write_text(sql.strip())

    out_path = tmp_path / "results.jsonl"
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
        ],
    )
    assert result.exit_code == 0, result.output

    records = [json.loads(line) for line in out_path.read_text().splitlines()]
    by_id = {rec["id"]: rec for rec in records}

    simple = by_id["q1_simple.sql#1"]
    assert simple["statement_type"] == "Select"
    assert simple["operators"]["scan"] == 1
    assert simple["features"].get("has_limit") is False

    aggregate = by_id["q2_aggregate.sql#1"]
    assert aggregate["statement_type"] == "Select"
    assert aggregate["operators"].get("aggregate", 0) >= 1
    assert aggregate["features"].get("has_group_by")
    assert aggregate["features"].get("has_order_by") is False

    join = by_id["q3_join.sql#1"]
    assert join["statement_type"] == "Select"
    assert join["features"].get("has_join")
    assert join["features"].get("has_order_by")
    assert join["features"].get("has_limit")

    window_union = by_id["q4_window_union.sql#1"]
    assert window_union["statement_type"] == "Select"
    assert window_union["features"].get("has_window")
    assert window_union["features"].get("has_union")
    assert window_union["operators"].get("cte", 0) == 1
    assert window_union["operators"].get("union_all", 0) == 1
    assert window_union["operators"].get("window", 0) >= 1

    complex_q = by_id["q5_complex.sql#1"]
    assert complex_q["statement_type"] == "Select"
    assert complex_q["features"].get("has_cte")
    assert complex_q["features"].get("has_join")

    totals = [
        rec["operator_total"]
        for rec in records
        if rec.get("mode") != "schema"
    ]
    assert all(total > 0 for total in totals)
    lengths = [rec["sql_length_bytes"] for rec in records]
    assert all(length > 0 for length in lengths)
