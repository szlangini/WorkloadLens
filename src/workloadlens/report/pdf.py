from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path
import statistics
from typing import Any, Callable, Dict, Iterable, List, Tuple, Optional, Set, Sequence

from ..utils.math import counter_shares
from ..datafiles import DataProfile
from dataclasses import dataclass

from .aggregate import AggregatedReport, AggregatedStats, AggregatedReportMap, SchemaProfile

DEFAULT_PALETTE = [
    "#5BC0EB",  # cyan
    "#FF8C00",  # orange
    "#9BC53D",  # green
    "#C3423F",  # red
    "#404E4D",  # charcoal
    "#7B68EE",  # medium slate blue / purple
    "#CCBB44",  # Tol yellow
    "#CC79A7",  # Wong reddish purple
    "#0072B2",  # Wong deep blue
    "#117733",  # Tol darker green
    "#44AA99",  # Tol teal
    "#DDAA33",  # Tol gold
]
GROUPED_BAR_FIGSIZE = (8.5, 6.2)
GROUPED_BAR_TICK_FONTSIZE = 15
GROUPED_BAR_VALUE_FONTSIZE = 13
GROUPED_BAR_LEGEND_FONTSIZE = 14
GROUPED_BAR_LABEL_FONTSIZE = 17
GROUPED_BAR_TITLE_FONTSIZE = 19
GROUPED_BAR_SHOW_PERCENT = False
# Thresholds for adapting the renderer to high-cardinality comparisons.
HORIZONTAL_BAR_THRESHOLD = 6        # N at which we pivot to horizontal bars
LEGEND_OUTSIDE_THRESHOLD = 6        # N at which the legend always goes below the axes
MIN_BAR_WIDTH = 0.05                # absolute floor for grouped-bar width
VALUE_LABEL_MIN_WIDTH = 0.10        # drop in-bar value labels below this width
ADAPTIVE_FONT_PIVOT = 4             # font scale = min(1, ADAPTIVE_FONT_PIVOT / N)
BASELINE_LABEL_KEYS = ("snowflake", "redshift", "production", "realworld", "amazon redshift")
BASELINE_BAR_COLOR = "#444444"
BASELINE_BAR_HATCH = "//"
BASELINE_LINE_COLOR = "#333333"
BASELINE_LINESTYLE = "--"
CDF_LINESTYLE_CYCLE_THRESHOLD = 4   # below this, all lines are solid
CDF_LINESTYLE_CYCLE = ("-", "--", "-.", ":")
CDF_FIGSIZE = (8.5, 4.9)
CDF_TICK_FONTSIZE = 15
CDF_LABEL_FONTSIZE = 17
CDF_TITLE_FONTSIZE = 19
CDF_LEGEND_FONTSIZE = 14

WORKLOAD_LABEL_ALIASES = {
    "tpch": "TPC-H",
    "tpcds": "TPC-DS",
    "prodds": "PROD-DS",
    "realworld": "PRODUCTION",
    "workloadstudies": "WORKLOAD STUDIES",
}

SERIES_LABEL_OVERRIDES: Dict[str, Dict[str, str]] = {
    "logical_types": {
        "production": "Snowflake",
        "realworld": "Snowflake",
    },
    "snowflake_series": {
        "production": "Snowflake",
        "realworld": "Snowflake",
    },
    "redshift_data_curves": {
        "production": "Amazon Redshift",
        "realworld": "Amazon Redshift",
    },
}


def _palette_for_workloads(palette: Iterable[str], count: int) -> List[str]:
    palette_list = list(palette)
    if not palette_list:
        palette_list = list(DEFAULT_PALETTE)
    if len(palette_list) < count:
        palette_list = palette_list + _extension_palette(count - len(palette_list), seed=len(palette_list))
    return palette_list[:count]


def _extension_palette(needed: int, *, seed: int = 0) -> List[str]:
    """Return matplotlib tab20 hex colours, offset by `seed`, to fill in beyond the curated palette."""
    try:
        import matplotlib

        cmap = matplotlib.colormaps.get_cmap("tab20")
    except Exception:
        return [DEFAULT_PALETTE[i % len(DEFAULT_PALETTE)] for i in range(needed)]
    colours: List[str] = []
    for i in range(needed):
        r, g, b, _ = cmap((seed + i) % 20 / 20.0)
        colours.append("#{:02X}{:02X}{:02X}".format(int(r * 255), int(g * 255), int(b * 255)))
    return colours


def _is_baseline_label(label: str) -> bool:
    """Treat published production-baseline series (Snowflake, Redshift, …) as references, not benchmarks."""
    if not label:
        return False
    lo = str(label).lower()
    return any(key in lo for key in BASELINE_LABEL_KEYS)


def _adaptive_grouped_figsize(n_categories: int, n_series: int, base: Tuple[float, float] = GROUPED_BAR_FIGSIZE) -> Tuple[float, float]:
    """Grow the figure with both axes' cardinality so bars/labels keep room to breathe."""
    base_w, base_h = base
    width = max(base_w, 1.0 * n_categories + 0.6 * n_series)
    height = max(base_h, 0.45 * n_categories + 0.2 * n_series)
    return width, height


def _adaptive_horizontal_figsize(n_categories: int, n_series: int, base: Tuple[float, float] = GROUPED_BAR_FIGSIZE) -> Tuple[float, float]:
    """Horizontal layout: width tracks value range, height tracks (categories × series)."""
    base_w, base_h = base
    width = max(base_w, 8.5)
    height = max(base_h, 0.45 * n_categories * max(1.0, n_series / 4.0) + 1.0)
    return width, height


def _font_scale(n_series: int) -> float:
    """Shrink fonts as the number of series grows past the pivot point."""
    if n_series <= ADAPTIVE_FONT_PIVOT:
        return 1.0
    return max(0.6, ADAPTIVE_FONT_PIVOT / float(n_series))

FEATURE_LABELS = {
    "has_order_by": "Order By",
    "has_join": "Join",
    "has_limit": "Limit",
    "has_union": "Union All",
    "has_group_by": "Group By",
    "has_window": "Window Functions",
    "has_cte": "CTEs",
}

LENGTH_BINS = [
    ("<10B", 10),
    ("<100B", 100),
    ("<500B", 500),
    ("<1KB", 1024),
    ("<10KB", 10 * 1024),
    ("<100KB", 100 * 1024),
    ("<1MB", 1024 * 1024),
    ("<10MB", 10 * 1024 * 1024),
    (">10MB", None),
]

OPERATOR_TOTAL_BINS = [
    ("1-10", 10),
    ("11-100", 100),
    ("101-1K", 1000),
    ("1K-10K", 10_000),
    (">=10K", None),
]

EXPRESSION_DISTRIBUTION_BINS = [
    ("0", 1),
    ("1-10", 11),
    ("11-100", 101),
    ("101-1K", 1001),
    (">=1K", None),
]

DEPTH_BINS = [
    ("1-2", 3),
    ("3-5", 6),
    ("6-10", 11),
    ("11-100", 101),
    ("100+", None),
]

JOIN_COUNT_BUCKETS = [
    ("0", 0, 0),
    ("1", 1, 1),
    ("2", 2, 2),
    ("3", 3, 3),
    ("4", 4, 4),
    ("5", 5, 5),
    ("6", 6, 6),
    ("7", 7, 7),
    ("8+", 8, None),
]

TYPE_BUCKET_MAP = {
    "INT": "Number",
    "DECIMAL": "Number",
    "DOUBLE": "Number",
    "FLOAT": "Number",
    "REAL": "Number",
    "BOOLEAN": "Boolean",
    "TEXT": "Text",
    "CHAR": "Text",
    "VARCHAR": "Text",
    "DATE": "Date",
    "TIMESTAMP": "Timestamp",
    "TIME": "Time",
}

JOIN_CONDITIONAL_BINS = [
    ("1-2", 1, 2),
    ("3-5", 3, 5),
    ("6-10", 6, 10),
    ("11-20", 11, 20),
    (">20", 21, None),
]

UNION_CONDITIONAL_BINS = [
    ("1", 1, 1),
    ("2", 2, 2),
    ("3-4", 3, 4),
    ("5-7", 5, 7),
    ("8-10", 8, 10),
    ("11-20", 11, 20),
    ("21-50", 21, 50),
    (">50", 51, None),
]

UNION_FAN_IN_BINS = [
    ("1-2", 1, 2),
    ("3-5", 3, 5),
    ("6-10", 6, 10),
    ("11-100", 11, 100),
    ("101-1000", 101, 1000),
    ("1000+", 1001, None),
]

GROUP_BY_KEY_BINS = [
    ("0", 0, 0),
    ("1-2", 1, 2),
    ("3-5", 3, 5),
    ("6-10", 6, 10),
    ("10+", 11, None),
]

LIMIT_WITH_ORDER_BINS = [
    ("0", 0, 0),
    ("1-10", 1, 10),
    ("11-100", 11, 100),
    ("101-1K", 101, 1_000),
    ("1K-10K", 1_001, 10_000),
    ("10K-100K", 10_001, 100_000),
    ("100K+", 100_001, None),
]

LIMIT_NO_ORDER_BINS = [
    ("0", 0, 0),
    ("1-10", 1, 10),
    ("11-100", 11, 100),
    ("101-1K", 101, 1_000),
    ("1K-10K", 1_001, 10_000),
    ("10K-100K", 10_001, 100_000),
    ("100K-1M", 100_001, 1_000_000),
    ("1M-10M", 1_000_001, 10_000_000),
    ("10M-100M", 10_000_001, 100_000_000),
    (">1B", 1_000_000_001, None),
]

TABLE_SIZE_BINS = [
    ("<1MB", 1 * 1024 * 1024),
    ("<10MB", 10 * 1024 * 1024),
    ("<100MB", 100 * 1024 * 1024),
    ("<1GB", 1024 * 1024 * 1024),
    ("<10GB", 10 * 1024 * 1024 * 1024),
    (">=10GB", None),
]

ROW_COUNT_BINS = [
    ("<1K", 1_000),
    ("<10K", 10_000),
    ("<100K", 100_000),
    ("<1M", 1_000_000),
    ("<10M", 10_000_000),
    ("<100M", 100_000_000),
    (">=100M", None),
]

BYTES_PER_ROW_BINS = [
    ("<10B", 10),
    ("<100B", 100),
    ("<1KB", 1_024),
    ("<10KB", 10 * 1_024),
    ("<100KB", 100 * 1_024),
    (">=100KB", None),
]

STRING_LENGTH_BINS = [
    ("<4 chars", 4),
    ("<8 chars", 8),
    ("<16 chars", 16),
    ("<32 chars", 32),
    ("<64 chars", 64),
    (">=64 chars", None),
]

OUTLIER_RATE_BINS = [
    ("0%", 1e-9),
    ("<0.1%", 0.001),
    ("<1%", 0.01),
    ("<5%", 0.05),
    ("<10%", 0.10),
    (">=10%", None),
]

MEAN_QERROR_BINS = [
    ("<1.5x", 1.5),
    ("<2x", 2.0),
    ("<5x", 5.0),
    ("<10x", 10.0),
    ("<100x", 100.0),
    (">=100x", None),
]


def _simple_bins_to_ranges(bins: List[Tuple[str, Optional[float]]]) -> List[Tuple[str, float, Optional[float]]]:
    ranges: List[Tuple[str, float, Optional[float]]] = []
    lower = 0.0
    for label, upper in bins:
        upper_value = float(upper) if upper is not None else None
        ranges.append((label, lower, upper_value))
        if upper_value is not None:
            lower = upper_value
    return ranges


TABLE_SIZE_RANGE_BINS = _simple_bins_to_ranges([(label, float(upper) if upper is not None else None) for label, upper in TABLE_SIZE_BINS])
ROW_COUNT_RANGE_BINS = _simple_bins_to_ranges([(label, float(upper) if upper is not None else None) for label, upper in ROW_COUNT_BINS])
BYTES_PER_ROW_RANGE_BINS = _simple_bins_to_ranges([(label, float(upper) if upper is not None else None) for label, upper in BYTES_PER_ROW_BINS])
STRING_LENGTH_RANGE_BINS = _simple_bins_to_ranges([(label, float(upper) if upper is not None else None) for label, upper in STRING_LENGTH_BINS])
OUTLIER_RATE_RANGE_BINS = _simple_bins_to_ranges([(label, float(upper) if upper is not None else None) for label, upper in OUTLIER_RATE_BINS])
MEAN_QERROR_RANGE_BINS = _simple_bins_to_ranges([(label, float(upper) if upper is not None else None) for label, upper in MEAN_QERROR_BINS])

FILTER_EXPRESSION_LABELS = {
    "equals": "equals",
    "isnotnull": "isnotnull",
    "and": "and",
    "ifthenelse": "ifthenelse",
    "notequal": "notequal",
    "in": "in",
    "not": "not",
    "greatereq": "greatereq",
    "contains": "contains",
    "isnull": "isnull",
    "or": "or",
    "lessthan": "lessthan",
    "greater": "greater",
    "lessequal": "lessequal",
    "between": "between",
    "exists": "exists",
}


def _ensure_matplotlib():
    try:
        import matplotlib  # type: ignore
        matplotlib.use("Agg", force=True)  # headless backend for CLI/test usage
        import matplotlib.pyplot  # type: ignore
        from matplotlib.backends.backend_pdf import PdfPages  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("matplotlib is required to generate PDF reports") from exc
    return matplotlib.pyplot, PdfPages


