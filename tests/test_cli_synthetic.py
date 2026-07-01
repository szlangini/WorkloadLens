# tests/test_cli_synthetic.py
from __future__ import annotations
import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from workloadlens.cli import app
from tests.utils_substrait import has_substrait_support


@pytest.mark.skipif(
    not has_substrait_support(),
    reason="Substrait extension not available (set SQLCOV_SUBSTRAIT_PATH or allow INSTALL substrait FROM community)",
)
def test_cli_synthetic_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """
    End-to-end CLI test with 3 synthetic queries and a small schema.

    We assert the *expected logical operators* appear in the Substrait plan:
      - Q1: read + filter + project
      - Q2: read + aggregate (+ having→ filter) (+ project)
      - Q3: 2x read + join (INNER) + filter (+ project)

    NOTE:
      Engines may insert additional projection nodes; therefore we assert
      LOWER BOUNDS for operator counts (>=), and exact counts where stable
      (e.g., reads in a simple 2-table join).
    """
    runner = CliRunner()

    # Ensure the CLI can find/load the local extension
    ext_path = os.environ.get("SQLCOV_SUBSTRAIT_PATH")
    monkeypatch.setenv("SQLCOV_SUBSTRAIT_PATH", ext_path or "")

    # ------------------------------------------------------------------ #
    # Prepare schema and queries
    # ------------------------------------------------------------------ #
    schema_sql = """
    CREATE TABLE t (id INT, a INT, b INT, x INT);
    CREATE TABLE u (id INT, y INT);
    """
    # Q1: simple projection + filter + read
    q1 = "SELECT a + 1 AS a1 FROM t WHERE b > 10;"
    # Q2: aggregation with GROUP BY and HAVING; add expression to force a top project
    q2 = "SELECT b, SUM(a) + 1 AS s1 FROM t GROUP BY b HAVING SUM(a) > 0;"
    # Q3: inner join with predicate and post-join filter
    q3 = "SELECT * FROM t JOIN u ON t.id = u.id WHERE t.x > 0;"

    schema_path = tmp_path / "schema.sql"
    schema_path.write_text(schema_sql)

    queries_dir = tmp_path / "queries"
    queries_dir.mkdir()
    (queries_dir / "q1.sql").write_text(q1)
    (queries_dir / "q2.sql").write_text(q2)
    (queries_dir / "q3.sql").write_text(q3)

    out_path = tmp_path / "out.jsonl"

    plans_dir = tmp_path / "plans"

    # ------------------------------------------------------------------ #
    # Run the CLI (require Substrait so we fail fast if unavailable)
    # ------------------------------------------------------------------ #
    result = runner.invoke(
        app,
        [
            "run",
            "--schema", str(schema_path),
            "--queries", str(queries_dir / "*.sql"),
            "--json", str(out_path),
            "--require-substrait",
            "--debug",
            "--disable-optimizer",
            "--dump-plans", str(plans_dir),  # optional but helpful
        ],
        env=os.environ.copy(),
    )

    assert result.exit_code == 0, f"CLI failed: {result.output}"

    # ------------------------------------------------------------------ #
    # Parse JSONL and index by query id
    # ------------------------------------------------------------------ #
    assert out_path.exists(), "coverage jsonl not written"
    records = [json.loads(line) for line in out_path.read_text().splitlines()]
    by_id = {rec["id"]: rec for rec in records}

    # Helper for safe operator count lookup
    def op(rec, name: str) -> int:
        return int(rec.get("operators", {}).get(name, 0))

    # ------------------------------------------------------------------ #
    # Q1 assertions
    # ------------------------------------------------------------------ #
    r1 = by_id["q1.sql#1"]
    if int(r1.get("operators", {}).get("filter", 0)) == 0:
        # help debugging: show normalized SQL and plan path
        print("Q1 normalized_sql:", r1.get("normalized_sql"))
        plan_path = plans_dir / "q1.sql#1.json"
        if plan_path.exists():
            print("Q1 plan file:", plan_path)
            print(plan_path.read_text()[:2000])  # first 2KB
    assert r1["mode"] == "plan_typed"
    assert op(r1, "scan") == 1
    assert op(r1, "filter") >= 1
    assert op(r1, "project") >= 1
    assert r1.get("join_types") in ({}, None)
    types1 = r1.get("operator_types") or {}
    assert int(types1.get("filter", {}).get("INT", 0)) >= 1
    shares1 = r1.get("operator_type_shares") or {}
    assert abs(shares1.get("filter", {}).get("INT", 0.0) - 1.0) < 1e-9
    op_shares1 = r1.get("operator_shares") or {}
    assert abs(op_shares1.get("scan", 0.0) - (1 / 3)) < 1e-9
    assert abs(op_shares1.get("filter", 0.0) - (1 / 3)) < 1e-9
    assert abs(op_shares1.get("project", 0.0) - (1 / 3)) < 1e-9
    features1 = r1.get("features") or {}
    assert features1.get("has_order_by") is False
    assert features1.get("has_limit") is False

    # ------------------------------------------------------------------ #
    # Q2 assertions
    # ------------------------------------------------------------------ #
    r2 = by_id["q2.sql#1"]
    assert r2["mode"] == "plan_typed"
    assert op(r2, "scan") == 1
    assert op(r2, "aggregate") >= 1
    # HAVING is typically represented as a filter above the aggregate
    assert op(r2, "filter") >= 1
    # Engines often place a project above the aggregate to compute derived cols
    assert op(r2, "project") >= 1
    types2 = r2.get("operator_types") or {}
    assert int(types2.get("aggregate", {}).get("INT", 0)) >= 1
    aggregate_result_types = types2.get("aggregate_result", {})
    assert any(
        int(aggregate_result_types.get(label, 0)) >= 1 for label in ("INT", "DECIMAL", "DOUBLE")
    )
    shares2 = r2.get("operator_type_shares") or {}
    assert abs(shares2.get("scan", {}).get("INT", 0.0) - 1.0) < 1e-9
    filt_shares = shares2.get("filter", {})
    assert filt_shares.get("INT", 0.0) + filt_shares.get("DECIMAL", 0.0) > 0.0
    op_shares2 = r2.get("operator_shares") or {}
    for op_name in ("scan", "aggregate", "filter", "project"):
        assert abs(op_shares2.get(op_name, 0.0) - 0.25) < 1e-9
    features2 = r2.get("features") or {}
    assert features2.get("has_group_by")
    assert features2.get("has_limit") is False

    # ------------------------------------------------------------------ #
    # Q3 assertions
    # ------------------------------------------------------------------ #
    r3 = by_id["q3.sql#1"]
    assert r3["mode"] == "plan_typed"
    # Two base tables
    assert op(r3, "scan") >= 2
    # Exactly one logical join
    assert op(r3, "join") >= 1
    # Count INNER join occurrence
    assert int(r3.get("join_types", {}).get("JOIN_TYPE_INNER", 0)) >= 1
    # Post-join filter
    assert op(r3, "filter") >= 1
    # Top-level projection is common even for SELECT *
    assert op(r3, "project") >= 1
    types3 = r3.get("operator_types") or {}
    assert int(types3.get("join", {}).get("INT", 0)) >= 1
    shares3 = r3.get("operator_type_shares") or {}
    assert abs(shares3.get("join", {}).get("INT", 0.0) - 1.0) < 1e-9
    op_shares3 = r3.get("operator_shares") or {}
    assert abs(op_shares3.get("scan", 0.0) - (2 / 6)) < 1e-9
    assert abs(op_shares3.get("project", 0.0) - (2 / 6)) < 1e-9
    assert abs(op_shares3.get("join", 0.0) - (1 / 6)) < 1e-9
    assert abs(op_shares3.get("filter", 0.0) - (1 / 6)) < 1e-9
    features3 = r3.get("features") or {}
    assert features3.get("has_join")
    assert features3.get("has_limit") is False
