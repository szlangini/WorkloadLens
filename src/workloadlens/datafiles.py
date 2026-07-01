from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import collections
import datetime
import decimal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple, Union
import json
import math
import os
import sys

import duckdb

from .utils.schema import SchemaTableMetadata

DATA_TABLE_RECORD = "data_table_stats"
DATA_COLUMN_RECORD = "data_column_stats"
DATA_HISTOGRAM_RECORD = "data_histogram_stats"
DEFAULT_EXTENSIONS = (".tbl", ".csv", ".dat", ".parquet")
DEFAULT_DELIMITER = "|"

_TEXT_TYPES = {"TEXT"}
_NUMERIC_TYPES = {"INT", "DOUBLE", "DECIMAL"}
_ORDERABLE_TYPES = _NUMERIC_TYPES | {"DATE", "TIMESTAMP", "TIME", "TEXT"}


@dataclass(frozen=True)
class _DataFileInfo:
    path: Path
    table: str
    format: str


@dataclass
class DataTableMetric:
    table: str
    file: str
    format: str
    size_bytes: int
    row_count: Optional[int] = None

    def to_record(self) -> dict:
        return {
            "record_type": DATA_TABLE_RECORD,
            "table": self.table,
            "file": self.file,
            "format": self.format,
            "size_bytes": self.size_bytes,
            "row_count": self.row_count,
        }

    @classmethod
    def from_record(cls, record: dict) -> "DataTableMetric":
        if record.get("record_type") and record.get("record_type") != DATA_TABLE_RECORD:
            raise ValueError(f"Unsupported record_type {record.get('record_type')}")
        return cls(
            table=str(record.get("table") or record.get("name") or ""),
            file=str(record.get("file") or ""),
            format=str(record.get("format") or ""),
            size_bytes=_safe_int(record.get("size_bytes")),
            row_count=_safe_optional_int(record.get("row_count")),
        )

    @property
    def bytes_per_row(self) -> Optional[float]:
        if isinstance(self.row_count, int) and self.row_count > 0 and self.size_bytes > 0:
            return self.size_bytes / self.row_count
        return None


@dataclass
class DataProfile:
    tables: List[DataTableMetric] = field(default_factory=list)
    columns: List["ColumnMCVMetric"] = field(default_factory=list)
    histograms: List["ColumnHistogramMetric"] = field(default_factory=list)
    ndv_summary: Dict[str, Any] = field(default_factory=dict)

    def sizes(self) -> List[int]:
        return [metric.size_bytes for metric in self.tables if metric.size_bytes >= 0]

    def row_counts(self) -> List[int]:
        return [metric.row_count for metric in self.tables if isinstance(metric.row_count, int) and metric.row_count >= 0]

    def topk_fractions(self) -> List[float]:
        return [metric.topk_fraction for metric in self.columns if metric.row_count > 0]

    def max_mcv_fractions(self) -> List[float]:
        return [metric.max_fraction for metric in self.columns if metric.row_count > 0]

    def null_fractions(self) -> List[float]:
        return [metric.null_fraction for metric in self.columns if metric.row_count > 0]

    def ndv_fractions(self) -> List[float]:
        return self.ndv_ratios(denominator="non_null", universe="all", clamp=True)

    def ndv_ratios(
        self,
        *,
        denominator: str = "rows",
        universe: str = "all",
        clamp: bool = True,
    ) -> List[float]:
        denom_key = (denominator or "rows").lower()
        universe_key = (universe or "all").lower()
        summary_key = f"{denom_key}_{universe_key}"
        if self.ndv_summary:
            ratios = _ndv_ratios_from_summary(self.ndv_summary, summary_key)
            if ratios:
                return [min(max(r, 0.0), 1.0) if clamp else r for r in ratios]

        eligible_columns = self._filtered_columns(universe_key)
        ratios: List[float] = []
        for metric in eligible_columns:
            denom = metric.row_count if denom_key == "rows" else metric.non_null_rows
            if denom and denom > 0:
                ratio = metric.distinct_count / denom
                if clamp:
                    ratio = min(max(ratio, 0.0), 1.0)
                ratios.append(ratio)
        return ratios

    def _filtered_columns(self, universe: str) -> List["ColumnMCVMetric"]:
        if universe == "eligible":
            return [
                metric
                for metric in self.columns
                if not metric.is_primary_key and not metric.is_foreign_key
            ]
        return list(self.columns)

    def requested_k(self) -> Optional[int]:
        values = {metric.requested_k for metric in self.columns if metric.requested_k > 0}
        if len(values) == 1:
            return values.pop()
        return None

    def histogram_q_errors(self) -> List[float]:
        return [metric.mean_q_error for metric in self.histograms if metric.mean_q_error > 0]

    @property
    def histogram_count(self) -> int:
        return len(self.histograms)

    @property
    def total_bytes(self) -> int:
        return sum(metric.size_bytes for metric in self.tables if metric.size_bytes > 0)

    @property
    def table_count(self) -> int:
        return len(self.tables)

    @property
    def column_count(self) -> int:
        return len(self.columns)

    @property
    def histogram_count(self) -> int:
        return len(self.histograms)

    def bytes_per_row_values(self) -> List[float]:
        values: List[float] = []
        for metric in self.tables:
            bpr = metric.bytes_per_row
            if bpr is not None:
                values.append(bpr)
        return values

    def run_length_means(self) -> List[float]:
        return [metric.mean_run_length for metric in self.columns if metric.mean_run_length is not None]

    def sorted_column_count(self) -> int:
        return sum(1 for metric in self.columns if metric.is_sorted)

    def numeric_outlier_rates(self) -> List[float]:
        return [metric.numeric_outlier_rate for metric in self.columns if metric.numeric_outlier_rate is not None]

    def string_avg_lengths(self) -> List[float]:
        return [metric.string_avg_length for metric in self.columns if metric.string_avg_length is not None]

    def string_max_lengths(self) -> List[float]:
        return [metric.string_max_length for metric in self.columns if metric.string_max_length is not None]

    def string_p95_lengths(self) -> List[float]:
        return [metric.string_p95_length for metric in self.columns if metric.string_p95_length is not None]


