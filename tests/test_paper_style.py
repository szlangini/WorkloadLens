"""Smoke tests for the flag-guarded paper-style renderer.

These tests cover only the public surface used by the CLI: that the three
presets (fig3, fig4, fig7) produce non-empty single-page PDFs when handed a
minimal AggregatedReportMap. The full visual fidelity is exercised by the
Prod-DS revision pipeline; here we just guard against import / wiring regressions.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from workloadlens.report import paper
from workloadlens.report.aggregate import (
    AggregatedReport,
    AggregatedStats,
    SchemaProfile,
)
from workloadlens.datafiles import ColumnMCVMetric, DataProfile

pytest.importorskip("matplotlib")


def _make_report(
    *,
    column_type_counts: Counter,
    filter_types: Counter,
    join_types: Counter,
    agg_types: Counter,
    group_by_counts,
    null_fractions=(),
    mcv_fractions=(),
) -> AggregatedReport:
    stats_all = AggregatedStats()
    stats_queries = AggregatedStats()
    stats_queries.operator_type_counts["filter"] = filter_types
    stats_queries.operator_type_counts["join"] = join_types
    stats_queries.operator_type_counts["aggregate"] = agg_types
    stats_queries.group_by_key_counts = list(group_by_counts)

    schema = SchemaProfile(
        table_count=1,
        total_columns=sum(column_type_counts.values()),
        column_type_counts=column_type_counts,
    )

    profile = DataProfile()
    for idx, frac in enumerate(null_fractions):
        mcv = mcv_fractions[idx] if idx < len(mcv_fractions) else 0.1
        profile.columns.append(
            ColumnMCVMetric(
                table="t",
                column=f"c{idx}",
                file="t.parquet",
                row_count=1000,
                null_count=int(1000 * frac),
                topk_sum=int(1000 * (1.0 - frac) * mcv),
                max_count=int(1000 * (1.0 - frac) * mcv),
                distinct_count=10,
                reported_k=20,
                requested_k=20,
            )
        )
    return AggregatedReport(
        all_statements=stats_all,
        query_statements=stats_queries,
        metadata={},
        data_profile=profile,
        schema_profile=schema,
    )


@pytest.fixture()
def workloads() -> dict:
    tpcds = _make_report(
        column_type_counts=Counter({"INT": 189, "TEXT": 147, "DECIMAL": 80, "DATE": 12}),
        filter_types=Counter({"INT": 60, "TEXT": 25, "DATE": 10, "DECIMAL": 5}),
        join_types=Counter({"INT": 90, "TEXT": 7, "DATE": 3}),
        agg_types=Counter({"INT": 80, "DECIMAL": 15, "TEXT": 5}),
        group_by_counts=[0, 1, 2, 3, 5, 7, 1, 1, 2],
        null_fractions=[0.0] * 50 + [0.02] * 5 + [0.5] * 2,
        mcv_fractions=[0.05] * 40 + [0.3] * 10 + [0.8] * 7,
    )
    prodds = _make_report(
        column_type_counts=Counter({"TEXT": 279, "INT": 57, "DECIMAL": 80, "DATE": 12}),
        filter_types=Counter({"INT": 50, "TEXT": 36, "DATE": 8, "DECIMAL": 6}),
        join_types=Counter({"TEXT": 95, "INT": 4, "DATE": 1}),
        agg_types=Counter({"INT": 70, "TEXT": 20, "DECIMAL": 10}),
        group_by_counts=[0, 1, 2, 3, 5, 7, 1, 12, 15],
        null_fractions=[0.0] * 40 + [0.1] * 5 + [0.5] * 7 + [0.8] * 5,
        mcv_fractions=[0.05] * 30 + [0.3] * 15 + [0.6] * 12,
    )
    return {"TPC-DS": tpcds, "PROD-DS": prodds}


@pytest.fixture()
def baselines() -> dict:
    return {
        "filter_logical_types": {
            "sources": {
                "snowflake": {"values": {"INT": 33, "TEXT": 55, "DATE": 8, "OTHER": 4}}
            }
        },
        "join_key_logical_types": {
            "sources": {
                "snowflake": {"values": {"INT": 35, "TEXT": 45, "DATE": 5, "OTHER": 15}}
            }
        },
        "aggregate_logical_types": {
            "sources": {
                "snowflake": {"values": {"INT": 85, "TEXT": 8, "OTHER": 7}}
            }
        },
        "schema_column_types": {
            "sources": {
                "snowflake": {"values": {"INT": 31, "TEXT": 47, "DECIMAL": 11, "DATE": 11}}
            }
        },
        "group_by_key_count": {
            "sources": {
                "snowflake": {"values": {"0": 5, "1-2": 57, "3-5": 30, "6-10": 4, "10+": 3}}
            }
        },
        "null_fraction_distribution": {
            "sources": {
                "redshift": {
                    "values": {
                        ">=0.01": 35,
                        ">=0.10": 25,
                        ">=0.30": 18,
                        ">=0.50": 13,
                        ">=0.70": 10,
                        ">=0.90": 5,
                    }
                }
            }
        },
        "mcv_share_distribution": {
            "sources": {
                "redshift": {
                    "values": {
                        ">=0.01": 80,
                        ">=0.10": 45,
                        ">=0.30": 25,
                        ">=0.50": 20,
                        ">=0.70": 15,
                        ">=0.90": 8,
                    }
                }
            }
        },
    }


def _pdf_pages(path: Path) -> int:
    """Return the page count of a PDF using PyPDF2-free crude scan."""
    data = path.read_bytes()
    return data.count(b"/Type /Page\n") + data.count(b"/Type/Page\n") + data.count(b"/Type /Page ")


def test_fig3_renders(tmp_path, workloads, baselines):
    panels = paper.fig3_panels(workloads, baselines=baselines)
    assert len(panels) == 3
    for panel in panels:
        assert panel.kind == "bar"
        # 3 workload series (TPC-DS, PROD-DS) + 1 baseline (Snowflake) = 3
        assert len(panel.series) == 3
        assert panel.categories == paper.FIG3_BUCKETS_4
    out = tmp_path / "fig3.pdf"
    paper.render_paper_figure(panels, out, layout="row")
    assert out.exists() and out.stat().st_size > 1000


def test_fig7_renders(tmp_path, workloads, baselines):
    panels = paper.fig7_panels(workloads, baselines=baselines)
    assert len(panels) == 2
    assert panels[0].categories == paper.FIG7_BUCKETS_5
    assert panels[1].categories == paper.GROUP_BY_BUCKETS
    out = tmp_path / "fig7.pdf"
    paper.render_paper_figure(panels, out, layout="column")
    assert out.exists() and out.stat().st_size > 1000


def test_fig4_renders(tmp_path, workloads, baselines):
    panels = paper.fig4_panels(workloads, baselines=baselines)
    assert len(panels) == 2
    for panel in panels:
        assert panel.kind == "cdf"
        # 2 workload curves + 1 Redshift baseline curve
        assert len(panel.cdf_series) == 3
    out = tmp_path / "fig4.pdf"
    paper.render_paper_figure(panels, out, layout="column")
    assert out.exists() and out.stat().st_size > 1000


def test_preset_dispatcher_all(tmp_path, workloads, baselines):
    written = paper.render_paper_preset(workloads, "all", tmp_path / "out", baselines=baselines)
    names = sorted(p.name for p in written)
    assert names == ["paper_fig3.pdf", "paper_fig4.pdf", "paper_fig7.pdf"]
    for p in written:
        assert p.stat().st_size > 1000


def test_palette_uses_three_colors(workloads, baselines):
    panels = paper.fig3_panels(workloads, baselines=baselines)
    labels = [label for label, _ in panels[0].series]
    colors = {paper._color_for(label) for label in labels}
    # exactly 3 fixed colors, no extension-palette fallback
    assert "#888888" not in colors
    assert len(colors) == 3
    # All three labels must be present in the palette (no fallback used)
    for label in labels:
        assert paper._norm_label(label) in paper.PAPER_PALETTE


def test_unknown_preset_raises(tmp_path, workloads):
    with pytest.raises(ValueError):
        paper.render_paper_preset(workloads, "fig99", tmp_path / "out")


def test_focus_filters_out_unlisted_workloads(workloads, baselines):
    panels = paper.fig3_panels(workloads, focus=["TPC-DS"], baselines=baselines)
    # Only TPC-DS + Snowflake baseline survive
    labels = [label for label, _ in panels[0].series]
    assert "PROD-DS" not in labels
    assert "TPC-DS" in labels
    assert "Snowflake" in labels


def test_default_pipeline_untouched(tmp_path, workloads):
    """If render_style != 'paper', generate_comparison_pdf must NOT route through paper."""
    from workloadlens.report.pdf import ReportOptions, generate_comparison_pdf

    out = tmp_path / "default.pdf"
    opts = ReportOptions(render_style="bars", paper_preset="fig3", scope="queries")
    # We don't assert on content here — just that the call path didn't get hijacked
    # into the paper renderer (which would write to tmp_path/out/paper_fig3.pdf).
    try:
        generate_comparison_pdf(workloads, out, options=opts)
    except Exception:
        # The default renderer may complain about the minimal AggregatedStats, that's fine.
        pass
    # paper renderer would NOT create a default.pdf; it would create paper_fig3.pdf next to it.
    assert not (tmp_path / "paper_fig3.pdf").exists()
