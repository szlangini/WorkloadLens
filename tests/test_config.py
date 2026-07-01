from __future__ import annotations

from pathlib import Path

import json

from workloadlens.config import ProjectConfig, load_config, save_config


def test_project_config_resolves_relative_paths(tmp_path: Path) -> None:
    config = ProjectConfig(
        schema="schema.sql",
        queries="queries/*.sql",
        data_dir="data",
        output_dir="artifacts",
    )
    config_path = tmp_path / "workloadlens.json"
    save_config(config, config_path)

    loaded = load_config(config_path)
    assert loaded.schema == "schema.sql"

    schema_path = loaded.resolve_schema(config_path)
    assert schema_path == tmp_path / "schema.sql"

    output_dir = loaded.resolve_output_dir(config_path)
    assert output_dir == tmp_path / "artifacts"

    coverage_path = loaded.resolve_coverage_json(config_path)
    assert coverage_path == output_dir / "coverage.jsonl"

    data_path = loaded.resolve_data_json(config_path)
    assert data_path == output_dir / "data_metrics.jsonl"

    report_path = loaded.resolve_report_pdf(config_path)
    assert report_path == output_dir / "workloadlens_report.pdf"


def test_load_config_rejects_non_object(tmp_path: Path) -> None:
    path = tmp_path / "workloadlens.json"
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    try:
        load_config(path)
    except ValueError:
        pass
    else:
        raise AssertionError("Expected ValueError for non-object config")