def scan_data_directory(
    source: Path,
    *,
    extensions: Sequence[str] | None = None,
    recursive: bool = True,
    count_rows: bool = True,
    threads: Optional[int] = None,
) -> List[DataTableMetric]:
    if not source.exists() or not source.is_dir():
        return []

    ext_set = _normalise_extensions(extensions or DEFAULT_EXTENSIONS)
    file_infos = list(_iter_data_file_infos(source, ext_set, recursive))
    metrics: List[DataTableMetric] = []

    worker_count = _resolve_thread_count(threads)

    def _collect_file_stats(info: _DataFileInfo) -> Tuple[_DataFileInfo, int, Optional[int]]:
        file_path = info.path
        try:
            size_bytes = file_path.stat().st_size
        except OSError:
            size_bytes = 0
        row_count: Optional[int] = None
        if count_rows:
            row_count = _count_rows(file_path)
        return info, size_bytes, row_count

    file_stats: List[Tuple[_DataFileInfo, int, Optional[int]]] = []
    if worker_count > 1 and file_infos:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            file_stats = list(executor.map(_collect_file_stats, file_infos))
    else:
        file_stats = [_collect_file_stats(info) for info in file_infos]

    grouped: Dict[Tuple[str, str], List[Tuple[Path, int, Optional[int]]]] = collections.defaultdict(list)
    for info, size_bytes, row_count in file_stats:
        grouped[(info.table, info.format)].append((info.path, size_bytes, row_count))

    for (table_name, file_format), entries in sorted(grouped.items(), key=lambda item: item[0]):
        total_size_bytes = sum(size for _, size, _ in entries)
        total_row_count: Optional[int] = None
        if count_rows:
            row_values = [row for _, _, row in entries]
            total_row_count = None if any(row is None for row in row_values) else sum(int(row or 0) for row in row_values)
        table_paths = [path for path, _, _ in entries]
        metrics.append(
            DataTableMetric(
                table=table_name,
                file=_logical_table_file_label(table_name, file_format, table_paths),
                format=file_format,
                size_bytes=total_size_bytes,
                row_count=total_row_count,
            )
        )

    return sorted(metrics, key=lambda metric: metric.table)


def list_data_files(
    source: Path,
    *,
    extensions: Sequence[str] | None = None,
    recursive: bool = True,
) -> List[Path]:
    if not source.exists() or not source.is_dir():
        return []
    ext_set = _normalise_extensions(extensions or DEFAULT_EXTENSIONS)
    return [info.path for info in _iter_data_file_infos(source, ext_set, recursive)]


def iter_data_metrics(paths: Iterable[Path]) -> Iterator[Union[DataTableMetric, "ColumnMCVMetric", "ColumnHistogramMetric"]]:
    for path in paths:
        if path.is_dir():
            for entry in sorted(path.glob("*.jsonl")):
                yield from iter_data_metrics([entry])
            continue
        if not path.exists():
            continue
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    text = line.strip()
                    if not text:
                        continue
                    try:
                        record = json.loads(text)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    record_type = record.get("record_type")
                    try:
                        if record_type == DATA_COLUMN_RECORD:
                            yield ColumnMCVMetric.from_record(record)
                        elif record_type == DATA_HISTOGRAM_RECORD:
                            yield ColumnHistogramMetric.from_record(record)
                        elif record_type == DATA_TABLE_RECORD or not record_type:
                            table_name = record.get("table") or record.get("name")
                            if not table_name:
                                continue
                            yield DataTableMetric.from_record(record)
                    except Exception:
                        continue
        except OSError:
            continue


def load_data_profile(paths: Iterable[Path]) -> DataProfile:
    tables: List[DataTableMetric] = []
    columns: List[ColumnMCVMetric] = []
    histograms: List[ColumnHistogramMetric] = []
    for metric in iter_data_metrics(paths):
        if isinstance(metric, DataTableMetric):
            tables.append(metric)
        elif isinstance(metric, ColumnMCVMetric):
            columns.append(metric)
        elif isinstance(metric, ColumnHistogramMetric):
            histograms.append(metric)
    return DataProfile(tables, columns, histograms)