def _slugify_label(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()
    return slug or "page"


def _display_workload_label(label: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "", label.lower())
    alias = WORKLOAD_LABEL_ALIASES.get(normalized)
    if alias:
        return alias
    return label.replace("_", " ").upper()


def _display_workload_labels(labels: Sequence[str]) -> List[str]:
    display_labels: List[str] = []
    seen: Set[str] = set()
    for label in labels:
        candidate = _display_workload_label(label)
        if candidate in seen:
            candidate = f"{candidate} ({label.replace('_', ' ').upper()})"
        display_labels.append(candidate)
        seen.add(candidate)
    return display_labels


def _normalize_label_key(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", label.lower())


def _legend_labels(labels: Sequence[str], overrides: Optional[Dict[str, str]] = None) -> List[str]:
    if not overrides:
        return list(labels)
    mapped: List[str] = []
    for label in labels:
        mapped.append(overrides.get(_normalize_label_key(label), label))
    return mapped


def _figure_name_hint(fig) -> Optional[str]:
    titles: List[str] = []
    # Prefer axes titles
    for ax in getattr(fig, "axes", []):
        title = ax.get_title()
        if title:
            titles.append(title)
    # Fallback to suptitle or any text drawn on the figure
    suptitle = getattr(fig, "_suptitle", None)
    if suptitle:
        text = getattr(suptitle, "get_text", lambda: "")()
        if text:
            titles.append(text)
    for text_obj in getattr(fig, "texts", []):
        text = getattr(text_obj, "get_text", lambda: "")()
        if text:
            titles.append(text)

    for candidate in titles:
        cleaned = candidate.strip()
        if cleaned:
            return cleaned.split("\n", 1)[0]
    return None


class _FigureExporter:
    def __init__(self, pdf_pages, png_dir: Path | None, prefix: str, png_dpi: int = 200) -> None:
        self._pdf = pdf_pages
        self._png_dir = Path(png_dir) if png_dir else None
        self._prefix = _slugify_label(prefix or "page")
        self._counter = 0
        self._png_dpi = png_dpi

    def savefig(self, fig, *args, **kwargs):
        self._counter += 1
        self._pdf.savefig(fig, *args, **kwargs)
        if not self._png_dir:
            return
        try:
            self._png_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        hint = _figure_name_hint(fig) or "page"
        name = f"{self._prefix}_{self._counter:03d}_{_slugify_label(hint)}.png"
        try:
            # Keep PNG canvas dimensions stable across plots with different text extents.
            # Tight bounding boxes are still used for PDF pages above.
            png_kwargs = dict(kwargs)
            png_kwargs.pop("bbox_inches", None)
            png_kwargs["dpi"] = self._png_dpi
            fig.savefig(self._png_dir / name, **png_kwargs)
        except Exception:
            fig.savefig(self._png_dir / name)

    def __getattr__(self, item):
        return getattr(self._pdf, item)


def _bucket_type(label: str) -> str:
    upper = label.upper()
    if upper in TYPE_BUCKET_MAP:
        return TYPE_BUCKET_MAP[upper]
    return label.replace("_", " ").title()


def _bucket_counter(counter: Counter) -> Counter:
    bucketed = Counter()
    for label, value in counter.items():
        if value <= 0:
            continue
        bucket = _bucket_type(str(label))
        bucketed[bucket] += value
    return bucketed


def _positive_counter(counter: Counter) -> Counter:
    return Counter({key: value for key, value in counter.items() if value > 0})


def _render_clause_heading(pdf, plt, title: str, subtitle: str) -> None:
    # Full-page section divider: each top-level signal section gets its own
    # printable cover so the following charts have a clear visual boundary.
    fig = plt.figure(figsize=(8.5, 11.0))
    fig.patch.set_facecolor("white")
    fig.text(0.5, 0.58, title, ha="center", va="center", fontsize=34, weight="bold", color="#0b66c3")
    fig.text(0.5, 0.50, subtitle, ha="center", va="center", fontsize=14, color="#253648")
    fig.add_artist(plt.Line2D([0.18, 0.82], [0.55, 0.55], color="#0b66c3", linewidth=1.2, transform=fig.transFigure))
    fig.add_artist(plt.Line2D([0.18, 0.82], [0.48, 0.48], color="#0b66c3", linewidth=0.6, transform=fig.transFigure))
    pdf.savefig(fig)
    plt.close(fig)


def _render_order_unique_summary(pdf, plt, unique_counter: Counter) -> None:
    if not unique_counter:
        return
    bucketed = _bucket_counter(unique_counter)
    total = sum(bucketed.values())
    if total <= 0:
        return

    rows = []
    for label, count in bucketed.most_common():
        share = (count / total) * 100
        rows.append((label, f"{count}", f"{share:.1f}%"))

    fig, ax = plt.subplots(figsize=(6.5, 3.0))
    fig.patch.set_facecolor("white")
    ax.axis("off")
    table = ax.table(
        cellText=rows,
        colLabels=["Type", "Queries", "Share"],
        loc="center",
    )
    table.auto_set_font_size(False)
    table.set_fontsize(10)
    table.scale(1.2, 1.3)
    ax.set_title("Order By Logical Types (per query presence)", fontsize=12, weight="bold")
    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


_JOIN_CATEGORY_RULES: List[Tuple[str, str]] = [
    ("SEMI", "Semi"),
    ("ANTI", "Anti"),
    ("INNER", "Inner"),
    ("CROSS", "Inner"),
    ("LEFT", "Outer"),
    ("RIGHT", "Outer"),
    ("FULL", "Outer"),
    ("OUTER", "Outer"),
]


def _join_type_shares(join_counts: Counter) -> Dict[str, float]:
    total = sum(join_counts.values())
    if total <= 0:
        return {}
    grouped = Counter()
    for key, value in join_counts.items():
        name = str(key).upper()
        category = None
        for token, label in _JOIN_CATEGORY_RULES:
            if token in name:
                category = label
                break
        if category:
            grouped[category] += value
    return {label: (count / total) * 100 for label, count in grouped.items() if count > 0}


def _join_count_shares(values: Iterable[int]) -> Dict[str, float]:
    positives = [value for value in values if value > 0]
    if not positives:
        return {}
    labels, percentages = _bin_percentages(positives, JOIN_RANGE_TABLE_BINS)
    return {label: pct for label, pct in zip(labels, percentages) if pct > 0}


def _normalize_type_label_simple(label: str) -> Optional[str]:
    if not label:
        return None
    name = str(label).upper()
    if name.startswith("TIMESTAMP"):
        return "TIMESTAMP"
    if name in {"DECIMAL", "NUMERIC", "INT", "INTEGER", "BIGINT", "SMALLINT", "TINYINT", "NUMBER"}:
        return "NUMBER"
    return name


def _normalize_type_counter(counter: Counter) -> Counter:
    normalized: Counter = Counter()
    for key, value in counter.items():
        if value <= 0:
            continue
        norm = _normalize_type_label_simple(key)
        if not norm:
            continue
        normalized[norm] += value
    return normalized


def _distribution_counts(values: Iterable[float], bins: List[Tuple[str, float, Optional[float]]]) -> Tuple[List[Tuple[str, int]], int]:
    values_list = [value for value in values if value is not None]
    counts = []
    total = 0
    for label, lower, upper in bins:
        bucket_total = 0
        for value in values_list:
            if value < lower:
                continue
            # Treat upper bounds as inclusive to avoid dropping boundary values (e.g., LIMIT=100).
            if upper is not None and value > upper:
                continue
            bucket_total += 1
        counts.append((label, bucket_total))
        total += bucket_total
    return counts, total


def _summary_counts(stats: AggregatedStats) -> Dict[str, int]:
    total = stats.total_queries
    substrait = stats.plan_typed
    parsed_ast = stats.ast_only + stats.schema_statements
    failed = total - (substrait + parsed_ast)
    if failed < 0:
        failed = 0
    return {
        "total": total,
        "parsed_ast": parsed_ast,
        "substrait": substrait,
        "failed": failed,
    }


RENDER_STYLE_BARS = "bars"
RENDER_STYLE_HEATMAP = "heatmap"
RENDER_STYLE_DOT_PLOT = "dot-plot"
RENDER_STYLE_SMALL_MULTIPLES = "small-multiples"
RENDER_STYLE_PAPER = "paper"
RENDER_STYLES = (
    RENDER_STYLE_BARS,
    RENDER_STYLE_HEATMAP,
    RENDER_STYLE_DOT_PLOT,
    RENDER_STYLE_SMALL_MULTIPLES,
    RENDER_STYLE_PAPER,
)


@dataclass
class ReportOptions:
    include_origin_types: bool = True
    scope: str = "all"
    operator_source: str = "AST"
    expect_data_metrics: bool = False
    png_output_dir: Path | None = None
    ndv_ratio_denominator: str = "non_null"
    ndv_column_universe: str = "all"
    ndv_debug: bool = False
    ndv_debug_output: Path | None = None
    render_style: str = RENDER_STYLE_BARS
    paper_preset: Optional[str] = None
    paper_focus_bars: Optional[List[str]] = None
    paper_focus_cdf: Optional[List[str]] = None
    paper_baselines_path: Optional[Path] = None


# Module-level current render style. Set by generate_comparison_pdf / related
# entry points so plot helpers (which don't take ReportOptions today) can
# dispatch without threading the option through every call site.
_CURRENT_RENDER_STYLE: str = RENDER_STYLE_BARS


def _set_render_style(style: Optional[str]) -> None:
    global _CURRENT_RENDER_STYLE
    if style and style in RENDER_STYLES:
        _CURRENT_RENDER_STYLE = style
    else:
        _CURRENT_RENDER_STYLE = RENDER_STYLE_BARS


def _current_render_style() -> str:
    return _CURRENT_RENDER_STYLE


def generate_pdf_report(
    bundle: AggregatedReport,
    output_path: Path,
    palette: Iterable[str] | None = None,
    options: ReportOptions | None = None,
) -> None:
    stats = bundle.all_statements
    if stats.total_queries == 0:
        raise ValueError("No query records available to generate a report")
    stats_queries = bundle.query_statements
    if options is None:
        options = ReportOptions()
    plt, PdfPages = _ensure_matplotlib()

    palette_list = list(palette or DEFAULT_PALETTE)
    if not palette_list:
        palette_list = DEFAULT_PALETTE

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    png_dir = options.png_output_dir
    with PdfPages(str(output_path)) as pdf_pages:
        pdf = _FigureExporter(pdf_pages, png_dir, output_path.stem)
        scope_label = "All statements (DDL + SELECT)" if options.scope != "queries" else "SELECT only"
        summary_operator_counts = stats_queries.operator_counts if stats_queries.operator_counts else stats.operator_counts
        operator_shares_all = counter_shares(summary_operator_counts)
        _render_summary_page(
            pdf,
            plt,
            stats,
            summary_operator_counts,
            operator_shares_all,
            scope_label=scope_label,
        )

        if stats.statement_types:
            _render_statement_type_page(pdf, plt, stats.statement_types, palette_list)
        if stats_queries.sql_lengths:
            _render_histogram(
                pdf,
                plt,
                stats_queries.sql_lengths,
                LENGTH_BINS,
                "Select Statement Length (bytes)",
                palette_list,
            )

        query_count = stats_queries.total_queries or (stats.total_queries - stats.schema_statements)
        if query_count <= 0:
            return

        operator_source_label = options.operator_source or bundle.metadata.get("operator_source") or "AST"
        operator_type_counts_expr = stats_queries.operator_type_counts_dict()
        operator_type_origin = stats_queries.origin_operator_type_counts_dict() if options.include_origin_types else None

        if stats_queries.feature_counts:
            _render_feature_table(pdf, plt, stats_queries, query_count)

        if stats_queries.operator_counts:
            _render_operator_bar_chart(
                pdf,
                plt,
                stats_queries.operator_counts,
                counter_shares(stats_queries.operator_counts),
                palette_list,
                source_label=operator_source_label,
            )

        if operator_type_counts_expr:
            _render_operator_type_pages(
                pdf,
                plt,
                stats_queries.operator_counts,
                operator_type_counts_expr,
                palette_list,
            )

        # SELECT clause
        _render_clause_heading(pdf, plt, "SELECT …", "Scalar expressions, aggregate expressions")

        if stats_queries.projection_expression_totals:
            _render_histogram(
                pdf,
                plt,
                stats_queries.projection_expression_totals,
                EXPRESSION_DISTRIBUTION_BINS,
                "Projection Expression Count (per query)",
                palette_list,
            )

        if stats_queries.scalar_expression_depths:
            _render_histogram(
                pdf,
                plt,
                stats_queries.scalar_expression_depths,
                DEPTH_BINS,
                "Scalar Expression Tree Height (max per query)",
                palette_list,
            )

        if stats_queries.aggregate_functions or stats_queries.projection_functions:
            _render_function_page(pdf, plt, stats_queries, palette_list)

        aggregate_counter: Optional[Counter] = None
        if operator_type_origin and operator_type_origin.get("aggregate"):
            aggregate_counter = operator_type_origin["aggregate"]
        elif operator_type_counts_expr and operator_type_counts_expr.get("aggregate"):
            aggregate_counter = operator_type_counts_expr["aggregate"]

        if aggregate_counter:
            aggregate_counts = Counter({"aggregate": stats_queries.operator_counts.get("aggregate", 0)})
            _render_operator_type_pages(
                pdf,
                plt,
                aggregate_counts,
                {"aggregate": aggregate_counter},
                palette_list,
                focus_ops=["aggregate"],
            )
        else:
            agg_counter = operator_type_counts_expr.get("aggregate") if operator_type_counts_expr else None
            if agg_counter:
                _render_logical_type_page(pdf, plt, {"aggregate": agg_counter}, palette_list)

        # FROM clause
        _render_clause_heading(pdf, plt, "FROM …", "Join key types, joins per query, input cardinalities")

        if stats_queries.join_counts:
            _render_join_type_table(pdf, plt, stats_queries.join_counts)

        if operator_type_origin and operator_type_origin.get("join"):
            _render_logical_type_page(pdf, plt, {"join": operator_type_origin["join"]}, palette_list)
        elif operator_type_counts_expr and operator_type_counts_expr.get("join"):
            _render_logical_type_page(pdf, plt, {"join": operator_type_counts_expr["join"]}, palette_list)

        if stats_queries.joins_per_query:
            _render_join_count_table(pdf, plt, stats_queries.joins_per_query)

        joins_with_activity = [value for value in stats_queries.joins_per_query if value > 0]
        if joins_with_activity:
            join_share = (len(joins_with_activity) / query_count) * 100 if query_count else 0.0
            note = f"Queries with joins: {join_share:.1f}% (N={len(joins_with_activity)})"
            _render_conditional_distribution_chart(
                pdf,
                plt,
                joins_with_activity,
                JOIN_CONDITIONAL_BINS,
                "Join Count Distribution",
                palette_list,
                note,
            )

        # WHERE clause
        _render_clause_heading(pdf, plt, "WHERE …", "Column types, predicate functions, nesting levels")

        if stats_queries.filter_expression_totals:
            _render_histogram(
                pdf,
                plt,
                stats_queries.filter_expression_totals,
                EXPRESSION_DISTRIBUTION_BINS,
                "Filter Expression Count (per query)",
                palette_list,
            )

        if stats_queries.filter_expression_ops:
            _render_filter_expression_table(pdf, plt, stats_queries.filter_expression_ops)

        if operator_type_origin and operator_type_origin.get("filter"):
            _render_logical_type_page(pdf, plt, {"filter": operator_type_origin["filter"]}, palette_list)
        elif operator_type_counts_expr and operator_type_counts_expr.get("filter"):
            _render_logical_type_page(pdf, plt, {"filter": operator_type_counts_expr["filter"]}, palette_list)

        if stats_queries.filter_predicate_depths:
            _render_histogram(
                pdf,
                plt,
                stats_queries.filter_predicate_depths,
                DEPTH_BINS,
                "Filter Predicate Depth (max per query)",
                palette_list,
            )

        # GROUP BY clause
        _render_clause_heading(pdf, plt, "GROUP BY …", "Grouping key cardinality and key types")

        if stats_queries.group_by_key_counts:
            _render_group_by_key_table(pdf, plt, stats_queries.group_by_key_counts)
        group_counter: Optional[Counter] = None
        if operator_type_origin and operator_type_origin.get("group_by"):
            group_counter = operator_type_origin["group_by"]
        elif operator_type_counts_expr and operator_type_counts_expr.get("group_by"):
            group_counter = operator_type_counts_expr["group_by"]
        if group_counter:
            group_counts = Counter({"group_by": stats_queries.operator_counts.get("aggregate", 0)})
            _render_operator_type_pages(
                pdf,
                plt,
                group_counts,
                {"group_by": group_counter},
                palette_list,
                focus_ops=["group_by"],
            )

        # ORDER BY clause
        _render_clause_heading(pdf, plt, "ORDER BY …", "Sorted rows and logical types")

        order_unique = stats_queries.top_level_order_types_unique
        payload = {}
        if operator_type_origin and operator_type_origin.get("sort"):
            payload["sort"] = operator_type_origin["sort"]
        elif operator_type_counts_expr and operator_type_counts_expr.get("sort"):
            payload["sort"] = operator_type_counts_expr["sort"]

        if payload:
            _render_logical_type_page(pdf, plt, payload, palette_list)
        if order_unique:
            _render_order_unique_summary(pdf, plt, order_unique)

        # LIMIT clause
        _render_clause_heading(pdf, plt, "LIMIT …", "Distribution of K (Top-K, limit without ORDER BY)")
        if stats_queries.limit_with_order or stats_queries.limit_without_order:
            _render_limit_distribution(pdf, plt, stats_queries, palette_list)

        # UNION ALL clause
        union_positive = [value for value in stats_queries.union_all_totals if value > 0]
        if stats_queries.union_all_totals:
            _render_clause_heading(pdf, plt, "UNION ALL …", "Occurrences of set operations")
            if union_positive:
                union_share = (len(union_positive) / query_count) * 100 if query_count else 0.0
                note = f"Queries with UNION ALL: {union_share:.1f}% (N={len(union_positive)})"
                _render_conditional_distribution_chart(
                    pdf,
                    plt,
                    union_positive,
                    UNION_CONDITIONAL_BINS,
                    "UNION ALL Occurrences",
                    palette_list,
                    note,
                )

        schema_profile = bundle.schema_profile
        if schema_profile and schema_profile.has_data():
            _render_clause_heading(pdf, plt, "SCHEMA …", "Column and primary key types from CREATE TABLE statements")
            _render_schema_summary_page(pdf, plt, schema_profile)
            if schema_profile.column_type_counts:
                _render_schema_distribution_chart(
                    pdf,
                    plt,
                    schema_profile.column_type_counts,
                    "Column Type Distribution",
                    palette_list,
                )
            if schema_profile.primary_key_type_counts:
                _render_schema_distribution_chart(
                    pdf,
                    plt,
                    schema_profile.primary_key_type_counts,
                    "Primary Key Type Distribution",
                    palette_list,
                )
            if schema_profile.foreign_key_type_counts:
                _render_schema_distribution_chart(
                    pdf,
                    plt,
                    schema_profile.foreign_key_type_counts,
                    "Foreign Key Type Distribution",
                    palette_list,
                )

        data_profile = bundle.data_profile
        has_data_metrics = bool(data_profile and data_profile.table_count > 0)
        if has_data_metrics and data_profile:
            if options.ndv_debug:
                workload_label = bundle.metadata.get("workload_label") or "workload"
                _emit_ndv_debug({workload_label: data_profile}, [workload_label], options)
            _render_clause_heading(pdf, plt, "DATA FILES …", "Table size and row distributions from scanned data")
            _render_data_summary_page(pdf, plt, data_profile)
            sizes = data_profile.sizes()
            if sizes:
                _render_histogram(pdf, plt, sizes, TABLE_SIZE_BINS, "Table Size Distribution (share of tables)", palette_list)
            row_counts = data_profile.row_counts()
            if row_counts:
                _render_histogram(pdf, plt, row_counts, ROW_COUNT_BINS, "Row Count Distribution (share of tables)", palette_list)
            if data_profile.column_count:
                _render_mcv_curves(pdf, plt, data_profile, palette_list, options)
            if data_profile.histogram_count:
                _render_histogram_skew_chart(pdf, plt, data_profile, palette_list)
            _render_additional_data_histograms(pdf, plt, data_profile, palette_list)
            if schema_profile and schema_profile.column_type_counts:
                _render_schema_distribution_chart(
                    pdf,
                    plt,
                    schema_profile.column_type_counts,
                    "Schema Column Type Distribution",
                    palette_list,
                )
            if (
                schema_profile
                and schema_profile.primary_key_type_counts
                and schema_profile.foreign_key_type_counts
            ):
                _render_key_type_distribution_chart(
                    pdf,
                    plt,
                    schema_profile.primary_key_type_counts,
                    schema_profile.foreign_key_type_counts,
                    "Primary vs Foreign Key Type Distribution",
                    palette_list,
                )
        elif options.expect_data_metrics:
            _render_missing_data_page(
                pdf,
                plt,
                "Data metrics were requested, but no data files were found or metrics could not be read.",
            )


def generate_comparison_pdf(
    workloads: AggregatedReportMap,
    output_path: Path,
    palette: Iterable[str] | None = None,
    options: ReportOptions | None = None,
) -> None:
    if not workloads:
        raise ValueError("No workloads provided for comparison")

    if options is None:
        options = ReportOptions()

    if options.render_style == RENDER_STYLE_PAPER and options.paper_preset:
        # Flag-guarded paper-style path. Leaves the default comparison pipeline
        # untouched; lives entirely in report.paper. See AGENTS.md for usage.
        from . import paper as _paper

        baselines = None
        if options.paper_baselines_path:
            baselines = _paper.load_baselines(Path(options.paper_baselines_path))
        focus_bars = options.paper_focus_bars or _paper.DEFAULT_FOCUS_BARS
        focus_cdf = options.paper_focus_cdf or _paper.DEFAULT_FOCUS_CDF
        _paper.render_paper_preset(
            workloads,
            options.paper_preset,
            Path(output_path),
            focus_bars=focus_bars,
            focus_cdf=focus_cdf,
            baselines=baselines,
        )
        return

    _set_render_style(options.render_style)
    plt, PdfPages = _ensure_matplotlib()
    labels = list(workloads.keys())
    display_labels = _display_workload_labels(labels)
    if display_labels != labels:
        workloads = {display: workloads[label] for display, label in zip(display_labels, labels)}
        labels = display_labels
    stats_scope: Dict[str, AggregatedStats] = {}
    for label in labels:
        report = workloads[label]
        stats_scope[label] = report.query_statements if options.scope == "queries" else report.all_statements
    metadata_map: Dict[str, Dict[str, Any]] = {label: workloads[label].metadata for label in labels}
    data_profiles: Dict[str, DataProfile | None] = {label: workloads[label].data_profile for label in labels}
    schema_profiles: Dict[str, SchemaProfile | None] = {label: workloads[label].schema_profile for label in labels}
    has_data_profiles = any(profile and profile.table_count > 0 for profile in data_profiles.values())
    has_schema_profiles = any(profile and profile.has_data() for profile in schema_profiles.values() if profile)

    if all(stats.total_queries == 0 for stats in stats_scope.values()):
        raise ValueError("No records available to render comparison report")

    palette_list = list(palette or DEFAULT_PALETTE)
    colors = _palette_for_workloads(palette_list, len(labels))

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    png_dir = options.png_output_dir
    with PdfPages(str(output_path)) as pdf_pages:
        pdf = _FigureExporter(pdf_pages, png_dir, output_path.stem)
        scope_label = "All statements (DDL + SELECT)" if options.scope != "queries" else "SELECT only"
        _render_comparison_summary(pdf, plt, stats_scope, labels, colors, scope_label)
        _render_comparison_key_metrics(pdf, plt, stats_scope, labels, scope_label)
        _render_comparison_statement_types(pdf, plt, stats_scope, labels, colors)
        _render_workload_metadata_table(pdf, plt, metadata_map, labels)

        if any(stats.sql_lengths or stats.sql_length_histogram for stats in stats_scope.values()):
            _render_comparison_histogram(
                pdf,
                plt,
                stats_scope,
                labels,
                colors,
                lambda s: s.sql_lengths,
                LENGTH_BINS,
                "Statement Length (bytes)",
                lambda s: s.sql_length_histogram,
            )

        if any(stats.feature_counts for stats in stats_scope.values()):
            _render_comparison_features(pdf, plt, stats_scope, labels, colors)
        _render_comparison_operator_mix(pdf, plt, stats_scope, labels, colors)
        _render_comparison_highlights(pdf, plt, stats_scope, labels)

        has_select_section = any(
            (
                stats.scalar_expression_depths
                or stats.projection_expression_totals
                or stats.aggregate_functions
                or stats.expression_functions
            )
            for stats in stats_scope.values()
        )
        if has_select_section:
            _render_clause_heading(pdf, plt, "SELECT …", "Scalar expressions, aggregate expressions")
        if any(stats.scalar_expression_depths or stats.scalar_expression_depth_histogram for stats in stats_scope.values()):
            _render_comparison_histogram(
                pdf,
                plt,
                stats_scope,
                labels,
                colors,
                lambda s: s.scalar_expression_depths,
                DEPTH_BINS,
                "Scalar Expression Tree Height (max per query)",
                lambda s: s.scalar_expression_depth_histogram,
            )

        if any(stats.projection_expression_totals for stats in stats_scope.values()):
            _render_comparison_histogram(
                pdf,
                plt,
                stats_scope,
                labels,
                colors,
                lambda s: s.projection_expression_totals,
                EXPRESSION_DISTRIBUTION_BINS,
                "Projection Expression Count (per query)",
                lambda s: s.projection_expression_histogram,
            )
        if has_select_section:
            _render_comparison_function_charts(pdf, plt, stats_scope, labels, colors)
        if any(
            stats.operator_type_counts.get("aggregate") if stats.operator_type_counts else None
            for stats in stats_scope.values()
        ):
            _render_comparison_aggregate_types(pdf, plt, stats_scope, labels, colors)

        has_from_section = any(
            stats.join_counts or any(value > 0 for value in stats.joins_per_query)
            for stats in stats_scope.values()
        )
        if has_from_section:
            _render_clause_heading(pdf, plt, "FROM …", "Join key types, joins per query, input cardinalities")
            _render_comparison_join_types(pdf, plt, stats_scope, labels, colors)
            _render_comparison_join_column_types(pdf, plt, stats_scope, labels, colors)
            _render_comparison_join_cardinality(pdf, plt, stats_scope, labels, colors)

        has_where_section = any(
            stats.filter_expression_totals
            or stats.filter_expression_ops
            or stats.filter_predicate_depths
            or (stats.operator_type_counts.get("filter") if stats.operator_type_counts else None)
            for stats in stats_scope.values()
        )
        if has_where_section:
            _render_clause_heading(pdf, plt, "WHERE …", "Column types, predicate functions, nesting levels")

        if any(stats.filter_expression_totals for stats in stats_scope.values()):
            _render_comparison_histogram(
                pdf,
                plt,
                stats_scope,
                labels,
                colors,
                lambda s: s.filter_expression_totals,
                EXPRESSION_DISTRIBUTION_BINS,
                "Filter Expression Count (per query)",
                lambda s: s.filter_expression_histogram,
            )

        if has_where_section:
            _render_comparison_filter_ops(pdf, plt, stats_scope, labels, colors)
            _render_comparison_filter_types(pdf, plt, stats_scope, labels, colors)

        if any(stats.filter_predicate_depths or stats.filter_predicate_depth_histogram for stats in stats_scope.values()):
            _render_comparison_histogram(
                pdf,
                plt,
                stats_scope,
                labels,
                colors,
                lambda s: s.filter_predicate_depths,
                DEPTH_BINS,
                "Filter Predicate Depth (max per query)",
                lambda s: s.filter_predicate_depth_histogram,
            )

        has_group_by_section = any(stats.group_by_key_counts for stats in stats_scope.values())
        if has_group_by_section:
            _render_clause_heading(pdf, plt, "GROUP BY …", "Grouping key cardinality and key types")
            _render_comparison_group_by_counts(pdf, plt, stats_scope, labels, colors)

        has_order_section = any(stats.top_level_order_types for stats in stats_scope.values())
        if has_order_section:
            _render_clause_heading(pdf, plt, "ORDER BY …", "Sorted rows and logical types")
            _render_comparison_order_types(pdf, plt, stats_scope, labels, colors)

        has_limit_section = any((stats.limit_with_order or stats.limit_without_order) for stats in stats_scope.values())
        if has_limit_section:
            _render_clause_heading(pdf, plt, "LIMIT …", "Distribution of K (Top-K, limit without ORDER BY)")
            _render_comparison_limit_distribution(pdf, plt, stats_scope, labels, colors)

        has_union_section = any(stats.union_all_totals for stats in stats_scope.values())
        if has_union_section:
            _render_clause_heading(pdf, plt, "UNION ALL …", "Occurrences of set operations")
            _render_comparison_union_distribution(pdf, plt, stats_scope, labels, colors)

        if has_data_profiles:
            _render_clause_heading(pdf, plt, "DATA FILES …", "Table sizes, row counts, and column metrics")
            if options.ndv_debug:
                _emit_ndv_debug(data_profiles, labels, options)
            _render_data_comparison_summary(pdf, plt, data_profiles, labels)
            _render_data_behavior_summary(pdf, plt, data_profiles, labels)
            _render_data_comparison_distribution(
                pdf,
                plt,
                data_profiles,
                labels,
                colors,
                "Table Size Distribution",
                lambda profile: profile.sizes() if profile else [],
                TABLE_SIZE_RANGE_BINS,
            )
            _render_data_comparison_distribution(
                pdf,
                plt,
                data_profiles,
                labels,
                colors,
                "Row Count Distribution",
                lambda profile: profile.row_counts() if profile else [],
                ROW_COUNT_RANGE_BINS,
            )
            _render_data_comparison_distribution(
                pdf,
                plt,
                data_profiles,
                labels,
                colors,
                "Bytes per Row (tables)",
                lambda profile: profile.bytes_per_row_values() if profile else [],
                BYTES_PER_ROW_RANGE_BINS,
            )
            _render_data_comparison_distribution(
                pdf,
                plt,
                data_profiles,
                labels,
                colors,
                "Numeric Outlier Rate (columns)",
                lambda profile: profile.numeric_outlier_rates() if profile else [],
                OUTLIER_RATE_RANGE_BINS,
            )
            _render_data_comparison_distribution(
                pdf,
                plt,
                data_profiles,
                labels,
                colors,
                "Mean Histogram Q-Error (columns)",
                lambda profile: profile.histogram_q_errors() if profile else [],
                MEAN_QERROR_RANGE_BINS,
            )
            _render_data_comparison_distribution(
                pdf,
                plt,
                data_profiles,
                labels,
                colors,
                "Average String Length (columns)",
                lambda profile: profile.string_avg_lengths() if profile else [],
                STRING_LENGTH_RANGE_BINS,
            )
            _render_data_percentile_curves(
                pdf,
                plt,
                data_profiles,
                labels,
                colors,
                "Null Fraction (Columns)",
                lambda profile: profile.null_fractions() if profile else [],
                legend_labels=_legend_labels(labels, SERIES_LABEL_OVERRIDES.get("redshift_data_curves")),
            )
            _render_data_percentile_curves(
                pdf,
                plt,
                data_profiles,
                labels,
                colors,
                _ndv_title(options),
                lambda profile: _ndv_profile_values(profile, options, clamp=True),
            )
            _render_data_percentile_curves(
                pdf,
                plt,
                data_profiles,
                labels,
                colors,
                "Maximum MCV Frequency",
                lambda profile: profile.max_mcv_fractions() if profile else [],
                legend_labels=_legend_labels(labels, SERIES_LABEL_OVERRIDES.get("redshift_data_curves")),
            )
            _render_data_percentile_curves(
                pdf,
                plt,
                data_profiles,
                labels,
                colors,
                "Sum of MCV frequencies (k)",
                lambda profile: profile.topk_fractions() if profile else [],
            )

        if has_schema_profiles:
            _render_clause_heading(pdf, plt, "SCHEMA …", "DDL-derived table and column types")
            _render_schema_comparison_summary(pdf, plt, schema_profiles, labels)
            compact_figsize = (GROUPED_BAR_FIGSIZE[0], GROUPED_BAR_FIGSIZE[1] * 0.75)
            emphasis_tick_size = GROUPED_BAR_TICK_FONTSIZE + 3
            emphasis_legend_size = GROUPED_BAR_LEGEND_FONTSIZE + 3
            _render_schema_counter_chart(
                pdf,
                plt,
                schema_profiles,
                labels,
                colors,
                "column_type_counts",
                "Column Type Distribution",
                keep_categories=["TEXT", "INT", "DECIMAL", "DATE"],
                legend_labels=_legend_labels(labels, SERIES_LABEL_OVERRIDES.get("snowflake_series")),
                x_tick_rotation=0,
                x_tick_ha="center",
                figsize=compact_figsize,
                fixed_y_max=100,
                y_tick_step=10,
                x_tick_fontsize=emphasis_tick_size,
                legend_fontsize=emphasis_legend_size,
                legend_borderpad=0.55,
                legend_labelspacing=0.55,
                legend_handletextpad=0.7,
            )
            _render_schema_counter_chart(
                pdf,
                plt,
                schema_profiles,
                labels,
                colors,
                "primary_key_type_counts",
                "Primary Key Type Distribution",
            )
            _render_schema_counter_chart(
                pdf,
                plt,
                schema_profiles,
                labels,
                colors,
                "foreign_key_type_counts",
                "Foreign Key Type Distribution",
            )


def generate_types_only_comparison_pdf(
    workloads: AggregatedReportMap,
    output_path: Path,
    palette: Iterable[str] | None = None,
    options: ReportOptions | None = None,
) -> None:
    if not workloads:
        raise ValueError("No workloads provided for comparison")

    if options is None:
        options = ReportOptions()

    _set_render_style(options.render_style)
    plt, PdfPages = _ensure_matplotlib()
    labels = list(workloads.keys())
    display_labels = _display_workload_labels(labels)
    if display_labels != labels:
        workloads = {display: workloads[label] for display, label in zip(display_labels, labels)}
        labels = display_labels
    stats_scope: Dict[str, AggregatedStats] = {}
    for label in labels:
        report = workloads[label]
        stats_scope[label] = report.query_statements if options.scope == "queries" else report.all_statements
    schema_profiles: Dict[str, SchemaProfile | None] = {label: workloads[label].schema_profile for label in labels}
    has_schema_profiles = any(profile and profile.has_data() for profile in schema_profiles.values() if profile)

    if all(stats.total_queries == 0 for stats in stats_scope.values()):
        raise ValueError("No records available to render comparison report")

    palette_list = list(palette or DEFAULT_PALETTE)
    colors = _palette_for_workloads(palette_list, len(labels))

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    png_dir = options.png_output_dir
    with PdfPages(str(output_path)) as pdf_pages:
        pdf = _FigureExporter(pdf_pages, png_dir, output_path.stem)
        scope_label = "All statements (DDL + SELECT)" if options.scope != "queries" else "SELECT only"
        _render_comparison_summary(pdf, plt, stats_scope, labels, colors, scope_label)
        _render_clause_heading(pdf, plt, "OPERATOR TYPES …", "Logical type mix per operator")
        _render_comparison_aggregate_types(pdf, plt, stats_scope, labels, colors)
        _render_comparison_join_column_types(pdf, plt, stats_scope, labels, colors)
        _render_comparison_filter_types(pdf, plt, stats_scope, labels, colors)
        _render_comparison_order_types(pdf, plt, stats_scope, labels, colors)

        if has_schema_profiles:
            _render_clause_heading(pdf, plt, "SCHEMA …", "Stored column logical types")
            _render_schema_comparison_summary(pdf, plt, schema_profiles, labels)
            compact_figsize = (GROUPED_BAR_FIGSIZE[0], GROUPED_BAR_FIGSIZE[1] * 0.75)
            emphasis_tick_size = GROUPED_BAR_TICK_FONTSIZE + 3
            emphasis_legend_size = GROUPED_BAR_LEGEND_FONTSIZE + 3
            _render_schema_counter_chart(
                pdf,
                plt,
                schema_profiles,
                labels,
                colors,
                "column_type_counts",
                "Column Type Distribution",
                keep_categories=["TEXT", "INT", "DECIMAL", "DATE"],
                legend_labels=_legend_labels(labels, SERIES_LABEL_OVERRIDES.get("snowflake_series")),
                x_tick_rotation=0,
                x_tick_ha="center",
                figsize=compact_figsize,
                fixed_y_max=100,
                y_tick_step=10,
                x_tick_fontsize=emphasis_tick_size,
                legend_fontsize=emphasis_legend_size,
                legend_borderpad=0.55,
                legend_labelspacing=0.55,
                legend_handletextpad=0.7,
            )


def generate_focused_comparison_pdf(
    workloads: AggregatedReportMap,
    output_path: Path,
    palette: Iterable[str] | None = None,
    options: ReportOptions | None = None,
) -> None:
    if not workloads:
        raise ValueError("No workloads provided for comparison")

    if options is None:
        options = ReportOptions()

    _set_render_style(options.render_style)
    plt, PdfPages = _ensure_matplotlib()
    labels = list(workloads.keys())
    display_labels = _display_workload_labels(labels)
    if display_labels != labels:
        workloads = {display: workloads[label] for display, label in zip(display_labels, labels)}
        labels = display_labels
    stats_scope: Dict[str, AggregatedStats] = {}
    for label in labels:
        report = workloads[label]
        stats_scope[label] = report.query_statements if options.scope == "queries" else report.all_statements

    if all(stats.total_queries == 0 for stats in stats_scope.values()):
        raise ValueError("No records available to render comparison report")

    palette_list = list(palette or DEFAULT_PALETTE)
    colors = _palette_for_workloads(palette_list, len(labels))

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    png_dir = options.png_output_dir
    with PdfPages(str(output_path)) as pdf_pages:
        pdf = _FigureExporter(pdf_pages, png_dir, output_path.stem)

        has_limit_section = any((stats.limit_with_order or stats.limit_without_order) for stats in stats_scope.values())
        has_group_by_section = any(stats.group_by_key_counts for stats in stats_scope.values())
        has_union_fan_in_section = any(stats.union_all_fan_in_values for stats in stats_scope.values())

        if not (has_limit_section or has_group_by_section or has_union_fan_in_section):
            raise ValueError("No limit, group-by, or union fan-in metrics available to render")

        if has_limit_section:
            _render_clause_heading(pdf, plt, "LIMIT …", "Distribution of K (Top-K, limit without ORDER BY)")
            _render_comparison_limit_distribution(pdf, plt, stats_scope, labels, colors)

        if has_group_by_section:
            _render_clause_heading(pdf, plt, "GROUP BY …", "Grouping key counts per SELECT statement")
            _render_comparison_group_by_counts(pdf, plt, stats_scope, labels, colors)

        if has_union_fan_in_section:
            _render_clause_heading(pdf, plt, "UNION ALL …", "Fan-in (number of inputs)")
            _render_comparison_union_fan_in_distribution(pdf, plt, stats_scope, labels, colors)
def _prepare_grouped_inputs(
    categories: Sequence[str],
    labels: Sequence[str],
    series_values: Sequence[Sequence[float]],
    title: str,
    group_other_max: Optional[float],
    keep_categories: Optional[Sequence[str]],
) -> Tuple[List[str], List[List[float]]]:
    """Shared pipeline: align lengths, optionally group/keep categories, drop empties."""
    cats = list(categories)
    vals = _align_series_lengths(cats, series_values)
    if group_other_max is not None:
        cats, vals = _group_small_categories(cats, vals, group_other_max)
    if keep_categories:
        cats, vals = _keep_categories_with_other(cats, vals, keep_categories)
    cats, vals = _filter_empty_categories(cats, vals)
    if not cats:
        return cats, vals
    vals = _align_series_lengths(cats, vals)
    if any(len(row) != len(cats) for row in vals):
        raise ValueError(
            f"Grouped bar length mismatch in '{title}': categories={len(cats)}, "
            f"series_lengths={[len(row) for row in vals]}"
        )
    return cats, vals


def _plot_heatmap(
    pdf,
    plt,
    categories: Sequence[str],
    labels: Sequence[str],
    series_values: Sequence[Sequence[float]],
    title: str,
    ylabel: str,
) -> None:
    """Workloads × categories heatmap with annotated cell values, baselines hatched."""
    if not categories or not labels:
        return
    n_labels = len(labels)
    n_categories = len(categories)
    scale = _font_scale(n_labels)

    try:
        import matplotlib

        cmap = matplotlib.colormaps.get_cmap("YlGnBu")
    except Exception:
        cmap = None

    fig_w = max(8.5, 0.9 * n_categories + 2.0)
    fig_h = max(4.0, 0.45 * n_labels + 2.0)
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    fig.patch.set_facecolor("white")

    rows = [list(row) for row in series_values]
    vmax = max((max(row) if row else 0.0) for row in rows) or 1.0

    im = ax.imshow(rows, aspect="auto", cmap=cmap, vmin=0, vmax=vmax)

    for i in range(n_labels):
        for j in range(n_categories):
            value = rows[i][j]
            text_color = "white" if value > vmax * 0.55 else "#1a1a1a"
            ax.text(j, i, f"{value:.1f}", ha="center", va="center", color=text_color, fontsize=10 * scale)

    # Mark baseline rows with a left edge bar.
    for i, label in enumerate(labels):
        if _is_baseline_label(label):
            ax.add_patch(plt.Rectangle((-0.5, i - 0.5), 0.08 * n_categories, 1.0, fill=False, edgecolor="black", linewidth=2.5, clip_on=False))

    ax.set_xticks(range(n_categories))
    ax.set_xticklabels(categories, rotation=45, ha="right", fontsize=GROUPED_BAR_TICK_FONTSIZE * scale)
    ax.set_yticks(range(n_labels))
    ax.set_yticklabels(labels, fontsize=GROUPED_BAR_TICK_FONTSIZE * scale)
    ax.set_title(title, fontsize=GROUPED_BAR_TITLE_FONTSIZE * scale, weight="bold")
    cbar = fig.colorbar(im, ax=ax, shrink=0.85)
    cbar.set_label(ylabel, fontsize=GROUPED_BAR_LABEL_FONTSIZE * scale)
    fig.tight_layout()
    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def _plot_dot_plot(
    pdf,
    plt,
    categories: Sequence[str],
    labels: Sequence[str],
    colors: Sequence[str],
    series_values: Sequence[Sequence[float]],
    title: str,
    ylabel: str,
    *,
    legend_outside: Optional[bool] = None,
) -> None:
    """Cleveland dot plot: categories on Y, dot per workload at its value."""
    if not categories or not labels:
        return
    n_labels = len(labels)
    n_categories = len(categories)
    scale = _font_scale(n_labels)
    if legend_outside is None:
        legend_outside = n_labels >= LEGEND_OUTSIDE_THRESHOLD

    fig_w = max(8.5, 6.0)
    fig_h = max(4.0, 0.55 * n_categories + 1.5)
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    fig.patch.set_facecolor("white")

    # Light guide line per category.
    for cat_idx in range(n_categories):
        ax.axhline(cat_idx, color="#dddddd", linewidth=0.8, zorder=0)

    seen_labels: Set[str] = set()
    for idx, label in enumerate(labels):
        is_baseline = _is_baseline_label(label)
        color = BASELINE_BAR_COLOR if is_baseline else colors[idx % len(colors)]
        marker = "s" if is_baseline else "o"
        size = 90 if is_baseline else 70
        ax.scatter(series_values[idx], range(n_categories), color=color, marker=marker, s=size, label=label, edgecolor="white", linewidth=0.8, zorder=3)
        seen_labels.add(label)

    ax.set_yticks(range(n_categories))
    ax.set_yticklabels(categories, fontsize=GROUPED_BAR_TICK_FONTSIZE * scale)
    ax.invert_yaxis()
    ax.set_xlabel(ylabel, fontsize=GROUPED_BAR_LABEL_FONTSIZE * scale)
    ax.set_title(title, fontsize=GROUPED_BAR_TITLE_FONTSIZE * scale, weight="bold")
    ax.grid(axis="x", linestyle="--", alpha=0.3)
    ax.tick_params(axis="x", labelsize=GROUPED_BAR_TICK_FONTSIZE * scale)

    if legend_outside:
        ax.legend(fontsize=GROUPED_BAR_LEGEND_FONTSIZE * scale, loc="upper center", bbox_to_anchor=(0.5, -0.18), ncol=min(n_labels, 5), frameon=True)
    else:
        ax.legend(fontsize=GROUPED_BAR_LEGEND_FONTSIZE * scale)
    fig.tight_layout()
    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def _plot_small_multiples(
    pdf,
    plt,
    categories: Sequence[str],
    labels: Sequence[str],
    colors: Sequence[str],
    series_values: Sequence[Sequence[float]],
    title: str,
    ylabel: str,
) -> None:
    """One mini bar chart per workload, shared y-axis."""
    if not categories or not labels:
        return
    n_labels = len(labels)
    n_categories = len(categories)
    cols = min(3, n_labels)
    rows = math.ceil(n_labels / cols)
    scale = _font_scale(max(n_labels, 4))

    fig_w = max(8.5, 3.0 * cols)
    fig_h = max(4.0, 2.4 * rows + 1.2)
    fig, axes = plt.subplots(rows, cols, figsize=(fig_w, fig_h), sharey=True)
    fig.patch.set_facecolor("white")
    flat_axes = axes.flatten() if hasattr(axes, "flatten") else [axes]

    vmax = max((max(row) if row else 0.0) for row in series_values) or 1.0

    for idx, label in enumerate(labels):
        ax = flat_axes[idx]
        is_baseline = _is_baseline_label(label)
        color = BASELINE_BAR_COLOR if is_baseline else colors[idx % len(colors)]
        hatch = BASELINE_BAR_HATCH if is_baseline else None
        ax.bar(range(n_categories), series_values[idx], color=color, hatch=hatch, edgecolor="black" if is_baseline else "none")
        ax.set_title(label, fontsize=GROUPED_BAR_TICK_FONTSIZE * scale, weight="bold")
        ax.set_xticks(range(n_categories))
        ax.set_xticklabels(categories, rotation=45, ha="right", fontsize=GROUPED_BAR_TICK_FONTSIZE * scale * 0.85)
        ax.set_ylim(0, vmax * 1.1)
        ax.grid(axis="y", linestyle="--", alpha=0.3)
        if idx % cols == 0:
            ax.set_ylabel(ylabel, fontsize=GROUPED_BAR_LABEL_FONTSIZE * scale)

    for extra in range(n_labels, len(flat_axes)):
        flat_axes[extra].axis("off")

    fig.suptitle(title, fontsize=GROUPED_BAR_TITLE_FONTSIZE * scale, weight="bold", y=0.995)
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def _plot_grouped_bars(
    pdf,
    plt,
    categories: Sequence[str],
    labels: Sequence[str],
    colors: Sequence[str],
    series_values: Sequence[Sequence[float]],
    title: str,
    ylabel: str = "Share (%)",
    *,
    label_threshold: float = 0.05,
    group_other_max: Optional[float] = None,
    keep_categories: Optional[Sequence[str]] = None,
    legend_outside: Optional[bool] = None,
    legend_ncol: Optional[int] = None,
    show_values: Optional[bool] = None,
    x_tick_rotation: float = 45,
    x_tick_ha: str = "right",
    y_max_scale_no_values: float = 1.0,
    figsize: Optional[Tuple[float, float]] = None,
    fixed_y_max: Optional[float] = None,
    y_tick_step: Optional[int] = None,
    x_tick_fontsize: Optional[float] = None,
    y_tick_fontsize: Optional[float] = None,
    legend_fontsize: Optional[float] = None,
    legend_borderpad: Optional[float] = None,
    legend_labelspacing: Optional[float] = None,
    legend_handletextpad: Optional[float] = None,
    orientation: Optional[str] = None,
    render_style: Optional[str] = None,
) -> None:
    if not categories or not labels:
        return

    n_series = len(labels)
    if n_series == 0:
        return

    style = render_style or _current_render_style() or RENDER_STYLE_BARS
    if style != RENDER_STYLE_BARS:
        cats, vals = _prepare_grouped_inputs(categories, labels, series_values, title, group_other_max, keep_categories)
        if not cats:
            return
        if style == RENDER_STYLE_HEATMAP:
            return _plot_heatmap(pdf, plt, cats, labels, vals, title, ylabel)
        if style == RENDER_STYLE_DOT_PLOT:
            return _plot_dot_plot(pdf, plt, cats, labels, colors, vals, title, ylabel, legend_outside=legend_outside)
        if style == RENDER_STYLE_SMALL_MULTIPLES:
            return _plot_small_multiples(pdf, plt, cats, labels, colors, vals, title, ylabel)
        # Unknown style: fall through to bars.

    categories = list(categories)
    series_values = _align_series_lengths(categories, series_values)
    if group_other_max is not None:
        categories, series_values = _group_small_categories(categories, series_values, group_other_max)
    if keep_categories:
        categories, series_values = _keep_categories_with_other(categories, series_values, keep_categories)
    categories, series_values = _filter_empty_categories(categories, series_values)
    if not categories:
        return
    series_values = _align_series_lengths(categories, series_values)
    if any(len(row) != len(categories) for row in series_values):
        raise ValueError(
            f"Grouped bar length mismatch in '{title}': categories={len(categories)}, "
            f"series_lengths={[len(row) for row in series_values]}"
        )

    # ---- adaptive layout decisions driven by N ----
    if orientation is None:
        orientation = "horizontal" if n_series >= HORIZONTAL_BAR_THRESHOLD else "vertical"
    horizontal = orientation == "horizontal"

    if legend_outside is None:
        legend_outside = n_series >= LEGEND_OUTSIDE_THRESHOLD

    width = max(MIN_BAR_WIDTH, 0.8 / n_series)
    if show_values is None:
        show_values = width >= VALUE_LABEL_MIN_WIDTH

    scale = _font_scale(n_series)

    base_positions = list(range(len(categories)))

    if figsize is None:
        figsize = _adaptive_horizontal_figsize(len(categories), n_series) if horizontal else _adaptive_grouped_figsize(len(categories), n_series)

    fig, ax = plt.subplots(figsize=figsize)
    fig.patch.set_facecolor("white")

    max_value = max((max(row) if row else 0.0) for row in series_values) if series_values else 0.0
    label_offset = max(max_value * 0.015, 0.2)

    # Axis limit setup.
    if horizontal:
        if fixed_y_max is not None:
            ax.set_xlim(0, fixed_y_max)
        elif max_value > 0:
            cap = max_value * max(1.0, y_max_scale_no_values) if not show_values else max_value + label_offset * (n_series + 2)
            ax.set_xlim(0, cap)
    else:
        if fixed_y_max is not None:
            ax.set_ylim(0, fixed_y_max)
        elif max_value > 0:
            if show_values:
                ax.set_ylim(0, max_value + label_offset * (n_series + 2))
            else:
                cap = max_value * max(1.0, y_max_scale_no_values)
                ax.set_ylim(0, cap)

    # Draw bars.
    for idx, label in enumerate(labels):
        offsets = [pos - 0.4 + (idx + 0.5) * width for pos in base_positions]
        values = series_values[idx]
        is_baseline = _is_baseline_label(label)
        color = BASELINE_BAR_COLOR if is_baseline else colors[idx % len(colors)]
        hatch = BASELINE_BAR_HATCH if is_baseline else None
        edgecolor = "black" if is_baseline else "none"
        if horizontal:
            bars = ax.barh(offsets, values, width, label=label, color=color, hatch=hatch, edgecolor=edgecolor)
        else:
            bars = ax.bar(offsets, values, width, label=label, color=color, hatch=hatch, edgecolor=edgecolor)
        if show_values:
            for bar, value in zip(bars, values):
                if value <= label_threshold:
                    continue
                stagger = label_offset * 0.2 * idx
                value_text = f"{value:.1f}%" if GROUPED_BAR_SHOW_PERCENT else f"{value:.1f}"
                if horizontal:
                    ax.text(
                        value + label_offset + stagger,
                        bar.get_y() + bar.get_height() / 2,
                        value_text,
                        ha="left",
                        va="center",
                        fontsize=GROUPED_BAR_VALUE_FONTSIZE * scale,
                    )
                else:
                    ax.text(
                        bar.get_x() + bar.get_width() / 2,
                        value + label_offset + stagger,
                        value_text,
                        ha="center",
                        va="bottom",
                        fontsize=GROUPED_BAR_VALUE_FONTSIZE * scale,
                    )

    x_tick_size = (x_tick_fontsize if x_tick_fontsize is not None else GROUPED_BAR_TICK_FONTSIZE) * scale
    y_tick_size = (y_tick_fontsize if y_tick_fontsize is not None else GROUPED_BAR_TICK_FONTSIZE) * scale
    legend_size = (legend_fontsize if legend_fontsize is not None else GROUPED_BAR_LEGEND_FONTSIZE) * scale
    label_size = GROUPED_BAR_LABEL_FONTSIZE * scale
    title_size = GROUPED_BAR_TITLE_FONTSIZE * scale

    if horizontal:
        ax.set_yticks(base_positions)
        ax.set_yticklabels(categories, fontsize=y_tick_size)
        ax.tick_params(axis="x", labelsize=x_tick_size)
        if y_tick_step and y_tick_step > 0:
            xmax = fixed_y_max if fixed_y_max is not None else ax.get_xlim()[1]
            upper = int(math.floor(float(xmax)))
            ax.set_xticks(list(range(0, upper + 1, int(y_tick_step))))
        ax.set_xlabel(ylabel, fontsize=label_size, fontweight="normal")
        ax.invert_yaxis()  # first category on top
        ax.grid(axis="x", linestyle="--", alpha=0.3)
    else:
        ax.set_xticks(base_positions)
        ax.set_xticklabels(categories, rotation=x_tick_rotation, ha=x_tick_ha, fontsize=x_tick_size)
        ax.tick_params(axis="y", labelsize=y_tick_size)
        if y_tick_step and y_tick_step > 0:
            ymax = fixed_y_max if fixed_y_max is not None else ax.get_ylim()[1]
            upper = int(math.floor(float(ymax)))
            ax.set_yticks(list(range(0, upper + 1, int(y_tick_step))))
        ax.set_ylabel(ylabel, fontsize=label_size, fontweight="normal")
        ax.grid(axis="y", linestyle="--", alpha=0.3)

    ax.set_title(title, fontsize=title_size, weight="bold")

    if legend_outside:
        handles, legend_labels = ax.get_legend_handles_labels()
        auto_ncol = legend_ncol or max(1, min(n_series, 5))
        fig.legend(
            handles,
            legend_labels,
            loc="upper center",
            bbox_to_anchor=(0.5, -0.02),
            ncol=auto_ncol,
            fontsize=legend_size,
            frameon=True,
            borderpad=legend_borderpad if legend_borderpad is not None else 0.4,
            labelspacing=legend_labelspacing if legend_labelspacing is not None else 0.5,
            handletextpad=legend_handletextpad if legend_handletextpad is not None else 0.6,
        )
    else:
        ax.legend(
            fontsize=legend_size,
            frameon=True,
            borderpad=legend_borderpad if legend_borderpad is not None else 0.4,
            labelspacing=legend_labelspacing if legend_labelspacing is not None else 0.5,
            handletextpad=legend_handletextpad if legend_handletextpad is not None else 0.6,
        )
    fig.tight_layout()
    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def _filter_empty_categories(
    categories: Sequence[str],
    series_values: Sequence[Sequence[float]],
) -> Tuple[List[str], List[List[float]]]:
    if not categories or not series_values:
        return list(categories), [list(row) for row in series_values]
    active_indices = [idx for idx in range(len(categories)) if any(row[idx] > 0 for row in series_values)]
    if not active_indices:
        return [], []
    filtered_categories = [categories[idx] for idx in active_indices]
    filtered_series = [[row[idx] for idx in active_indices] for row in series_values]
    return filtered_categories, filtered_series


def _align_series_lengths(
    categories: Sequence[str],
    series_values: Sequence[Sequence[float]],
) -> List[List[float]]:
    target_len = len(categories)
    aligned: List[List[float]] = []
    for row in series_values:
        values = list(row)
        if len(values) < target_len:
            values.extend([0.0] * (target_len - len(values)))
        elif len(values) > target_len:
            values = values[:target_len]
        aligned.append(values)
    return aligned


def _group_small_categories(
    categories: Sequence[str],
    series_values: Sequence[Sequence[float]],
    threshold: float,
    *,
    other_label: str = "Other",
) -> Tuple[List[str], List[List[float]]]:
    if not categories or not series_values:
        return list(categories), [list(row) for row in series_values]
    max_per_category: List[float] = []
    for idx in range(len(categories)):
        max_val = max((row[idx] for row in series_values), default=0.0)
        max_per_category.append(max_val)
    small_indices = [idx for idx, value in enumerate(max_per_category) if value < threshold]
    if not small_indices:
        return list(categories), [list(row) for row in series_values]
    keep_indices = [idx for idx in range(len(categories)) if idx not in small_indices]
    grouped_categories = [categories[idx] for idx in keep_indices]
    grouped_series: List[List[float]] = []
    other_totals: List[float] = []
    for row in series_values:
        kept = [row[idx] for idx in keep_indices]
        other_total = sum(row[idx] for idx in small_indices)
        other_totals.append(other_total)
        grouped_series.append(kept)
    if any(value > 0 for value in other_totals):
        grouped_categories.append(other_label)
        for row, other_total in zip(grouped_series, other_totals):
            row.append(other_total)
    return grouped_categories, grouped_series


def _keep_categories_with_other(
    categories: Sequence[str],
    series_values: Sequence[Sequence[float]],
    keep: Sequence[str],
    *,
    other_label: str = "Other",
) -> Tuple[List[str], List[List[float]]]:
    if not categories or not series_values:
        return list(categories), [list(row) for row in series_values]
    keep_set = {name.lower() for name in keep}
    keep_indices = [idx for idx, name in enumerate(categories) if str(name).lower() in keep_set]
    if not keep_indices:
        return list(categories), [list(row) for row in series_values]
    out_categories = [categories[idx] for idx in keep_indices]
    out_series: List[List[float]] = []
    other_totals: List[float] = []
    for row in series_values:
        kept = [row[idx] for idx in keep_indices]
        other_total = sum(value for idx, value in enumerate(row) if idx not in keep_indices)
        out_series.append(kept)
        other_totals.append(other_total)
    if any(value > 0 for value in other_totals):
        out_categories.append(other_label)
        for row, other_total in zip(out_series, other_totals):
            row.append(other_total)
    return out_categories, out_series


def _build_counter_series(
    stats_map: Dict[str, AggregatedStats],
    labels: Sequence[str],
    counter_getter,
    top_n: Optional[int] = 10,
) -> Tuple[List[str], List[List[float]]]:
    combined = Counter()
    for stats in stats_map.values():
        combined.update(_positive_counter(counter_getter(stats)))
    if top_n is None:
        categories = [name for name, _ in combined.most_common()]
    else:
        categories = [name for name, _ in combined.most_common(top_n)]
    if not categories:
        return [], []

    series: List[List[float]] = []
    for label in labels:
        counter = _positive_counter(counter_getter(stats_map[label]))
        total = sum(counter.values())
        if total <= 0:
            series.append([0.0 for _ in categories])
            continue
        shares = [(counter.get(cat, 0) / total) * 100 for cat in categories]
        series.append(shares)
    filtered_categories, filtered_series = _filter_empty_categories(categories, series)
    return filtered_categories, filtered_series


def _build_distribution_series(
    stats_map: Dict[str, AggregatedStats],
    labels: Sequence[str],
    values_getter,
    bins: List[Tuple[str, float, Optional[float]]],
    histogram_getter=None,
) -> Tuple[List[str], List[List[float]], bool]:
    categories = [label for label, *_ in bins]
    series: List[List[float]] = []
    any_values = False
    for label in labels:
        values = list(values_getter(stats_map[label]) or [])
        histogram = histogram_getter(stats_map[label]) if histogram_getter else None
        if histogram:
            percents = _histogram_percentages(histogram, categories)
            if any(percent > 0 for percent in percents):
                any_values = True
            series.append(percents)
            continue
        counts, total = _distribution_counts(values, bins)
        if total:
            any_values = True
        percentages = [(count / total) * 100 if total else 0.0 for _, count in counts]
        series.append(percentages)
    return categories, series, any_values


def _render_comparison_summary(
    pdf,
    plt,
    stats_map: Dict[str, AggregatedStats],
    labels: Sequence[str],
    colors: Sequence[str],
    scope_label: str,
) -> None:
    fig, axes = plt.subplots(2, 1, figsize=(8.5, 6.5), gridspec_kw={"height_ratios": [1, 3]})
    fig.patch.set_facecolor("white")

    header_ax, table_ax = axes
    header_ax.axis("off")
    table_ax.axis("off")

    header_ax.text(0.5, 0.75, "SQL Coverage Comparison", ha="center", va="center", fontsize=22, weight="bold")
    if scope_label:
        header_ax.text(0.5, 0.3, f"Scope: {scope_label}", ha="center", fontsize=12, color="#253648")

    metrics = [
        ("Statements analysed", lambda s: s.total_queries, False),
        ("Plan typed", lambda s: s.plan_typed, True),
        ("AST fallback", lambda s: s.ast_only, True),
        ("Errors", lambda s: s.error_queries, True),
    ]
    if any(stats.schema_statements for stats in stats_map.values()):
        metrics.append(("Schema records", lambda s: s.schema_statements, False))

    column_labels = ["Metric", *labels]
    table_rows: List[List[str]] = []
    for name, func, show_share in metrics:
        row = [name]
        for label in labels:
            stats = stats_map[label]
            value = func(stats)
            if show_share and stats.total_queries > 0:
                share = (value / stats.total_queries) * 100
                row.append(f"{value} ({share:.1f}%)")
            else:
                row.append(str(value))
        table_rows.append(row)

    table = table_ax.table(cellText=table_rows, colLabels=column_labels, loc="center", cellLoc="center")
    table.auto_set_font_size(False)
    table.set_fontsize(11)
    table.auto_set_column_width(list(range(len(column_labels))))
    table.scale(1.1, 1.3)

    fig.tight_layout()
    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def _render_comparison_key_metrics(
    pdf,
    plt,
    stats_map: Dict[str, AggregatedStats],
    labels: Sequence[str],
    scope_label: str,
) -> None:
    def _join_share(stats: AggregatedStats) -> Optional[str]:
        if not stats.joins_per_query:
            return None
        join_queries = sum(1 for value in stats.joins_per_query if value > 0)
        return f"{join_queries} ({_percent(join_queries, len(stats.joins_per_query))})"

    def _union_share(stats: AggregatedStats) -> Optional[str]:
        if not stats.union_all_totals:
            return None
        union_queries = sum(1 for value in stats.union_all_totals if value > 0)
        if union_queries == 0:
            return None
        return f"{union_queries} ({_percent(union_queries, len(stats.union_all_totals))})"

    metrics: List[Tuple[str, Callable[[AggregatedStats], Optional[str]]]] = [
        ("Average operators/query", lambda s: f"{statistics.mean(s.operator_totals):.1f}" if s.operator_totals else None),
        (
            "Median SQL size",
            lambda s: _format_bytes(statistics.median(s.sql_lengths)) if s.sql_lengths else None,
        ),
    ]
    if any(stats.schema_statements for stats in stats_map.values()):
        metrics.append(("Schema/view statements", lambda s: str(s.schema_statements)))

    metrics.extend(
        [
            (
                "Average joins/query",
                lambda s: f"{statistics.mean(s.joins_per_query):.1f}" if s.joins_per_query else None,
            ),
            (
                "Queries with joins",
                _join_share,
            ),
            (
                "Queries with UNION ALL",
                _union_share,
            ),
            (
                "Average UNION ALLs (when present)",
                lambda s: (
                    f"{statistics.mean([v for v in s.union_all_totals if v > 0]):.1f}"
                    if any(v > 0 for v in s.union_all_totals)
                    else None
                ),
            ),
        ]
    )

    rows: List[List[str]] = []
    for name, func in metrics:
        values: List[str] = []
        for label in labels:
            stats = stats_map[label]
            value = func(stats)
            values.append(value if value is not None else "n/a")
        if all(val == "n/a" for val in values):
            continue
        rows.append([name, *values])

    if not rows:
        return

    fig_height = 1.2 + 0.3 * max(len(rows), 1)
    fig, ax = plt.subplots(figsize=(8.5, fig_height))
    fig.patch.set_facecolor("white")
    ax.axis("off")
    title = "Key Totals"
    if scope_label:
        title += f" ({scope_label})"
    ax.set_title(title, fontsize=14, weight="bold", loc="left", color="#1f2933")
    table = ax.table(cellText=rows, colLabels=["Metric", *labels], loc="center", cellLoc="left")
    table.auto_set_font_size(False)
    table.set_fontsize(10)
    table.scale(1, 1.2)
    fig.tight_layout()
    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def _render_comparison_highlights(
    pdf,
    plt,
    stats_map: Dict[str, AggregatedStats],
    labels: Sequence[str],
) -> None:
    headers = ["Workload", "Top operators", "Top aggregate functions", "Top scalar expressions"]
    rows: List[List[str]] = []

    for label in labels:
        stats = stats_map[label]
        total_ops = sum(stats.operator_counts.values())
        top_ops = []
        if total_ops > 0:
            for op, count in stats.operator_counts.most_common(3):
                share = (count / total_ops) * 100
                top_ops.append(f"{_title_case(op)} ({share:.1f}%)")
        agg_funcs = [name for name, _ in stats.aggregate_functions.most_common(3)]
        scalar_funcs = [name for name, _ in stats.expression_functions.most_common(3)]
        rows.append(
            [
                label,
                ", ".join(top_ops) if top_ops else "n/a",
                ", ".join(agg_funcs) if agg_funcs else "n/a",
                ", ".join(scalar_funcs) if scalar_funcs else "n/a",
            ]
        )

    fig_height = 1.2 + 0.35 * max(len(rows), 1)
    fig, ax = plt.subplots(figsize=(8.5, fig_height))
    fig.patch.set_facecolor("white")
    ax.axis("off")
    ax.set_title("Operator + Function Highlights", fontsize=14, weight="bold", loc="left", color="#1f2933")
    table = ax.table(cellText=rows, colLabels=headers, loc="center", cellLoc="left")
    table.auto_set_font_size(False)
    table.set_fontsize(10)
    table.scale(1, 1.2)
    fig.tight_layout()
    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def _render_comparison_statement_types(
    pdf,
    plt,
    stats_map: Dict[str, AggregatedStats],
    labels: Sequence[str],
    colors: Sequence[str],
    top_n: int = 6,
) -> None:
    combined = Counter()
    for stats in stats_map.values():
        combined.update(_positive_counter(stats.statement_types))
    if not combined:
        return

    categories_raw = [name for name, _ in combined.most_common(top_n)]
    series: List[List[float]] = []
    for label in labels:
        counter = _positive_counter(stats_map[label].statement_types)
        total = sum(counter.values())
        if total <= 0:
            series.append([0.0 for _ in categories_raw])
            continue
        series.append([(counter.get(cat, 0) / total) * 100 for cat in categories_raw])

    categories_raw, series = _filter_empty_categories(categories_raw, series)
    if not categories_raw:
        return
    display = [_title_case(name) for name in categories_raw]
    _plot_grouped_bars(pdf, plt, display, labels, colors, series, "Statement Type Mix")


def _render_comparison_operator_mix(
    pdf,
    plt,
    stats_map: Dict[str, AggregatedStats],
    labels: Sequence[str],
    colors: Sequence[str],
    top_n: int = 8,
) -> None:
    combined = Counter()
    for stats in stats_map.values():
        combined.update(_positive_counter(stats.operator_counts))
    if not combined:
        return

    categories_raw = [name for name, _ in combined.most_common(top_n)]
    series: List[List[float]] = []
    for label in labels:
        counter = _positive_counter(stats_map[label].operator_counts)
        total = sum(counter.values())
        if total <= 0:
            series.append([0.0 for _ in categories_raw])
            continue
        series.append([(counter.get(cat, 0) / total) * 100 for cat in categories_raw])

    categories_raw, series = _filter_empty_categories(categories_raw, series)
    if not categories_raw:
        return
    display = [_title_case(name) for name in categories_raw]
    _plot_grouped_bars(pdf, plt, display, labels, colors, series, "Operator Mix (share of operators)")


def _render_comparison_histogram(
    pdf,
    plt,
    stats_map: Dict[str, AggregatedStats],
    labels: Sequence[str],
    colors: Sequence[str],
    value_getter,
    bins: List[Tuple[str, Optional[int]]],
    title: str,
    histogram_getter: Callable[[AggregatedStats], Counter] | None = None,
) -> None:
    categories = [label for label, _ in bins]
    series: List[List[float]] = []
    any_values = False
    for label in labels:
        stats = stats_map[label]
        histogram = histogram_getter(stats) if histogram_getter else None
        if histogram:
            percents = _histogram_percentages(histogram, categories)
            if any(percent > 0 for percent in percents):
                any_values = True
            series.append(percents)
            continue

        values = list(value_getter(stats) or [])
        if values:
            any_values = True
        _, percents = _bin_percentages(values, bins)
        series.append(percents)
    if not any_values:
        return
    _plot_grouped_bars(pdf, plt, categories, labels, colors, series, title)


def _render_comparison_features(
    pdf,
    plt,
    stats_map: Dict[str, AggregatedStats],
    labels: Sequence[str],
    colors: Sequence[str],
) -> None:
    feature_keys = list(FEATURE_LABELS.keys())
    display = [FEATURE_LABELS[key] for key in feature_keys]

    series: List[List[float]] = []
    for label in labels:
        stats = stats_map[label]
        total = stats.total_queries or 0
        row: List[float] = []
        for key in feature_keys:
            count = stats.feature_counts.get(key, 0)
            row.append((count / total) * 100 if total > 0 else 0.0)
        series.append(row)

    active_indices = [idx for idx in range(len(feature_keys)) if any(row[idx] > 0 for row in series)]
    if not active_indices:
        return

    filtered_display = [display[idx] for idx in active_indices]
    filtered_series: List[List[float]] = []
    for row in series:
        filtered_series.append([row[idx] for idx in active_indices])

    _plot_grouped_bars(
        pdf,
        plt,
        filtered_display,
        labels,
        colors,
        filtered_series,
        "Feature prevalence (share of queries)",
    )


def _render_comparison_function_charts(
    pdf,
    plt,
    stats_map: Dict[str, AggregatedStats],
    labels: Sequence[str],
    colors: Sequence[str],
) -> None:
    function_entries = [
        ("Aggregate Function Share", lambda s: s.aggregate_functions),
        ("Scalar Expression Function Share", lambda s: s.expression_functions),
    ]
    for title, getter in function_entries:
        categories, series = _build_counter_series(stats_map, labels, getter, top_n=8)
        if not categories:
            continue
        _plot_grouped_bars(pdf, plt, categories, labels, colors, series, title)


def _render_comparison_join_types(
    pdf,
    plt,
    stats_map: Dict[str, AggregatedStats],
    labels: Sequence[str],
    colors: Sequence[str],
) -> None:
    categories, series = _build_counter_series(stats_map, labels, lambda s: s.join_counts, top_n=8)
    if not categories:
        return
    display = [_title_case(name.replace("JOIN_TYPE_", "").replace("_", " ")) for name in categories]
    _plot_grouped_bars(pdf, plt, display, labels, colors, series, "Join Type Distribution")


def _render_comparison_join_column_types(
    pdf,
    plt,
    stats_map: Dict[str, AggregatedStats],
    labels: Sequence[str],
    colors: Sequence[str],
) -> None:
    categories, series = _build_counter_series(
        stats_map,
        labels,
        lambda s: _normalize_type_counter(s.operator_type_counts.get("join", Counter())),
        top_n=None,
    )
    if not categories:
        return
    display = [_title_case(name) for name in categories]
    legend_labels = _legend_labels(labels, SERIES_LABEL_OVERRIDES.get("logical_types"))
    logical_figsize = (GROUPED_BAR_FIGSIZE[0], GROUPED_BAR_FIGSIZE[1] * 0.75)
    emphasis_tick_size = GROUPED_BAR_TICK_FONTSIZE + 3
    emphasis_legend_size = GROUPED_BAR_LEGEND_FONTSIZE + 3
    _plot_grouped_bars(
        pdf,
        plt,
        display,
        legend_labels,
        colors,
        series,
        "Join Key Logical Types",
        keep_categories=["Number", "Text", "Date"],
        legend_outside=False,
        show_values=False,
        x_tick_rotation=0,
        x_tick_ha="center",
        y_max_scale_no_values=1.02,
        figsize=logical_figsize,
        fixed_y_max=100,
        y_tick_step=10,
        x_tick_fontsize=emphasis_tick_size,
        legend_fontsize=emphasis_legend_size,
        legend_borderpad=0.55,
        legend_labelspacing=0.55,
        legend_handletextpad=0.7,
    )


def _render_comparison_filter_ops(
    pdf,
    plt,
    stats_map: Dict[str, AggregatedStats],
    labels: Sequence[str],
    colors: Sequence[str],
) -> None:
    categories, series = _build_counter_series(stats_map, labels, lambda s: s.filter_expression_ops, top_n=10)
    if not categories:
        return
    display = [_title_case(name) for name in categories]
    _plot_grouped_bars(pdf, plt, display, labels, colors, series, "Filter Predicate Operations")


def _render_comparison_filter_types(
    pdf,
    plt,
    stats_map: Dict[str, AggregatedStats],
    labels: Sequence[str],
    colors: Sequence[str],
) -> None:
    categories, series = _build_counter_series(
        stats_map,
        labels,
        lambda s: _normalize_type_counter(s.operator_type_counts.get("filter", Counter())),
        top_n=None,
    )
    if not categories:
        return
    display = [_title_case(name) for name in categories]
    legend_labels = _legend_labels(labels, SERIES_LABEL_OVERRIDES.get("logical_types"))
    logical_figsize = (GROUPED_BAR_FIGSIZE[0], GROUPED_BAR_FIGSIZE[1] * 0.75)
    emphasis_tick_size = GROUPED_BAR_TICK_FONTSIZE + 3
    emphasis_legend_size = GROUPED_BAR_LEGEND_FONTSIZE + 3
    _plot_grouped_bars(
        pdf,
        plt,
        display,
        legend_labels,
        colors,
        series,
        "Filter Column Logical Types",
        keep_categories=["Number", "Text", "Date"],
        legend_outside=False,
        show_values=False,
        x_tick_rotation=0,
        x_tick_ha="center",
        y_max_scale_no_values=1.02,
        figsize=logical_figsize,
        fixed_y_max=100,
        y_tick_step=10,
        x_tick_fontsize=emphasis_tick_size,
        legend_fontsize=emphasis_legend_size,
        legend_borderpad=0.55,
        legend_labelspacing=0.55,
        legend_handletextpad=0.7,
    )


def _render_comparison_aggregate_types(
    pdf,
    plt,
    stats_map: Dict[str, AggregatedStats],
    labels: Sequence[str],
    colors: Sequence[str],
) -> None:
    categories, series = _build_counter_series(
        stats_map,
        labels,
        lambda s: _normalize_type_counter(s.operator_type_counts.get("aggregate", Counter())),
        top_n=None,
    )
    if not categories:
        return
    display = [_title_case(name) for name in categories]
    legend_labels = _legend_labels(labels, SERIES_LABEL_OVERRIDES.get("logical_types"))
    logical_figsize = (GROUPED_BAR_FIGSIZE[0], GROUPED_BAR_FIGSIZE[1] * 0.75)
    emphasis_tick_size = GROUPED_BAR_TICK_FONTSIZE + 3
    emphasis_legend_size = GROUPED_BAR_LEGEND_FONTSIZE + 3
    _plot_grouped_bars(
        pdf,
        plt,
        display,
        legend_labels,
        colors,
        series,
        "Aggregation Logical Types",
        keep_categories=["Number", "Text", "Date"],
        legend_outside=False,
        show_values=False,
        x_tick_rotation=0,
        x_tick_ha="center",
        y_max_scale_no_values=1.02,
        figsize=logical_figsize,
        fixed_y_max=100,
        y_tick_step=10,
        x_tick_fontsize=emphasis_tick_size,
        legend_fontsize=emphasis_legend_size,
        legend_borderpad=0.55,
        legend_labelspacing=0.55,
        legend_handletextpad=0.7,
    )


def _render_comparison_order_types(
    pdf,
    plt,
    stats_map: Dict[str, AggregatedStats],
    labels: Sequence[str],
    colors: Sequence[str],
) -> None:
    combined = Counter()
    for stats in stats_map.values():
        if stats.top_level_order_types:
            combined.update(_positive_counter(_normalize_type_counter(Counter(stats.top_level_order_types))))
        elif stats.order_by_types_histogram:
            combined.update(_positive_counter(_normalize_type_counter(Counter(stats.order_by_types_histogram))))
        elif stats.top_level_order_types_unique:
            combined.update(_positive_counter(_normalize_type_counter(Counter(stats.top_level_order_types_unique))))

    categories = [name for name, _ in combined.most_common()]
    if not categories:
        return

    series: List[List[float]] = []
    for label in labels:
        stats = stats_map[label]
        hist = stats.order_by_types_histogram
        if hist:
            normalized = _positive_counter(_normalize_type_counter(Counter(hist)))
            row = _histogram_percentages(normalized, categories)
        else:
            counter = _positive_counter(
                _normalize_type_counter(Counter(stats.top_level_order_types or stats.top_level_order_types_unique or Counter()))
            )
            total = sum(counter.values())
            if 90 <= total <= 110:
                row = _histogram_percentages(counter, categories)
            else:
                row = [(counter.get(cat, 0) / total) * 100 if total > 0 else 0.0 for cat in categories]
        series.append(row)

    categories, series = _filter_empty_categories(categories, series)
    if not categories:
        return
    display = [_title_case(name) for name in categories]
    _plot_grouped_bars(pdf, plt, display, labels, colors, series, "Top ORDER BY Types")


def _render_comparison_limit_distribution(
    pdf,
    plt,
    stats_map: Dict[str, AggregatedStats],
    labels: Sequence[str],
    colors: Sequence[str],
) -> None:
    datasets = [
        ("LIMIT with ORDER BY", lambda s: s.limit_with_order, LIMIT_WITH_ORDER_BINS, lambda s: s.limit_with_order_histogram),
        ("LIMIT without ORDER BY", lambda s: s.limit_without_order, LIMIT_NO_ORDER_BINS, lambda s: s.limit_without_order_histogram),
    ]
    for title, getter, bins, hist_getter in datasets:
        categories, series, any_values = _build_distribution_series(stats_map, labels, getter, bins, hist_getter)
        if not any_values:
            continue
        _plot_grouped_bars(pdf, plt, categories, labels, colors, series, title, show_values=False)


def _render_comparison_union_distribution(
    pdf,
    plt,
    stats_map: Dict[str, AggregatedStats],
    labels: Sequence[str],
    colors: Sequence[str],
) -> None:
    categories, series, any_values = _build_distribution_series(
        stats_map,
        labels,
        lambda s: s.union_all_totals,
        UNION_CONDITIONAL_BINS,
        lambda s: s.union_all_histogram,
    )
    if not any_values:
        return
    _plot_grouped_bars(pdf, plt, categories, labels, colors, series, "UNION ALL Occurrences")


def _render_comparison_union_fan_in_distribution(
    pdf,
    plt,
    stats_map: Dict[str, AggregatedStats],
    labels: Sequence[str],
    colors: Sequence[str],
) -> None:
    categories, series, any_values = _build_distribution_series(
        stats_map,
        labels,
        lambda s: s.union_all_fan_in_values,
        UNION_FAN_IN_BINS,
        lambda s: s.union_all_fan_in_histogram,
    )
    if not any_values:
        return
    _plot_grouped_bars(pdf, plt, categories, labels, colors, series, "UNION ALL Fan-In (inputs)")


def _render_comparison_group_by_counts(
    pdf,
    plt,
    stats_map: Dict[str, AggregatedStats],
    labels: Sequence[str],
    colors: Sequence[str],
) -> None:
    categories, series, any_values = _build_distribution_series(
        stats_map,
        labels,
        lambda s: s.group_by_key_counts,
        GROUP_BY_KEY_BINS,
        lambda s: s.group_by_key_histogram,
    )
    if not any_values:
        return
    legend_labels = _legend_labels(labels, SERIES_LABEL_OVERRIDES.get("snowflake_series"))
    compact_figsize = (GROUPED_BAR_FIGSIZE[0], GROUPED_BAR_FIGSIZE[1] * 0.75)
    emphasis_tick_size = GROUPED_BAR_TICK_FONTSIZE + 3
    emphasis_legend_size = GROUPED_BAR_LEGEND_FONTSIZE + 3
    _plot_grouped_bars(
        pdf,
        plt,
        categories,
        legend_labels,
        colors,
        series,
        "GROUP BY Key Counts",
        show_values=False,
        x_tick_rotation=0,
        x_tick_ha="center",
        figsize=compact_figsize,
        fixed_y_max=100,
        y_tick_step=10,
        x_tick_fontsize=emphasis_tick_size,
        legend_fontsize=emphasis_legend_size,
        legend_borderpad=0.55,
        legend_labelspacing=0.55,
        legend_handletextpad=0.7,
    )


def _render_comparison_join_cardinality(
    pdf,
    plt,
    stats_map: Dict[str, AggregatedStats],
    labels: Sequence[str],
    colors: Sequence[str],
) -> None:
    categories, series, any_values = _build_distribution_series(
        stats_map,
        labels,
        lambda s: [value for value in s.joins_per_query if value > 0],
        JOIN_CONDITIONAL_BINS,
        lambda s: s.joins_per_query_histogram,
    )
    if not any_values:
        return
    _plot_grouped_bars(pdf, plt, categories, labels, colors, series, "Joins per Query (when joins present)")


def _render_summary_page(
    pdf,
    plt,
    stats: AggregatedStats,
    operator_counts: Dict[str, int],
    operator_shares: Dict[str, float],
    title: str = "SQL Coverage Report",
    scope_label: str = "",
) -> None:
    fig = plt.figure(figsize=(8.5, 11))
    fig.patch.set_facecolor("white")
    fig.text(0.5, 0.94, title, ha="center", va="top", fontsize=22, weight="bold")
    subtitle = f"{stats.total_queries} statement{'s' if stats.total_queries != 1 else ''} analysed"
    fig.text(0.5, 0.90, subtitle, ha="center", fontsize=13, color="#444444")
    if scope_label:
        fig.text(0.5, 0.86, f"Scope: {scope_label}", ha="center", fontsize=12, color="#253648")
    query_total = max(stats.total_queries - stats.schema_statements, 0)

    summary = _summary_counts(stats)

    def _section(x: float, y: float, title: str, rows: List[Tuple[str, str]], bullet: bool = False) -> float:
        if not rows:
            return y
        fig.text(x, y, title, fontsize=13, weight="bold", color="#1f2933")
        y -= 0.035
        for label, value in rows:
            if bullet:
                fig.text(x + 0.015, y, f"• {label}: {value}", fontsize=11, color="#253648")
            else:
                fig.text(x, y, f"{label}: {value}", fontsize=11, color="#253648")
            y -= 0.03
        return y - 0.02

    key_rows: List[Tuple[str, str]] = [
        ("Total statements", str(summary["total"])),
        ("Parsed OK (AST)", str(summary["parsed_ast"])),
        ("Substrait plans generated", str(summary["substrait"])) ,
        ("Failed (parse/plan)", str(summary["failed"])),
    ]
    if stats.schema_statements:
        key_rows.append(("Schema/view statements", str(stats.schema_statements)))
    if stats.operator_totals:
        avg_ops = statistics.mean(stats.operator_totals)
        key_rows.append(("Average operators/query", f"{avg_ops:.1f}"))
    if stats.sql_lengths:
        median_sql = statistics.median(stats.sql_lengths)
        key_rows.append(("Median SQL size", _format_bytes(median_sql)))

    join_rows: List[Tuple[str, str]] = []
    if stats.joins_per_query:
        avg_joins = statistics.mean(stats.joins_per_query)
        join_rows.append(("Average joins/query", f"{avg_joins:.1f}"))
        join_queries = sum(1 for value in stats.joins_per_query if value > 0)
        join_rows.append(
            ("Queries with joins", f"{join_queries} ({_percent(join_queries, len(stats.joins_per_query))})")
        )

    union_rows: List[Tuple[str, str]] = []
    if stats.union_all_totals:
        union_queries = sum(1 for value in stats.union_all_totals if value > 0)
        if union_queries:
            union_rows.append(
                ("Queries with UNION ALL", f"{union_queries} ({_percent(union_queries, len(stats.union_all_totals))})")
            )
            avg_union = statistics.mean([value for value in stats.union_all_totals if value > 0])
            union_rows.append(("Average UNION ALLs (when present)", f"{avg_union:.1f}"))

    left_x = 0.08
    left_y = 0.82
    left_y = _section(left_x, left_y, "Key Totals", key_rows)
    left_y = _section(left_x, left_y, "Joins", join_rows)
    left_y = _section(left_x, left_y, "Set Operations", union_rows)

    total_queries = stats.total_queries or 1
    statement_lines: List[Tuple[str, str]] = []
    for name, count in stats.statement_types.most_common(5):
        statement_lines.append((name, f"{count} ({_percent(count, total_queries)})"))

    feature_lines: List[Tuple[str, str]] = []
    if stats.feature_counts:
        feature_stats: List[Tuple[str, float]] = []
        for key, label in FEATURE_LABELS.items():
            count = stats.feature_counts.get(key, 0)
            share = (count / query_total) * 100 if query_total else 0.0
            feature_stats.append((label, share))
        feature_stats.sort(key=lambda item: item[1], reverse=True)
        for label, share in feature_stats[:5]:
            feature_lines.append((label, f"{share:.1f}%"))

    operator_lines: List[Tuple[str, str]] = []
    if operator_counts:
        for op, count in operator_counts.most_common(5):
            share = operator_shares.get(op, 0.0) * 100
            operator_lines.append((op.replace("_", " ").title(), f"{count} ({share:.1f}%)"))

    top_funcs = stats.aggregate_functions.most_common(3)
    function_lines: List[Tuple[str, str]] = []
    if top_funcs:
        function_lines.append(("Top aggregate functions", ", ".join(func for func, _ in top_funcs)))
    expr_funcs = stats.expression_functions.most_common(3)
    if expr_funcs:
        function_lines.append(("Top scalar expressions", ", ".join(func for func, _ in expr_funcs)))

    right_x = 0.55
    right_y = 0.82
    right_y = _section(right_x, right_y, "Statement Types", statement_lines, bullet=True)
    right_y = _section(right_x, right_y, "Feature Prevalence", feature_lines, bullet=True)
    right_y = _section(right_x, right_y, "Operator Highlights", operator_lines, bullet=True)
    _section(right_x, right_y, "Function Highlights", function_lines)

    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def _render_statement_type_page(pdf, plt, statement_types: Counter, palette: List[str]) -> None:
    if not statement_types:
        return
    labels, counts = zip(*statement_types.most_common())
    fig, ax = plt.subplots(figsize=(8.5, 5))
    fig.patch.set_facecolor("white")
    indices = range(len(labels))
    colors = [palette[i % len(palette)] for i in indices]
    bars = ax.bar(indices, counts, color=colors)
    ax.set_title("Statement Types", fontsize=14, weight="bold")
    ax.set_xlabel("Type", fontsize=12)
    ax.set_ylabel("Count", fontsize=12)
    ax.set_xticks(indices)
    ax.set_xticklabels(labels, rotation=45, ha="right")
    ax.grid(axis="y", linestyle="--", alpha=0.3)
    total = sum(counts) or 1
    for bar, count in zip(bars, counts):
        share = (count / total) * 100
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height(),
            f"{share:.1f}%",
            ha="center",
            va="bottom",
            fontsize=10,
        )
    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def _render_operator_bar_chart(
    pdf,
    plt,
    operator_counts: Dict[str, int],
    operator_shares: Dict[str, float],
    palette: List[str],
    source_label: Optional[str] = None,
) -> None:
    fig, ax = plt.subplots(figsize=(8.5, 5))
    fig.patch.set_facecolor("white")

    operators, counts = zip(*operator_counts.most_common())
    colors = [palette[i % len(palette)] for i in range(len(operators))]
    bars = ax.bar(range(len(operators)), counts, color=colors)

    title = "Operator Frequency"
    if source_label:
        title += f" (source: {source_label})"
    ax.set_title(_title_case(title), fontsize=14, weight="bold")
    ax.set_xlabel("Operator", fontsize=12)
    ax.set_ylabel("Count", fontsize=12)
    ax.set_xticks(range(len(operators)))
    ax.set_xticklabels([op.replace("_", " ") for op in operators], rotation=45, ha="right")
    ax.grid(axis="y", linestyle="--", alpha=0.3)

    for bar, op in zip(bars, operators):
        share = operator_shares.get(op, 0.0) * 100
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height(), f"{share:.1f}%", ha="center", va="bottom", fontsize=10)

    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def _render_operator_type_pages(
    pdf,
    plt,
    operator_counts: Dict[str, int],
    operator_type_counts: Dict[str, Counter],
    palette: List[str],
    title_suffix: str = "",
    focus_ops: Optional[List[str]] = None,
) -> None:
    if not operator_type_counts:
        return

    excluded = {"join", "filter", "sort", "project", "aggregate_result", "scan", "aggregate", "group_by"}
    if focus_ops is not None:
        top_ops = [op for op in focus_ops if op in operator_type_counts]
    else:
        top_ops = [
            op for op, _ in operator_counts.most_common() if op in operator_type_counts and op not in excluded
        ]
        if not top_ops:
            top_ops = [op for op in operator_type_counts.keys() if op not in excluded]
    if not top_ops:
        return

    if focus_ops is not None:
        subsets = [top_ops]
        columns = 1
    else:
        chunk_size = 4
        subsets = [top_ops[i : i + chunk_size] for i in range(0, len(top_ops), chunk_size)]
        columns = 2

    for subset in subsets:
        if not subset:
            continue
        if columns == 1:
            rows = len(subset)
            fig_height = max(3.5, rows * 2.6)
            fig, axes = plt.subplots(rows, 1, figsize=(11, fig_height))
        else:
            rows = math.ceil(len(subset) / columns)
            fig, axes = plt.subplots(rows, columns, figsize=(8.5, 11))
        fig.patch.set_facecolor("white")
        if hasattr(axes, "flatten"):
            axes = axes.flatten()
        else:
            axes = [axes]
        axes = list(axes)

        for ax, op in zip(axes, subset):
            counter = _bucket_counter(operator_type_counts.get(op, Counter()))
            if not counter:
                ax.axis("off")
                continue
            labels, values = _top_counter_items(counter, top_n=5, other_label="Other")
            shares = counter_shares(Counter(dict(zip(labels, values))))
            colors = [palette[j % len(palette)] for j in range(len(labels))]
            y_positions = range(len(labels))
            ax.barh(y_positions, [shares[label] * 100 for label in labels], color=colors)
            ax.set_yticks(y_positions)
            ax.set_yticklabels(labels)
            ax.invert_yaxis()
            ax.set_xlabel("Share (%)", fontsize=11)
            suffix = f" {title_suffix}" if title_suffix else ""
            ax.set_title(_title_case(f"Type share for {op.replace('_', ' ')}{suffix}"), fontsize=12, weight="bold")
            ax.set_xlim(0, 100)
            ax.grid(axis="x", linestyle="--", alpha=0.3)
            for pos, label in zip(y_positions, labels):
                share_pct = shares.get(label, 0.0) * 100
                text_x = min(share_pct + 2.5, 97)
                ax.text(text_x, pos, f"{share_pct:.1f}%", va="center", fontsize=10)

        for ax in axes[len(subset) :]:
            ax.axis("off")

        fig.tight_layout()
        pdf.savefig(fig, bbox_inches="tight")
        plt.close(fig)


