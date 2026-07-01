from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from typer.testing import CliRunner

from workloadlens.cli import app
from workloadlens.datafiles import ColumnHistogramMetric, ColumnMCVMetric, DataProfile, DataTableMetric
from workloadlens.report import (
    AggregatedReport,
    AggregatedStats,
    aggregated_report_from_dict,
    aggregated_report_to_dict,
)
from workloadlens.report.aggregate import SchemaProfile


def _sample_report() -> AggregatedReport:
    all_stats = AggregatedStats(
        total_queries=2,
        operator_counts=Counter({"scan": 3, "project": 2}),
        join_counts=Counter({"JOIN_TYPE_INNER": 1}),
        sql_lengths=[12, 24],
        operator_totals=[4, 6],
    )
    all_stats.limit_with_order = [10]
    query_stats = AggregatedStats(
        total_queries=1,
        operator_counts=Counter({"scan": 2}),
        join_counts=Counter({"JOIN_TYPE_INNER": 1}),
        sql_lengths=[12],
        operator_totals=[4],
    )
    data_profile = DataProfile(
        tables=[DataTableMetric(table="t1", file="/tmp/t1", format="tbl", size_bytes=10, row_count=1)],
        columns=[
            ColumnMCVMetric(
                table="t1",
                column="c1",
                file="/tmp/t1",
                row_count=1,
                null_count=0,
                topk_sum=1,
                max_count=1,
                distinct_count=1,
                reported_k=1,
                requested_k=1,
                mean_run_length=1.0,
                is_sorted=True,
                string_avg_length=None,
                string_max_length=None,
                string_p95_length=None,
                numeric_outlier_rate=None,
                sample_fraction=None,
            )
        ],
        histograms=[
            ColumnHistogramMetric(
                table="t1",
                column="c1",
                column_type="INT",
                bucket_count=1,
                mean_q_error=1.0,
                max_q_error=1.0,
                non_null_rows=1,
                ndv=1,
            )
        ],
    )
    schema_profile = SchemaProfile(
        table_count=1,
        total_columns=1,
        column_type_counts=Counter({"INT": 1}),
        tables_with_primary_keys=1,
        total_primary_key_columns=1,
        primary_key_type_counts=Counter({"INT": 1}),
        tables_with_foreign_keys=0,
        total_foreign_key_columns=0,
        foreign_key_type_counts=Counter(),
    )
    return AggregatedReport(all_stats, query_stats, {"operator_source": "AST"}, data_profile, schema_profile)


def test_aggregated_round_trip() -> None:
    report = _sample_report()
    payload = aggregated_report_to_dict(report)
    rebuilt = aggregated_report_from_dict(payload)

    assert rebuilt.metadata == report.metadata
    assert rebuilt.all_statements.total_queries == report.all_statements.total_queries
    assert rebuilt.all_statements.operator_counts == report.all_statements.operator_counts
    assert rebuilt.all_statements.join_counts == report.all_statements.join_counts
    assert rebuilt.all_statements.sql_lengths == report.all_statements.sql_lengths
    assert rebuilt.query_statements.operator_totals == report.query_statements.operator_totals

    assert rebuilt.data_profile
    assert rebuilt.data_profile.table_count == report.data_profile.table_count
    assert rebuilt.data_profile.column_count == report.data_profile.column_count
    assert rebuilt.data_profile.histogram_count == report.data_profile.histogram_count

    assert rebuilt.schema_profile
    assert rebuilt.schema_profile.table_count == report.schema_profile.table_count
    assert rebuilt.schema_profile.column_type_counts == report.schema_profile.column_type_counts
    assert rebuilt.schema_profile.primary_key_type_counts == report.schema_profile.primary_key_type_counts


def test_compare_accepts_aggregated_input(tmp_path: Path) -> None:
    report = _sample_report()
    agg_path = tmp_path / "agg.json"
    out_pdf = tmp_path / "out.pdf"
    out_json = tmp_path / "out.json"
    agg_payload = {"agg": aggregated_report_to_dict(report)}
    agg_path.write_text(json.dumps(agg_payload), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "compare",
            "--aggregated-in",
            str(agg_path),
            "--out",
            str(out_pdf),
            "--json-out",
            str(out_json),
            "--no-open",
        ],
    )
    assert result.exit_code == 0, result.output
    assert out_pdf.exists() and out_pdf.stat().st_size > 0

    written = json.loads(out_json.read_text(encoding="utf-8"))
    assert "agg" in written
    assert written["agg"]["metadata"].get("operator_source") == "AST"


def test_report_json_out(tmp_path: Path) -> None:
    coverage_path = tmp_path / "coverage.jsonl"
    data_path = tmp_path / "data.jsonl"
    out_pdf = tmp_path / "out.pdf"
    out_json = tmp_path / "out.json"

    coverage_record = {
        "id": "q1.sql#1",
        "file": "q1.sql",
        "mode": "ast_only",
        "is_query": True,
        "operators": {"scan": 1, "project": 1},
    }
    coverage_path.write_text(json.dumps(coverage_record) + "\n", encoding="utf-8")
    data_record = {
        "record_type": "data_table_stats",
        "table": "t1",
        "file": "t1.tbl",
        "format": "tbl",
        "size_bytes": 10,
        "row_count": 1,
    }
    data_path.write_text(json.dumps(data_record) + "\n", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "report",
            "--sql",
            str(coverage_path),
            "--data",
            str(data_path),
            "--out",
            str(out_pdf),
            "--json-out",
            str(out_json),
            "--no-open",
        ],
    )
    assert result.exit_code == 0, result.output
    assert out_pdf.exists() and out_pdf.stat().st_size > 0

    payload = json.loads(out_json.read_text(encoding="utf-8"))
    assert payload.get("metadata") is not None
    assert payload.get("data_profile") is not None
