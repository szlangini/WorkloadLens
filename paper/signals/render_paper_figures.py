"""Render the WorkloadLens paper figures + tables (Sections 3-5).

Reads the per-signal CSVs produced by ``aggregate_signals.py`` and the raw
per-column ``data_metrics.jsonl`` scans, and renders the publication artifacts.

PAPER ARTIFACT MAP (what each output reproduces in the manuscript):
  Figure 2a  Join-key logical types ........ fig_join_key_logical_types.{pdf,png}
  Figure 2b  Aggregate-input logical types . fig_aggregate_logical_types.{pdf,png}
  Figure 3a  NULL fraction CDF ............. fig_null_fraction_cdf.{pdf,png}
  Figure 3b  MCV share CDF ................. fig_mcv_share_cdf.{pdf,png}
  Figure 6a  Schema column types .......... fig_schema_column_types.{pdf,png}
  Figure 6b  GROUP BY key count ........... fig_group_by_key_count.{pdf,png}
  Table 3    LIMIT magnitude (no ORDER BY) . table3_limit_magnitude.{tex,md,csv}

SUPPORTING artifacts (used in the analysis / appendix, not the 13-page body):
  Filter logical types ................... fig_filter_logical_types.{pdf,png}
  LIMIT magnitude (bar form of Table 3) .. fig_limit_magnitude.{pdf,png}
  Closeness-to-production distance summary  fig_production_distance_summary_{A,B,C}

Baselines (where available): Snowflake [27], Amazon Redshift [31], Tableau [33].

All figure styling lives in the WorkloadLens package (``workloadlens.report.
paper``) so the artifact and the tool stay in lock-step.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

import matplotlib  # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from workloadlens.report import paper
from workloadlens.report.aggregate import aggregate_labeled_jsonl

# paper/signals/render_paper_figures.py  ->  paper/
PAPER_DIR = Path(__file__).resolve().parent.parent

# Configurable roots; set in main() from CLI args (defaults point at the
# reproduction working tree so the script is self-contained).
ANALYSES = PAPER_DIR / "work" / "analyses"
DATA_DIR = PAPER_DIR / "work" / "signals"
OUT_DIR = PAPER_DIR / "work" / "figures"

# Query-shape figures (filter / join / aggregate / GROUP BY): JCC-H is kept out
# because its 22 templates are reused verbatim from TPC-H, so it would draw an
# indistinguishable second TPC-H line on AST signals.
BENCHES_7 = [
    ("prodds", "PROD-DS"),
    ("tpcds", "TPC-DS"),
    ("tpch", "TPC-H"),
    ("dsb", "DSB"),
    ("clickbench", "ClickBench"),
    ("job", "JOB"),
    ("redbench", "RedBench"),
]
BENCH_KEYS_7 = [k for k, _ in BENCHES_7]
BENCH_LABELS_7 = [v for _, v in BENCHES_7]
LABEL_BY_KEY = dict(BENCHES_7)

# Data-side figures (NULL CDF, MCV CDF): JCC-H gets its own entry, sourced from
# the skewed (-k) data scan in analyses/jcch_skewed/. Its contribution is
# data-value skew, invisible to AST signals but measurable on per-column MCV.
BENCHES_8_DATA = BENCHES_7 + [("jcch_skewed", "JCC-H")]
BENCH_KEYS_8_DATA = [k for k, _ in BENCHES_8_DATA]
BENCH_LABELS_8_DATA = [v for _, v in BENCHES_8_DATA]


def _read_csv(path: Path) -> List[Dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _bench_rows(path: Path, bench_keys: Sequence[str], categories: Sequence[str]) -> List[List[float]]:
    """Return [bench_idx][category_idx] of percent values, in the order of bench_keys."""
    rows = _read_csv(path)
    by_bench: Dict[str, Dict[str, str]] = {r["bench"]: r for r in rows}
    out: List[List[float]] = []
    for key in bench_keys:
        r = by_bench.get(key)
        if not r:
            out.append([0.0] * len(categories))
            continue
        out.append([float(r.get(c, 0) or 0) for c in categories])
    return out


def _baseline_row(path: Path, source_key: str, categories: Sequence[str]) -> List[float] | None:
    rows = _read_csv(path)
    by_bench = {r["bench"]: r for r in rows}
    r = by_bench.get(source_key)
    if not r:
        return None
    return [float(r.get(c, 0) or 0) for c in categories]


# 4-bucket type categories used by all type-mix figures. NUMBER = INT + DECIMAL.
TYPE_BUCKETS_4: List[str] = ["NUMBER", "TEXT", "DATE", "OTHER"]


def _project_to_4buckets(values_5: List[float]) -> List[float]:
    """Map [INT, TEXT, DECIMAL, DATE, OTHER] -> [NUMBER, TEXT, DATE, OTHER]."""
    if len(values_5) < 5:
        values_5 = list(values_5) + [0.0] * (5 - len(values_5))
    return [
        values_5[0] + values_5[2],  # NUMBER = INT + DECIMAL
        values_5[1],                # TEXT
        values_5[3],                # DATE
        values_5[4],                # OTHER
    ]


# LIMIT-magnitude buckets. The raw CSV carries 9 fine magnitude bins; the figure
# collapses them to 5 readable buckets, while the distance metric and Table 3
# keep the fine bins.
LIMIT_RAW_9: List[str] = ["0", "1-10", "11-100", "101-1K", "1K-10K",
                          "10K-100K", "100K-1M", "1M-10M", ">10M"]
LIMIT_BUCKETS_5: List[str] = ["0", "1-100", "100-10K", "10K-1M", ">1M"]


def _project_limit_5(values_9: List[float]) -> List[float]:
    """Collapse the 9 raw LIMIT-magnitude bins to the 5 displayed buckets."""
    if len(values_9) < 9:
        values_9 = list(values_9) + [0.0] * (9 - len(values_9))
    return [
        values_9[0],                # 0
        values_9[1] + values_9[2],  # 1-100
        values_9[3] + values_9[4],  # 100-10K
        values_9[5] + values_9[6],  # 10K-1M
        values_9[7] + values_9[8],  # >1M
    ]


# ---------------------------------------------------------------------------
# Categorical (vertical grouped bar) figures
# ---------------------------------------------------------------------------

CATEGORICAL_FIGS = [
    {
        "name": "fig_schema_column_types",      # paper Figure 6a
        "csv": "schema_column_types.csv",
        "kind": "type4",
        # Snowflake [27] reports no schema column types (query-workload study);
        # schema baseline = Redshift Table 7 + Tableau Fig 1.
        "baselines": [("redshift", "Redshift"), ("tableau", "Tableau")],
        "title": "Schema Column Type Distribution",
        "na_marker": None,
        "legend_ncol": 3,
        "y_max": 122,
    },
    {
        "name": "fig_filter_logical_types",     # supporting
        "csv": "filter_logical_types.csv",
        "kind": "type4",
        "baselines": [("snowflake", "Snowflake")],
        "title": "Filter Column Logical Types",
        "na_marker": None,
        "legend_ncol": 2,
        "y_max": 108,
    },
    {
        "name": "fig_join_key_logical_types",   # paper Figure 2a
        "csv": "join_key_logical_types.csv",
        "kind": "type4",
        "baselines": [("snowflake", "Snowflake")],
        "title": "Join Key Logical Types",
        # ClickBench: single-table -> has no joins -> annotate "n/a"
        "na_marker": {"ClickBench": "no joins"},
        "legend_ncol": 2,
        "y_max": 108,
    },
    {
        "name": "fig_aggregate_logical_types",  # paper Figure 2b
        "csv": "aggregate_logical_types.csv",
        "kind": "type4",
        "baselines": [("snowflake", "Snowflake")],
        "title": "Aggregation Logical Types",
        "na_marker": None,
        "legend_ncol": 2,
        "y_max": 108,
    },
    {
        "name": "fig_group_by_key_count",       # paper Figure 6b
        "csv": "group_by_key_count.csv",
        "kind": "groupby",
        "categories": ["0", "1-2", "3-5", "6-10", "10+"],
        "baselines": [("snowflake", "Snowflake")],
        "title": "GROUP BY Key Count Distribution",
        "na_marker": None,
        "legend_ncol": 2,
        "y_max": 108,
    },
    {
        "name": "fig_limit_magnitude",          # bar form of Table 3
        "csv": "limit_magnitude.csv",
        "kind": "limit5",
        "baselines": [("snowflake", "Snowflake")],
        "title": "LIMIT Magnitude Distribution",
        "na_marker": None,
        "legend_ncol": 2,
        "y_max": 162,
    },
]


def render_categorical_figure(spec: dict) -> Path:
    csv_path = DATA_DIR / spec["csv"]
    if spec.get("kind") == "type4":
        raw_cats = ["INT", "TEXT", "DECIMAL", "DATE", "OTHER"]
        bench_matrix_5 = _bench_rows(csv_path, BENCH_KEYS_7, raw_cats)
        bench_matrix = [_project_to_4buckets(row) for row in bench_matrix_5]
        categories = TYPE_BUCKETS_4
        baseline_rows = []
        for baseline_key, baseline_label in spec["baselines"]:
            b_raw = _baseline_row(csv_path, baseline_key, raw_cats)
            if b_raw is not None:
                baseline_rows.append((baseline_label, _project_to_4buckets(b_raw)))
    elif spec.get("kind") == "limit5":
        bench_matrix_9 = _bench_rows(csv_path, BENCH_KEYS_7, LIMIT_RAW_9)
        bench_matrix = [_project_limit_5(row) for row in bench_matrix_9]
        categories = LIMIT_BUCKETS_5
        baseline_rows = []
        for baseline_key, baseline_label in spec["baselines"]:
            b_raw = _baseline_row(csv_path, baseline_key, LIMIT_RAW_9)
            if b_raw is not None:
                baseline_rows.append((baseline_label, _project_limit_5(b_raw)))
    else:
        categories = spec["categories"]
        bench_matrix = _bench_rows(csv_path, BENCH_KEYS_7, categories)
        baseline_rows = []
        for baseline_key, baseline_label in spec["baselines"]:
            b = _baseline_row(csv_path, baseline_key, categories)
            if b is not None:
                baseline_rows.append((baseline_label, b))

    # Display order: Production -> PROD-DS -> TPC-DS -> rest.
    series: List = []
    series.extend(baseline_rows)
    for label, values in zip(BENCH_LABELS_7, bench_matrix):
        series.append((label, values))

    fig, ax = plt.subplots(figsize=(paper.TWO_COL_WIDTH_IN, 2.7))
    paper.draw_vertical_grouped_bars(
        ax,
        categories,
        series,
        title=spec["title"],
        highlight="PROD-DS",
        na_marker=spec.get("na_marker"),
        legend=True,
        legend_loc=spec.get("legend_loc", "upper right"),
        legend_ncol=spec.get("legend_ncol", 2),
        y_max=spec.get("y_max", 110),
    )
    out_path = OUT_DIR / f"{spec['name']}.pdf"
    paper.save_paper_pdf(fig, out_path)
    plt.close(fig)
    return out_path


# ---------------------------------------------------------------------------
# CDF figures (raw column-level data + Redshift overlay)
# ---------------------------------------------------------------------------


def _percentile_curve(values: Sequence[float]) -> Tuple[List[float], List[float]]:
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return [], []
    n = len(vals)
    xs = [100.0 * (i + 1) / n for i in range(n)]
    ys = [100.0 * v for v in vals]
    return xs, ys


def _redshift_overlay(csv_path: Path) -> Tuple[List[float], List[float]]:
    """Read the 6-bucket Redshift baseline and return it as a (x, y) curve."""
    rows = _read_csv(csv_path)
    r = next((row for row in rows if row["bench"] == "redshift"), None)
    if not r:
        return [], []
    thresholds = [(0.01, "share_>=0.01"), (0.10, "share_>=0.10"),
                  (0.30, "share_>=0.30"), (0.50, "share_>=0.50"),
                  (0.70, "share_>=0.70"), (0.90, "share_>=0.90")]
    pts: List[Tuple[float, float]] = []
    for thr, col in thresholds:
        share = float(r.get(col, 0) or 0)
        pts.append((100.0 - share, thr * 100.0))
    pts.sort()
    if pts and pts[-1][0] < 100.0:
        pts.append((100.0, 100.0))
    return [p[0] for p in pts], [p[1] for p in pts]


def render_cdf_figure(
    name: str, accessor, csv_path: Path, title: str, ylabel: str,
    *,
    legend_loc: str = "upper left",
    legend_ncol: int = 3,
    legend_fontsize: float | None = None,
    fig_height: float = 2.8,
) -> Path:
    workloads = _load_workloads()
    series: List[Tuple[str, List[float], List[float]]] = []
    zero_benches: List[str] = []
    for key, label in BENCHES_8_DATA:
        report = workloads.get(label)
        if not report or not report.data_profile:
            continue
        values = accessor(report.data_profile)
        xs, ys = _percentile_curve(values)
        if not xs:
            continue
        if max(ys) < 0.5:
            zero_benches.append(label)
            continue
        series.append((label, xs, ys))
    bxs, bys = _redshift_overlay(csv_path)
    if bxs:
        series.append(("Redshift", bxs, bys))

    note = f"Omitted (all-zero): {', '.join(zero_benches)}" if zero_benches else None

    fig, ax = plt.subplots(figsize=(paper.TWO_COL_WIDTH_IN, fig_height))
    paper.draw_multi_line_cdf(
        ax, series, title=title, ylabel=ylabel,
        legend=True,
        legend_loc=legend_loc, legend_ncol=legend_ncol,
        legend_fontsize=legend_fontsize,
        extra_legend_note=note,
    )
    out_path = OUT_DIR / f"{name}.pdf"
    paper.save_paper_pdf(fig, out_path)
    plt.close(fig)
    return out_path


_WORKLOADS_CACHE = None


def _load_workloads():
    global _WORKLOADS_CACHE
    if _WORKLOADS_CACHE is not None:
        return _WORKLOADS_CACHE
    union = {label: key for key, label in BENCHES_8_DATA}
    coverage_map = {label: [ANALYSES / key / "coverage.jsonl"] for label, key in union.items()}
    data_map = {label: [ANALYSES / key / "data_metrics.jsonl"] for label, key in union.items()}
    _WORKLOADS_CACHE = aggregate_labeled_jsonl(coverage_map, data_map)
    return _WORKLOADS_CACHE


# ---------------------------------------------------------------------------
# Table 3 — LIMIT magnitude (no ORDER BY)
# ---------------------------------------------------------------------------

# Paper Table 3 row selection + order. Production baseline (Snowflake [27]) is
# transcribed into the CSV by aggregate_signals.py from the yaml.
TABLE3_ROWS = [
    ("tpcds", "TPC-DS"),
    ("dsb", "DSB"),
    ("clickbench", "ClickBench"),
    ("snowflake", "Snowflake [27]"),
    ("prodds", "Prod-DS"),
]
TABLE3_BUCKETS = ["0", "1-10", "11-100", "101-1K", "1K-10K",
                  "10K-100K", "100K-1M", "1M-10M", ">10M"]


def render_table3_limit_magnitude(sf_label: str = "") -> List[Path]:
    """Emit the paper's Table 3 (LIMIT magnitude, no ORDER BY) as csv/md/tex.

    All nine fine magnitude buckets are kept (the manuscript collapses the
    empty 100K-1M column for width only — no data is dropped here). The Prod-DS
    row is scale-dependent: at SF=100 its LIMIT extracts land in 1M-10M and
    reproduce the Snowflake extract mode; at SF=10 they sit ~two decades lower.
    """
    csv_path = DATA_DIR / "limit_magnitude.csv"
    rows = {r["bench"]: r for r in _read_csv(csv_path)}

    def cells(bench_key: str) -> List[str]:
        r = rows.get(bench_key, {})
        return [f"{float(r.get(b, 0) or 0):g}" for b in TABLE3_BUCKETS]

    cap = f" (SF={sf_label})" if sf_label else ""
    out_paths: List[Path] = []

    # --- CSV (machine-readable) -------------------------------------------
    csv_lines = ["dataset," + ",".join(TABLE3_BUCKETS)]
    for key, label in TABLE3_ROWS:
        if key not in rows:
            continue
        csv_lines.append(label.replace(",", "") + "," + ",".join(cells(key)))
    p = OUT_DIR / "table3_limit_magnitude.csv"
    p.write_text("\n".join(csv_lines) + "\n")
    out_paths.append(p)

    # --- Markdown ----------------------------------------------------------
    md = [f"**Table 3: LIMIT magnitude (no ORDER BY){cap}.** "
          "Percent of LIMIT clauses per magnitude bucket.", "",
          "| Dataset | " + " | ".join(TABLE3_BUCKETS) + " |",
          "|" + "---|" * (len(TABLE3_BUCKETS) + 1)]
    for key, label in TABLE3_ROWS:
        if key not in rows:
            continue
        md.append(f"| {label} | " + " | ".join(cells(key)) + " |")
    p = OUT_DIR / "table3_limit_magnitude.md"
    p.write_text("\n".join(md) + "\n")
    out_paths.append(p)

    # --- LaTeX (booktabs) --------------------------------------------------
    colspec = "l" + "r" * len(TABLE3_BUCKETS)
    tex = [r"\begin{table}[t]", r"  \centering",
           rf"  \caption{{LIMIT magnitude (no \texttt{{ORDER BY}}){cap}~\cite{{snowflake}}.}}",
           r"  \label{tab:limit-magnitude}",
           rf"  \begin{{tabular}}{{{colspec}}}", r"    \toprule",
           "    Dataset & " + " & ".join(b.replace(">", r"$>$") for b in TABLE3_BUCKETS) + r" \\",
           r"    \midrule"]
    for key, label in TABLE3_ROWS:
        if key not in rows:
            continue
        tex.append("    " + label + " & " + " & ".join(cells(key)) + r" \\")
    tex += [r"    \bottomrule", r"  \end{tabular}", r"\end{table}"]
    p = OUT_DIR / "table3_limit_magnitude.tex"
    p.write_text("\n".join(tex) + "\n")
    out_paths.append(p)

    return out_paths


# ---------------------------------------------------------------------------
# Supporting: closeness-to-production distance summary (3 variants)
# ---------------------------------------------------------------------------

DISTANCE_SIGNALS = [
    ("Schema types",   "schema_column_types.csv",       "redshift",  ["INT", "TEXT", "DECIMAL", "DATE", "OTHER"], True),
    ("Filter types",   "filter_logical_types.csv",      "snowflake", ["INT", "TEXT", "DECIMAL", "DATE", "OTHER"], True),
    ("Join-key types", "join_key_logical_types.csv",    "snowflake", ["INT", "TEXT", "DECIMAL", "DATE", "OTHER"], True),
    ("Agg. types",     "aggregate_logical_types.csv",   "snowflake", ["INT", "TEXT", "DECIMAL", "DATE", "OTHER"], True),
    ("GROUP BY keys",  "group_by_key_count.csv",        "snowflake", ["0", "1-2", "3-5", "6-10", "10+"], False),
    ("LIMIT magn.",    "limit_magnitude.csv",           "snowflake",
        ["0", "1-10", "11-100", "101-1K", "1K-10K", "10K-100K", "100K-1M", "1M-10M", ">10M"], False),
    ("NULL fraction",  "null_fraction_distribution.csv","redshift",
        ["share_>=0.01", "share_>=0.10", "share_>=0.30", "share_>=0.50", "share_>=0.70", "share_>=0.90"], False),
    ("MCV share",      "mcv_share_distribution.csv",    "redshift",
        ["share_>=0.01", "share_>=0.10", "share_>=0.30", "share_>=0.50", "share_>=0.70", "share_>=0.90"], False),
]


def _l1_distance(a: Sequence[float], b: Sequence[float]) -> float:
    n = min(len(a), len(b))
    if n == 0:
        return 0.0
    return sum(abs(float(a[i]) - float(b[i])) for i in range(n)) / n


def _jcch_skewed_cdf_row(signal_csv: str) -> List[float] | None:
    workloads = _load_workloads()
    report = workloads.get("JCC-H")
    if not report or not report.data_profile:
        return None
    profile = report.data_profile
    if "null" in signal_csv:
        values = profile.null_fractions()
    elif "mcv" in signal_csv:
        values = profile.max_mcv_fractions()
    else:
        return None
    if not values:
        return None
    thresholds = [0.01, 0.10, 0.30, 0.50, 0.70, 0.90]
    n = len(values)
    return [round(100.0 * sum(1 for v in values if v >= t) / n, 2) for t in thresholds]


def _compute_distance_matrix() -> Tuple[List[str], List[str], List[List[float]]]:
    signal_labels = [s[0] for s in DISTANCE_SIGNALS]
    bench_keys_8 = list(BENCH_KEYS_7) + ["jcch"]
    bench_labels_8 = list(BENCH_LABELS_7) + ["JCC-H"]
    distances: List[List[float]] = []
    for key in bench_keys_8:
        row: List[float] = []
        for (_label, fname, baseline_key, cats, collapse) in DISTANCE_SIGNALS:
            csv_path = DATA_DIR / fname
            if not csv_path.exists():
                row.append(0.0)
                continue
            baseline_vec = _baseline_row(csv_path, baseline_key, cats)
            if baseline_vec is None:
                row.append(0.0)
                continue
            if key == "jcch" and ("null" in fname or "mcv" in fname):
                bench_vec = _jcch_skewed_cdf_row(fname)
                if bench_vec is None:
                    bench_vec = _bench_rows(csv_path, [key], cats)[0]
            else:
                bench_vec = _bench_rows(csv_path, [key], cats)[0]
            if collapse:
                bench_vec = _project_to_4buckets(bench_vec)
                baseline_vec = _project_to_4buckets(baseline_vec)
            row.append(_l1_distance(bench_vec, baseline_vec))
        distances.append(row)
    return bench_labels_8, signal_labels, distances


def render_distance_summary_A() -> Path:
    bench_labels, _signal_labels, distances = _compute_distance_matrix()
    means = [sum(row) / len(row) if row else 0.0 for row in distances]
    paired = sorted(zip(bench_labels, means), key=lambda p: p[1])
    sorted_labels = [p[0] for p in paired]
    sorted_means = [p[1] for p in paired]

    fig, ax = plt.subplots(figsize=(paper.TWO_COL_WIDTH_IN, 2.4))
    paper.draw_distance_bars(
        ax, sorted_labels, sorted_means,
        title="Closeness to Production: Mean L1 Distance per Benchmark",
        ylabel="Mean L1 distance (pp)",
        highlight="PROD-DS",
    )
    out_path = OUT_DIR / "fig_production_distance_summary_A.pdf"
    paper.save_paper_pdf(fig, out_path)
    plt.close(fig)
    return out_path


def render_distance_summary_B() -> Path:
    bench_labels, signal_labels, distances = _compute_distance_matrix()
    means = [sum(row) / len(row) if row else 0.0 for row in distances]
    order = sorted(range(len(bench_labels)), key=lambda i: means[i])
    benches = [bench_labels[i] for i in order]
    dist_by_bench = [distances[i] for i in order]
    x_max = max((max(row) for row in distances if row), default=1.0) * 1.12

    ncols, nrows = 4, 2
    fig, axes = plt.subplots(nrows, ncols,
                             figsize=(paper.TWO_COL_WIDTH_IN, 3.9),
                             sharex=True)
    for s_idx, signal in enumerate(signal_labels):
        r, c = divmod(s_idx, ncols)
        ax = axes[r][c]
        vals = [dist_by_bench[b_idx][s_idx] for b_idx in range(len(benches))]
        paper.draw_distance_small_multiple(
            ax, benches, vals,
            highlight="PROD-DS",
            show_ylabels=(c == 0),
            show_xlabel=(r == nrows - 1),
            x_max=x_max,
            title=signal,
        )
    fig.suptitle(
        "Per-Signal L1 Distance to Production (Lower = Closer; PROD-DS hatched)",
        fontsize=10, fontweight="bold",
    )
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    out_path = OUT_DIR / "fig_production_distance_summary_B.pdf"
    paper.save_paper_pdf(fig, out_path)
    plt.close(fig)
    return out_path


def render_distance_summary_C() -> Path:
    bench_labels, signal_labels, distances = _compute_distance_matrix()
    means = [sum(row) / len(row) if row else 0.0 for row in distances]
    order = sorted(range(len(bench_labels)), key=lambda i: means[i])
    bench_sorted = [bench_labels[i] for i in order]
    dist_sorted = [distances[i] for i in order]

    fig, ax = plt.subplots(figsize=(paper.TWO_COL_WIDTH_IN, 3.0))
    paper.draw_distance_heatmap(
        ax, bench_sorted, signal_labels, dist_sorted,
        title="Per-Signal L1 Distance to Production (Sorted by Mean; Green = Closer)",
        vmax=30.0, highlight="PROD-DS",
    )
    out_path = OUT_DIR / "fig_production_distance_summary_C.pdf"
    paper.save_paper_pdf(fig, out_path)
    plt.close(fig)
    return out_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv=None) -> int:
    global ANALYSES, DATA_DIR, OUT_DIR
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--analyses-dir", type=Path, default=PAPER_DIR / "work" / "analyses",
                    help="dir with one <bench>/{coverage,data_metrics}.jsonl per benchmark")
    ap.add_argument("--data-dir", type=Path, default=PAPER_DIR / "work" / "signals",
                    help="dir with per-signal CSVs from aggregate_signals.py")
    ap.add_argument("--out-dir", type=Path, default=PAPER_DIR / "work" / "figures",
                    help="dir to write figures + Table 3")
    ap.add_argument("--sf", default="", help="scale-factor label for Table 3 caption (e.g. 10, 100)")
    args = ap.parse_args(argv)

    ANALYSES = args.analyses_dir
    DATA_DIR = args.data_dir
    OUT_DIR = args.out_dir

    paper.apply_paper_style()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"[paper-figures] analyses={ANALYSES}")
    print(f"[paper-figures] data={DATA_DIR}")
    print(f"[paper-figures] writing to {OUT_DIR}")
    written: List[Path] = []

    # Figure 2 (a,b), Figure 6 (a,b), supporting filter + limit-bar.
    for spec in CATEGORICAL_FIGS:
        path = render_categorical_figure(spec)
        written.append(path)
        print(f"  {path.name:50s} {path.stat().st_size:>7d} B")

    # Figure 3a — NULL fraction CDF.
    path = render_cdf_figure(
        "fig_null_fraction_cdf",
        lambda profile: profile.null_fractions(),
        DATA_DIR / "null_fraction_distribution.csv",
        title="NULL Fraction Distribution",
        ylabel="NULL fraction (%)",
        legend_loc="upper left",
        legend_ncol=2,
    )
    written.append(path)
    print(f"  {path.name:50s} {path.stat().st_size:>7d} B")

    # Figure 3b — MCV share CDF.
    path = render_cdf_figure(
        "fig_mcv_share_cdf",
        lambda profile: profile.max_mcv_fractions(),
        DATA_DIR / "mcv_share_distribution.csv",
        title="MCV Share Distribution",
        ylabel="MCV share (%)",
        legend_loc="upper left",
        legend_ncol=2,
        legend_fontsize=7.0,
    )
    written.append(path)
    print(f"  {path.name:50s} {path.stat().st_size:>7d} B")

    # Supporting: distance summary (3 variants).
    for renderer in (render_distance_summary_A, render_distance_summary_B, render_distance_summary_C):
        try:
            path = renderer()
            written.append(path)
            print(f"  {path.name:50s} {path.stat().st_size:>7d} B")
        except Exception as exc:  # supporting figures must never block the main set
            print(f"  [warn] {renderer.__name__} skipped: {exc}")

    # Table 3 — LIMIT magnitude (csv/md/tex).
    for p in render_table3_limit_magnitude(sf_label=args.sf):
        written.append(p)
        print(f"  {p.name:50s} {p.stat().st_size:>7d} B")

    print(f"[paper-figures] wrote {len(written)} files to {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