def _render_logical_type_page(
    pdf,
    plt,
    operator_type_counts: Dict[str, Counter],
    palette: List[str],
    title_suffix: str = "",
    order_by_override: Optional[Counter] = None,
) -> None:
    suffix = f" {title_suffix}" if title_suffix else ""
    groups = [
        (_title_case(f"Join Keys Logical Types{suffix}"), _bucket_counter(operator_type_counts.get("join", Counter()))),
        (_title_case(f"Filter Column Logical Types{suffix}"), _bucket_counter(operator_type_counts.get("filter", Counter()))),
    ]

    order_source = order_by_override or operator_type_counts.get("sort", Counter())
    if order_source:
        if order_by_override is not None:
            order_counter = _bucket_counter(Counter(order_source))
        else:
            order_counter = _bucket_counter(order_source)
        groups.append((_title_case(f"Order By Logical Types{suffix}"), order_counter))
    valid = [(title, counter) for title, counter in groups if counter]
    if not valid:
        return

    rows = len(valid)
    height = max(3.5, rows * 2.6)
    fig, axes = plt.subplots(rows, 1, figsize=(8.5, height))
    fig.patch.set_facecolor("white")
    if rows == 1:
        axes = [axes]

    for ax, (title, counter) in zip(axes, valid):
        labels, counts = zip(*counter.most_common(7))
        total = sum(counts) or 1
        shares = [(count / total) * 100 for count in counts]
        y_positions = range(len(labels))
        colors = [palette[i % len(palette)] for i in range(len(labels))]
        ax.barh(y_positions, shares, color=colors)
        ax.set_yticks(list(y_positions))
        ax.set_yticklabels(labels)
        ax.invert_yaxis()
        ax.set_xlim(0, 100)
        ax.grid(axis="x", linestyle="--", alpha=0.3)
        ax.set_xlabel("Share (%)", fontsize=11)
        ax.set_title(_title_case(title), fontsize=12, weight="bold")
        for pos, share in zip(y_positions, shares):
            display_share = f"{share:.1f}%"
            offset = min(share + 2.5, 97)
            ax.text(offset, pos, display_share, va="center", fontsize=10)

    fig.tight_layout(h_pad=1.0)
    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def _render_function_page(pdf, plt, stats: AggregatedStats, palette: List[str]) -> None:
    entries = [
        ("Top Aggregate Functions", stats.aggregate_functions),
        ("Top Scalar Expressions", stats.expression_functions),
    ]
    available = [(title, counter) for title, counter in entries if counter]
    if not available:
        return

    columns = len(available)
    fig, axes = plt.subplots(1, columns, figsize=(5 * columns, 5))
    if columns == 1:
        axes = [axes]
    fig.patch.set_facecolor("white")

    for ax, (title, counter) in zip(axes, available):
        labels, counts = zip(*counter.most_common(10))
        total = sum(counts) or 1
        shares = [(count / total) * 100 for count in counts]
        colors = [palette[i % len(palette)] for i in range(len(labels))]
        ax.bar(range(len(labels)), shares, color=colors)
        ax.set_title(_title_case(title), fontsize=12, weight="bold")
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels(labels, rotation=45, ha="right")
        ax.set_ylabel("Share (%)", fontsize=11)
        ax.set_ylim(0, max(shares) * 1.15 if shares else 1)
        ax.grid(axis="y", linestyle="--", alpha=0.3)
        for idx, share in enumerate(shares):
            ax.text(idx, share, f"{share:.1f}%", ha="center", va="bottom", fontsize=9)

    fig.tight_layout()
    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def _render_feature_table(pdf, plt, stats: AggregatedStats, query_total: int) -> None:
    fig, ax = plt.subplots(figsize=CDF_FIGSIZE)
    fig.patch.set_facecolor("white")
    ax.axis("off")

    labels = []
    shares = []
    for key, label in FEATURE_LABELS.items():
        count = stats.feature_counts.get(key, 0)
        labels.append(label)
        share = (count / query_total) * 100 if query_total else 0.0
        shares.append(f"{share:.1f}%")

    table_data = [[label, share] for label, share in zip(labels, shares)]
    table = ax.table(cellText=table_data, colLabels=["Feature", "Prevalence"], loc="center")
    table.auto_set_font_size(False)
    table.set_fontsize(10)
    table.scale(1, 1.5)
    ax.set_title(_title_case("Feature prevalence (SELECT statements)"), fontsize=12, weight="bold")

    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def _render_join_type_table(pdf, plt, join_counts: Counter) -> None:
    shares = _join_type_shares(join_counts)
    if not shares:
        return
    order = ["Inner", "Outer", "Semi", "Anti"]
    table_rows = []
    for category in order:
        share = shares.get(category)
        if share is not None:
            table_rows.append((_title_case(category), f"{share:.1f}%"))
    if not table_rows:
        return

    fig, ax = plt.subplots(figsize=(6, 2.5))
    fig.patch.set_facecolor("white")
    ax.axis("off")

    table = ax.table(
        cellText=[[label, share] for label, share in table_rows],
        colLabels=["Join Type", "Share (%)"],
        loc="center",
    )
    table.auto_set_font_size(False)
    table.set_fontsize(10)
    table.scale(1.2, 1.4)
    ax.set_title(_title_case("Join Type Distribution"), fontsize=12, weight="bold")

    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def _render_join_count_table(pdf, plt, join_values: Iterable[int]) -> None:
    shares = _join_count_shares(join_values)
    if not shares:
        return

    rows = [(label, f"{shares[label]:.1f}%") for label in shares]
    fig, ax = plt.subplots(figsize=(6, 3))
    fig.patch.set_facecolor("white")
    ax.axis("off")

    table = ax.table(cellText=rows, colLabels=["Join Count", "Share (%)"], loc="center")
    table.auto_set_font_size(False)
    table.set_fontsize(10)
    table.scale(1.2, 1.4)
    ax.set_title(_title_case("Join Count Distribution"), fontsize=12, weight="bold")

    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def _render_filter_expression_table(pdf, plt, filter_ops: Counter) -> None:
    total = sum(filter_ops.values())
    if total <= 0:
        return

    top_items = filter_ops.most_common(10)
    rows = []
    for key, value in top_items:
        share = (value / total) * 100
        label = FILTER_EXPRESSION_LABELS.get(key, key)
        rows.append((_title_case(label.replace("_", " ")), f"{share:.1f}%"))

    if not rows:
        return

    fig, ax = plt.subplots(figsize=(6.5, 3.5))
    fig.patch.set_facecolor("white")
    ax.axis("off")

    table = ax.table(
        cellText=[[label, share] for label, share in rows],
        colLabels=["Expr", "Share (%)"],
        loc="center",
    )
    table.auto_set_font_size(False)
    table.set_fontsize(10)
    table.scale(1.2, 1.4)
    ax.set_title(_title_case("Filter predicate distribution"), fontsize=12, weight="bold")

    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def _render_group_by_key_table(pdf, plt, group_counts: List[int]) -> None:
    if not group_counts:
        return

    total = len(group_counts)
    if total <= 0:
        return

    shares: List[str] = []
    for label, lower, upper in GROUP_BY_KEY_BINS:
        count = 0
        for value in group_counts:
            if value < lower:
                continue
            if upper is not None and value > upper:
                continue
            count += 1
        share = (count / total) * 100
        shares.append(f"{share:.1f}%")

    if not shares:
        return

    fig, ax = plt.subplots(figsize=(6.5, 2.75))
    fig.patch.set_facecolor("white")
    ax.axis("off")

    col_labels = [label for label, _lower, _upper in GROUP_BY_KEY_BINS]
    table = ax.table(
        cellText=[shares],
        rowLabels=["Share (%)"],
        colLabels=col_labels,
        loc="center",
    )
    table.auto_set_font_size(False)
    table.set_fontsize(10)
    table.scale(1.1, 1.3)
    ax.set_title(_title_case("Aggregation group by keys"), fontsize=12, weight="bold")

    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def _render_count_distribution(
    pdf,
    plt,
    values: List[int],
    title: str,
    palette: List[str],
    include_zero: bool = False,
    max_bars: int = 8,
    bucket_scheme: Optional[List[Tuple[str, int, Optional[int]]]] = None,
) -> None:
    if not values:
        return
    filtered = [int(value) for value in values if include_zero or value > 0]
    if not filtered:
        return
    if bucket_scheme:
        bucket_counts = Counter()
        for value in filtered:
            for label, lower, upper in bucket_scheme:
                if value < lower:
                    continue
                if upper is None or value <= upper:
                    bucket_counts[label] += 1
                    break
        labels = [label for label, _, _ in bucket_scheme if bucket_counts.get(label)]
        counts = [bucket_counts[label] for label in labels]
    else:
        counter = Counter(filtered)
        sorted_items = sorted(counter.items(), key=lambda item: item[0])
        if len(sorted_items) > max_bars:
            head = sorted_items[: max_bars - 1]
            tail = sorted_items[max_bars - 1 :]
            threshold = tail[0][0]
            remainder = sum(count for _, count in tail)
            bucketed = head + [(f"{threshold}+", remainder)]
        else:
            bucketed = [(str(value), count) for value, count in sorted_items]
        labels = [str(label) for label, _ in bucketed]
        counts = [count for _, count in bucketed]

    if not labels:
        return

    fig, ax = plt.subplots(figsize=(8.5, 4.5))
    fig.patch.set_facecolor("white")

    positions = range(len(labels))
    colors = [palette[i % len(palette)] for i in positions]
    bars = ax.bar(positions, counts, color=colors)

    ax.set_title(title, fontsize=14, weight="bold")
    ax.set_xlabel("Count", fontsize=12)
    ax.set_ylabel("Queries", fontsize=12)
    ax.set_xticks(list(positions))
    ax.set_xticklabels(labels)
    ax.grid(axis="y", linestyle="--", alpha=0.3)

    total = sum(counts) or 1
    for idx, bar in enumerate(bars):
        share = (counts[idx] / total) * 100
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height(),
            f"{share:.1f}%",
            ha="center",
            va="bottom",
            fontsize=10,
        )

    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def _render_conditional_distribution_chart(
    pdf,
    plt,
    values: List[int],
    bins: List[Tuple[str, int, Optional[int]]],
    title: str,
    palette: List[str],
    note: Optional[str] = None,
) -> None:
    if not values:
        return
    counts, total = _distribution_counts(values, bins)
    if total == 0:
        return
    labels = [label for label, _ in counts]
    totals = [count for _, count in counts]
    fig, ax = plt.subplots(figsize=(8.5, 4.5))
    fig.patch.set_facecolor("white")
    positions = range(len(labels))
    colors = [palette[i % len(palette)] for i in positions]
    bars = ax.bar(positions, totals, color=colors)
    ax.set_title(title, fontsize=14, weight="bold")
    ax.set_xlabel("Bucket", fontsize=12)
    ax.set_ylabel("Queries", fontsize=12)
    ax.set_xticks(list(positions))
    ax.set_xticklabels(labels)
    ax.grid(axis="y", linestyle="--", alpha=0.3)
    for idx, bar in enumerate(bars):
        share = (totals[idx] / total) * 100 if total else 0.0
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height(),
            f"{share:.1f}%",
            ha="center",
            va="bottom",
            fontsize=10,
        )
    if note:
        ax.text(0.98, 0.98, note, ha="right", va="top", transform=ax.transAxes, fontsize=10, color="#253648")
    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def _render_limit_distribution(pdf, plt, stats: AggregatedStats, palette: List[str]) -> None:
    counts_with, total_with = _distribution_counts(stats.limit_with_order, LIMIT_WITH_ORDER_BINS)
    if not total_with:
        hist_counts, hist_total = _counts_from_histogram(stats.limit_with_order_histogram, LIMIT_WITH_ORDER_BINS)
        if hist_total:
            counts_with = hist_counts
            total_with = hist_total

    counts_without, total_without = _distribution_counts(stats.limit_without_order, LIMIT_NO_ORDER_BINS)
    if not total_without:
        hist_counts, hist_total = _counts_from_histogram(stats.limit_without_order_histogram, LIMIT_NO_ORDER_BINS)
        if hist_total:
            counts_without = hist_counts
            total_without = hist_total

    # Always render both tables so the LIMIT page is never empty.
    panels: List[Tuple[str, List[Tuple[str, int]], int]] = [
        ("LIMIT with ORDER BY", counts_with, total_with),
        ("LIMIT without ORDER BY", counts_without, total_without),
    ]
    cols = len(panels)
    fig, axes = plt.subplots(1, cols, figsize=(6 * cols, 4))
    fig.patch.set_facecolor("white")
    axes_iterable = axes
    if not isinstance(axes_iterable, (list, tuple)):
        try:
            axes_iterable = list(axes_iterable)
        except TypeError:
            axes_iterable = [axes_iterable]
    axes_list = list(axes_iterable)
    for ax, (title, table_counts, total) in zip(axes_list, panels):
        ax.axis("off")
        rows = []
        for label, count in table_counts:
            share = (count / total) * 100 if total else 0.0
            rows.append([label, count, f"{share:.1f}%"])
        table = ax.table(
            cellText=rows,
            colLabels=["Bucket", "Count", "Share"],
            loc="center",
        )
        table.auto_set_font_size(False)
        table.set_fontsize(9)
        table.scale(1, 1.4)
        ax.set_title(title + " (SELECT statements)", fontsize=12, weight="bold")
        ax.text(0.5, 0.05, f"N={total}", ha="center", va="bottom", transform=ax.transAxes, fontsize=10, color="#253648")
    fig.tight_layout()
    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def _render_histogram(
    pdf, plt, values: List[float], bins: List[Tuple[str, Optional[float]]], title: str, palette: List[str]
) -> None:
    labels, percentages = _bin_percentages(values, bins)
    fig, ax = plt.subplots(figsize=(8.5, 5))
    fig.patch.set_facecolor("white")

    colors = [palette[i % len(palette)] for i in range(len(labels))]
    ax.bar(range(len(labels)), percentages, color=colors)
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, rotation=45, ha="right")
    ax.set_ylabel("Share (%)", fontsize=12)
    ax.set_title(title, fontsize=14, weight="bold")
    ax.grid(axis="y", linestyle="--", alpha=0.3)

    for idx, pct in enumerate(percentages):
        ax.text(idx, pct, f"{pct:.1f}%", ha="center", va="bottom", fontsize=10)

    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def _render_schema_summary_page(pdf, plt, profile: SchemaProfile) -> None:
    fig, ax = plt.subplots(figsize=(8.5, 3.5))
    fig.patch.set_facecolor("white")
    ax.axis("off")

    pk_share = _percent(profile.tables_with_primary_keys, profile.table_count) if profile.table_count else "0%"
    fk_share = _percent(profile.tables_with_foreign_keys, profile.table_count) if profile.table_count else "0%"
    lines = [
        ("Tables parsed", _format_integer(profile.table_count)),
        ("Columns parsed", _format_integer(profile.total_columns)),
        ("Distinct column types", _format_integer(len(profile.column_type_counts))),
    ]
    if profile.tables_with_primary_keys:
        lines.extend(
            [
                ("Tables w/ primary key", f"{_format_integer(profile.tables_with_primary_keys)} ({pk_share})"),
                ("Primary key columns", _format_integer(profile.total_primary_key_columns)),
                ("Primary key types", _format_integer(len(profile.primary_key_type_counts))),
            ]
        )
    else:
        lines.append(("Tables w/ primary key", "None detected"))
    if profile.tables_with_foreign_keys:
        lines.extend(
            [
                ("Tables w/ foreign key", f"{_format_integer(profile.tables_with_foreign_keys)} ({fk_share})"),
                ("Foreign key columns", _format_integer(profile.total_foreign_key_columns)),
                ("Foreign key types", _format_integer(len(profile.foreign_key_type_counts))),
            ]
        )
    else:
        lines.append(("Tables w/ foreign key", "None detected"))

    fig.text(0.5, 0.85, "Schema Overview", ha="center", va="center", fontsize=18, weight="bold", color="#0b66c3")
    y = 0.65
    for label, value in lines:
        fig.text(0.25, y, label + ":", ha="left", va="center", fontsize=12, weight="bold")
        fig.text(0.58, y, value, ha="left", va="center", fontsize=12)
        y -= 0.1

    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def _render_schema_distribution_chart(
    pdf,
    plt,
    counter: Counter,
    title: str,
    palette: List[str],
) -> None:
    labels, values = _top_counter_items(counter, top_n=8)
    if not labels:
        return
    total = sum(values)
    fig, ax = plt.subplots(figsize=(8.5, 4.5))
    fig.patch.set_facecolor("white")
    colors = [palette[idx % len(palette)] for idx in range(len(labels))]
    positions = range(len(labels))
    bars = ax.bar(positions, values, color=colors)
    ax.set_xticks(list(positions))
    ax.set_xticklabels([_title_case(label) for label in labels], rotation=45, ha="right")
    ax.set_ylabel("Columns", fontsize=12)
    ax.set_title(title, fontsize=14, weight="bold")
    ax.grid(axis="y", linestyle="--", alpha=0.3)

    for idx, bar in enumerate(bars):
        share = (values[idx] / total) * 100 if total else 0.0
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height(),
            f"{share:.1f}%",
            ha="center",
            va="bottom",
            fontsize=10,
        )

    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def _render_key_type_distribution_chart(
    pdf,
    plt,
    primary_counts: Counter,
    foreign_counts: Counter,
    title: str,
    palette: List[str],
) -> None:
    combined = Counter(primary_counts)
    combined.update(foreign_counts)
    labels = [label for label, _ in combined.most_common(8)]
    if not labels:
        return

    pk_values = [primary_counts.get(label, 0) for label in labels]
    fk_values = [foreign_counts.get(label, 0) for label in labels]
    pk_total = sum(primary_counts.values())
    fk_total = sum(foreign_counts.values())
    colors = list(palette or DEFAULT_PALETTE)
    if len(colors) < 2:
        colors = colors + DEFAULT_PALETTE[: 2 - len(colors)]

    positions = list(range(len(labels)))
    width = 0.35
    fig, ax = plt.subplots(figsize=(8.5, 4.5))
    fig.patch.set_facecolor("white")

    pk_bars = ax.bar([pos - width / 2 for pos in positions], pk_values, width, label="Primary Key", color=colors[0])
    fk_bars = ax.bar([pos + width / 2 for pos in positions], fk_values, width, label="Foreign Key", color=colors[1])

    def _annotate(bars, total):
        if total <= 0:
            return
        for bar in bars:
            height = bar.get_height()
            if height <= 0:
                continue
            share = (height / total) * 100 if total else 0.0
            ax.text(bar.get_x() + bar.get_width() / 2, height, f"{share:.1f}%", ha="center", va="bottom", fontsize=9)

    _annotate(pk_bars, pk_total)
    _annotate(fk_bars, fk_total)

    ax.set_xticks(positions)
    ax.set_xticklabels([_title_case(label) for label in labels], rotation=45, ha="right")
    ax.set_ylabel("Columns", fontsize=12)
    ax.set_title(title, fontsize=14, weight="bold")
    ax.legend()
    ax.grid(axis="y", linestyle="--", alpha=0.3)

    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def _render_data_summary_page(pdf, plt, profile: DataProfile) -> None:
    fig, ax = plt.subplots(figsize=(8.5, 4))
    fig.patch.set_facecolor("white")
    ax.axis("off")

    sizes = profile.sizes()
    rows = profile.row_counts()
    p90_size = _percentile_value(sizes, 90)
    p99_size = _percentile_value(sizes, 99)
    p90_rows = _percentile_value(rows, 90)
    p99_rows = _percentile_value(rows, 99)
    row_coverage = (len(rows) / profile.table_count * 100) if rows and profile.table_count else 0.0
    row_line = f"{len(rows)}/{profile.table_count} tables ({row_coverage:.1f}%)" if profile.table_count else "0"

    lines: List[Tuple[str, str]] = []

    def add_section(title: str) -> None:
        lines.append(("__SECTION__", title))

    add_section("Tables & Data Volume")
    lines.append(("Tables scanned", str(profile.table_count)))
    lines.append(("Total data volume", _format_bytes(profile.total_bytes)))
    if sizes:
        lines.append(("P90 table size (per table)", _format_bytes(p90_size) if p90_size is not None else "n/a"))
        lines.append(("P99 table size (per table)", _format_bytes(p99_size) if p99_size is not None else "n/a"))

    add_section("Row Coverage")
    lines.append(("Tables w/ row counts", row_line if rows else "Not collected"))
    if rows:
        lines.append(("P90 rows per table", _format_integer(p90_rows)))
        lines.append(("P99 rows per table", _format_integer(p99_rows)))

    add_section("Column Behavior")
    run_lengths = profile.run_length_means()
    if run_lengths:
        median_run = _percentile_value(run_lengths, 50)
        lines.append(("Median run length", _format_float(median_run, 2)))
    sorted_columns = profile.sorted_column_count()
    if profile.column_count:
        sorted_share = sorted_columns / profile.column_count
        lines.append(
            (
                "Columns sorted",
                f"{sorted_columns}/{profile.column_count} ({_format_percent(sorted_share)})",
            )
        )
    histogram_q = profile.histogram_q_errors()
    if histogram_q:
        lines.append(("Columns w/ histogram skew", str(profile.histogram_count)))

    fig.text(0.5, 0.9, "Data Files Overview", ha="center", va="center", fontsize=18, weight="bold", color="#0b66c3")
    y = 0.78
    for label, value in lines:
        if label == "__SECTION__":
            fig.text(0.25, y, value, ha="left", va="center", fontsize=13, weight="bold", color="#0b66c3")
            y -= 0.08
            continue
        fig.text(0.25, y, label + ":", ha="left", va="center", fontsize=12, weight="bold")
        fig.text(0.58, y, value, ha="left", va="center", fontsize=12)
        y -= 0.06

    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def _render_mcv_curves(pdf, plt, profile: DataProfile, palette: Sequence[str], options: ReportOptions) -> None:
    charts: List[Tuple[str, List[float], bool]] = []
    topk = profile.topk_fractions()
    if topk:
        charts.append(("Sum of MCV frequencies", topk, True))
    max_mcv = profile.max_mcv_fractions()
    if max_mcv:
        charts.append(("Maximum MCV frequency", max_mcv, True))
    nulls = profile.null_fractions()
    if nulls:
        charts.append(("Null fraction", nulls, False))
    ndv = profile.ndv_ratios(
        denominator=options.ndv_ratio_denominator,
        universe=options.ndv_column_universe,
        clamp=True,
    )
    if ndv:
        charts.append((_ndv_title(options), ndv, False))
    if not charts:
        return

    fig, axes = plt.subplots(1, len(charts), figsize=(6 * len(charts), 3))
    if len(charts) == 1:
        axes = [axes]
    color = palette[0] if palette else DEFAULT_PALETTE[0]
    k_value = profile.requested_k()

    for ax, (title, values, show_k) in zip(axes, charts):
        xs, ys = _percentile_curve(values)
        ax.plot(xs, ys, color, linewidth=2)
        ax.set_xlim(0, 100)
        ax.set_ylim(0, 100)
        ax.set_xlabel("Percentile of columns")
        ax.set_ylabel("Fraction (%)")
        label = title
        if show_k and k_value:
            label = f"{title} (k={k_value})"
        ax.set_title(label, fontsize=12, weight="bold")
        ax.grid(True, linestyle="--", alpha=0.3)

    fig.tight_layout()
    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def _render_histogram_skew_chart(pdf, plt, profile: DataProfile, palette: Sequence[str]) -> None:
    values = [value for value in profile.histogram_q_errors() if value > 0]
    xs, ys = _percentile_curve_unbounded(values)
    if not xs:
        return
    color = palette[0] if palette else DEFAULT_PALETTE[0]
    fig, ax = plt.subplots(figsize=(6, 3))
    fig.patch.set_facecolor("white")
    ax.plot(xs, ys, color=color, linewidth=2)
    ax.set_xlim(0, 100)
    ax.set_xlabel("Percentile of numeric columns")
    ax.set_ylabel("Mean Q-error (log scale)")
    ax.set_yscale("log")
    ax.set_title("Histogram Skew (mean Q-error)", fontsize=12, weight="bold")
    ax.grid(True, which="both", linestyle="--", alpha=0.3)
    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def _render_additional_data_histograms(pdf, plt, profile: DataProfile, palette: Sequence[str]) -> None:
    palette_list = list(palette or DEFAULT_PALETTE)
    distributions: List[Tuple[List[float], List[Tuple[str, Optional[float]]], str]] = []

    q_errors = profile.histogram_q_errors()
    if q_errors:
        distributions.append((q_errors, MEAN_QERROR_BINS, "Mean Histogram Q-Error (columns)"))

    bytes_per_row = profile.bytes_per_row_values()
    if bytes_per_row:
        distributions.append((bytes_per_row, BYTES_PER_ROW_BINS, "Bytes per Row (tables)"))

    outlier_rates = profile.numeric_outlier_rates()
    if outlier_rates:
        distributions.append((outlier_rates, OUTLIER_RATE_BINS, "Numeric Column Outlier Rate"))

    string_lengths = profile.string_avg_lengths()
    if string_lengths:
        distributions.append((string_lengths, STRING_LENGTH_BINS, "Average String Length (columns)"))

    for values, bins, title in distributions:
        _render_histogram(pdf, plt, values, bins, title, palette_list)


