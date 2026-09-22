"""Paper-style figure renderer (flag-guarded, not used by default pipeline).

This module is invoked when ``ReportOptions.render_style == "paper"`` and a
preset is selected via ``ReportOptions.paper_preset``. It produces single-page
multi-panel PDFs that are intended to be ``\\includegraphics``'d into LaTeX,
matching the visual style of the Prod-DS / Scaling-Analytical-Benchmarks paper:

* compact vertical grouped bars (3.4 x 2.6 per panel)
* in-plot legend, small font
* light horizontal grid only
* fixed 3-color palette (TPC-DS / PROD-DS / Snowflake) by default
* sub-captions ``(a)``, ``(b)``, ``(c)`` below each panel

The default WorkloadLens pipeline is *not* touched by this module. To use it::

    workloadlens compare -c tpcds=... -c prodds=... \\
        --paper-style --preset fig3 \\
        --baselines /path/to/production_baselines.yaml \\
        --out paper_fig3.pdf
"""

from __future__ import annotations

import os

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .aggregate import AggregatedReport, AggregatedReportMap, AggregatedStats, SchemaProfile


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

# Fixed palette matched to the published paper figures. Keys are matched against
# the workload label after a normalize step (upper-cased, ``_``/`` ``-stripped),
# so both "PROD-DS", "prodds", and "Prod-DS" all map to the same color.
PAPER_PALETTE: Dict[str, str] = {
    # Curated palette. Baselines are brand-coloured (Snowflake sky-blue,
    # Redshift brick, Tableau royal purple). Benches stay distinct and fresh —
    # no muddy charcoals, no plain black.
    "PRODDS": "#FF8C00",       # vivid orange — hero
    "TPCDS":  "#2A9D8F",       # deep teal — fresh anchor
    "TPCH":   "#1F5C99",       # cobalt
    "DSB":    "#9BC53D",       # apple green (TPC-DS family)
    "JCCH":   "#8C5A2B",       # sepia brown (data-side only)
    "CLICKBENCH": "#D6549A",   # rose pink
    "JOB":    "#E07A5F",       # terracotta
    "REDBENCH": "#F2C744",     # gold
    # Baselines (brand-coloured)
    "SNOWFLAKE": "#5BC0EB",          # Snowflake brand sky blue (dashed in CDFs)
    "AMAZONREDSHIFT": "#C3423F",     # Redshift brick red
    "REDSHIFT": "#C3423F",
    "TABLEAU": "#6A4C93",            # Tableau brand royal purple
}

# Categorical bucket palette for stacked-bar fills (per type bucket, not per bench).
TYPE_BUCKET_COLORS: Dict[str, str] = {
    # Logical-type buckets
    "INT":      "#3B82C4",
    "Number":   "#3B82C4",
    "TEXT":     "#F4A261",
    "Text":     "#F4A261",
    "DECIMAL":  "#7FB069",
    "DATE":     "#9B59B6",
    "Date":     "#9B59B6",
    "Other":    "#B0B0B0",
    "OTHER":    "#B0B0B0",
    # GROUP BY key-count buckets (sequential, light -> dark)
    "0":        "#E5E5E5",
    "1-2":      "#A6CEE3",
    "3-5":      "#7FA9C8",
    "6-10":     "#3B82C4",
    "10+":      "#1F4E79",
    # LIMIT magnitude buckets (sequential, light -> dark)
    "1-10":     "#FCE4A6",
    "11-100":   "#F8C264",
    "101-1K":   "#F39C40",
    "1K-10K":   "#E07C24",
    "10K-100K": "#C25410",
    "100K-1M":  "#9B370A",
    "1M-10M":   "#6B2007",
    ">10M":     "#3E0F03",
}

# Distinct line style for production baselines in CDF plots.
BASELINE_LABELS = {"Snowflake", "Amazon Redshift", "Redshift", "Tableau"}

# Default focus order for the bar-chart presets (Fig 3, Fig 7).
DEFAULT_FOCUS_BARS: List[str] = ["TPC-DS", "PROD-DS", "Snowflake"]

# Default focus order for the CDF preset (Fig 4) — Redshift instead of Snowflake.
DEFAULT_FOCUS_CDF: List[str] = ["TPC-DS", "PROD-DS", "Amazon Redshift"]

# Type bucket layouts used by the presets.
FIG3_BUCKETS_4: List[str] = ["Number", "Text", "Date", "Other"]      # INT+DECIMAL collapsed
FIG7_BUCKETS_5: List[str] = ["TEXT", "INT", "DECIMAL", "DATE", "Other"]
GROUP_BY_BUCKETS: List[str] = ["0", "1-2", "3-5", "6-10", "10+"]

# YAML signal keys -> preset hooks. Used by the CLI wrapper to load values.
SUPPORTED_PRESETS: Tuple[str, ...] = ("fig3", "fig4", "fig7", "all")


# ---------------------------------------------------------------------------
# Panel data model
# ---------------------------------------------------------------------------


@dataclass
class PaperPanelSpec:
    """One panel inside a paper figure.

    For ``kind="bar"`` each entry in ``series`` is one bar group; ``categories``
    are the x-tick labels. For ``kind="cdf"`` each series is plotted as a line;
    ``categories`` is ignored and ``x_values`` / ``y_values`` carry the curve.
    """

    kind: str  # "bar" | "cdf"
    title: str
    subcaption: Optional[str] = None
    categories: List[str] = field(default_factory=list)
    series: List[Tuple[str, List[float]]] = field(default_factory=list)
    cdf_series: List[Tuple[str, List[float], List[float]]] = field(default_factory=list)
    ylabel: str = "Share (%)"
    xlabel: Optional[str] = None
    legend_loc: str = "upper right"
    y_max: float = 100.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _norm_label(label: str) -> str:
    return "".join(ch for ch in str(label).upper() if ch.isalnum())


def _color_for(label: str, fallback: str = "#888888") -> str:
    return PAPER_PALETTE.get(_norm_label(label), fallback)


def _normalize_type_5bucket(label: str) -> Optional[str]:
    """Map a raw DDL/operator type label to one of TEXT/INT/DECIMAL/DATE/Other."""
    if not label:
        return None
    name = str(label).upper().strip()
    if name in {"VARCHAR", "CHAR", "STRING", "TEXT", "BPCHAR", "CHARACTER VARYING"}:
        return "TEXT"
    if name in {"INT", "INTEGER", "BIGINT", "SMALLINT", "TINYINT", "INT2", "INT4", "INT8"}:
        return "INT"
    if name in {"DECIMAL", "NUMERIC", "NUMBER"}:
        return "DECIMAL"
    if name in {"DATE", "TIMESTAMP", "TIMESTAMPTZ"} or name.startswith("TIMESTAMP"):
        return "DATE"
    return "Other"


def _normalize_type_4bucket(label: str) -> Optional[str]:
    """Map to Fig 3 buckets: Number = INT+DECIMAL collapsed."""
    five = _normalize_type_5bucket(label)
    if five is None:
        return None
    if five in {"INT", "DECIMAL"}:
        return "Number"
    if five == "TEXT":
        return "Text"
    if five == "DATE":
        return "Date"
    return "Other"


def _shares(counter: Counter, buckets: Sequence[str], normalizer) -> List[float]:
    """Return percent-shares over ``buckets`` after applying ``normalizer``."""
    bucketed: Counter = Counter()
    for key, value in (counter or {}).items():
        if not isinstance(value, (int, float)) or value <= 0:
            continue
        norm = normalizer(key)
        if norm is None:
            continue
        bucketed[norm] += float(value)
    total = sum(bucketed.values())
    if total <= 0:
        return [0.0] * len(buckets)
    return [round(100.0 * bucketed.get(b, 0.0) / total, 1) for b in buckets]


def _group_by_shares(counts: Sequence[int]) -> List[float]:
    if not counts:
        return [0.0] * len(GROUP_BY_BUCKETS)
    hist = Counter()
    for value in counts:
        v = int(value)
        if v == 0:
            hist["0"] += 1
        elif v <= 2:
            hist["1-2"] += 1
        elif v <= 5:
            hist["3-5"] += 1
        elif v <= 10:
            hist["6-10"] += 1
        else:
            hist["10+"] += 1
    total = sum(hist.values())
    return [round(100.0 * hist.get(b, 0) / total, 1) for b in GROUP_BY_BUCKETS]


def _percentile_curve(values: Sequence[float]) -> Tuple[List[float], List[float]]:
    """Sorted percentile curve: x = percentile (0..100), y = value (* 100)."""
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return [], []
    n = len(vals)
    xs = [100.0 * (i + 1) / n for i in range(n)]
    ys = [100.0 * v for v in vals]
    return xs, ys


