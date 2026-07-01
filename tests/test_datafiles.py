from __future__ import annotations

import json
from pathlib import Path
import importlib.util

import pytest
from typer.testing import CliRunner

from workloadlens.cli import app
from workloadlens.datafiles import (
    DATA_TABLE_RECORD,
    load_data_profile,
    scan_data_directory,
    compute_column_mcv_metrics,
)
from workloadlens.utils.schema import parse_schema_tables

DUCKDB_AVAILABLE = importlib.util.find_spec("duckdb") is not None


def _make_file(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines)
    if lines:
        text += "\n"
    path.write_text(text, encoding="utf-8")


def test_scan_data_directory_collects_sizes_and_rows(tmp_path) -> None:
    customer = tmp_path / "customer.tbl"
    orders = tmp_path / "orders.csv"
    nested = tmp_path / "nested" / "lineitem.tbl"
    store_sales = tmp_path / "store_sales.dat"

    _make_file(customer, ["1|a", "2|b", ""])
    _make_file(orders, ["id,value", "1,foo", "2,bar"])
    _make_file(nested, ["col"])
    _make_file(store_sales, ["1|x", "2|y"])

    metrics = scan_data_directory(tmp_path)

    names = [entry.table for entry in metrics]
    assert names == ["customer", "lineitem", "orders", "store_sales"]
    customer_entry = next(entry for entry in metrics if entry.table == "customer")
    assert customer_entry.size_bytes > 0
    assert customer_entry.row_count == 3
    assert customer_entry.bytes_per_row is not None


def test_scan_data_directory_aggregates_sharded_tables(tmp_path) -> None:
    shard_one = tmp_path / "lineitem.tbl.1"
    shard_two = tmp_path / "lineitem.tbl.2"
    nation = tmp_path / "nation.tbl"

    _make_file(shard_one, ["1|x", "2|y"])
    _make_file(shard_two, ["3|z"])
    _make_file(nation, ["1|UNITED STATES"])

    metrics = scan_data_directory(tmp_path)
    names = [entry.table for entry in metrics]
    assert names == ["lineitem", "nation"]

    lineitem = next(entry for entry in metrics if entry.table == "lineitem")
    assert lineitem.row_count == 3
    assert lineitem.size_bytes == shard_one.stat().st_size + shard_two.stat().st_size
    assert lineitem.file.endswith("lineitem.tbl.*")