def _render_workload_metadata_table(
    pdf,
    plt,
    metadata_map: Dict[str, Dict[str, Any]],
    labels: Sequence[str],
) -> None:
    if not metadata_map:
        return
    headers = [
        "Workload",
        "Configs",
        "Schemas",
        "Queries",
        "Data Directories",
        "Coverage",
        "Data Metrics",
    ]

    def _format_meta(value: Any) -> str:
        if value is None:
            return "n/a"
        if isinstance(value, list):
            if not value:
                return "n/a"
            preview = value[:3]
            text = "\n".join(preview)
            if len(value) > 3:
                text += f"\n(+{len(value) - 3} more)"
            return text
        return str(value)

    rows: List[List[str]] = []
    for label in labels:
        meta = metadata_map.get(label) or {}
        rows.append(
            [
                label,
                _format_meta(meta.get("config_paths")),
                _format_meta(meta.get("schema_paths")),
                _format_meta(meta.get("query_specs")),
                _format_meta(meta.get("data_directories")),
                _format_meta(meta.get("coverage_files")),
                _format_meta(meta.get("data_metrics")),
            ]
        )

    fig_height = 1.5 + 0.45 * len(rows)
    fig, ax = plt.subplots(figsize=(8.5, fig_height))
    fig.patch.set_facecolor("white")
    ax.axis("off")
    table = ax.table(cellText=rows, colLabels=headers, loc="center", cellLoc="left")
    table.auto_set_font_size(False)
    table.set_fontsize(8.5)
    table.scale(1, 1.3)
    fig.tight_layout()
    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def _render_data_comparison_summary(pdf, plt, profiles: Dict[str, DataProfile | None], labels: Sequence[str]) -> None:
    headers = [
        "Workload",
        "Tables",
        "Total bytes",
        "Median table size",
        "Median rows / table",
        "Median bytes / row",
        "Row coverage",
        "Columns sorted",
    ]
    rows: List[List[str]] = []
    for label in labels:
        profile = profiles.get(label)
        if not profile or profile.table_count <= 0:
            rows.append([label, "n/a", "n/a", "n/a", "n/a", "n/a", "n/a", "n/a"])
            continue
        sizes = profile.sizes()
        rows_list = profile.row_counts()
        bytes_per_row = profile.bytes_per_row_values()
        median_size = _percentile_value(sizes, 50)
        median_rows = _percentile_value(rows_list, 50)
        median_bytes = _percentile_value(bytes_per_row, 50)
        sorted_columns = profile.sorted_column_count()
        sorted_text = "n/a"
        if profile.column_count:
            sorted_text = f"{sorted_columns}/{profile.column_count} ({_format_percent(sorted_columns / profile.column_count)})"
        coverage = "n/a"
        if profile.table_count:
            with_rows = len(rows_list)
            coverage = f"{with_rows}/{profile.table_count} ({_format_percent(with_rows / profile.table_count)})"
        rows.append(
            [
                label,
                _format_integer(profile.table_count),
                _format_bytes(profile.total_bytes),
                _format_bytes(median_size) if median_size is not None else "n/a",
                _format_integer(median_rows),
                _format_bytes(median_bytes) if median_bytes is not None else "n/a",
                coverage,
                sorted_text,
            ]
        )

    fig_height = 1.6 + 0.35 * max(len(rows), 1)
    fig, ax = plt.subplots(figsize=(8.5, fig_height))
    fig.patch.set_facecolor("white")
    ax.axis("off")
    table = ax.table(cellText=rows, colLabels=headers, loc="center", cellLoc="left")
    table.auto_set_font_size(False)
    table.set_fontsize(9)
    table.scale(1, 1.2)
    fig.tight_layout()
    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def _build_profile_distribution_series(
    profiles: Dict[str, DataProfile | None],
    labels: Sequence[str],
    values_getter,
    bins: List[Tuple[str, float, Optional[float]]],
) -> Tuple[List[str], List[List[float]], bool]:
    categories = [label for label, *_ in bins]
    series: List[List[float]] = []
    any_values = False
    for label in labels:
        profile = profiles.get(label)
        values = values_getter(profile) if profile else []
        counts, total = _distribution_counts(values, bins)
        if total:
            any_values = True
        percentages = [(count / total) * 100 if total else 0.0 for _, count in counts]
        series.append(percentages)
    return categories, series, any_values


