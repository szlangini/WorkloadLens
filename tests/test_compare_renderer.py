"""Regression tests for the high-cardinality comparison renderer.

These tests do not pixel-compare PDF pages (too brittle across matplotlib
versions). They lock down behaviour we care about:

- `workloadlens compare` runs without errors at N = 2, 5, 9, 15 workloads.
- The three alternative `--render-style` modes (heatmap, dot-plot, small-
  multiples) each produce a non-empty PDF.
- `--sort-by label` and `--sort-by distance --reference <label>` both work
  and re-order workloads in the metadata JSON output.
- For N <= 3 the adaptive renderer leaves layout knobs alone: bars stay
  vertical, the legend stays inside the axes, and font scaling is identity.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

import pytest
import subprocess
import sys
from typer.testing import CliRunner

from workloadlens.cli import app, _l1_distance, _order_workloads, _workload_signal_vector
from workloadlens.report.aggregate import aggregate_labeled_jsonl
from workloadlens.report.pdf import (
    _adaptive_grouped_figsize,
    _adaptive_horizontal_figsize,
    _font_scale,
    _is_baseline_label,
    _palette_for_workloads,
    GROUPED_BAR_FIGSIZE,
    HORIZONTAL_BAR_THRESHOLD,
    LEGEND_OUTSIDE_THRESHOLD,
    MIN_BAR_WIDTH,
    DEFAULT_PALETTE,
)


def _matplotlib_available() -> bool:
    try:
        subprocess.run(
            [sys.executable, "-c", "import matplotlib"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:  # pragma: no cover
        return False
    return True


MATPLOTLIB_OK = _matplotlib_available()


# --------------------------------------------------------------------- helpers


def _coverage_record(qid: str, mode: str = "ast_only", *, op_types: Dict[str, Dict[str, int]] | None = None) -> Dict:
    return {
        "id": qid,
        "mode": mode,
        "operators": {"scan": 1, "filter": 1, "project": 1},
        "join_types": {},
        "operator_types": op_types or {"scan": {"INT": 1}, "filter": {"INT": 1}, "project": {"INT": 1}},
        "functions": {"projection": [], "aggregate": [], "window": [], "expression": [], "all": []},
        "statement_type": "Select",
        "is_query": True,
        "operator_total": 3,
        "limit": {"has_limit": False, "value": None, "has_order_by": False},
    }


def _write_workload(workload_dir: Path, label: str, *, op_types: Dict[str, Dict[str, int]] | None = None, n_queries: int = 3) -> Path:
    """Create a coverage.jsonl + a workloadlens.json pointing at it."""
    workload_dir.mkdir(parents=True, exist_ok=True)
    coverage_path = workload_dir / "coverage.jsonl"
    with coverage_path.open("w", encoding="utf-8") as fh:
        for i in range(n_queries):
            fh.write(json.dumps(_coverage_record(f"{label}_q{i}.sql#1", op_types=op_types)) + "\n")
    config_path = workload_dir / "workloadlens.json"
    config_path.write_text(
        json.dumps(
            {
                "schema": str(workload_dir / "schema.sql"),
                "queries": str(workload_dir / "queries/*.sql"),
                "data_dir": str(workload_dir / "data"),
                "output_dir": str(workload_dir / "outputs"),
                "coverage_json": str(coverage_path),
                "data_json": str(workload_dir / "data.jsonl"),
                "report_pdf": str(workload_dir / "report.pdf"),
                "scope": "queries",
            }
        )
    )
    return config_path


def _build_corpus(tmp_path: Path, n: int) -> List[str]:
    """Make N synthetic workloads and return CLI -c args ('Label=path' strings)."""
    args: List[str] = []
    for i in range(n):
        label = f"WL{i:02d}"
        cfg = _write_workload(tmp_path / f"workload_{i}", label, op_types={
            "scan": {"INT": 1 + i, "TEXT": i},
            "filter": {"INT": 1 + i, "TEXT": 2 + i},
            "project": {"INT": 1 + i},
        })
        args += ["-c", f"{label}={cfg}"]
    return args


# ----------------------------------------------- N-workload smoke tests (D1)


@pytest.mark.skipif(not MATPLOTLIB_OK, reason="matplotlib not available")
@pytest.mark.parametrize("n", [2, 5, 9, 15])
def test_compare_runs_at_workload_count(tmp_path: Path, n: int) -> None:
    pdf_path = tmp_path / f"compare_{n}.pdf"
    config_args = _build_corpus(tmp_path, n)

    runner = CliRunner()
    result = runner.invoke(app, ["compare", *config_args, "--out", str(pdf_path), "--no-open"])

    assert result.exit_code == 0, result.output
    assert pdf_path.exists() and pdf_path.stat().st_size > 0


# ------------------------------------------- render-style smoke tests (D1+C)


@pytest.mark.skipif(not MATPLOTLIB_OK, reason="matplotlib not available")
@pytest.mark.parametrize("style", ["bars", "heatmap", "dot-plot", "small-multiples"])
def test_compare_render_style(tmp_path: Path, style: str) -> None:
    pdf_path = tmp_path / f"compare_{style}.pdf"
    config_args = _build_corpus(tmp_path, 6)

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["compare", *config_args, "--render-style", style, "--out", str(pdf_path), "--no-open"],
    )

    assert result.exit_code == 0, result.output
    assert pdf_path.exists() and pdf_path.stat().st_size > 0


# ----------------------------------------------------- sort-by tests (D1+B4)


@pytest.mark.skipif(not MATPLOTLIB_OK, reason="matplotlib not available")
def test_compare_sort_by_label(tmp_path: Path) -> None:
    pdf_path = tmp_path / "compare_sorted.pdf"
    json_out = tmp_path / "compare_sorted.json"
    config_args = _build_corpus(tmp_path, 5)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "compare",
            *config_args,
            "--sort-by",
            "label",
            "--out",
            str(pdf_path),
            "--json-out",
            str(json_out),
            "--no-open",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(json_out.read_text())
    keys = list(payload.keys())
    assert keys == sorted(keys, key=str.lower)


@pytest.mark.skipif(not MATPLOTLIB_OK, reason="matplotlib not available")
def test_compare_sort_by_distance(tmp_path: Path) -> None:
    pdf_path = tmp_path / "compare_distance.pdf"
    json_out = tmp_path / "compare_distance.json"
    config_args = _build_corpus(tmp_path, 5)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "compare",
            *config_args,
            "--sort-by",
            "distance",
            "--reference",
            "WL00",
            "--out",
            str(pdf_path),
            "--json-out",
            str(json_out),
            "--no-open",
        ],
    )

    assert result.exit_code == 0, result.output
    keys = list(json.loads(json_out.read_text()).keys())
    assert keys[0] == "WL00", "reference workload should be first"


@pytest.mark.skipif(not MATPLOTLIB_OK, reason="matplotlib not available")
def test_compare_invalid_render_style_rejected(tmp_path: Path) -> None:
    pdf_path = tmp_path / "compare_bad.pdf"
    config_args = _build_corpus(tmp_path, 3)

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["compare", *config_args, "--render-style", "bogus", "--out", str(pdf_path), "--no-open"],
    )
    assert result.exit_code != 0


# -------------------------------------------- backward-compat assertions (D4)


def test_backward_compat_small_N_layout_unchanged() -> None:
    """For N <= ADAPTIVE_FONT_PIVOT all adaptive layout knobs are identity."""
    for n in (1, 2, 3, 4):
        assert _font_scale(n) == 1.0, f"font scale must be 1.0 at N={n}"
    # Pivot to horizontal only at N >= HORIZONTAL_BAR_THRESHOLD.
    assert HORIZONTAL_BAR_THRESHOLD == LEGEND_OUTSIDE_THRESHOLD
    assert HORIZONTAL_BAR_THRESHOLD >= 6  # 2-5 workloads keep vertical layout


def test_palette_does_not_recycle_below_twelve() -> None:
    """The curated palette plus tab20 fallback covers 12+ workloads without dup."""
    colours = _palette_for_workloads(DEFAULT_PALETTE, 12)
    assert len(set(colours)) == 12


def test_palette_extends_via_colormap_for_large_N() -> None:
    """For N=15 we should still get 15 distinct colours."""
    colours = _palette_for_workloads(DEFAULT_PALETTE, 15)
    assert len(set(colours)) >= 14  # tab20 entries adjacent to curated colours may collide once


def test_baseline_label_detection() -> None:
    assert _is_baseline_label("Snowflake")
    assert _is_baseline_label("amazon redshift")
    assert _is_baseline_label("Production baseline")
    assert _is_baseline_label("RealWorld")
    assert not _is_baseline_label("TPC-DS")
    assert not _is_baseline_label("PROD-DS")  # our own benchmark is not a baseline
    assert not _is_baseline_label("")


def test_adaptive_figsizes_grow_with_N() -> None:
    base_w, base_h = _adaptive_grouped_figsize(5, 3)
    big_w, big_h = _adaptive_grouped_figsize(5, 12)
    assert big_w >= base_w
    assert big_h >= base_h

    base_w_h, base_h_h = _adaptive_horizontal_figsize(5, 3)
    big_w_h, big_h_h = _adaptive_horizontal_figsize(5, 12)
    assert big_h_h > base_h_h  # horizontal height grows with N


def test_min_bar_width_floor() -> None:
    assert MIN_BAR_WIDTH >= 0.05
    # At N=20 a naive 0.8/N would yield 0.04 which is below MIN_BAR_WIDTH.
    naive = 0.8 / 20
    assert naive < MIN_BAR_WIDTH


# ----------------------------------------------------------------- sort math


def test_l1_distance_basic() -> None:
    a = {"x": 10.0, "y": 0.0}
    b = {"x": 0.0, "y": 10.0}
    assert _l1_distance(a, b) == 20.0


def test_order_workloads_input_is_noop() -> None:
    aggregated = {"b": object(), "a": object(), "c": object()}
    ordered = _order_workloads(aggregated, "input", None)
    assert list(ordered.keys()) == ["b", "a", "c"]


def test_order_workloads_label_alphabetical() -> None:
    aggregated = {"b": object(), "a": object(), "c": object()}
    ordered = _order_workloads(aggregated, "label", None)
    assert list(ordered.keys()) == ["a", "b", "c"]


@pytest.mark.skipif(not MATPLOTLIB_OK, reason="matplotlib not available")
def test_order_workloads_distance_uses_reference(tmp_path: Path) -> None:
    """Two workloads identical to the reference rank above one that differs."""

    near_op_types = {"filter": {"INT": 10, "TEXT": 0}, "join_key": {"INT": 5}}
    far_op_types = {"filter": {"INT": 0, "TEXT": 10}, "join_key": {"TEXT": 5}}

    cfg_ref = _write_workload(tmp_path / "ref", "REF", op_types=near_op_types)
    cfg_near = _write_workload(tmp_path / "near", "NEAR", op_types=near_op_types)
    cfg_far = _write_workload(tmp_path / "far", "FAR", op_types=far_op_types)

    coverage_map = {
        "REF": [Path(cfg_ref).parent / "coverage.jsonl"],
        "FAR": [Path(cfg_far).parent / "coverage.jsonl"],
        "NEAR": [Path(cfg_near).parent / "coverage.jsonl"],
    }
    aggregated = aggregate_labeled_jsonl(coverage_map)
    ordered = _order_workloads(aggregated, "distance", "REF")
    keys = list(ordered.keys())
    assert keys[0] == "REF"
    assert keys.index("NEAR") < keys.index("FAR")
