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


def _line_style_for(label: str) -> str:
    return "--" if label in BASELINE_LABELS else "-"


def _legend_with_isolated_note(
    ax,
    handles: Sequence[Any],
    labels: Sequence[str],
    note: str,
    *,
    loc: str,
    ncol: int,
    note_marker: Optional[str] = None,
    fontsize: Optional[float] = None,
    note_fontsize: Optional[float] = None,
) -> None:
    """Draw the entry grid plus ``note`` on its OWN isolated last line.

    matplotlib fills legends column-major, so an explanatory note appended as a
    normal entry lands awkwardly beside an unrelated label. Instead we render
    two frameless legends — the bench grid, then the note pinned just beneath it
    — and wrap both in a single rounded white box so the note reads as the
    legend's last line rather than a grid cell.
    """
    from matplotlib.lines import Line2D  # noqa: WPS433
    from matplotlib.patches import FancyBboxPatch  # noqa: WPS433

    # borderaxespad insets the grid from the axes edge so the wrapping box
    # keeps a tiny margin off the right/left spine instead of sitting on it.
    main_kw: Dict[str, Any] = dict(
        loc=loc, ncol=ncol, frameon=False, fancybox=True,
        handlelength=1.2, handletextpad=0.4, borderpad=0.4,
        labelspacing=0.3, columnspacing=1.0, borderaxespad=1.0,
    )
    if fontsize is not None:
        main_kw["fontsize"] = fontsize
    leg_main = ax.legend(list(handles), list(labels), **main_kw)
    ax.add_artist(leg_main)

    fig = ax.figure
    fig.canvas.draw()
    rend = fig.canvas.get_renderer()
    inv = ax.transAxes.inverted()
    bb_disp = leg_main.get_window_extent(rend)
    main_fs = leg_main._fontsize
    pad_px = leg_main.borderpad * main_fs * fig.dpi / 72.0
    row_gap_px = 0.3 * main_fs * fig.dpi / 72.0
    # Pin the note flush under the last grid row: align x with the grid's
    # content (inside the left pad) and pull y up so only one normal row-gap
    # separates it — kills the double border-pad gap that made the note read
    # like it sat on a wasted second line.
    anchor_x = bb_disp.x0 + pad_px
    anchor_y = bb_disp.y0 + pad_px - row_gap_px
    ax_ax, ax_ay = inv.transform((anchor_x, anchor_y))

    if note_marker:
        note_handle = Line2D([], [], linestyle="none", marker=note_marker,
                             markersize=8, markerfacecolor="#1A1A1A",
                             markeredgecolor="white", markeredgewidth=0.3)
        hl, htp = 1.2, 0.4
    else:
        note_handle = Line2D([], [], linestyle="none", marker="none")
        hl, htp = 0.0, 0.0

    note_kw: Dict[str, Any] = dict(
        loc="upper left", bbox_to_anchor=(ax_ax, ax_ay),
        bbox_transform=ax.transAxes, frameon=False,
        handlelength=hl, handletextpad=htp, borderpad=0.0,
    )
    note_kw["fontsize"] = note_fontsize if note_fontsize is not None else (fontsize or 8)
    leg_note = ax.legend([note_handle], [note], **note_kw)

    fig.canvas.draw()
    bb1 = leg_main.get_window_extent(rend).transformed(inv)
    bb2 = leg_note.get_window_extent(rend).transformed(inv)
    x0, x1 = min(bb1.x0, bb2.x0), max(bb1.x1, bb2.x1)
    y0, y1 = min(bb1.y0, bb2.y0), max(bb1.y1, bb2.y1)
    pad = 0.010
    patch = FancyBboxPatch(
        (x0 - pad, y0 - pad), (x1 - x0) + 2 * pad, (y1 - y0) + 2 * pad,
        transform=ax.transAxes,
        boxstyle="round,pad=0,rounding_size=0.015",
        facecolor="white", edgecolor="#bbbbbb",
        linewidth=0.8, zorder=4.0, mutation_aspect=1.0,
    )
    ax.add_patch(patch)


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
        ax.set_title(title, pad=4)
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
    extra_legend_note: Optional[str] = None,
) -> None:
    """Plot multiple percentile curves. Baseline labels are auto-dashed.

    ``extra_legend_note`` adds one borderless text-only row to the legend box
    (handle drawn invisibly) — used to fold the "Omitted (all-zero): …" note
    into the legend instead of floating it as a separate annotation.
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
        )
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)
    ax.set_xticks(range(0, 101, 20))
    ax.set_yticks(range(0, 101, 20))
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    if title:
        ax.set_title(title, pad=6)
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
                fontsize=legend_fontsize, note_fontsize=8,
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
        ax.set_title(title, pad=6)
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
    span = (x_max if x_max else (max(vals) if vals else 1.0)) or 1.0
    if highlight in benches:
        h_idx = benches.index(highlight)
        ax.axhspan(h_idx - 0.5, h_idx + 0.5, color=_color_for(highlight),
                   alpha=0.13, zorder=0)
    for i, (bench, val) in enumerate(zip(benches, vals)):
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
        values = [max_distance - max(0.0, min(max_distance, float(distances[i][j]))) for j in range(n_signals)]
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
        ax.set_title(title, pad=14)
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
    im = ax.imshow(data, cmap="RdYlGn_r", vmin=0, vmax=vmax, aspect="auto")
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
            ax.text(j, i, f"{data[i, j]:.0f}", ha="center", va="center",
                    fontsize=6.5,
                    color="white" if data[i, j] > vmax * 0.55 else "#1a1a1a")
    if title:
        ax.set_title(title, pad=6)
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
                ax.text(
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
    if title:
        ax.set_title(title, pad=6)
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
