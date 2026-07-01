from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from workloadlens.cli import app
from workloadlens.config import ProjectConfig, save_config

DUCKDB_AVAILABLE = importlib.util.find_spec("duckdb") is not None


def _write_file(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines)
    if lines:
        text += "\n"
    path.write_text(text, encoding="utf-8")


@pytest.mark.skipif(not DUCKDB_AVAILABLE, reason="duckdb not installed")
def test_pipeline_end_to_end(tmp_path: Path) -> None:
    """
    Full pipeline smoke test (run + data + report) on a tiny workload.

    Validates that coverage JSONL, data metrics, and the PDF are all produced
    without requiring Substrait (AST-only fallback).
    """

    schema_sql = """
    CREATE TABLE orders (
        order_id INT,
        customer_id INT,
        total_amount INT
    );

    CREATE TABLE lineitem (
        order_id INT,
        sku INT,
        qty INT
    );
    """
    schema_path = tmp_path / "schema.sql"
    schema_path.write_text(schema_sql, encoding="utf-8")

    queries_dir = tmp_path / "queries"
    queries_dir.mkdir()
    (queries_dir / "q1.sql").write_text(
        "SELECT customer_id, SUM(total_amount) AS spend FROM orders GROUP BY customer_id;",
        encoding="utf-8",
    )
    (queries_dir / "q2.sql").write_text(
        """
        SELECT o.order_id, SUM(li.qty) AS total_qty
        FROM orders o
        JOIN lineitem li ON o.order_id = li.order_id
        WHERE o.total_amount > 50
        GROUP BY o.order_id;
        """,
        encoding="utf-8",
    )

    data_dir = tmp_path / "data"
    _write_file(
        data_dir / "orders.tbl",
        [
            "1|10|120|",
            "2|20|80|",
            "3|30|40|",
        ],
    )
    _write_file(
        data_dir / "lineitem.tbl",
        [
            "1|100|5|",
            "1|200|3|",
            "2|300|7|",
        ],
    )

    config = ProjectConfig(
        schema=str(schema_path),
        queries=str(queries_dir / "*.sql"),
        data_dir=str(data_dir),
        output_dir=str(tmp_path / "outputs"),
        scope="queries",
    )
    config_path = tmp_path / "workloadlens.json"
    save_config(config, config_path)

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["pipeline", "--config", str(config_path), "--no-open", "--json-out", str(tmp_path / "agg.json")],
        env=os.environ.copy(),
    )
    assert result.exit_code == 0, f"pipeline failed: {result.output}"

    coverage_path = config.resolve_coverage_json(config_path)
    data_metrics_path = config.resolve_data_json(config_path)
    pdf_path = config.resolve_report_pdf(config_path)

    assert coverage_path.exists()
    records = [json.loads(line) for line in coverage_path.read_text(encoding="utf-8").splitlines()]
    ids = {rec["id"] for rec in records if rec.get("is_query")}
    assert ids == {"q1.sql#1", "q2.sql#1"}

    assert data_metrics_path.exists()
    metric_lines = [json.loads(line) for line in data_metrics_path.read_text(encoding="utf-8").splitlines()]
    record_types = {entry.get("record_type") for entry in metric_lines}
    assert record_types.issuperset({"data_table_stats", "data_column_stats"})

    agg_json = tmp_path / "agg.json"
    assert agg_json.exists()
    agg_payload = json.loads(agg_json.read_text(encoding="utf-8"))
    assert isinstance(agg_payload, dict)
    assert agg_payload.get("metadata") is not None

    # The report command appends a timestamp to the PDF filename, so we
    # glob for any file matching the stem pattern in the reports directory.
    pdf_candidates = list(pdf_path.parent.glob(f"{pdf_path.stem}*.pdf"))
    assert pdf_candidates, f"No PDF found matching {pdf_path.stem}*.pdf in {pdf_path.parent}"
    assert pdf_candidates[0].stat().st_size > 0