def _render_data_comparison_distribution(
    pdf,
    plt,
    profiles: Dict[str, DataProfile | None],
    labels: Sequence[str],
    colors: Sequence[str],
    title: str,
    values_getter,
    bins: List[Tuple[str, float, Optional[float]]],
) -> None:
    categories, series, any_values = _build_profile_distribution_series(profiles, labels, values_getter, bins)
    if not any_values:
        return
    _plot_grouped_bars(pdf, plt, categories, labels, colors, series, title)


def _render_data_percentile_curves(
    pdf,
    plt,
    profiles: Dict[str, DataProfile | None],
    labels: Sequence[str],
    colors: Sequence[str],
    title: str,
    values_getter,
    *,
    clamp: bool = True,
    legend_labels: Optional[Sequence[str]] = None,
) -> None:
    n_series = len(labels)
    scale = _font_scale(n_series)
    # Height grows with N so the legend below has room to render.
    base_w, base_h = CDF_FIGSIZE
    fig_h = base_h + (0.3 * max(0, n_series - CDF_LINESTYLE_CYCLE_THRESHOLD))
    fig, ax = plt.subplots(figsize=(base_w, fig_h))
    fig.patch.set_facecolor("white")
    has_data = False
    cycle_styles = n_series >= CDF_LINESTYLE_CYCLE_THRESHOLD
    for idx, (label, color) in enumerate(zip(labels, colors)):
        profile = profiles.get(label)
        values = values_getter(profile) if profile else []
        xs, ys = _percentile_curve(values, clamp=clamp)
        if not xs:
            continue
        has_data = True
        legend_label = legend_labels[idx] if legend_labels and idx < len(legend_labels) else label
        is_baseline = _is_baseline_label(legend_label)
        if is_baseline:
            line_color = BASELINE_LINE_COLOR
            linestyle = BASELINE_LINESTYLE
            linewidth = 2.4
        else:
            line_color = color
            linestyle = CDF_LINESTYLE_CYCLE[idx % len(CDF_LINESTYLE_CYCLE)] if cycle_styles else "-"
            linewidth = 2.0
        ax.plot(xs, ys, label=legend_label, color=line_color, linestyle=linestyle, linewidth=linewidth)
    if not has_data:
        plt.close(fig)
        return
    ax.set_title(title, fontsize=CDF_TITLE_FONTSIZE * scale, weight="bold")
    ax.set_xlabel("Percentile of columns", fontsize=CDF_LABEL_FONTSIZE * scale)
    ax.set_ylabel("Fraction (%)", fontsize=CDF_LABEL_FONTSIZE * scale)
    ax.set_xlim(0, 100)
    ax.grid(True, linestyle="--", alpha=0.3)
    ax.tick_params(axis="both", labelsize=CDF_TICK_FONTSIZE * scale)
    if n_series >= LEGEND_OUTSIDE_THRESHOLD:
        ax.legend(
            fontsize=CDF_LEGEND_FONTSIZE * scale,
            loc="upper center",
            bbox_to_anchor=(0.5, -0.18),
            ncol=min(n_series, 5),
            frameon=True,
        )
    else:
        ax.legend(fontsize=CDF_LEGEND_FONTSIZE * scale)
    fig.tight_layout()
    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def _ndv_profile_values(profile: DataProfile | None, options: ReportOptions, clamp: bool) -> List[float]:
    if not profile:
        return []
    return profile.ndv_ratios(
        denominator=options.ndv_ratio_denominator,
        universe=options.ndv_column_universe,
        clamp=clamp,
    )


