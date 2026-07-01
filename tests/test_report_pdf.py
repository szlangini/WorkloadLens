from __future__ import annotations

import json
from pathlib import Path

import subprocess
import sys

import pytest
from typer.testing import CliRunner

from workloadlens.cli import app
from workloadlens.report import (
    aggregate_jsonl_paths,
    generate_focused_comparison_pdf,
    ReportOptions,
)
from workloadlens.report.pdf import _FigureExporter, _ensure_matplotlib, GROUPED_BAR_FIGSIZE
from workloadlens.datafiles import DATA_TABLE_RECORD


def _matplotlib_available() -> bool:
    try:
        subprocess.run(
            [sys.executable, "-c", "import matplotlib"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except Exception:  # pragma: no cover - environment dependent
        return False
    return True


MATPLOTLIB_OK = _matplotlib_available()


def _read_png_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as fh:
        signature = fh.read(8)
        assert signature == b"\x89PNG\r\n\x1a\n"
        _ = fh.read(4)  # chunk length
        chunk_type = fh.read(4)
        assert chunk_type == b"IHDR"
        width = int.from_bytes(fh.read(4), "big")
        height = int.from_bytes(fh.read(4), "big")
    return width, height


def _write_sample_coverage(json_path: Path) -> None:
    records = [
        {
            "id": "q1.sql#1",
            "mode": "plan_typed",
            "operators": {"scan": 1, "filter": 1, "project": 1},
            "join_types": {},
            "operator_types": {
                "scan": {"INT": 1},
                "filter": {"INT": 1},
                "project": {"INT": 1},
            },
            "functions": {"projection": [], "aggregate": [], "window": [], "expression": [], "all": []},
            "statement_type": "Select",
            "is_query": True,
            "operator_total": 3,
            "limit": {"has_limit": False, "value": None, "has_order_by": False},
        },
        {
            "id": "q2.sql#1",
            "mode": "ast_only",
            "operators": {"scan": 2, "join": 1, "project": 2},
            "join_types": {"JOIN_TYPE_INNER": 1},
            "operator_types": {
                "scan": {"INT": 2},
                "join": {"INT": 2},
                "project": {"INT": 2},
            },
            "functions": {
                "projection": ["SUM"],
                "aggregate": ["SUM"],
                "window": [],
                "expression": [],
                "all": ["SUM"],
            },
            "statement_type": "Select",
            "is_query": True,
            "operator_total": 5,
            "limit": {"has_limit": False, "value": None, "has_order_by": False},
        },
    ]

    with json_path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")


@pytest.mark.skipif(not MATPLOTLIB_OK, reason="matplotlib not available")
def test_pdf_report_generation(tmp_path: Path):
    json_path = tmp_path / "coverage.jsonl"
    pdf_path = tmp_path / "report.pdf"

    _write_sample_coverage(json_path)

    runner = CliRunner()
    result = runner.invoke(app, ["report", str(json_path), "--out", str(pdf_path), "--no-open"])

    assert result.exit_code == 0, result.output
    assert pdf_path.exists()
    assert pdf_path.stat().st_size > 0


@pytest.mark.skipif(not MATPLOTLIB_OK, reason="matplotlib not available")
def test_png_exports_use_stable_canvas_size(tmp_path: Path) -> None:
    plt, PdfPages = _ensure_matplotlib()
    pdf_path = tmp_path / "report.pdf"
    png_dir = tmp_path / "png"

    with PdfPages(str(pdf_path)) as pdf_pages:
        exporter = _FigureExporter(pdf_pages, png_dir, "report", png_dpi=200)
        for labels, title in [
            (["A", "B"], "Short labels"),
            (["Very long category label one", "Very long category label two"], "Long labels"),
        ]:
            fig, ax = plt.subplots(figsize=GROUPED_BAR_FIGSIZE)
            ax.bar([0, 1], [10, 20])
            ax.set_xticks([0, 1])
            ax.set_xticklabels(labels, rotation=45, ha="right")
            ax.set_title(title)
            exporter.savefig(fig, bbox_inches="tight")
            plt.close(fig)

    exported = sorted(png_dir.glob("*.png"))
    assert len(exported) == 2

    dims = [_read_png_dimensions(path) for path in exported]
    assert dims[0] == dims[1]
    assert dims[0] == (1700, 1240)


@pytest.mark.skipif(not MATPLOTLIB_OK, reason="matplotlib not available")
def test_pdf_report_generation_with_data(tmp_path: Path):
    json_path = tmp_path / "coverage.jsonl"
    data_path = tmp_path / "data.jsonl"
    pdf_path = tmp_path / "report_data.pdf"

    _write_sample_coverage(json_path)
    data_records = [
        {
            "record_type": DATA_TABLE_RECORD,
            "table": "lineitem",
            "file": "lineitem.tbl",
            "format": "tbl",
            "size_bytes": 1024,
            "row_count": 3,
        }
    ]
    data_path.write_text("\n".join(json.dumps(record) for record in data_records), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["report", "--sql", str(json_path), "--data", str(data_path), "--out", str(pdf_path), "--no-open"],
    )

    assert result.exit_code == 0, result.output
    assert pdf_path.exists()
    assert pdf_path.stat().st_size > 0


@pytest.mark.skipif(not MATPLOTLIB_OK, reason="matplotlib not available")
def test_focused_comparison_pdf_generation(tmp_path: Path) -> None:
    coverage_a = tmp_path / "a.jsonl"
    coverage_b = tmp_path / "b.jsonl"
    pdf_path = tmp_path / "focused_compare.pdf"

    records_a = [
        {
            "id": "a.sql#1",
            "mode": "ast_only",
            "operators": {"scan": 1, "sort": 1, "limit": 1, "union_all": 1},
            "join_types": {},
            "functions": {"projection": [], "aggregate": [], "window": [], "expression": [], "all": []},
            "statement_type": "Select",
            "is_query": True,
            "operator_total": 4,
            "limit_with_order_values": [10],
            "limit_without_order_values": [100],
            "group_by_key_counts": [2],
            "union_all_fan_in_values": [3],
        }
    ]
    records_b = [
        {
            "id": "b.sql#1",
            "mode": "ast_only",
            "operators": {"scan": 1, "limit": 1},
            "join_types": {},
            "functions": {"projection": [], "aggregate": [], "window": [], "expression": [], "all": []},
            "statement_type": "Select",
            "is_query": True,
            "operator_total": 2,
            "limit_with_order_values": [0],
            "limit_without_order_values": [50],
            "group_by_key_counts": [1],
            "union_all_fan_in_values": [2],
        }
    ]
    coverage_a.write_text("\n".join(json.dumps(rec) for rec in records_a), encoding="utf-8")
    coverage_b.write_text("\n".join(json.dumps(rec) for rec in records_b), encoding="utf-8")

    reports = {
        "A": aggregate_jsonl_paths([coverage_a]),
        "B": aggregate_jsonl_paths([coverage_b]),
    }
    generate_focused_comparison_pdf(reports, pdf_path, options=ReportOptions(scope="queries"))

    assert pdf_path.exists()
    assert pdf_path.stat().st_size > 0


def test_aggregate_collects_join_and_union_metrics(tmp_path: Path) -> None:
    json_path = tmp_path / "metrics.jsonl"
    records = [
        {
            "id": "q1.sql#1",
            "mode": "plan_typed",
            "operators": {"scan": 1, "join": 2, "union_all": 1},
            "join_types": {"JOIN_TYPE_INNER": 2},
            "statement_type": "Select",
            "is_query": True,
            "group_by_key_count": 3,
        },
        {
            "id": "q2.sql#1",
            "mode": "ast_only",
            "operators": {"scan": 1},
            "join_types": {},
            "statement_type": "Select",
            "is_query": True,
            "group_by_key_count": 0,
        },
    ]
    json_path.write_text("\n".join(json.dumps(rec) for rec in records), encoding="utf-8")

    bundle = aggregate_jsonl_paths([json_path])
    stats = bundle.query_statements

    assert stats.statement_types["Select"] == 2
    assert stats.joins_per_query.count(2) == 1
    assert stats.joins_per_query.count(0) == 1
    assert stats.union_all_totals.count(1) == 1
    assert stats.union_all_totals.count(0) == 1
    assert stats.group_by_key_counts == [3, 0]


def test_aggregate_group_by_counts_list_support(tmp_path: Path) -> None:
    json_path = tmp_path / "metrics_list.jsonl"
    records = [
        {
            "id": "q3.sql#1",
            "mode": "ast_only",
            "operators": {"scan": 1},
            "join_types": {},
            "statement_type": "Select",
            "is_query": True,
            "group_by_key_counts": [2, 4],
            "group_by_key_count": 4,
        },
        {
            "id": "q4.sql#1",
            "mode": "ast_only",
            "operators": {"scan": 1},
            "join_types": {},
            "statement_type": "Select",
            "is_query": True,
            "group_by_key_counts": [0],
            "group_by_key_count": 0,
        },
    ]
    json_path.write_text("\n".join(json.dumps(rec) for rec in records), encoding="utf-8")

    bundle = aggregate_jsonl_paths([json_path])
    stats = bundle.query_statements

    assert stats.group_by_key_counts == [2, 4, 0]


def test_aggregate_includes_data_profile(tmp_path: Path) -> None:
    json_path = tmp_path / "coverage.jsonl"
    data_path = tmp_path / "data.jsonl"
    _write_sample_coverage(json_path)
    data_record = {
        "record_type": DATA_TABLE_RECORD,
        "table": "orders",
        "file": "orders.tbl",
        "format": "tbl",
        "size_bytes": 2048,
        "row_count": 10,
    }
    data_path.write_text(json.dumps(data_record), encoding="utf-8")

    bundle = aggregate_jsonl_paths([json_path], [data_path])
    assert bundle.data_profile is not None
    assert bundle.data_profile.table_count == 1
    assert bundle.data_profile.total_bytes == 2048
