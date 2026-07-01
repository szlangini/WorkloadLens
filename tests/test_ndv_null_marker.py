from __future__ import annotations

from pathlib import Path
import importlib.util

import pytest

from workloadlens.datafiles import compute_column_mcv_metrics
from workloadlens.utils.schema import parse_schema_tables


@pytest.mark.skipif(importlib.util.find_spec("duckdb") is None, reason="duckdb not installed")
def test_null_marker_as_null(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    table_path = data_dir / "sample.tbl"
    table_path.write_text("\\N|x\nx|y\n", encoding="utf-8")

    schema_sql = """
    CREATE TABLE sample (
        col1 TEXT,
        col2 TEXT
    );
    """
    schema_tables = parse_schema_tables(schema_sql)
    metrics, _ = compute_column_mcv_metrics(
        [table_path],
        schema_tables,
        requested_k=2,
        delimiter="|",
        null_marker="\\N",
    )
    m1 = next(m for m in metrics if m.column == "col1")
    assert m1.null_count == 1
    assert m1.row_count == 2
    assert m1.distinct_count == 1  # 'x'
    assert m1.non_null_rows == 1