def _ndv_title(options: ReportOptions) -> str:
    denom = (options.ndv_ratio_denominator or "non_null").lower()
    universe = (options.ndv_column_universe or "all").lower()
    denom_label = "non-null rows" if denom == "non_null" else "rows"
    universe_suffix = " (eligible columns)" if universe == "eligible" else ""
    return f"Distinct Value Ratio (NDV/{denom_label}){universe_suffix}"


def _render_data_behavior_summary(pdf, plt, profiles: Dict[str, DataProfile | None], labels: Sequence[str]) -> None:
    headers = [
        "Workload",
        "Columns sorted",
        "Columns w/ histogram skew",
        "Median run length",
    ]
    rows: List[List[str]] = []
    for label in labels:
        profile = profiles.get(label)
        if not profile or profile.column_count <= 0:
            rows.append([label, "n/a", "n/a", "n/a"])
            continue
        sorted_columns = profile.sorted_column_count()
        sorted_text = f"{sorted_columns}/{profile.column_count} ({_format_percent(sorted_columns / profile.column_count)})"
        histogram_text = str(profile.histogram_count) if profile.histogram_count else "0"
        run_lengths = profile.run_length_means()
        run_median = _percentile_value(run_lengths, 50) if run_lengths else None
        rows.append(
            [
                label,
                sorted_text,
                histogram_text,
                _format_float(run_median, 2),
            ]
        )

    fig_height = 1.2 + 0.35 * max(len(rows), 1)
    fig, ax = plt.subplots(figsize=(8.5, fig_height))
    fig.patch.set_facecolor("white")
    ax.axis("off")
    table = ax.table(cellText=rows, colLabels=headers, loc="center", cellLoc="left")
    table.auto_set_font_size(False)
    table.set_fontsize(9)
    table.scale(1, 1.2)
    fig.tight_layout()
    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def _render_data_behavior_summary(pdf, plt, profiles: Dict[str, DataProfile | None], labels: Sequence[str]) -> None:
    headers = [
        "Workload",
        "Columns sorted",
        "Columns w/ histogram skew",
        "Median run length",
    ]
    rows: List[List[str]] = []
    for label in labels:
        profile = profiles.get(label)
        if not profile or profile.column_count <= 0:
            rows.append([label, "n/a", "n/a", "n/a"])
            continue
        sorted_columns = profile.sorted_column_count()
        sorted_text = f"{sorted_columns}/{profile.column_count} ({_format_percent(sorted_columns / profile.column_count)})"
        histogram_text = str(profile.histogram_count) if profile.histogram_count else "0"
        run_lengths = profile.run_length_means()
        run_median = _percentile_value(run_lengths, 50) if run_lengths else None
        rows.append(
            [
                label,
                sorted_text,
                histogram_text,
                _format_float(run_median, 2),
            ]
        )

    fig_height = 1.2 + 0.35 * max(len(rows), 1)
    fig, ax = plt.subplots(figsize=(8.5, fig_height))
    fig.patch.set_facecolor("white")
    ax.axis("off")
    table = ax.table(cellText=rows, colLabels=headers, loc="center", cellLoc="left")
    table.auto_set_font_size(False)
    table.set_fontsize(9)
    table.scale(1, 1.2)
    fig.tight_layout()
    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def _render_schema_comparison_summary(
    pdf,
    plt,
    profiles: Dict[str, SchemaProfile | None],
    labels: Sequence[str],
) -> None:
    headers = [
        "Workload",
        "Tables",
        "Columns",
        "Tables w/ PK",
        "PK columns",
        "Tables w/ FK",
        "FK columns",
    ]
    rows: List[List[str]] = []
    for label in labels:
        profile = profiles.get(label)
        if not profile or not profile.has_data():
            rows.append([label, "n/a", "n/a", "n/a", "n/a", "n/a", "n/a"])
            continue
        pk_share = (
            _format_percent(profile.tables_with_primary_keys / profile.table_count)
            if profile.table_count
            else "n/a"
        )
        fk_share = (
            _format_percent(profile.tables_with_foreign_keys / profile.table_count)
            if profile.table_count
            else "n/a"
        )
        rows.append(
            [
                label,
                _format_integer(profile.table_count),
                _format_integer(profile.total_columns),
                f"{_format_integer(profile.tables_with_primary_keys)} ({pk_share})",
                _format_integer(profile.total_primary_key_columns),
                f"{_format_integer(profile.tables_with_foreign_keys)} ({fk_share})",
                _format_integer(profile.total_foreign_key_columns),
            ]
        )
    # Guard against mismatched row lengths if upstream data is missing columns.
    expected_cols = len(headers)
    padded_rows: List[List[str]] = []
    for row in rows:
        if len(row) < expected_cols:
            row = row + ["n/a"] * (expected_cols - len(row))
        padded_rows.append(row)
    rows = padded_rows
    fig_height = 1.4 + 0.35 * max(len(rows), 1)
    fig, ax = plt.subplots(figsize=(8.5, fig_height))
    fig.patch.set_facecolor("white")
    ax.axis("off")
    table = ax.table(cellText=rows, colLabels=headers, loc="center", cellLoc="left")
    table.auto_set_font_size(False)
    table.set_fontsize(9)
    table.scale(1, 1.2)
    fig.tight_layout()
    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def _build_schema_counter_series(
    profiles: Dict[str, SchemaProfile | None],
    labels: Sequence[str],
    attr: str,
    top_n: int = 8,
) -> Tuple[List[str], List[List[float]], bool]:
    combined = Counter()
    for profile in profiles.values():
        counter = getattr(profile, attr, None)
        if counter:
            combined.update(counter)
    categories = [name for name, _ in combined.most_common(top_n)]
    if not categories:
        return [], [], False
    series: List[List[float]] = []
    any_values = False
    for label in labels:
        profile = profiles.get(label)
        counter = getattr(profile, attr, Counter()) if profile else Counter()
        total = sum(counter.values())
        if total:
            any_values = True
        shares = [(counter.get(cat, 0) / total) * 100 if total else 0.0 for cat in categories]
        series.append(shares)
    return categories, series, any_values