def _baseline_curve_from_yaml(values_by_bucket: Dict[str, float],
                              bucket_order: Sequence[str]) -> Tuple[List[float], List[float]]:
    """Synthesise a step curve for a Redshift-style threshold table.

    The yaml stores ``{">=0.01": 35, ">=0.10": 25, ...}`` — share-of-columns at
    each threshold. We plot it on the same axes as the workload percentile
    curves by mapping ``share-of-columns`` to the x-axis (``100 - share``,
    i.e. the percentile at which the threshold is crossed) and the threshold
    itself * 100 to the y-axis.
    """
    xs: List[float] = []
    ys: List[float] = []
    for bucket in bucket_order:
        share = values_by_bucket.get(bucket)
        if share is None:
            continue
        try:
            threshold = float(bucket.replace(">=", "").strip())
        except ValueError:
            continue
        xs.append(100.0 - float(share))
        ys.append(threshold * 100.0)
    pairs = sorted(zip(xs, ys), key=lambda p: p[0])
    return [p[0] for p in pairs], [p[1] for p in pairs]


# ---------------------------------------------------------------------------
# Matplotlib drawing
# ---------------------------------------------------------------------------


def _ensure_mpl():
    import matplotlib  # noqa: WPS433
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # noqa: WPS433
    return plt


def _style_axes(ax) -> None:
    ax.set_ylim(0, 100)
    ax.set_yticks(range(0, 101, 10))
    ax.tick_params(axis="both", labelsize=7)
    ax.yaxis.grid(True, linestyle="-", linewidth=0.4, alpha=0.3, color="#888888")
    ax.xaxis.grid(False)
    ax.set_axisbelow(True)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_linewidth(0.6)


def _draw_bar_panel(ax, panel: PaperPanelSpec) -> None:
    import numpy as np  # noqa: WPS433
    n_cats = len(panel.categories)
    n_series = len(panel.series)
    if n_cats == 0 or n_series == 0:
        ax.set_axis_off()
        return
    x = np.arange(n_cats)
    group_width = 0.78
    bar_width = group_width / n_series
    for idx, (label, values) in enumerate(panel.series):
        offset = (idx - (n_series - 1) / 2.0) * bar_width
        ax.bar(
            x + offset,
            values,
            bar_width,
            label=label,
            color=_color_for(label),
            edgecolor="none",
        )
    ax.set_xticks(x)
    ax.set_xticklabels(panel.categories, fontsize=7)
    ax.set_ylabel(panel.ylabel, fontsize=8)
    if panel.xlabel:
        ax.set_xlabel(panel.xlabel, fontsize=8)
    ax.set_title(panel.title, fontsize=8.5, pad=4)
    _style_axes(ax)
    ax.legend(
        loc=panel.legend_loc,
        fontsize=6.5,
        frameon=True,
        framealpha=0.85,
        edgecolor="#cccccc",
        handlelength=1.2,
        handletextpad=0.4,
        borderpad=0.3,
        labelspacing=0.25,
    )


def _draw_cdf_panel(ax, panel: PaperPanelSpec) -> None:
    has_series = False
    for label, xs, ys in panel.cdf_series:
        if not xs or not ys:
            continue
        has_series = True
        ax.plot(
            xs,
            ys,
            label=label,
            color=_color_for(label),
            linewidth=1.2,
            drawstyle="steps-post",
        )
    if not has_series:
        ax.set_axis_off()
        return
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)
    ax.set_xticks(range(0, 101, 20))
    ax.set_yticks(range(0, 101, 20))
    ax.tick_params(axis="both", labelsize=7)
    ax.set_xlabel(panel.xlabel or "Percentile of columns", fontsize=8)
    ax.set_ylabel(panel.ylabel, fontsize=8)
    ax.set_title(panel.title, fontsize=8.5, pad=4)
    ax.grid(True, linestyle="-", linewidth=0.4, alpha=0.3, color="#888888")
    ax.set_axisbelow(True)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_linewidth(0.6)
    ax.legend(
        loc=panel.legend_loc,
        fontsize=6.5,
        frameon=True,
        framealpha=0.85,
        edgecolor="#cccccc",
        handlelength=1.4,
        handletextpad=0.4,
        borderpad=0.3,
        labelspacing=0.25,
    )


def render_paper_figure(
    panels: Sequence[PaperPanelSpec],
    out_path: Path,
    *,
    layout: str = "row",
    figsize: Optional[Tuple[float, float]] = None,
) -> None:
    if not panels:
        raise ValueError("render_paper_figure: no panels provided")
    plt = _ensure_mpl()
    n = len(panels)
    if layout == "row":
        fig, axes = plt.subplots(1, n, figsize=figsize or (3.4 * n, 2.6))
    elif layout == "column":
        fig, axes = plt.subplots(n, 1, figsize=figsize or (3.4, 2.6 * n))
    else:
        raise ValueError(f"layout must be 'row' or 'column', got {layout!r}")
    if n == 1:
        axes = [axes]
    else:
        axes = list(axes)
    for ax, panel in zip(axes, panels):
        if panel.kind == "bar":
            _draw_bar_panel(ax, panel)
        elif panel.kind == "cdf":
            _draw_cdf_panel(ax, panel)
        else:
            raise ValueError(f"unknown panel kind: {panel.kind!r}")
        if panel.subcaption:
            ax.annotate(
                panel.subcaption,
                xy=(0.5, -0.28),
                xycoords="axes fraction",
                ha="center",
                va="top",
                fontsize=7.5,
                style="italic",
            )
    fig.tight_layout()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out_path), format="pdf", bbox_inches="tight")
    plt.close(fig)


# ---------------------------------------------------------------------------
# Preset builders
# ---------------------------------------------------------------------------


def _select_focus(workloads: AggregatedReportMap,
                  focus: Sequence[str]) -> List[Tuple[str, AggregatedReport]]:
    by_norm = {_norm_label(label): (label, report) for label, report in workloads.items()}
    out: List[Tuple[str, AggregatedReport]] = []
    for wanted in focus:
        match = by_norm.get(_norm_label(wanted))
        if match is not None:
            out.append((wanted, match[1]))
    return out


def _baseline_shares(baseline_values: Dict[str, float],
                     buckets: Sequence[str],
                     collapse_to_number: bool = False) -> List[float]:
    """Project a baseline dict onto a target bucket list."""
    if collapse_to_number:
        merged = {
            "Number": float(baseline_values.get("INT", 0) or 0)
                       + float(baseline_values.get("DECIMAL", 0) or 0),
            "Text": float(baseline_values.get("TEXT", 0) or 0),
            "Date": float(baseline_values.get("DATE", 0) or 0),
            "Other": float(baseline_values.get("OTHER", 0) or 0),
        }
        return [round(merged.get(b, 0.0), 1) for b in buckets]
    return [round(float(baseline_values.get(b, 0.0) or 0.0), 1) for b in buckets]


def fig3_panels(
    workloads: AggregatedReportMap,
    *,
    focus: Sequence[str] = DEFAULT_FOCUS_BARS,
    baselines: Optional[Dict[str, Dict[str, Any]]] = None,
    baseline_source: str = "snowflake",
) -> List[PaperPanelSpec]:
    """Filter / join-key / aggregate logical types — 4-bucket Number/Text/Date/Other."""
    selected = _select_focus(workloads, focus)
    if not selected:
        return []

    panels: List[PaperPanelSpec] = []
    specs = [
        ("filter", "Filter Column Logical Types", "(a) Filters shift toward strings."),
        ("join", "Join Key Logical Types", "(b) Join keys shift toward strings."),
        ("aggregate", "Aggregation Logical Types", "(c) Aggregations remain numeric heavy."),
    ]
    yaml_keys = {
        "filter": "filter_logical_types",
        "join": "join_key_logical_types",
        "aggregate": "aggregate_logical_types",
    }
    for op_key, title, sub in specs:
        series: List[Tuple[str, List[float]]] = []
        for label, report in selected:
            op_counts = report.query_statements.operator_type_counts_dict().get(op_key) or Counter()
            shares = _shares(op_counts, FIG3_BUCKETS_4, _normalize_type_4bucket)
            series.append((label, shares))
        if baselines:
            baseline = (baselines.get(yaml_keys[op_key], {}).get("sources", {}).get(baseline_source))
            if baseline:
                values = baseline.get("values") or {}
                series.append((
                    _focus_baseline_label(focus, baseline_source),
                    _baseline_shares(values, FIG3_BUCKETS_4, collapse_to_number=True),
                ))
        panels.append(PaperPanelSpec(
            kind="bar",
            title=title,
            subcaption=sub,
            categories=list(FIG3_BUCKETS_4),
            series=series,
        ))
    return panels