def write_data_metrics(
    metrics: Iterable[Union[DataTableMetric, "ColumnMCVMetric", "ColumnHistogramMetric"]],
    output: Path,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        for metric in metrics:
            record = metric.to_record() if hasattr(metric, "to_record") else metric
            handle.write(json.dumps(record) + "\n")


@dataclass
class ColumnMCVMetric:
    table: str
    column: str
    file: str
    row_count: int
    null_count: int
    topk_sum: int
    max_count: int
    distinct_count: int
    reported_k: int
    requested_k: int
    column_type: Optional[str] = None
    mean_run_length: Optional[float] = None
    is_sorted: Optional[bool] = None
    string_avg_length: Optional[float] = None
    string_max_length: Optional[float] = None
    string_p95_length: Optional[float] = None
    numeric_outlier_rate: Optional[float] = None
    sample_fraction: Optional[float] = None
    is_primary_key: bool = False
    is_foreign_key: bool = False

    def to_record(self) -> dict:
        return {
            "record_type": DATA_COLUMN_RECORD,
            "table": self.table,
            "column": self.column,
            "file": self.file,
            "column_type": self.column_type,
            "row_count": self.row_count,
            "null_count": self.null_count,
            "topk_sum": self.topk_sum,
            "max_count": self.max_count,
            "distinct_count": self.distinct_count,
            "reported_k": self.reported_k,
            "requested_k": self.requested_k,
            "mean_run_length": self.mean_run_length,
            "is_sorted": self.is_sorted,
            "string_avg_length": self.string_avg_length,
            "string_max_length": self.string_max_length,
            "string_p95_length": self.string_p95_length,
            "numeric_outlier_rate": self.numeric_outlier_rate,
            "sample_fraction": self.sample_fraction,
            "is_primary_key": self.is_primary_key,
            "is_foreign_key": self.is_foreign_key,
        }

    @classmethod
    def from_record(cls, record: dict) -> "ColumnMCVMetric":
        return cls(
            table=str(record.get("table") or ""),
            column=str(record.get("column") or ""),
            file=str(record.get("file") or ""),
            column_type=str(record.get("column_type") or "") or None,
            row_count=_safe_int(record.get("row_count")),
            null_count=_safe_int(record.get("null_count")),
            topk_sum=_safe_int(record.get("topk_sum")),
            max_count=_safe_int(record.get("max_count")),
            distinct_count=_safe_int(record.get("distinct_count")),
            reported_k=_safe_int(record.get("reported_k")),
            requested_k=_safe_int(record.get("requested_k")),
            mean_run_length=_safe_optional_float(record.get("mean_run_length")),
            is_sorted=bool(record.get("is_sorted")) if record.get("is_sorted") is not None else None,
            string_avg_length=_safe_optional_float(record.get("string_avg_length")),
            string_max_length=_safe_optional_float(record.get("string_max_length")),
            string_p95_length=_safe_optional_float(record.get("string_p95_length")),
            numeric_outlier_rate=_safe_optional_float(record.get("numeric_outlier_rate")),
            sample_fraction=_safe_optional_float(record.get("sample_fraction")),
            is_primary_key=bool(record.get("is_primary_key")),
            is_foreign_key=bool(record.get("is_foreign_key")),
        )

    @property
    def null_fraction(self) -> float:
        return (self.null_count / self.row_count) if self.row_count > 0 else 0.0

    @property
    def topk_fraction(self) -> float:
        non_null = self.non_null_rows
        return (self.topk_sum / non_null) if non_null > 0 else 0.0

    @property
    def max_fraction(self) -> float:
        non_null = self.non_null_rows
        return (self.max_count / non_null) if non_null > 0 else 0.0

    @property
    def non_null_rows(self) -> int:
        return max(self.row_count - self.null_count, 0)

    @property
    def ndv_fraction(self) -> float:
        non_null = self.non_null_rows
        return (self.distinct_count / non_null) if non_null > 0 else 0.0

    @property
    def ndv_over_rows(self) -> Optional[float]:
        if self.row_count <= 0:
            return None
        return self.distinct_count / self.row_count

    @property
    def ndv_over_non_null(self) -> Optional[float]:
        non_null = self.non_null_rows
        if non_null <= 0:
            return None
        return self.distinct_count / non_null


def _ndv_ratios_from_summary(summary: Dict[str, Any], key: str) -> List[float]:
    entry = summary.get("ndv_percentiles") or summary.get("ndv_summary") or summary
    if not isinstance(entry, dict):
        return []
    payload = entry.get(key) if key in entry else entry.get("values" if key == "rows_all" else "")
    # If the key mapping isn't direct, allow ndv_percentiles[key] -> percentiles/values
    if payload is None and isinstance(entry.get("ndv_percentiles"), dict):
        payload = entry["ndv_percentiles"].get(key)
    if not payload:
        return []
    if isinstance(payload, list) and payload and isinstance(payload[0], (int, float)):
        ordered = sorted(v for v in payload if v is not None)
        return ordered
    if isinstance(payload, list):
        points: List[Tuple[float, float]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            try:
                p = float(item.get("p"))
                v = float(item.get("value"))
            except (TypeError, ValueError):
                continue
            points.append((p, v))
        return _interpolate_percentiles(points)
    return []


def _interpolate_percentiles(points: List[Tuple[float, float]]) -> List[float]:
    if not points:
        return []
    ordered = sorted(points, key=lambda x: x[0])
    samples = []
    for pct in range(1, 101):
        samples.append(_interp_value(ordered, pct))
    return samples


def _interp_value(points: List[Tuple[float, float]], pct: float) -> float:
    lower_pt = None
    upper_pt = None
    for p, v in points:
        if p <= pct:
            lower_pt = (p, v)
        if p >= pct:
            upper_pt = (p, v)
            break
    if lower_pt is None:
        return upper_pt[1] if upper_pt else 0.0
    if upper_pt is None:
        return lower_pt[1]
    lp, lv = lower_pt
    up, uv = upper_pt
    if up == lp:
        return uv
    fraction = (pct - lp) / (up - lp)
    return lv + (uv - lv) * fraction


@dataclass
class ColumnHistogramMetric:
    table: str
    column: str
    column_type: str
    bucket_count: int
    mean_q_error: float
    max_q_error: float
    non_null_rows: int
    ndv: int

    def to_record(self) -> dict:
        return {
            "record_type": DATA_HISTOGRAM_RECORD,
            "table": self.table,
            "column": self.column,
            "column_type": self.column_type,
            "bucket_count": self.bucket_count,
            "mean_q_error": self.mean_q_error,
            "max_q_error": self.max_q_error,
            "non_null_rows": self.non_null_rows,
            "ndv": self.ndv,
        }

    @classmethod
    def from_record(cls, record: dict) -> "ColumnHistogramMetric":
        return cls(
            table=str(record.get("table") or ""),
            column=str(record.get("column") or ""),
            column_type=str(record.get("column_type") or ""),
            bucket_count=_safe_int(record.get("bucket_count")),
            mean_q_error=_safe_float(record.get("mean_q_error")),
            max_q_error=_safe_float(record.get("max_q_error")),
            non_null_rows=_safe_int(record.get("non_null_rows")),
            ndv=_safe_int(record.get("ndv")),
        )


def compute_column_mcv_metrics(
    files: Sequence[Path],
    schema: Dict[str, SchemaTableMetadata],
    *,
    requested_k: int = 20,
    delimiter: str = DEFAULT_DELIMITER,
    null_marker: Optional[str] = None,
    logger: Optional[Callable[[str], None]] = None,
    sample_fraction: Optional[float] = None,
    threads: Optional[int] = None,
) -> Tuple[List[ColumnMCVMetric], List[ColumnHistogramMetric]]:
    if requested_k <= 0:
        raise ValueError("requested_k must be greater than zero")

    default_exts = _normalise_extensions(DEFAULT_EXTENSIONS)
    table_files: Dict[str, List[Path]] = collections.defaultdict(list)
    for file_path in files:
        path = Path(file_path)
        info = _classify_data_file(path, default_exts)
        table_name = info.table.lower() if info else path.stem.lower()
        table_files[table_name].append(path)

    con = duckdb.connect()
    _configure_duckdb_threads(con, threads)
    metrics: List[ColumnMCVMetric] = []
    histogram_metrics: List[ColumnHistogramMetric] = []
    temp_index = 0
    try:
        for table_name in sorted(table_files.keys()):
            source_files = sorted(table_files[table_name], key=lambda item: str(item))
            table_meta = schema.get(table_name)
            source_info = _classify_data_file(source_files[0], default_exts) if source_files else None
            source_format = source_info.format if source_info else source_files[0].suffix.lstrip(".").lower()
            source_label = _logical_table_file_label(table_name, source_format, source_files)
            if not table_meta:
                if logger:
                    logger(f"[yellow]Skipping column stats for {source_label}: not found in schema.[/yellow]")
                continue
            column_names = list(table_meta.columns.keys())
            if not column_names:
                continue
            relation = _load_table_relation(con, source_files, delimiter, null_marker)
            duck_columns = relation.columns
            if len(duck_columns) < len(column_names):
                if logger:
                    logger(
                        f"[yellow]Skipping column stats for {source_label}: file has {len(duck_columns)} columns, expected {len(column_names)}.[/yellow]"
                    )
                continue
            projection = ", ".join(
                f"{duck_columns[idx]} AS {_quote_identifier(column_names[idx])}" for idx in range(len(column_names))
            )
            named_relation = relation.project(projection)
            temp_index += 1
            temp_table = f"workloadlens_mcv_{temp_index}"
            con.execute(f"DROP TABLE IF EXISTS {temp_table}")
            named_relation.create(temp_table)
            sample_table = temp_table
            created_sample_table = False
            applied_sample_fraction = None
            if sample_fraction and 0 < sample_fraction < 1:
                sample_table = f"{temp_table}_sample"
                con.execute(f"DROP TABLE IF EXISTS {sample_table}")
                con.execute(
                    f"CREATE TEMP TABLE {sample_table} AS "
                    f"SELECT * FROM {temp_table} WHERE random() <= {sample_fraction}"
                )
                created_sample_table = True
                applied_sample_fraction = sample_fraction
                sample_count_row = con.execute(f"SELECT COUNT(*) FROM {sample_table}").fetchone()
                sample_count = int(sample_count_row[0]) if sample_count_row and sample_count_row[0] is not None else 0
                if sample_count == 0:
                    if logger:
                        logger(
                            f"[yellow]Sampling for {source_label} produced zero rows; falling back to full column stats.[/yellow]"
                        )
                    con.execute(f"DROP TABLE {sample_table}")
                    sample_table = temp_table
                    created_sample_table = False
                    applied_sample_fraction = None
            try:
                for column_name in column_names:
                    ident = _quote_identifier(column_name)
                    total_rows, null_count = con.execute(
                        f"SELECT COUNT(*) AS total_rows, "
                        f"COALESCE(SUM(CASE WHEN {ident} IS NULL THEN 1 ELSE 0 END), 0) AS null_count "
                        f"FROM {sample_table}"
                    ).fetchone()
                    total_rows = int(total_rows or 0)
                    null_count = int(null_count or 0)
                    freq_rows = con.execute(
                        f"SELECT COUNT(*) AS freq "
                        f"FROM {sample_table} WHERE {ident} IS NOT NULL "
                        f"GROUP BY {ident} ORDER BY freq DESC LIMIT {requested_k}"
                    ).fetchall()
                    freqs = [int(row[0]) for row in freq_rows if row and row[0] is not None]
                    topk_sum = sum(freqs)
                    max_count = max(freqs) if freqs else 0
                    distinct_row = con.execute(
                        f"SELECT COUNT(DISTINCT {ident}) FROM {sample_table} WHERE {ident} IS NOT NULL"
                    ).fetchone()
                    distinct_count = int(distinct_row[0]) if distinct_row and distinct_row[0] is not None else 0
                    column_type = (table_meta.columns.get(column_name) or "").upper()
                    is_primary_key = column_name in (table_meta.primary_key_columns or {})
                    is_foreign_key = column_name in (table_meta.foreign_key_columns or {})
                    mean_run_length = _compute_mean_run_length(con, sample_table, ident)
                    is_sorted = (
                        _compute_sorted_flag(con, sample_table, ident) if column_type in _ORDERABLE_TYPES else None
                    )
                    string_avg_length = None
                    string_max_length = None
                    string_p95_length = None
                    if column_type in _TEXT_TYPES:
                        string_stats = _compute_string_length_stats(con, sample_table, ident)
                        if string_stats:
                            string_avg_length, string_max_length, string_p95_length = string_stats
                    numeric_outlier_rate = None
                    if column_type in _NUMERIC_TYPES:
                        numeric_outlier_rate = _compute_outlier_rate(con, sample_table, ident)
                    mcv_metric = ColumnMCVMetric(
                        table=table_name,
                        column=column_name,
                        file=source_label,
                        column_type=column_type or None,
                        row_count=total_rows,
                        null_count=null_count,
                        topk_sum=topk_sum,
                        max_count=max_count,
                        distinct_count=distinct_count,
                        reported_k=len(freqs),
                        requested_k=requested_k,
                        mean_run_length=mean_run_length,
                        is_sorted=is_sorted,
                        string_avg_length=string_avg_length,
                        string_max_length=string_max_length,
                        string_p95_length=string_p95_length,
                        numeric_outlier_rate=numeric_outlier_rate,
                        sample_fraction=applied_sample_fraction,
                        is_primary_key=is_primary_key,
                        is_foreign_key=is_foreign_key,
                    )
                    metrics.append(mcv_metric)
                    histogram_metric = _compute_histogram_metric(
                        con,
                        sample_table,
                        table_name,
                        column_name,
                        ident,
                        table_meta.columns.get(column_name),
                        total_rows,
                        null_count,
                        distinct_count,
                        topk=requested_k,
                        min_rows=_MIN_HISTOGRAM_ROWS,
                    )
                    if histogram_metric:
                        histogram_metrics.append(histogram_metric)
            finally:
                if created_sample_table:
                    con.execute(f"DROP TABLE IF EXISTS {sample_table}")
                con.execute(f"DROP TABLE {temp_table}")
    finally:
        con.close()
    return metrics, histogram_metrics


_MAX_HISTOGRAM_BUCKETS = 254
_HISTOGRAM_TYPES = {"INT", "DOUBLE", "DECIMAL", "DATE", "TIMESTAMP"}
_MIN_WIDTH = sys.float_info.min
_MIN_HISTOGRAM_ROWS = 5


def _to_numeric_value(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, decimal.Decimal):
        try:
            return float(value)
        except Exception:
            return None
    if isinstance(value, datetime.date) and not isinstance(value, datetime.datetime):
        # Use ordinal days to preserve ordering
        return float(value.toordinal())
    if isinstance(value, datetime.datetime):
        return float(value.timestamp())
    return None


def compute_histogram_skew_qerror(
    values: Sequence[Any], num_buckets: int, topk: int = 20, min_rows: int = _MIN_HISTOGRAM_ROWS
) -> Optional[Tuple[float, float, int, int]]:
    """
    Compute mean/max histogram q-error after removing NULLs and top-k MCVs.
    Returns (mean_q, max_q, used_bucket_count, remaining_ndv) or None if not applicable.
    """
    if num_buckets <= 0 or topk < 0:
        return None
    filtered = [v for v in values if v is not None]
    if len(filtered) < min_rows:
        return None

    counts = collections.Counter(filtered)
    top_values = {val for val, _ in counts.most_common(topk)}
    remaining = [v for v in filtered if v not in top_values]
    if len(remaining) < min_rows:
        return None

    numeric_values: List[float] = []
    for val in remaining:
        num_val = _to_numeric_value(val)
        if num_val is None:
            continue
        numeric_values.append(num_val)

    if len(numeric_values) < min_rows:
        return None

    min_val = min(numeric_values)
    max_val = max(numeric_values)
    if math.isclose(min_val, max_val):
        return None

    ndv = len(set(numeric_values))
    if ndv <= num_buckets:
        return None

    bucket_count = min(num_buckets, len(numeric_values) - 1, ndv - 1)
    if bucket_count <= 0:
        return None

    numeric_values.sort()
    boundaries: List[float] = []
    for idx in range(bucket_count + 1):
        pct = idx / bucket_count
        pos = pct * (len(numeric_values) - 1)
        lower_idx = int(math.floor(pos))
        upper_idx = int(math.ceil(pos))
        if lower_idx == upper_idx:
            boundaries.append(numeric_values[lower_idx])
        else:
            fraction = pos - lower_idx
            boundaries.append(
                numeric_values[lower_idx] + (numeric_values[upper_idx] - numeric_values[lower_idx]) * fraction
            )

    range_width = max_val - min_val
    uniform_width = range_width / bucket_count if bucket_count > 0 else 0.0
    if uniform_width <= 0:
        return None

    q_errors: List[float] = []
    for idx in range(len(boundaries) - 1):
        left = boundaries[idx]
        right = boundaries[idx + 1]
        width = float(right) - float(left)
        q_errors.append(_bucket_q_error(uniform_width, width))

    if not q_errors:
        return None

    mean_q = sum(q_errors) / len(q_errors)
    max_q = max(q_errors)
    return mean_q, max_q, bucket_count, ndv


def _compute_histogram_metric(
    con: duckdb.DuckDBPyConnection,
    source_table: str,
    table_name: str,
    column_name: str,
    quoted_column: str,
    column_type: Optional[str],
    total_rows: int,
    null_count: int,
    distinct_count: int,
    *,
    topk: int = 20,
    min_rows: int = 5,
    max_buckets: int = _MAX_HISTOGRAM_BUCKETS,
) -> Optional[ColumnHistogramMetric]:
    metric, _ = _compute_histogram_metric_with_reason(
        con,
        source_table,
        table_name,
        column_name,
        quoted_column,
        column_type,
        total_rows,
        null_count,
        distinct_count,
        topk=topk,
        min_rows=min_rows,
        max_buckets=max_buckets,
    )
    return metric


def _compute_histogram_metric_with_reason(
    con: duckdb.DuckDBPyConnection,
    source_table: str,
    table_name: str,
    column_name: str,
    quoted_column: str,
    column_type: Optional[str],
    total_rows: int,
    null_count: int,
    distinct_count: int,
    *,
    topk: int = 20,
    min_rows: int = 5,
    max_buckets: int = _MAX_HISTOGRAM_BUCKETS,
) -> Tuple[Optional[ColumnHistogramMetric], Optional[str]]:
    dtype = (column_type or "").upper()
    max_buckets = max(1, int(max_buckets or _MAX_HISTOGRAM_BUCKETS))
    if dtype not in _HISTOGRAM_TYPES:
        return None, "type"

    non_null_rows = max(total_rows - null_count, 0)
    if non_null_rows < min_rows or distinct_count <= 1:
        return None, "rows"

    value_expr = "epoch_ms(val)" if dtype in {"DATE", "TIMESTAMP"} else "CAST(val AS DOUBLE)"
    filtered_cte = (
        f"WITH non_null AS ("
        f"    SELECT {quoted_column} AS val FROM {source_table} WHERE {quoted_column} IS NOT NULL"
        f"), "
        f"mcv AS ("
        f"    SELECT val, COUNT(*) AS freq FROM non_null GROUP BY val ORDER BY freq DESC LIMIT {topk}"
        f"), "
        f"filtered AS ("
        f"    SELECT val FROM non_null WHERE val NOT IN (SELECT val FROM mcv)"
        f") "
    )

    stats_query = (
        filtered_cte
        + "SELECT COUNT(*) AS row_count, COUNT(DISTINCT val) AS ndv, MIN(val) AS min_val, MAX(val) AS max_val FROM filtered"
    )
    try:
        stats_row = con.execute(stats_query).fetchone()
    except duckdb.Error:
        return None, "error"
    if not stats_row:
        return None, "missing"

    remaining_rows = int(stats_row[0] or 0)
    ndv_remaining = int(stats_row[1] or 0)
    min_val_raw = stats_row[2]
    max_val_raw = stats_row[3]

    if remaining_rows < min_rows or ndv_remaining <= 1:
        return None, "rows"

    min_val = _to_numeric_value(min_val_raw)
    max_val = _to_numeric_value(max_val_raw)
    if min_val is None or max_val is None or math.isclose(min_val, max_val):
        return None, "range"

    desired_buckets = min(max_buckets, remaining_rows - 1)
    if ndv_remaining <= desired_buckets or desired_buckets <= 0:
        return None, "ndv"

    bucket_count = min(desired_buckets, ndv_remaining - 1)
    if bucket_count <= 0:
        return None, "ndv"

    fractions = ", ".join(f"{idx / bucket_count:.10f}" for idx in range(bucket_count + 1))
    quantiles_expr = f"LIST_VALUE({fractions})"
    quantile_query = filtered_cte + f"SELECT quantile_cont({value_expr}, {quantiles_expr}) FROM filtered"
    try:
        quantile_row = con.execute(quantile_query).fetchone()
    except duckdb.Error:
        return None, "error"
    if not quantile_row:
        return None, "missing"

    boundaries = quantile_row[0]
    if not boundaries or len(boundaries) < 2:
        return None, "missing"

    numeric_boundaries: List[float] = []
    for bound in boundaries:
        num_val = _to_numeric_value(bound)
        if num_val is None:
            return None, "range"
        numeric_boundaries.append(num_val)

    uniform_width = (numeric_boundaries[-1] - numeric_boundaries[0]) / bucket_count
    if uniform_width <= 0:
        return None, "range"

    q_errors: List[float] = []
    for idx in range(len(numeric_boundaries) - 1):
        width = numeric_boundaries[idx + 1] - numeric_boundaries[idx]
        q_errors.append(_bucket_q_error(uniform_width, width))

    if not q_errors:
        return None, "missing"

    mean_q = sum(q_errors) / len(q_errors)
    max_q = max(q_errors)
    return ColumnHistogramMetric(
        table=table_name,
        column=column_name,
        column_type=dtype,
        bucket_count=bucket_count,
        mean_q_error=mean_q,
        max_q_error=max_q,
        non_null_rows=remaining_rows,
        ndv=ndv_remaining,
    ), None


def _bucket_q_error(uniform_width: float, actual_width: float) -> float:
    uniform = abs(uniform_width)
    if uniform <= 0:
        return 0.0
    actual = abs(actual_width)
    actual = max(actual, _MIN_WIDTH)
    ratio = uniform / actual
    if ratio < 1.0:
        ratio = 1.0 / ratio
    return ratio


def _load_table_relation(
    con: duckdb.DuckDBPyConnection,
    file_path: Union[Path, Sequence[Path]],
    delimiter: str,
    null_marker: Optional[str],
) -> duckdb.DuckDBPyRelation:
    if isinstance(file_path, Path):
        paths = [file_path]
    else:
        paths = list(file_path)
    if not paths:
        raise ValueError("No data file paths were provided")

    # Detect Parquet / CSV by extension of the first file.
    suffix = paths[0].suffix.lower()
    is_parquet = suffix == ".parquet"
    is_csv = suffix == ".csv"

    path_literals = [str(p).replace("'", "''") for p in paths]
    if len(path_literals) == 1:
        source_expr = f"'{path_literals[0]}'"
    else:
        source_expr = "[" + ", ".join(f"'{value}'" for value in path_literals) + "]"

    if is_parquet:
        return con.sql(f"SELECT * FROM read_parquet({source_expr})")

    if is_csv:
        extra = ", ignore_errors=true"
        if null_marker:
            escaped = null_marker.replace("'", "''")
            extra += f", nullstr=['{escaped}']"
        return con.sql(f"SELECT * FROM read_csv_auto({source_expr}{extra})")

    delim_literal = delimiter.replace("'", "''") if delimiter else ""
    actual_delim = delim_literal or "|"
    if null_marker:
        escaped = null_marker.replace("'", "''")
        null_clause = f", nullstr=['{escaped}']"
    else:
        null_clause = ""
    base_query = f"""
        SELECT *
        FROM read_csv_auto(
            {source_expr},
            delim='{actual_delim}',
            quote='',
            escape='',
            header=False,
            sample_size=-1,
            ignore_errors=False,
            null_padding=True
            {null_clause}
        )
    """
    try:
        return con.sql(base_query)
    except duckdb.Error:
        null_clause_fallback = null_clause
        fallback_query = f"""
            SELECT *
            FROM read_csv_auto(
                {source_expr},
                delim='{actual_delim}',
                quote='',
                escape='',
                header=False,
                sample_size=2048,
                ignore_errors=True,
                null_padding=True
                {null_clause_fallback}
            )
        """
        return con.sql(fallback_query)


def _compute_mean_run_length(con: duckdb.DuckDBPyConnection, source_table: str, ident: str) -> Optional[float]:
    query = f"""
        WITH ordered AS (
            SELECT {ident} AS val, ROW_NUMBER() OVER () AS rn
            FROM {source_table}
        ),
        flags AS (
            SELECT
                rn,
                CASE
                    WHEN LAG(val) OVER (ORDER BY rn) IS NULL THEN 1
                    WHEN LAG(val) OVER (ORDER BY rn) IS DISTINCT FROM val THEN 1
                    ELSE 0
                END AS boundary
            FROM ordered
        ),
        groups AS (
            SELECT
                rn,
                SUM(COALESCE(boundary, 1)) OVER (ORDER BY rn ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS grp
            FROM flags
        ),
        group_lengths AS (
            SELECT COUNT(*) AS cnt
            FROM groups
            GROUP BY grp
        )
        SELECT AVG(cnt)::DOUBLE FROM group_lengths
    """
    try:
        row = con.execute(query).fetchone()
    except duckdb.Error:
        return None
    if not row:
        return None
    result = row[0]
    return float(result) if result is not None else None


def _compute_sorted_flag(con: duckdb.DuckDBPyConnection, source_table: str, ident: str) -> Optional[bool]:
    query = f"""
        WITH ordered AS (
            SELECT {ident} AS val, ROW_NUMBER() OVER () AS rn
            FROM {source_table}
        ),
        lagged AS (
            SELECT
                val,
                LAG(val) OVER (ORDER BY rn) AS prev_val,
                LEAD(val) OVER (ORDER BY rn) AS next_val
            FROM ordered
        )
        SELECT
            MIN(CASE WHEN prev_val IS NULL OR val IS NULL OR val >= prev_val THEN 1 ELSE 0 END) AS non_decreasing,
            MIN(CASE WHEN next_val IS NULL OR val IS NULL OR val <= next_val THEN 1 ELSE 0 END) AS non_increasing
        FROM lagged
    """
    try:
        row = con.execute(query).fetchone()
    except duckdb.Error:
        return None
    if not row:
        return None
    non_decreasing, non_increasing = row
    if non_decreasing is None and non_increasing is None:
        return None
    if non_decreasing == 1 or non_increasing == 1:
        return True
    return False


def _compute_string_length_stats(
    con: duckdb.DuckDBPyConnection,
    source_table: str,
    ident: str,
) -> Optional[Tuple[float, float, float]]:
    query = f"""
        WITH lengths AS (
            SELECT LENGTH({ident}) AS len
            FROM {source_table}
            WHERE {ident} IS NOT NULL
        )
        SELECT AVG(len)::DOUBLE, MAX(len)::DOUBLE, quantile_cont(len, 0.95)
        FROM lengths
    """
    try:
        row = con.execute(query).fetchone()
    except duckdb.Error:
        return None
    if not row:
        return None
    avg_len, max_len, p95_len = row
    if avg_len is None and max_len is None and p95_len is None:
        return None
    return (
        float(avg_len) if avg_len is not None else None,
        float(max_len) if max_len is not None else None,
        float(p95_len) if p95_len is not None else None,
    )


def _compute_outlier_rate(con: duckdb.DuckDBPyConnection, source_table: str, ident: str) -> Optional[float]:
    stats_query = f"""
        SELECT
            COUNT(*) AS total_rows,
            quantile_cont(CAST({ident} AS DOUBLE), 0.25) AS q1,
            quantile_cont(CAST({ident} AS DOUBLE), 0.75) AS q3,
            quantile_cont(CAST({ident} AS DOUBLE), 0.01) AS p01,
            quantile_cont(CAST({ident} AS DOUBLE), 0.99) AS p99,
            MIN(CAST({ident} AS DOUBLE)) AS min_val,
            MAX(CAST({ident} AS DOUBLE)) AS max_val
        FROM {source_table}
        WHERE {ident} IS NOT NULL
    """
    try:
        totals = con.execute(stats_query).fetchone()
    except duckdb.Error:
        return None
    if not totals:
        return None
    total_rows = totals[0]
    if not total_rows:
        return None
    q1, q3, p01, p99, min_val, max_val = totals[1], totals[2], totals[3], totals[4], totals[5], totals[6]
    lower = None
    upper = None
    if q1 is not None and q3 is not None:
        iqr = q3 - q1
        if iqr is not None and not math.isclose(iqr, 0.0):
            lower = q1 - 1.5 * iqr
            upper = q3 + 1.5 * iqr
    if lower is None or upper is None or not math.isfinite(lower) or not math.isfinite(upper) or lower >= upper:
        lower = p01 if p01 is not None else min_val
        upper = p99 if p99 is not None else max_val
        if lower is None or upper is None or not math.isfinite(lower) or not math.isfinite(upper) or lower >= upper:
            return 0.0
    try:
        outlier_row = con.execute(
            f"""
            SELECT COUNT(*)
            FROM {source_table}
            WHERE {ident} IS NOT NULL AND (CAST({ident} AS DOUBLE) < ? OR CAST({ident} AS DOUBLE) > ?)
            """,
            [lower, upper],
        ).fetchone()
    except duckdb.Error:
        return None
    outlier_count = int(outlier_row[0]) if outlier_row and outlier_row[0] is not None else 0
    if outlier_count <= 0:
        return 0.0
    return outlier_count / total_rows if total_rows else None

def _logical_table_file_label(table_name: str, file_format: str, paths: Sequence[Path]) -> str:
    if not paths:
        return f"{table_name}.{file_format}"
    ordered = sorted(paths, key=lambda item: str(item))
    if len(ordered) == 1:
        return str(ordered[0])
    first_parent = ordered[0].parent
    if all(path.parent == first_parent for path in ordered):
        return str(first_parent / f"{table_name}.{file_format}.*")
    return f"{ordered[0]} (+{len(ordered) - 1} shards)"


def _classify_data_file(path: Path, extensions: set[str]) -> Optional[_DataFileInfo]:
    if not path.is_file():
        return None

    suffixes = [suffix.lower() for suffix in path.suffixes]
    if not suffixes:
        return None

    if suffixes[-1] in extensions:
        ext = suffixes[-1]
        table_name = path.name[: -len(ext)]
        if table_name:
            return _DataFileInfo(path=path, table=table_name, format=ext.lstrip("."))

    if len(suffixes) >= 2 and suffixes[-2] in extensions and suffixes[-1].lstrip(".").isdigit():
        ext = suffixes[-2]
        suffix = ext + suffixes[-1]
        table_name = path.name[: -len(suffix)]
        if table_name:
            return _DataFileInfo(path=path, table=table_name, format=ext.lstrip("."))

    return None


def _iter_data_file_infos(source: Path, extensions: set[str], recursive: bool) -> Iterator[_DataFileInfo]:
    iterator = source.rglob("*") if recursive else source.glob("*")
    for path in iterator:
        info = _classify_data_file(path, extensions)
        if info:
            yield info


def _iter_data_files(source: Path, extensions: set[str], recursive: bool) -> Iterator[Path]:
    for info in _iter_data_file_infos(source, extensions, recursive):
        yield info.path


def _normalise_extensions(entries: Sequence[str]) -> set[str]:
    ext_values = set()
    for entry in entries:
        ext = entry.lower().strip()
        if not ext:
            continue
        if not ext.startswith("."):
            ext = "." + ext
        ext_values.add(ext)
    return ext_values if ext_values else set(DEFAULT_EXTENSIONS)


def _count_rows(path: Path) -> Optional[int]:
    if path.suffix.lower() == ".parquet":
        try:
            con = duckdb.connect()
            result = con.sql(f"SELECT COUNT(*) FROM read_parquet('{str(path).replace(chr(39), chr(39)+chr(39))}')")
            row_count = result.fetchone()[0]
            con.close()
            return row_count
        except Exception:
            return None
    try:
        with path.open("rb") as handle:
            row_count = 0
            saw_data = False
            ended_with_newline = True
            for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
                row_count += chunk.count(b"\n")
                saw_data = True
                ended_with_newline = chunk.endswith(b"\n")
            if saw_data and not ended_with_newline:
                row_count += 1
            return row_count
    except OSError:
        return None


def _safe_int(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if value is None:
        return default
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _safe_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float)):
        return float(value)
    if value is None:
        return default
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _safe_optional_int(value: object) -> Optional[int]:
    if value is None:
        return None
    result = _safe_int(value, default=0)
    return result


def _safe_optional_float(value: object) -> Optional[float]:
    if value is None:
        return None
    return _safe_float(value, default=0.0)


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _resolve_thread_count(requested: Optional[int]) -> int:
    if isinstance(requested, int) and requested > 0:
        return min(requested, max(1, os.cpu_count() or 1))
    return 1


def _configure_duckdb_threads(con: duckdb.DuckDBPyConnection, threads: Optional[int]) -> None:
    if not threads or threads <= 0:
        return
    try:
        con.execute(f"PRAGMA threads={int(threads)}")
    except duckdb.Error:
        pass