def _render_schema_counter_chart(
    pdf,
    plt,
    profiles: Dict[str, SchemaProfile | None],
    labels: Sequence[str],
    colors: Sequence[str],
    attr: str,
    title: str,
    keep_categories: Optional[Sequence[str]] = None,
    legend_labels: Optional[Sequence[str]] = None,
    x_tick_rotation: float = 45,
    x_tick_ha: str = "right",
    figsize: Optional[Tuple[float, float]] = None,
    fixed_y_max: Optional[float] = None,
    y_tick_step: Optional[int] = None,
    x_tick_fontsize: Optional[float] = None,
    legend_fontsize: Optional[float] = None,
    legend_borderpad: Optional[float] = None,
    legend_labelspacing: Optional[float] = None,
    legend_handletextpad: Optional[float] = None,
) -> None:
    categories, series, any_values = _build_schema_counter_series(profiles, labels, attr)
    if not any_values:
        return
    _plot_grouped_bars(
        pdf,
        plt,
        categories,
        legend_labels or labels,
        colors,
        series,
        title,
        ylabel="Share (%)",
        keep_categories=keep_categories,
        show_values=False,
        x_tick_rotation=x_tick_rotation,
        x_tick_ha=x_tick_ha,
        figsize=figsize,
        fixed_y_max=fixed_y_max,
        y_tick_step=y_tick_step,
        x_tick_fontsize=x_tick_fontsize,
        legend_fontsize=legend_fontsize,
        legend_borderpad=legend_borderpad,
        legend_labelspacing=legend_labelspacing,
        legend_handletextpad=legend_handletextpad,
    )