@pytest.mark.skipif(not DUCKDB_AVAILABLE, reason="duckdb not installed")
def test_cli_data_command_writes_jsonl(tmp_path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    file_path = data_dir / "partsupp.tbl"
    _make_file(file_path, ["1|a", "2|b", "3|c"])

    out_path = tmp_path / "metrics.jsonl"
    runner = CliRunner()
    result = runner.invoke(app, ["data", str(data_dir), "--out", str(out_path), "--no-rows"])

    assert result.exit_code == 0, result.output
    assert out_path.exists()

    lines = [json.loads(line) for line in out_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert lines[0]["record_type"] == DATA_TABLE_RECORD
    assert lines[0]["table"] == "partsupp"
    assert lines[0]["row_count"] is None


def test_load_data_profile_reads_jsonl(tmp_path) -> None:
    json_path = tmp_path / "metrics.jsonl"
    records = [
        {
            "record_type": DATA_TABLE_RECORD,
            "table": "catalog_sales",
            "file": "catalog_sales.tbl",
            "format": "tbl",
            "size_bytes": 10,
            "row_count": 2,
        },
        {
            "record_type": DATA_TABLE_RECORD,
            "table": "web_sales",
            "file": "web_sales.tbl",
            "format": "tbl",
            "size_bytes": 20,
            "row_count": None,
        },
        {
            "record_type": "data_column_stats",
            "table": "catalog_sales",
            "column": "cs_item_sk",
            "file": "catalog_sales.tbl",
            "row_count": 10,
            "null_count": 2,
            "topk_sum": 6,
            "max_count": 4,
            "distinct_count": 3,
            "reported_k": 2,
            "requested_k": 3,
            "mean_run_length": 1.5,
            "is_sorted": True,
            "numeric_outlier_rate": 0.1,
            "sample_fraction": 0.3,
        },
        {
            "record_type": "data_histogram_stats",
            "table": "catalog_sales",
            "column": "cs_item_sk",
            "column_type": "INT",
            "bucket_count": 5,
            "mean_q_error": 1.5,
            "max_q_error": 2.0,
            "non_null_rows": 8,
            "ndv": 3,
        },
    ]
    json_path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")

    profile = load_data_profile([json_path])
    assert profile.table_count == 2
    assert profile.total_bytes == 30
    assert profile.row_counts() == [2]
    assert profile.column_count == 1
    assert profile.topk_fractions() == [0.75]
    assert profile.max_mcv_fractions() == [0.5]
    assert profile.null_fractions() == [0.2]
    assert profile.ndv_fractions() == [3 / 8]
    assert profile.ndv_ratios(denominator="rows") == [pytest.approx(0.3)]
    assert profile.histogram_count == 1
    assert profile.histogram_q_errors() == [1.5]
    assert profile.bytes_per_row_values() == [5.0]
    assert profile.run_length_means() == [1.5]
    assert profile.sorted_column_count() == 1
    assert profile.numeric_outlier_rates() == [0.1]
    assert profile.columns[0].sample_fraction == pytest.approx(0.3)


@pytest.mark.skipif(not DUCKDB_AVAILABLE, reason="duckdb not installed")
def test_compute_column_mcv_metrics(tmp_path) -> None:


    data_dir = tmp_path / "data"
    data_dir.mkdir()
    table_path = data_dir / "sample.tbl"
    _make_file(table_path, ["1|alpha", "2|alpha", "3|beta", "4|"])

    schema_sql = """
    CREATE TABLE sample (
        id INT,
        category TEXT
    );
    """
    tables = parse_schema_tables(schema_sql)
    metrics, histogram_metrics = compute_column_mcv_metrics([table_path], tables, requested_k=2)
    assert len(metrics) == 2
    id_metric = next(metric for metric in metrics if metric.column == "id")
    assert id_metric.row_count == 4
    assert id_metric.topk_sum == 2
    assert id_metric.distinct_count == 4
    assert id_metric.is_sorted is True
    assert id_metric.mean_run_length == pytest.approx(1.0)
    assert id_metric.numeric_outlier_rate == pytest.approx(0.0)
    category_metric = next(metric for metric in metrics if metric.column == "category")
    assert category_metric.null_count == 1
    assert category_metric.topk_sum == 3
    assert category_metric.max_count == 2
    assert category_metric.reported_k == 2
    assert category_metric.distinct_count == 2
    assert category_metric.mean_run_length == pytest.approx(4 / 3)
    assert category_metric.is_sorted is True
    assert category_metric.string_avg_length == pytest.approx(14 / 3)
    assert category_metric.string_max_length == pytest.approx(5.0)
    assert category_metric.string_p95_length == pytest.approx(5.0)
    assert category_metric.sample_fraction is None
    assert len(histogram_metrics) == 0


@pytest.mark.skipif(not DUCKDB_AVAILABLE, reason="duckdb not installed")
def test_compute_column_mcv_metrics_with_sharded_input(tmp_path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    shard_one = data_dir / "sample.tbl.1"
    shard_two = data_dir / "sample.tbl.2"
    _make_file(shard_one, ["1|alpha", "2|alpha"])
    _make_file(shard_two, ["3|beta", "4|"])

    schema_sql = """
    CREATE TABLE sample (
        id INT,
        category TEXT
    );
    """
    tables = parse_schema_tables(schema_sql)
    metrics, _ = compute_column_mcv_metrics([shard_one, shard_two], tables, requested_k=2)

    assert len(metrics) == 2
    id_metric = next(metric for metric in metrics if metric.column == "id")
    category_metric = next(metric for metric in metrics if metric.column == "category")

    assert id_metric.row_count == 4
    assert id_metric.distinct_count == 4
    assert category_metric.null_count == 1
    assert category_metric.topk_sum == 3
    assert category_metric.file.endswith("sample.tbl.*")


@pytest.mark.skipif(not DUCKDB_AVAILABLE, reason="duckdb not installed")
def test_compute_column_mcv_metrics_with_sampling(tmp_path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    table_path = data_dir / "sample.tbl"
    _make_file(table_path, [f"{idx}|value{idx}" for idx in range(1, 101)])

    schema_sql = """
    CREATE TABLE sample (
        id INT,
        val TEXT
    );
    """
    tables = parse_schema_tables(schema_sql)
    sample_fraction = 0.5
    metrics, _ = compute_column_mcv_metrics(
        [table_path], tables, requested_k=2, sample_fraction=sample_fraction
    )
    assert any(metric.sample_fraction == pytest.approx(sample_fraction) for metric in metrics)