def fig7_panels(
    workloads: AggregatedReportMap,
    *,
    focus: Sequence[str] = DEFAULT_FOCUS_BARS,
    baselines: Optional[Dict[str, Dict[str, Any]]] = None,
    baseline_source: str = "snowflake",
) -> List[PaperPanelSpec]:
    """Column type distribution (schema) + GROUP BY key count — 5-bucket each."""
    selected = _select_focus(workloads, focus)
    if not selected:
        return []

    # Panel (a): schema column types — 5 bucket TEXT/INT/DECIMAL/DATE/Other
    col_series: List[Tuple[str, List[float]]] = []
    for label, report in selected:
        profile = report.schema_profile
        counter = profile.column_type_counts if profile and profile.has_data() else Counter()
        col_series.append((label, _shares(counter, FIG7_BUCKETS_5, _normalize_type_5bucket)))
    if baselines:
        baseline = baselines.get("schema_column_types", {}).get("sources", {}).get(baseline_source)
        if baseline:
            values = baseline.get("values") or {}
            col_series.append((
                _focus_baseline_label(focus, baseline_source),
                _baseline_shares(values, FIG7_BUCKETS_5),
            ))

    # Panel (b): GROUP BY key counts
    gb_series: List[Tuple[str, List[float]]] = []
    for label, report in selected:
        gb_series.append((label, _group_by_shares(report.query_statements.group_by_key_counts)))
    if baselines:
        baseline = baselines.get("group_by_key_count", {}).get("sources", {}).get(baseline_source)
        if baseline:
            values = baseline.get("values") or {}
            gb_series.append((
                _focus_baseline_label(focus, baseline_source),
                [round(float(values.get(b, 0.0) or 0.0), 1) for b in GROUP_BY_BUCKETS],
            ))

    return [
        PaperPanelSpec(
            kind="bar",
            title="Column Type Distribution",
            subcaption="(a) Column type distribution.",
            categories=list(FIG7_BUCKETS_5),
            series=col_series,
        ),
        PaperPanelSpec(
            kind="bar",
            title="GROUP BY Key Counts",
            subcaption="(b) GROUP BY key distribution.",
            categories=list(GROUP_BY_BUCKETS),
            series=gb_series,
        ),
    ]