def _percentile_curve(values: Sequence[float], *, clamp: bool = True) -> Tuple[List[float], List[float]]:
    ordered = sorted(v for v in values if v is not None)
    if not ordered:
        return [], []
    n = len(ordered)
    xs = [((idx + 1) / n) * 100 for idx in range(n)]
    if clamp:
        ys = [min(max(value, 0.0), 1.0) * 100 for value in ordered]
    else:
        ys = [value * 100 for value in ordered]
    return xs, ys


def _percentile_curve_unbounded(values: Sequence[float]) -> Tuple[List[float], List[float]]:
    ordered = sorted(v for v in values if v is not None and v > 0)
    if not ordered:
        return [], []
    n = len(ordered)
    xs = [((idx + 1) / n) * 100 for idx in range(n)]
    return xs, ordered


def _render_missing_data_page(pdf, plt, message: str) -> None:
    fig, ax = plt.subplots(figsize=(8.5, 3))
    fig.patch.set_facecolor("white")
    ax.axis("off")
    ax.text(0.5, 0.6, "Data Files", ha="center", va="center", fontsize=18, weight="bold", color="#0b66c3")
    ax.text(0.5, 0.35, message, ha="center", va="center", fontsize=12, color="#c3423f")
    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def _format_bytes(value: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    number = float(value)
    for unit in units:
        if number < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{number:.0f} {unit}"
            return f"{number:.1f} {unit}"
        number /= 1024
    return f"{number:.1f} TB"


def _top_counter_items(counter: Counter, top_n: int = 5, other_label: str = "Other") -> Tuple[List[str], List[int]]:
    if len(counter) <= top_n:
        items = counter.most_common()
        labels, values = zip(*items)
        return list(labels), list(values)

    top_items = counter.most_common(top_n)
    top_total = sum(value for _, value in top_items)
    other_total = sum(counter.values()) - top_total
    labels = [key for key, _ in top_items]
    values = [value for _, value in top_items]
    if other_total > 0:
        labels.append(other_label)
        values.append(other_total)
    return labels, values


def _percent(value: int, total: int) -> str:
    if total == 0:
        return "0%"
    return f"{(value / total) * 100:.1f}%"


def _bin_percentages(values: List[float], bins: List[Tuple[str, Optional[float]]]) -> Tuple[List[str], List[float]]:
    if not values:
        labels = [label for label, _ in bins]
        return labels, [0.0 for _ in bins]

    counts = [0 for _ in bins]
    for value in values:
        for idx, (_, upper) in enumerate(bins):
            if upper is None or value < upper:
                counts[idx] += 1
                break
    total = len(values)
    percentages = [(count / total) * 100 for count in counts]
    labels = [label for label, _ in bins]
    return labels, percentages


def _histogram_percentages(histogram: Dict[str, float] | Counter, categories: Sequence[str]) -> List[float]:
    if not histogram:
        return [0.0 for _ in categories]
    total = sum(histogram.values())
    if total <= 0:
        return [0.0 for _ in categories]
    # Interpret near-100 totals as percentages; otherwise treat values as counts.
    base = 100.0 if 90.0 <= total <= 110.0 else float(total)
    return [(float(histogram.get(label, 0.0)) / base) * 100 for label in categories]


def _counts_from_histogram(histogram: Dict[str, float] | Counter, bins: List[Tuple[str, int, Optional[int]]]) -> Tuple[List[Tuple[str, float]], float]:
    if not histogram:
        return [], 0.0
    total = sum(histogram.values())
    if total <= 0:
        return [], 0.0
    base = 100.0 if 90.0 <= total <= 110.0 else float(total)
    labels = [label for label, _, _ in bins]
    counts = [(label, float(histogram.get(label, 0.0))) for label in labels]
    return counts, base


def _percentile_value(values: Sequence[float], percentile: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    percentile = max(0.0, min(100.0, percentile))
    position = (percentile / 100) * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[int(position)]
    lower_value = ordered[lower]
    upper_value = ordered[upper]
    fraction = position - lower
    interpolated = lower_value + (upper_value - lower_value) * fraction
    return interpolated


def _emit_ndv_debug(
    profiles: Dict[str, DataProfile | None],
    labels: Sequence[str],
    options: ReportOptions,
) -> None:
    combos = [
        ("rows", "all"),
        ("non_null", "all"),
        ("rows", "eligible"),
        ("non_null", "eligible"),
    ]
    percentiles = [50, 60, 70, 80, 85, 90, 95]
    lines: List[str] = []
    for denom, universe in combos:
        lines.append(f"[ndv] denominator={denom}, universe={universe}")
        for label in labels:
            profile = profiles.get(label)
            values = (
                profile.ndv_ratios(denominator=denom, universe=universe, clamp=False)
                if profile
                else []
            )
            stats = [f"p{p}={_format_float(_percentile_value(values, p), 4)}" for p in percentiles]
            lines.append(f"  {label}: n={len(values)} " + " ".join(stats))

    column_rows = _collect_ndv_columns(profiles, labels)
    if column_rows:
        lines.append("[ndv] 20 lowest ndv_over_rows")
        for row in column_rows[:20]:
            lines.append(_format_ndv_row(row))
        lines.append("[ndv] 20 highest ndv_over_rows")
        for row in column_rows[-20:]:
            lines.append(_format_ndv_row(row))

    debug_text = "\n".join(lines)
    print(debug_text)
    if options.ndv_debug_output:
        try:
            options.ndv_debug_output.parent.mkdir(parents=True, exist_ok=True)
            options.ndv_debug_output.write_text(debug_text + "\n", encoding="utf-8")
        except Exception:
            pass


def _collect_ndv_columns(
    profiles: Dict[str, DataProfile | None],
    labels: Sequence[str],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for label in labels:
        profile = profiles.get(label)
        if not profile:
            continue
        for metric in profile.columns:
            if metric.row_count <= 0:
                continue
            ratio_rows = metric.ndv_over_rows
            ratio_non_null = metric.ndv_over_non_null
            if ratio_rows is None:
                continue
            rows.append(
                {
                    "label": label,
                    "table": metric.table,
                    "column": metric.column,
                    "column_type": metric.column_type,
                    "row_count": metric.row_count,
                    "non_null_rows": metric.non_null_rows,
                    "distinct_count": metric.distinct_count,
                    "ndv_over_rows": ratio_rows,
                    "ndv_over_non_null": ratio_non_null,
                }
            )
    rows.sort(key=lambda row: row["ndv_over_rows"])
    return rows


def _format_ndv_row(row: Dict[str, Any]) -> str:
    ndv_rows = row.get("ndv_over_rows")
    ndv_non_null = row.get("ndv_over_non_null")
    return (
        f"  {row.get('label')}.{row.get('table')}.{row.get('column')} "
        f"type={row.get('column_type') or 'n/a'} "
        f"rows={row.get('row_count')} non_null={row.get('non_null_rows')} ndv={row.get('distinct_count')} "
        f"ndv/rows={_format_float(ndv_rows, 4)} ndv/non_null={_format_float(ndv_non_null, 4)}"
    )


def _format_integer(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return f"{int(round(value)):,}"


def _format_q_error(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    magnitude = abs(value)
    if magnitude == 0:
        return "0.00"
    if magnitude >= 1_000_000 or magnitude < 0.01:
        return f"{value:.2e}"
    return f"{value:.2f}"


def _format_float(value: Optional[float], decimals: int = 2) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{decimals}f}"


def _format_percent(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.2f}%"


def _title_case(text: str) -> str:
    titled = text.title()
    replacements = {
        "Sql": "SQL",
        "Ctes": "CTEs",
        "Union All": "UNION ALL",
        "Join": "Join",
        "Max": "Max",
        "Per": "Per",
    }
    for key, value in replacements.items():
        titled = titled.replace(key, value)
    return titled
JOIN_RANGE_TABLE_BINS = [
    ("1-2", 3),
    ("3-5", 6),
    ("6-10", 11),
    ("11-20", 21),
    ("21-30", 31),
    ("31-50", 51),
    ("51-100", 101),
    ("101-500", 501),
    ("501-1000", 1001),
    ("1000+", None),
]
