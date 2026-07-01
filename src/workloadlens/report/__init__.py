from __future__ import annotations

from .aggregate import (
    AggregatedReport,
    AggregatedStats,
    aggregate_jsonl_paths,
    aggregate_labeled_jsonl,
    aggregated_report_from_dict,
    aggregated_report_to_dict,
)
from .pdf import (
    generate_pdf_report,
    generate_comparison_pdf,
    generate_focused_comparison_pdf,
    generate_types_only_comparison_pdf,
    DEFAULT_PALETTE,
    ReportOptions,
)

__all__ = [
    "AggregatedStats",
    "AggregatedReport",
    "aggregate_jsonl_paths",
    "aggregate_labeled_jsonl",
    "aggregated_report_from_dict",
    "aggregated_report_to_dict",
    "generate_pdf_report",
    "generate_comparison_pdf",
    "generate_focused_comparison_pdf",
    "generate_types_only_comparison_pdf",
    "DEFAULT_PALETTE",
    "ReportOptions",
]