def fig4_panels(
    workloads: AggregatedReportMap,
    *,
    focus: Sequence[str] = DEFAULT_FOCUS_CDF,
    baselines: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[PaperPanelSpec]:
    """NULL fraction + MCV share percentile curves with Redshift baseline overlay."""
    selected = _select_focus(workloads, focus)

    null_series: List[Tuple[str, List[float], List[float]]] = []
    mcv_series: List[Tuple[str, List[float], List[float]]] = []
    for label, report in selected:
        profile = report.data_profile
        if profile is None:
            continue
        nxs, nys = _percentile_curve(profile.null_fractions())
        if nxs:
            null_series.append((label, nxs, nys))
        mxs, mys = _percentile_curve(profile.max_mcv_fractions())
        if mxs:
            mcv_series.append((label, mxs, mys))

    bucket_order = [">=0.01", ">=0.10", ">=0.30", ">=0.50", ">=0.70", ">=0.90"]
    if baselines:
        null_baseline = baselines.get("null_fraction_distribution", {}).get("sources", {}).get("redshift")
        if null_baseline:
            xs, ys = _baseline_curve_from_yaml(null_baseline.get("values") or {}, bucket_order)
            if xs:
                null_series.append(("Amazon Redshift", xs, ys))
        mcv_baseline = baselines.get("mcv_share_distribution", {}).get("sources", {}).get("redshift")
        if mcv_baseline:
            xs, ys = _baseline_curve_from_yaml(mcv_baseline.get("values") or {}, bucket_order)
            if xs:
                mcv_series.append(("Amazon Redshift", xs, ys))

    return [
        PaperPanelSpec(
            kind="cdf",
            title="Null Fraction (Columns)",
            subcaption="(a) NULL fraction profile.",
            ylabel="Fraction (%)",
            xlabel="Percentile of columns",
            cdf_series=null_series,
            legend_loc="upper left",
        ),
        PaperPanelSpec(
            kind="cdf",
            title="Maximum MCV Frequency",
            subcaption="(b) Max MCV share.",
            ylabel="Fraction (%)",
            xlabel="Percentile of columns",
            cdf_series=mcv_series,
            legend_loc="upper left",
        ),
    ]


def _focus_baseline_label(focus: Sequence[str], source: str) -> str:
    """Pick a human label for the baseline series based on source key."""
    source_to_label = {
        "snowflake": "Snowflake",
        "redshift": "Amazon Redshift",
        "tableau": "Tableau",
    }
    return source_to_label.get(source.lower(), source.title())


# ---------------------------------------------------------------------------
# Top-level preset dispatcher
# ---------------------------------------------------------------------------


def render_paper_preset(
    workloads: AggregatedReportMap,
    preset: str,
    out_path: Path,
    *,
    focus_bars: Sequence[str] = DEFAULT_FOCUS_BARS,
    focus_cdf: Sequence[str] = DEFAULT_FOCUS_CDF,
    baselines: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[Path]:
    """Dispatch to the requested preset, returning the list of written files.

    For ``preset == "all"`` we emit three sibling files in ``out_path``'s
    parent: ``paper_fig3.pdf``, ``paper_fig4.pdf``, ``paper_fig7.pdf``.
    Otherwise we honor the caller's chosen ``out_path``.
    """
    preset = (preset or "").lower()
    if preset not in SUPPORTED_PRESETS:
        raise ValueError(f"unknown paper preset {preset!r}; supported: {SUPPORTED_PRESETS}")

    out_path = Path(out_path)
    written: List[Path] = []

    if preset == "all":
        base_dir = out_path.parent if out_path.suffix else out_path
        base_dir.mkdir(parents=True, exist_ok=True)
        targets = [
            ("fig3", base_dir / "paper_fig3.pdf", "row"),
            ("fig7", base_dir / "paper_fig7.pdf", "column"),
            ("fig4", base_dir / "paper_fig4.pdf", "column"),
        ]
    else:
        layout = "row" if preset == "fig3" else "column"
        targets = [(preset, out_path, layout)]

    for name, path, layout in targets:
        if name == "fig3":
            panels = fig3_panels(workloads, focus=focus_bars, baselines=baselines)
        elif name == "fig7":
            panels = fig7_panels(workloads, focus=focus_bars, baselines=baselines)
        elif name == "fig4":
            panels = fig4_panels(workloads, focus=focus_cdf, baselines=baselines)
        else:
            continue
        if not panels:
            continue
        render_paper_figure(panels, path, layout=layout)
        written.append(path)
    return written


# ---------------------------------------------------------------------------
# YAML loader (kept tiny + dependency-light)
# ---------------------------------------------------------------------------


def load_baselines(path: Path) -> Dict[str, Dict[str, Any]]:
    """Return the ``signals`` sub-mapping of a production_baselines.yaml file."""
    try:
        import yaml  # noqa: WPS433
    except ImportError as exc:  # pragma: no cover - exercised in environments without PyYAML
        raise RuntimeError(
            "PyYAML is required to load --baselines; install with `pip install pyyaml`"
        ) from exc
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    signals = doc.get("signals") or {}
    if not isinstance(signals, dict):
        raise ValueError(f"{path}: expected top-level 'signals:' mapping")
    return signals


# ---------------------------------------------------------------------------
# Publication-style primitives
#
# These helpers are used by the per-figure revision wrappers (one PDF per
# figure). They are intentionally CSV-/array-driven, not AggregatedReport-
# driven, so the WorkloadLens library stays generic.
# ---------------------------------------------------------------------------


# ACM/PVLDB single-column = 252 pt = 3.5 in; two-column spread = ~7.16 in.
SINGLE_COL_WIDTH_IN: float = 3.5
TWO_COL_WIDTH_IN: float = 7.16


def apply_paper_style() -> None:
    """Configure matplotlib rcParams to match the published Prod-DS paper.

    Calibrated against benchmark_paper_new.pdf Figure 3 (sampled from PDF):
    sans-serif (DejaVu Sans), bold ~10pt title, 9pt axis labels, 8pt ticks,
    9pt legend, thin black bar edges, full 4-spine box, subtle dashed grid.
    """
    import matplotlib  # noqa: WPS433
    matplotlib.use("Agg")
    from matplotlib import font_manager  # noqa: WPS433

    available = {f.name for f in font_manager.fontManager.ttflist}
    sans_candidates = [
        "DejaVu Sans",
        "Arial",
        "Helvetica",
        "Liberation Sans",
        "sans-serif",
    ]
    chosen = next((c for c in sans_candidates if c in available), "DejaVu Sans")

    matplotlib.rcParams.update({
        "font.family": "sans-serif",
        "font.sans-serif": [chosen],
        "font.size": 9,
        "axes.titlesize": 10,
        "axes.titleweight": "bold",
        "axes.labelsize": 9,
        "xtick.labelsize": 8,
        "ytick.labelsize": 8,
        "legend.fontsize": 9,
        "axes.linewidth": 0.6,
        "xtick.major.width": 0.5,
        "ytick.major.width": 0.5,
        "xtick.major.size": 3.0,
        "ytick.major.size": 3.0,
        "pdf.fonttype": 42,        # embed Type-42 fonts (LaTeX-friendly)
        "ps.fonttype": 42,
        "axes.unicode_minus": False,
    })


# ── Figure titles ───────────────────────────────────────────────
# Figure-level headers are ON by default, because a WorkloadLens plot is usually read
# on its own -- in a report, a slide, a terminal -- where nothing else says what it
# shows. Reviewer D3(d) of the July 2026 round objected to headers only in the paper's
# figures, which sit under LaTeX captions that already name them; that is the exception,
# and paper/signals/render_paper_figures.py turns them off for itself rather than
# imposing the paper's convention on every caller. `PLOT_TITLES=0` (also `false`, `no`,
# `off`) suppresses them anywhere. Panel labels inside a small-multiple grid are NOT
# affected either way: they name which signal a panel shows and are content, not a
# repetition of a caption.
_TITLE_TRUTHY = {"1", "true", "yes", "on"}
_TITLE_FALSY = {"0", "false", "no", "off"}
# PLOT_LEGACY_STYLE=1 renders the figures the way they looked before the July 2026
# review round: in-plot titles on, curves separated by colour alone. It exists so the
# same data can be shown in the old and the new style side by side; it is never used
# for the paper itself.
LEGACY_STYLE: bool = os.environ.get("PLOT_LEGACY_STYLE", "0").strip().lower() in _TITLE_TRUTHY
SHOW_FIGURE_TITLES: bool = (
    LEGACY_STYLE or os.environ.get("PLOT_TITLES", "1").strip().lower() not in _TITLE_FALSY
)


def set_figure_titles_enabled(flag: bool) -> None:
    """Override the PLOT_TITLES default for this process.

    A caller that always wants one behaviour -- the paper renderer, which has captions --
    sets it here instead of relying on the ambient default.
    """
    global SHOW_FIGURE_TITLES
    SHOW_FIGURE_TITLES = bool(flag)


def set_figure_title(target: Any, text: Optional[str], **kwargs: Any) -> None:
    """Draw a figure-level header only when titles are enabled (see PLOT_TITLES)."""
    if not text or not SHOW_FIGURE_TITLES:
        return
    setter = getattr(target, "set_title", None) or getattr(target, "suptitle")
    setter(text, **kwargs)


def _line_style_for(label: str) -> str:
    return "--" if label in BASELINE_LABELS else "-"


# Reviewer D9 of the July 2026 round: "The legend of Figure 3 includes JOB, but
# it is difficult to identify the corresponding curve in the figure." The eight
# benchmark curves used to differ by colour alone, which fails in greyscale, for
# colour-vision deficiency, and whenever two curves run close together. Each
# series now carries a distinct dash pattern AND a distinct sparse marker, so a
# reader can match legend to curve on shape alone.
# ``phase`` staggers each series' markers along the curve so that two benchmarks
# running on top of each other (JOB and RedBench do, over the last third of the
# MCV curve) still show their own symbols instead of stacking them at the same x.
CURVE_STYLES: Dict[str, Dict[str, Any]] = {
    # bench            dash pattern (on, off, ...)           marker  phase
    "PRODDS":     {"dashes": (None, None),                   "marker": "o", "phase": 0.02},
    "TPCDS":      {"dashes": (None, None),                   "marker": "s", "phase": 0.04},
    "TPCH":       {"dashes": (3.2, 1.3),                     "marker": "^", "phase": 0.06},
    "DSB":        {"dashes": (1.3, 1.3),                     "marker": "D", "phase": 0.08},
    "CLICKBENCH": {"dashes": (4.5, 1.3, 1.0, 1.3),           "marker": "v", "phase": 0.10},
    "JOB":        {"dashes": (5.5, 1.5, 1.0, 1.5, 1.0, 1.5), "marker": "P", "phase": 0.12},
    "REDBENCH":   {"dashes": (2.4, 1.2),                     "marker": "X", "phase": 0.14},
    "JCCH":       {"dashes": (1.0, 1.0),                     "marker": "*", "phase": 0.16},
}
# Production baselines stay marker-free: they are reference lines, not benchmarks,
# and the dashed-without-marker look keeps that distinction readable at a glance.
_BASELINE_CURVE_STYLE: Dict[str, Any] = {"dashes": (4.0, 1.6), "marker": None}


def curve_style_for(label: str, *, markevery: float = 0.16) -> Dict[str, Any]:
    """Line kwargs that identify a series by shape as well as by colour.

    Returns ``dashes``/``marker`` (plus marker sizing) for the benchmark or
    baseline named by ``label``; unknown labels fall back to a plain solid line.
    """
    if LEGACY_STYLE:
        return {}
    key = _norm_label(label)
    if label in BASELINE_LABELS or key in {_norm_label(b) for b in BASELINE_LABELS}:
        style = dict(_BASELINE_CURVE_STYLE)
    else:
        style = dict(CURVE_STYLES.get(key, {"dashes": (None, None), "marker": None}))
    out: Dict[str, Any] = {}
    dashes = style.get("dashes")
    if dashes and dashes[0] is not None:
        out["dashes"] = dashes
    marker = style.get("marker")
    if marker:
        out.update({
            "marker": marker,
            "markersize": 4.6 if marker == "*" else 4.0,
            # (start, stride) as fractions of the curve length: the per-series
            # start phase keeps overlapping curves' markers from coinciding.
            "markevery": (float(style.get("phase", 0.02)), markevery),
            # A thin light rim keeps a marker readable where another curve is
            # drawn over it (reviewer D9: JOB was hidden under RedBench).
            "markeredgecolor": "white",
            "markeredgewidth": 0.6,
            "fillstyle": "full",
        })
    return out


# Explanatory legend notes ("Omitted, all-zero: …", "★ reported in production") are set
# smaller than the series labels and wrapped to the width of the label grid, so a note can
# never widen the legend box over a curve. 0.85 is the top of the 80-85% band the author
# asked for -- these figures are typeset at ~0.51x, so every point of note size counts.
_NOTE_SCALE: float = 0.85

# Tried in order when a caller asks for automatic placement; the first location whose whole
# box (background included) clears every curve wins, so the requested location still comes
# first and only moves when it would cover data.
_LEGEND_LOC_CANDIDATES: Tuple[str, ...] = (
    "upper left", "upper right", "lower left", "lower right",
    "center left", "center right", "upper center", "lower center", "center",
)


def _text_width_px(fig, rend, text: str, fontsize: float) -> float:
    """Width of ``text`` in display pixels at ``fontsize``, measured, not estimated."""
    probe = fig.text(0.0, 0.0, text, fontsize=fontsize)
    try:
        return float(probe.get_window_extent(rend).width)
    finally:
        probe.remove()


def _wrap_to_width(fig, rend, text: str, fontsize: float, max_px: float) -> List[str]:
    """Greedy wrap of one paragraph to ``max_px``, then pull back a single-word orphan.

    A word wider than the budget is kept on its own line rather than dropped: the caller's
    width is a layout target, not a hard clip.
    """
    words = text.split()
    if not words:
        return []
    lines: List[str] = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        if not current or _text_width_px(fig, rend, trial, fontsize) <= max_px:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    # Orphan: a last line holding one word reads as a typesetting accident. Pull a word down
    # from the line above when the result still fits.
    if len(lines) >= 2 and len(lines[-1].split()) == 1:
        head = lines[-2].split()
        if len(head) >= 2:
            moved = head.pop()
            candidate = f"{moved} {lines[-1]}"
            if _text_width_px(fig, rend, candidate, fontsize) <= max_px:
                lines[-2], lines[-1] = " ".join(head), candidate
    return lines


def _curve_cloud(ax):
    """Every plotted curve as a densified point cloud in axes coordinates, cached per axes.

    Densifying once and testing points is both simpler and far faster than clipping segments
    against thousands of candidate rectangles, and 0.004 axes units is finer than any line
    width at these figure sizes, so nothing slips between samples.
    """
    import numpy as np  # noqa: WPS433

    cached = ax.__dict__.get("_wl_curve_cloud")
    if cached is not None:
        return cached
    to_axes = (ax.transData + ax.transAxes.inverted()).transform
    step = 0.004
    pts_all, owner_all = [], []
    for idx, line in enumerate(ax.lines):
        if not line.get_visible():
            continue
        xs, ys = np.asarray(line.get_xdata(), float), np.asarray(line.get_ydata(), float)
        if xs.size == 0:
            continue
        pts = np.asarray(to_axes(np.column_stack([xs, ys])), float)
        dense = [pts[:1]]
        for i in range(1, len(pts)):
            a, b = pts[i - 1], pts[i]
            n = int(max(abs(b[0] - a[0]), abs(b[1] - a[1])) / step)
            if n > 1:
                t = np.linspace(0.0, 1.0, min(n, 4000) + 1)[1:, None]
                dense.append(a + (b - a) * t)
            else:
                dense.append(b[None, :])
        pts = np.vstack(dense)
        pts_all.append(pts)
        owner_all.append(np.full(len(pts), idx, dtype=int))
    if not pts_all:
        cloud = (np.zeros((0, 2)), np.zeros(0, dtype=int))
    else:
        cloud = (np.vstack(pts_all), np.concatenate(owner_all))
    ax.__dict__["_wl_curve_cloud"] = cloud
    return cloud


def _legend_overlap_score(ax, rect) -> float:
    """How many plotted curves the legend box would cover, in axes coordinates.

    Counts CURVES first, not points: the thing to avoid is hiding a series at all, so a box
    grazing two lines is worse than one sitting on a single line for longer. The point term
    only breaks ties between placements that cover the same number of curves.
    """
    import numpy as np  # noqa: WPS433

    score = 0.0
    # Bars are areas, not polylines: test them as rectangles so the score means the same
    # thing on a grouped-bar figure as on a CDF. Visible labels and marker scatters count
    # too — a legend box over a rotated "80" hides data just as effectively as one over a
    # curve, and that is what the LIMIT panel's note was sitting on.
    fig = ax.figure
    fig.canvas.draw()
    rend = fig.canvas.get_renderer()
    inv = ax.transAxes.inverted()
    for txt in ax.texts:
        if not txt.get_visible() or not txt.get_text().strip():
            continue
        bb = txt.get_window_extent(rend).transformed(inv)
        if bb.x0 < rect[2] and bb.x1 > rect[0] and bb.y0 < rect[3] and bb.y1 > rect[1]:
            score += 1.0
    for coll in ax.collections:
        if not coll.get_visible():
            continue
        try:
            bb = coll.get_window_extent(rend).transformed(inv)
        except Exception:                                        # noqa: BLE001
            continue
        if bb.x0 < rect[2] and bb.x1 > rect[0] and bb.y0 < rect[3] and bb.y1 > rect[1]:
            score += 1.0
    to_axes = (ax.transData + ax.transAxes.inverted()).transform
    for patch in ax.patches:
        if type(patch).__name__ != "Rectangle" or not patch.get_visible():
            continue
        (bx0, by0), (bx1, by1) = to_axes([
            (patch.get_x(), patch.get_y()),
            (patch.get_x() + patch.get_width(), patch.get_y() + patch.get_height()),
        ])
        if (min(bx0, bx1) < rect[2] and max(bx0, bx1) > rect[0]
                and min(by0, by1) < rect[3] and max(by0, by1) > rect[1]):
            score += 1.0

    pts, owners = _curve_cloud(ax)
    if len(pts) == 0:
        return score
    inside = ((pts[:, 0] >= rect[0]) & (pts[:, 0] <= rect[2])
              & (pts[:, 1] >= rect[1]) & (pts[:, 1] <= rect[3]))
    if not inside.any():
        return score
    hit_curves = int(np.unique(owners[inside]).size)
    return score + hit_curves + min(int(inside.sum()), 999) / 1000.0


def _clear_anchor_for_box(ax, width: float, height: float, prefer):
    """Slide a box of this size over the axes and return the emptiest upper-left anchor.

    Named corners are where a legend belongs, and they are tried first by the caller. This is
    the fallback for a plot like the MCV CDF where every corner has a curve in it: scan the
    whole area on a fine grid, keep the positions that cover nothing, and among those take the
    one nearest the position the figure asked for. Returns ``None`` when nothing is clear.
    """
    import numpy as np  # noqa: WPS433

    margin = 0.012
    step = 0.01
    xs = np.arange(margin, max(margin, 1.0 - margin - width) + 1e-9, step)
    ys = np.arange(margin + height, min(1.0 - margin, 1.0) + 1e-9, step)
    if xs.size == 0 or ys.size == 0:
        return None
    best, best_cost = None, None
    for x0 in xs:
        for y_top in ys:
            rect = (x0, y_top - height, x0 + width, y_top)
            if _legend_overlap_score(ax, rect) > 0.0:
                continue
            cost = (x0 - prefer[0]) ** 2 + (y_top - prefer[1]) ** 2
            if best_cost is None or cost < best_cost:
                best, best_cost = (float(x0), float(y_top)), cost
    return best


def _legend_with_isolated_note(
    ax,
    handles: Sequence[Any],
    labels: Sequence[str],
    note: Any,
    *,
    loc: str,
    ncol: int,
    note_marker: Optional[str] = None,
    fontsize: Optional[float] = None,
    note_fontsize: Optional[float] = None,
    note_scale: float = _NOTE_SCALE,
    auto_place: bool = False,
) -> None:
    """Draw the entry grid plus ``note`` as a smaller, wrapped block beneath it.

    matplotlib fills legends column-major, so an explanatory note appended as a normal entry
    lands awkwardly beside an unrelated label. Instead we render two frameless legends -- the
    bench grid, then the note pinned just beneath it -- and wrap both in a single rounded
    white box so the note reads as the legend's last line rather than a grid cell.

    ``note`` is one string or a sequence of paragraphs, each starting on its own line. Notes
    are set at ``note_scale`` of the grid's font size and wrapped to the width of the grid
    **measured without them**, so an explanation can lengthen the box but never widen it.

    With ``auto_place`` the finished box -- background included -- is tested against every
    curve at each candidate location, and the first one that covers nothing wins. ``loc`` is
    tried first, so a figure only moves its legend when the requested corner sits on data.
    """
    from matplotlib.lines import Line2D  # noqa: WPS433
    from matplotlib.patches import FancyBboxPatch  # noqa: WPS433

    paragraphs = [note] if isinstance(note, str) else [p for p in note if p]
    fig = ax.figure
    fig.canvas.draw()
    rend = fig.canvas.get_renderer()

    def build(place: Any) -> Dict[str, Any]:
        # borderaxespad insets the grid from the axes edge so the wrapping box keeps a tiny
        # margin off the spine instead of sitting on it. ``place`` is either one of
        # matplotlib's location names or an explicit (x, y_top) anchor in axes coordinates.
        main_kw: Dict[str, Any] = dict(
            ncol=ncol, frameon=False, fancybox=True,
            handlelength=1.2, handletextpad=0.4, borderpad=0.4,
            labelspacing=0.3, columnspacing=1.0,
        )
        box_tr = ax.transAxes
        if isinstance(place, str):
            main_kw.update(loc=place, borderaxespad=1.0)
        else:
            main_kw.update(loc="upper left", borderaxespad=0.0,
                           bbox_to_anchor=tuple(place), bbox_transform=ax.transAxes)
        if fontsize is not None:
            main_kw["fontsize"] = fontsize
        leg_main = ax.legend(list(handles), list(labels), **main_kw)
        ax.add_artist(leg_main)

        fig.canvas.draw()
        inv = box_tr.inverted()
        bb_disp = leg_main.get_window_extent(rend)
        main_fs = leg_main._fontsize
        px_per_pt = fig.dpi / 72.0
        pad_px = leg_main.borderpad * main_fs * px_per_pt
        row_gap_px = 0.3 * main_fs * px_per_pt
        note_fs = note_fontsize if note_fontsize is not None else round(main_fs * note_scale, 2)

        # The budget is the grid's own content width -- the box as it stands WITHOUT any note.
        avail_px = bb_disp.width - 2 * pad_px
        hl, htp = (1.2, 0.4) if note_marker else (0.0, 0.0)
        if note_marker:
            avail_px -= (hl + htp) * note_fs * px_per_pt
        lines: List[str] = []
        for para in paragraphs:
            lines.extend(_wrap_to_width(fig, rend, para, note_fs, avail_px))
        if os.environ.get("WL_LEGEND_DEBUG"):
            print(f"[legend] place={place} grid_px={bb_disp.width:.1f} pad={pad_px:.1f} "
                  f"avail={avail_px:.1f} main_fs={main_fs} note_fs={note_fs} "
                  f"lines={lines}", flush=True)

        artists: List[Any] = [leg_main]
        if lines:
            # Pin the note flush under the last grid row: align x with the grid's content
            # (inside the left pad) and pull y up so only one normal row-gap separates it.
            anchor = inv.transform((bb_disp.x0 + pad_px, bb_disp.y0 + pad_px - row_gap_px))
            if note_marker:
                note_handle = Line2D([], [], linestyle="none", marker=note_marker,
                                     markersize=8, markerfacecolor="#1A1A1A",
                                     markeredgecolor="white", markeredgewidth=0.3)
            else:
                note_handle = Line2D([], [], linestyle="none", marker="none")
            leg_note = ax.legend(
                [note_handle], ["\n".join(lines)],
                loc="upper left", bbox_to_anchor=(anchor[0], anchor[1]),
                bbox_transform=box_tr, frameon=False,
                handlelength=hl, handletextpad=htp, borderpad=0.0,
                labelspacing=0.3, fontsize=note_fs,
            )
            artists.append(leg_note)

        fig.canvas.draw()
        boxes = [a.get_window_extent(rend).transformed(inv) for a in artists]
        x0 = min(b.x0 for b in boxes)
        x1 = max(b.x1 for b in boxes)
        y0 = min(b.y0 for b in boxes)
        y1 = max(b.y1 for b in boxes)
        pad = 0.010
        rect = (x0 - pad, y0 - pad, x1 + pad, y1 + pad)
        patch = FancyBboxPatch(
            (rect[0], rect[1]), rect[2] - rect[0], rect[3] - rect[1],
            transform=box_tr,
            boxstyle="round,pad=0,rounding_size=0.015",
            facecolor="white", edgecolor="#bbbbbb",
            linewidth=0.8, zorder=4.0, mutation_aspect=1.0,
        )
        patch.set_clip_on(False)
        ax.add_patch(patch)
        artists.append(patch)

        def destroy() -> None:
            for artist in artists:
                artist.remove()
            if ax.legend_ in artists:
                ax.legend_ = None

        return {"rect": rect, "destroy": destroy, "loc": place}

    order = ((loc,) + tuple(c for c in _LEGEND_LOC_CANDIDATES if c != loc)
             if auto_place else (loc,))

    best: Optional[Dict[str, Any]] = None
    for candidate in order:
        built = build(candidate)
        built["score"] = _legend_overlap_score(ax, built["rect"])
        if best is None or built["score"] < best["score"]:
            if best is not None:
                best["destroy"]()
            best = built
        else:
            built["destroy"]()
        if best["score"] <= 0.0:
            break

    if auto_place and best is not None and best["score"] > 0.0:
        # No named corner is clear -- the MCV CDF is full from corner to corner. Slide a box
        # of exactly this size over the whole axes and take the emptiest spot near the corner
        # the figure asked for. Anchoring is approximate (the legend's own padding shifts it),
        # so re-anchor once against the measured offset before accepting the result.
        x0, y0, x1, y1 = best["rect"]
        w, h = x1 - x0, y1 - y0
        loc_name = str(loc)
        prefer_x = (0.0 if "left" in loc_name
                    else (1.0 - w if "right" in loc_name else 0.5 - w / 2))
        prefer_y = (1.0 if "upper" in loc_name
                    else (h if "lower" in loc_name else 0.5 + h / 2))
        prefer = (prefer_x, prefer_y)
        anchor = _clear_anchor_for_box(ax, w, h, prefer)
        if anchor is not None:
            for _ in range(2):
                candidate_build = build(anchor)
                rect = candidate_build["rect"]
                dx, dy = anchor[0] - rect[0], anchor[1] - rect[3]
                if abs(dx) < 1e-4 and abs(dy) < 1e-4:
                    break
                candidate_build["destroy"]()
                anchor = (anchor[0] + dx, anchor[1] + dy)
            else:
                candidate_build = build(anchor)
            candidate_build["score"] = _legend_overlap_score(ax, candidate_build["rect"])
            if candidate_build["score"] < best["score"]:
                best["destroy"]()
                best = candidate_build
            else:
                candidate_build["destroy"]()

    if best is not None:
        ax.__dict__.setdefault("_wl_legend_placements", []).append(
            {"loc": best["loc"], "rect": best["rect"], "score": best["score"]})
        if os.environ.get("WL_LEGEND_DEBUG"):
            cloud = _curve_cloud(ax)[0]
            print(f"[legend] CHOSEN loc={best['loc']} rect="
                  f"{[round(float(v), 3) for v in best['rect']]} score={best['score']} "
                  f"cloud={len(cloud)} pts", flush=True)


def draw_horizontal_stacked_bars(
    ax,
    rows: Sequence[str],
    categories: Sequence[str],
    matrix: Sequence[Sequence[float]],
    *,
    title: Optional[str] = None,
    xlabel: str = "Share (%)",
    show_xaxis: bool = True,
    annotate_dominant: bool = True,
    annotate_threshold: float = 12.0,
    legend: bool = True,
) -> None:
    """Horizontal stacked bars. One bar per row in ``rows``, segments per category.

    ``matrix[i][j]`` is the percent share of category ``categories[j]`` in row
    ``rows[i]``. Rows that sum to <100 leave whitespace on the right.
    """
    import numpy as np  # noqa: WPS433

    n_rows = len(rows)
    if n_rows == 0:
        ax.set_axis_off()
        return

    y_pos = np.arange(n_rows)
    left = np.zeros(n_rows, dtype=float)
    for j, cat in enumerate(categories):
        widths = np.array([float(matrix[i][j]) for i in range(n_rows)])
        ax.barh(
            y_pos,
            widths,
            left=left,
            height=0.62,
            color=TYPE_BUCKET_COLORS.get(cat, "#888888"),
            label=cat,
            edgecolor="white",
            linewidth=0.4,
        )
        if annotate_dominant:
            fill_hex = TYPE_BUCKET_COLORS.get(cat, "#888888").lstrip("#")
            r, g, b = int(fill_hex[0:2], 16), int(fill_hex[2:4], 16), int(fill_hex[4:6], 16)
            luminance = 0.299 * r + 0.587 * g + 0.114 * b
            text_color = "white" if luminance < 140 else "#222222"
            for i, width in enumerate(widths):
                if width >= annotate_threshold:
                    ax.text(
                        left[i] + width / 2.0,
                        y_pos[i],
                        f"{width:.0f}",
                        ha="center",
                        va="center",
                        fontsize=6.5,
                        color=text_color,
                    )
        left += widths

    ax.set_yticks(y_pos)
    ax.set_yticklabels(list(rows))
    ax.invert_yaxis()
    ax.set_xlim(0, 100)
    if show_xaxis:
        ax.set_xlabel(xlabel)
        ax.set_xticks(range(0, 101, 20))
    else:
        ax.set_xticks([])
    if title:
        set_figure_title(ax, title, pad=4)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    ax.xaxis.grid(True, linestyle="-", linewidth=0.3, alpha=0.4, color="#888888")
    ax.set_axisbelow(True)
    if legend:
        ax.legend(
            loc="upper center",
            bbox_to_anchor=(0.5, -0.18),
            ncol=min(len(categories), 5),
            frameon=False,
            handlelength=1.0,
            handletextpad=0.4,
            columnspacing=1.0,
        )


def draw_multi_line_cdf(
    ax,
    series: Sequence[Tuple[str, Sequence[float], Sequence[float]]],
    *,
    title: Optional[str] = None,
    xlabel: str = "Percentile of columns",
    ylabel: str = "Value (%)",
    legend: bool = True,
    legend_loc: str = "upper left",
    legend_ncol: int = 2,
    legend_fontsize: Optional[float] = None,
    note_fontsize: Optional[float] = None,
    extra_legend_note: Any = None,
) -> None:
    """Plot multiple percentile curves. Baseline labels are auto-dashed.

    ``extra_legend_note`` adds a borderless text-only block to the legend box (handle drawn
    invisibly) — used to fold the "Omitted, all-zero: …" notes into the legend instead of
    floating them as a separate annotation. It is one string or a sequence of paragraphs,
    each starting on its own line; every paragraph is set smaller than the series labels and
    wrapped to the width of the label grid, so a note lengthens the box but never widens it.
    The finished box is then placed where it covers no curve.

    ``note_fontsize`` overrides that 85 % rule with an absolute size. The MCV panel uses it to
    carry the same note size as the NULL panel beside it in Figure 6, which its own smaller
    series labels would otherwise scale away.
    """
    if not series:
        ax.set_axis_off()
        return
    for label, xs, ys in series:
        if not xs or not ys:
            continue
        ax.plot(
            xs,
            ys,
            label=label,
            color=_color_for(label),
            linestyle=_line_style_for(label),
            linewidth=1.8 if label in BASELINE_LABELS else 1.6,
            **curve_style_for(label),
        )
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)
    ax.set_xticks(range(0, 101, 20))
    ax.set_yticks(range(0, 101, 20))
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    if title:
        set_figure_title(ax, title, pad=6)
    ax.grid(True, linestyle=":", linewidth=0.4, alpha=0.5, color="#aaaaaa")
    ax.set_axisbelow(True)
    # Paper-style: keep all 4 spines
    for spine in ax.spines.values():
        spine.set_linewidth(0.6)
    if legend:
        handles, labels = ax.get_legend_handles_labels()
        if extra_legend_note:
            # Fold the "Omitted (all-zero): …" note onto the legend's own
            # isolated last line instead of into the column grid.
            _legend_with_isolated_note(
                ax, handles, labels, extra_legend_note,
                loc=legend_loc, ncol=legend_ncol, note_marker=None,
                fontsize=legend_fontsize, note_fontsize=note_fontsize,
                auto_place=True,
            )
        else:
            legend_kwargs = dict(
                loc=legend_loc,
                frameon=True,
                framealpha=1.0,  # fully opaque: curves never show through the box
                edgecolor="#bbbbbb",
                fancybox=True,
                handlelength=1.4,
                handletextpad=0.4,
                ncol=legend_ncol,
                borderpad=0.4,
                labelspacing=0.3,
                borderaxespad=1.0,
            )
            if legend_fontsize is not None:
                legend_kwargs["fontsize"] = legend_fontsize
            ax.legend(handles, labels, **legend_kwargs)


def draw_distance_bars(
    ax,
    benchmarks: Sequence[str],
    distances: Sequence[float],
    *,
    title: Optional[str] = None,
    ylabel: str = "Mean L1 distance to production (pp)",
    highlight: Optional[str] = None,
    y_max: Optional[float] = None,
) -> None:
    """Vertical bar chart: one bar per benchmark, height = aggregate distance."""
    import numpy as np  # noqa: WPS433
    x = np.arange(len(benchmarks))
    colors = [_color_for(b) for b in benchmarks]
    # Uniform edge width — PROD-DS is set apart by its diagonal hatch alone.
    hatches = ["///" if b == highlight else "" for b in benchmarks]
    bars = ax.bar(x, distances, color=colors, edgecolor="#1A1A1A",
                  linewidth=0.7, hatch=hatches, width=0.7)
    ax.set_xticks(x)
    ax.set_xticklabels(list(benchmarks), rotation=0, ha="center")
    ax.set_ylabel(ylabel)
    if title:
        set_figure_title(ax, title, pad=6)
    ax.yaxis.grid(True, linestyle=":", linewidth=0.4, alpha=0.5, color="#aaaaaa")
    ax.set_axisbelow(True)
    for spine in ax.spines.values():
        spine.set_linewidth(0.6)
    # Headroom above the tallest bar so the value annotation does not collide
    # with the top spine (the JOB label was previously clipping the box).
    top = y_max if y_max is not None else (max(distances) * 1.22 if distances else 1.0)
    ax.set_ylim(0, top)
    for rect, value in zip(bars, distances):
        ax.text(
            rect.get_x() + rect.get_width() / 2.0,
            rect.get_height() + top * 0.015,
            f"{value:.1f}",
            ha="center",
            va="bottom",
            fontsize=8,
        )


def draw_distance_small_multiple(
    ax,
    benchmarks: Sequence[str],
    values: Sequence[float],
    *,
    highlight: Optional[str] = None,
    show_ylabels: bool = True,
    show_xlabel: bool = False,
    x_max: Optional[float] = None,
    title: Optional[str] = None,
) -> None:
    """One faceted panel: horizontal bars of distance for a single signal.

    Bench order is fixed by the caller (same across every panel) so the small
    multiples read as a trellis — y-position alone identifies the benchmark and
    labels need only appear on the left column. ``highlight`` adds the diagonal
    hatch that flags PROD-DS everywhere else in the paper.
    """
    import numpy as np  # noqa: WPS433
    benches = list(benchmarks)
    vals = list(values)
    y = np.arange(len(benches))
    # Spotlight the highlighted bench: a soft band behind its row ties it
    # across panels, peer bars recede (lower alpha), and PROD-DS stays at full
    # strength + hatched so "our solution" is the focal point in every panel —
    # the eye reads at a glance that the orange bar is consistently short
    # (= close to production) across signals.
    _finite = [v for v in vals if v == v]          # v != v  <=> NaN gap
    span = (x_max if x_max else (max(_finite) if _finite else 1.0)) or 1.0
    if highlight in benches:
        h_idx = benches.index(highlight)
        ax.axhspan(h_idx - 0.5, h_idx + 0.5, color=_color_for(highlight),
                   alpha=0.13, zorder=0)
    for i, (bench, val) in enumerate(zip(benches, vals)):
        if val != val:      # NaN = signal not measurable for this benchmark
            continue
        is_h = bench == highlight
        ax.barh(
            i, val,
            color=_color_for(bench),
            edgecolor="#1A1A1A",
            linewidth=0.9 if is_h else 0.6,
            hatch="///" if is_h else "",
            height=0.72,
            alpha=1.0 if is_h else 0.45,
            zorder=3 if is_h else 2,
        )
        if is_h:
            ax.text(val + span * 0.02, i, f"{val:.0f}", va="center", ha="left",
                    fontsize=6.5, fontweight="bold", color="#1A1A1A", zorder=4)
    ax.set_yticks(y)
    if show_ylabels:
        ax.set_yticklabels(list(benchmarks), fontsize=7)
        for tick_label, bench in zip(ax.get_yticklabels(), benchmarks):
            if bench == highlight:
                tick_label.set_fontweight("bold")
    else:
        ax.set_yticklabels([])
    ax.invert_yaxis()  # first (closest) bench on top
    if x_max:
        ax.set_xlim(0, x_max)
    if show_xlabel:
        ax.set_xlabel("L1 dist. (pp)", fontsize=7)
    ax.tick_params(axis="x", labelsize=6.5)
    if title:
        ax.set_title(title, fontsize=8.5, pad=3)
    ax.xaxis.grid(True, linestyle=":", linewidth=0.4, alpha=0.5, color="#aaaaaa")
    ax.set_axisbelow(True)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_linewidth(0.6)


def draw_distance_radar(
    ax,
    benchmarks: Sequence[str],
    signal_labels: Sequence[str],
    distances: Sequence[Sequence[float]],
    *,
    title: Optional[str] = None,
    max_distance: float = 50.0,
) -> None:
    """Radar / spider chart. One axis per signal, one polyline per benchmark.

    ``distances[i][j]`` is the distance between benchmark ``i`` and the production
    baseline on signal ``j`` (in pp). We plot ``max - distance`` so that being
    *closer* to production puts the benchmark *closer to the outer ring*.

    ``ax`` MUST have been created with ``projection='polar'``.
    """
    import numpy as np  # noqa: WPS433
    n_signals = len(signal_labels)
    if n_signals < 3:
        ax.set_axis_off()
        return
    theta = np.linspace(0, 2 * np.pi, n_signals, endpoint=False).tolist()
    theta_closed = theta + [theta[0]]
    for i, bench in enumerate(benchmarks):
        values = [
            max_distance - max(0.0, min(max_distance, float(distances[i][j])))
            if distances[i][j] == distances[i][j] else float("nan")
            for j in range(n_signals)
        ]
        values.append(values[0])
        ax.plot(theta_closed, values, color=_color_for(bench), linewidth=1.2, label=bench,
                linestyle=_line_style_for(bench))
        ax.fill(theta_closed, values, color=_color_for(bench), alpha=0.06)
    ax.set_xticks(theta)
    ax.set_xticklabels(list(signal_labels), fontsize=6.5)
    ax.set_yticks([max_distance * f for f in (0.25, 0.5, 0.75, 1.0)])
    ax.set_yticklabels([f"{max_distance * f:.0f}" for f in (0.25, 0.5, 0.75, 1.0)], fontsize=5.5)
    ax.set_rlabel_position(180 / n_signals)
    ax.set_ylim(0, max_distance)
    if title:
        set_figure_title(ax, title, pad=14)
    ax.legend(
        loc="lower center",
        bbox_to_anchor=(0.5, -0.18),
        ncol=min(len(benchmarks), 5),
        frameon=False,
        fontsize=6.5,
    )


def draw_distance_heatmap(
    ax,
    benchmarks: Sequence[str],
    signal_labels: Sequence[str],
    distances: Sequence[Sequence[float]],
    *,
    title: Optional[str] = None,
    vmax: float = 50.0,
    cbar_label: str = "L1 distance to production (pp)",
    highlight: Optional[str] = None,
) -> None:
    """Heatmap of distances. Lower = closer to production = greener.

    Thin white separators between cells, bold ``highlight`` row label, and a
    slim colorbar give a clean spot-matrix read of the bench × signal grid.
    """
    import numpy as np  # noqa: WPS433
    data = np.asarray(distances, dtype=float)
    import matplotlib as _mpl
    _cmap = _mpl.colormaps["RdYlGn_r"].with_extremes(bad="#f2f2f2")
    im = ax.imshow(np.ma.masked_invalid(np.asarray(data, dtype=float)),
                   cmap=_cmap, vmin=0, vmax=vmax, aspect="auto")
    ax.set_xticks(range(len(signal_labels)))
    ax.set_xticklabels(list(signal_labels), rotation=30, ha="right", fontsize=7.5)
    ax.set_yticks(range(len(benchmarks)))
    ax.set_yticklabels(list(benchmarks), fontsize=8)
    for tick_label, bench in zip(ax.get_yticklabels(), benchmarks):
        if bench == highlight:
            tick_label.set_fontweight("bold")
    # White cell separators (minor-tick grid) for a crisp spot-matrix look.
    ax.set_xticks(np.arange(-0.5, len(signal_labels), 1), minor=True)
    ax.set_yticks(np.arange(-0.5, len(benchmarks), 1), minor=True)
    ax.grid(which="minor", color="white", linewidth=1.2)
    ax.tick_params(which="minor", length=0)
    for i in range(data.shape[0]):
        for j in range(data.shape[1]):
            value = data[i, j]
            if value != value:        # NaN = signal not measurable for this row
                ax.text(j, i, "–", ha="center", va="center",
                        fontsize=6.5, color="#888888")
                continue
            ax.text(j, i, f"{value:.0f}", ha="center", va="center",
                    fontsize=6.5,
                    color="white" if value > vmax * 0.55 else "#1a1a1a")
    if title:
        set_figure_title(ax, title, pad=6)
    cbar = ax.figure.colorbar(im, ax=ax, fraction=0.035, pad=0.02)
    cbar.set_label(cbar_label, fontsize=7)
    cbar.ax.tick_params(labelsize=6.5)
    cbar.outline.set_linewidth(0.5)


def save_paper_pdf(fig, out_path: Path) -> Path:
    """Save a figure as a vector PDF with paper-friendly defaults."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out_path), format="pdf", bbox_inches="tight", pad_inches=0.02)
    return out_path


def _tuck_overflowing_value_labels(ax, labels) -> None:
    """Move a bar's value label inside the bar when it would run past the axes ceiling.

    A rotated "100" above a bar that already reaches 100 can be taller than the headroom, and
    with `clip_on` it prints as "10". Labels that do not fit above their bar are set just
    inside it instead, in whichever of black or white reads on that bar's colour. Values,
    limits and type sizes are untouched, and a label that fits above its bar does not move.
    """
    if not labels:
        return
    import matplotlib.colors as mcolors  # noqa: WPS433

    fig = ax.figure
    fig.canvas.draw()
    rend = fig.canvas.get_renderer()
    to_data = ax.transData.inverted()
    ceiling = ax.get_ylim()[1]
    span = ceiling - ax.get_ylim()[0]
    for artist, value, bar_color in labels:
        bb = artist.get_window_extent(rend)
        if to_data.transform((bb.x0, bb.y1))[1] <= ceiling - span * 0.005:
            continue
        red, green, blue = mcolors.to_rgb(bar_color)
        luminance = 0.299 * red + 0.587 * green + 0.114 * blue
        artist.set_va("top")
        artist.set_position((artist.get_position()[0], value - span * 0.012))
        artist.set_color("white" if luminance < 0.55 else "#1A1A1A")
        artist.set_zorder(7)


def draw_vertical_grouped_bars(
    ax,
    categories: Sequence[str],
    series: Sequence[Tuple[str, Sequence[float]]],
    *,
    title: Optional[str] = None,
    ylabel: str = "Share (%)",
    y_max: float = 100.0,
    highlight: Optional[str] = None,
    na_marker: Optional[Dict[str, str]] = None,
    legend: bool = True,
    legend_loc: str = "below",
    legend_ncol: Optional[int] = None,
    value_labels: bool = True,   # per-bar % labels ON by default
) -> None:
    """Vertical grouped bars (paper Fig 3 style).

    One cluster per ``categories`` entry, one colored bar per ``series`` within
    each cluster. Bench labels come from the series tuples; per-bench colors
    are looked up in ``PAPER_PALETTE``.

    ``highlight`` (a label) adds a thin black outline + thicker edge.
    ``na_marker`` maps a label to a short string (e.g. {"ClickBench": "n/a"})
    rendered just above the x-axis when that series' values are all zero.
    """
    import numpy as np  # noqa: WPS433
    n_cats = len(categories)
    n_series = len(series)
    if n_cats == 0 or n_series == 0:
        ax.set_axis_off()
        return

    x = np.arange(n_cats, dtype=float)
    group_width = 0.82
    bar_width = group_width / n_series

    value_label_artists: List[Tuple[Any, float, str]] = []
    for idx, (label, values) in enumerate(series):
        offset = (idx - (n_series - 1) / 2.0) * bar_width
        color = _color_for(label)
        is_highlight = label == highlight
        is_baseline = label in BASELINE_LABELS
        # Uniform edge width — PROD-DS is set apart by its diagonal hatch alone
        # (stacking a thicker border on top of the hatch is visually noisy).
        edge_width = 0.7
        hatch = "///" if is_highlight else None
        ax.bar(
            x + offset,
            values,
            bar_width,
            label=label,
            color=color,
            edgecolor="#1A1A1A",
            linewidth=edge_width,
            hatch=hatch,
            zorder=3 if is_highlight else 2,
        )
        # Production baselines get a small black star above each non-zero bar
        # so reviewers can spot the "production target" at a glance, even
        # before reading the legend.
        if is_baseline:
            for j in range(n_cats):
                v = values[j]
                if v > 0.5:
                    ax.scatter(
                        x[j] + offset,
                        v + 2.5,
                        marker="*",
                        s=22,
                        color="#1A1A1A",
                        edgecolors="white",
                        linewidths=0.3,
                        zorder=5,
                    )
        # n/a annotation: when a series is empty (all zeros), mark it visually.
        # Drawn in the series' OWN colour (so e.g. ClickBench's "no joins" reads
        # in ClickBench magenta and ties straight back to its legend swatch) and
        # bold at a larger size — the old thin grey 5.5pt text was unreadable at
        # print scale, making the empty slot look like a plain missing bar.
        if na_marker and label in na_marker and all(v == 0 for v in values):
            for j in range(n_cats):
                ax.text(
                    x[j] + offset,
                    2.0,
                    na_marker[label],
                    ha="center",
                    va="bottom",
                    fontsize=8.0,
                    fontweight="bold",
                    color=color,
                    rotation=90,
                    zorder=5,
                )

        # --- opt-in per-bar value labels (default OFF). Sebastian's note: the
        #     small bars had no % on top. Vertical, small, just above each bar;
        #     for production baselines lifted clear of the star marker. ---
        if value_labels:
            for j in range(n_cats):
                v = values[j]
                if v <= 0:
                    continue
                txt = f"{v:.0f}" if v >= 1 else f"{v:.1f}"
                artist = ax.text(
                    x[j] + offset,
                    v + (4.5 if is_baseline else 0.8),
                    txt,
                    ha="center",
                    va="bottom",
                    fontsize=7.5,
                    rotation=90,
                    color="#333333",
                    zorder=6,
                    clip_on=True,
                )
                value_label_artists.append((artist, v, color))

    ax.set_xticks(x)
    ax.set_xticklabels(list(categories))
    ax.set_ylabel(ylabel)
    top = y_max
    if value_labels:
        peak = max((max(vals) for _, vals in series if vals), default=0.0)
        top = max(y_max, peak + 14)  # headroom for the vertical labels above the tallest bars (e.g. 100)
    ax.set_ylim(0, top)
    # Cap tick labels at 100 even when y_max > 100 (the headroom is for the
    # in-plot legend, not for plotted data).
    tick_top = 100 if y_max > 100 else int(y_max)
    ax.set_yticks(range(0, tick_top + 1, 10))
    _tuck_overflowing_value_labels(ax, value_label_artists)
    if title:
        set_figure_title(ax, title, pad=6)
    # Paper-style: keep all 4 spines for a full box around the plot.
    for spine in ax.spines.values():
        spine.set_linewidth(0.6)
    ax.yaxis.grid(True, linestyle=":", linewidth=0.4, alpha=0.5, color="#aaaaaa")
    ax.set_axisbelow(True)
    if legend:
        ncol = legend_ncol or 1
        handles, labels = ax.get_legend_handles_labels()
        # When production baselines are drawn we mark their bars with a ★ and
        # explain it on the legend's own isolated last line (not jammed into a
        # grid cell beside an unrelated bench).
        has_baseline = any(lbl in BASELINE_LABELS for lbl in labels)
        if legend_loc == "below":
            ax.legend(
                handles, labels,
                loc="upper center",
                bbox_to_anchor=(0.5, -0.14),
                ncol=ncol,
                frameon=False,
                handlelength=1.0,
                handletextpad=0.4,
                columnspacing=1.0,
            )
        elif has_baseline:
            _legend_with_isolated_note(
                ax, handles, labels, "reported in production",
                loc=legend_loc, ncol=ncol, note_marker="*",
            )
        else:
            # Paper-style: inside top-right with rounded gray frame.
            ax.legend(
                handles, labels,
                loc=legend_loc,
                frameon=True,
                framealpha=1.0,
                edgecolor="#bbbbbb",
                fancybox=True,
                handlelength=1.2,
                handletextpad=0.4,
                ncol=ncol,
                borderpad=0.4,
                labelspacing=0.3,
                borderaxespad=1.0,
            )
