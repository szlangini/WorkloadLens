from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional

from ..utils.types import normalize_type_label
from ..utils.schema import parse_schema_tables
from ..datafiles import (
    ColumnHistogramMetric,
    ColumnMCVMetric,
    DataProfile,
    DataTableMetric,
    load_data_profile,
)


@dataclass
class AggregatedStats:
    total_queries: int = 0
    plan_typed: int = 0
    ast_only: int = 0
    error_queries: int = 0
    schema_statements: int = 0
    query_statements: int = 0
    operator_counts: Counter = field(default_factory=Counter)
    join_counts: Counter = field(default_factory=Counter)
    operator_type_counts: Dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))
    origin_operator_type_counts: Dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))
    projection_functions: Counter = field(default_factory=Counter)
    aggregate_functions: Counter = field(default_factory=Counter)
    window_functions: Counter = field(default_factory=Counter)
    expression_functions: Counter = field(default_factory=Counter)
    feature_counts: Counter = field(default_factory=Counter)
    filter_expression_ops: Counter = field(default_factory=Counter)
    sql_length_histogram: Counter = field(default_factory=Counter)
    scalar_expression_depth_histogram: Counter = field(default_factory=Counter)
    filter_predicate_depth_histogram: Counter = field(default_factory=Counter)
    group_by_key_histogram: Counter = field(default_factory=Counter)
    joins_per_query_histogram: Counter = field(default_factory=Counter)
    union_all_histogram: Counter = field(default_factory=Counter)
    union_all_fan_in_histogram: Counter = field(default_factory=Counter)
    order_by_types_histogram: Counter = field(default_factory=Counter)
    projection_expression_histogram: Counter = field(default_factory=Counter)
    filter_expression_histogram: Counter = field(default_factory=Counter)
    limit_with_order_histogram: Counter = field(default_factory=Counter)
    limit_without_order_histogram: Counter = field(default_factory=Counter)
    sql_lengths: List[int] = field(default_factory=list)
    operator_totals: List[int] = field(default_factory=list)
    statement_types: Counter = field(default_factory=Counter)
    joins_per_query: List[int] = field(default_factory=list)
    union_all_totals: List[int] = field(default_factory=list)
    union_all_fan_in_values: List[int] = field(default_factory=list)
    limit_with_order: List[int] = field(default_factory=list)
    limit_without_order: List[int] = field(default_factory=list)
    projection_expression_totals: List[int] = field(default_factory=list)
    filter_expression_totals: List[int] = field(default_factory=list)
    scalar_expression_depths: List[int] = field(default_factory=list)
    filter_predicate_depths: List[int] = field(default_factory=list)
    group_by_key_counts: List[int] = field(default_factory=list)
    top_level_order_types: Counter = field(default_factory=Counter)
    top_level_order_types_unique: Counter = field(default_factory=Counter)

    def operator_type_counts_dict(self) -> Dict[str, Counter]:
        return {op: Counter(counts) for op, counts in self.operator_type_counts.items() if counts}

    def origin_operator_type_counts_dict(self) -> Dict[str, Counter]:
        return {op: Counter(counts) for op, counts in self.origin_operator_type_counts.items() if counts}


@dataclass
class SchemaProfile:
    table_count: int = 0
    total_columns: int = 0
    column_type_counts: Counter = field(default_factory=Counter)
    tables_with_primary_keys: int = 0
    total_primary_key_columns: int = 0
    primary_key_type_counts: Counter = field(default_factory=Counter)
    tables_with_foreign_keys: int = 0
    total_foreign_key_columns: int = 0
    foreign_key_type_counts: Counter = field(default_factory=Counter)

    def has_data(self) -> bool:
        return bool(
            self.table_count
            or self.total_columns
            or self.column_type_counts
            or self.primary_key_type_counts
            or self.foreign_key_type_counts
        )


@dataclass
class AggregatedReport:
    all_statements: AggregatedStats
    query_statements: AggregatedStats
    metadata: Dict[str, Any] = field(default_factory=dict)
    data_profile: DataProfile | None = None
    schema_profile: SchemaProfile | None = None


AggregatedReportMap = Dict[str, AggregatedReport]


def aggregate_records(
    records: Iterable[dict],
    predicate: Optional[Callable[[dict], bool]] = None,
) -> AggregatedStats:
    stats = AggregatedStats()

    for record in records:
        if not isinstance(record, dict):
            continue
        if predicate and not predicate(record):
            continue

        stats.total_queries += 1

        mode = record.get("mode")
        is_schema = mode == "schema"
        if mode == "plan_typed":
            stats.plan_typed += 1
        elif mode == "ast_only":
            stats.ast_only += 1
        elif mode == "error":
            stats.error_queries += 1
        elif is_schema:
            stats.schema_statements += 1
        if record.get("is_query"):
            stats.query_statements += 1
            proj_count = record.get("projection_expression_count")
            if isinstance(proj_count, int) and proj_count >= 0:
                stats.projection_expression_totals.append(proj_count)
            filt_count = record.get("filter_expression_count")
            if isinstance(filt_count, int) and filt_count >= 0:
                stats.filter_expression_totals.append(filt_count)
            scalar_depth = record.get("max_scalar_expression_depth")
            if isinstance(scalar_depth, int) and scalar_depth >= 0:
                stats.scalar_expression_depths.append(scalar_depth)
            filter_depth = record.get("max_filter_predicate_depth")
            if isinstance(filter_depth, int) and filter_depth > 0:
                stats.filter_predicate_depths.append(filter_depth)
            appended = False
            group_key_list = record.get("group_by_key_counts")
            if isinstance(group_key_list, list):
                for value in group_key_list:
                    if isinstance(value, int) and value >= 0:
                        stats.group_by_key_counts.append(value)
                        appended = True
            if not appended:
                group_keys = record.get("group_by_key_count")
                if isinstance(group_keys, int) and group_keys >= 0:
                    stats.group_by_key_counts.append(group_keys)

            union_fan_in = record.get("union_all_fan_in_values")
            if isinstance(union_fan_in, list):
                for value in union_fan_in:
                    if isinstance(value, int) and value >= 2:
                        stats.union_all_fan_in_values.append(value)

        errors = record.get("errors")
        if errors and mode != "error":
            stats.error_queries += 1

        operators = record.get("operators") or {}
        if isinstance(operators, dict):
            normalized_ops: Dict[str, int] = {}
            for name, value in operators.items():
                key = name
                if name == "read":
                    key = "scan"
                elif name == "fetch":
                    key = "limit"
                normalized_ops[key] = normalized_ops.get(key, 0) + int(value)
            stats.operator_counts.update(normalized_ops)
            if record.get("is_query"):
                stats.joins_per_query.append(int(normalized_ops.get("join", 0)))
                stats.union_all_totals.append(int(normalized_ops.get("union_all", 0)))

        joins = record.get("join_types") or {}
        if isinstance(joins, dict):
            stats.join_counts.update(joins)

        op_types = record.get("operator_types") or {}
        if isinstance(op_types, dict):
            for op_name, counts in op_types.items():
                if not isinstance(counts, dict):
                    continue
                key = "scan" if op_name == "read" else op_name
                for dtype, value in counts.items():
                    norm = normalize_type_label(dtype)
                    if not norm:
                        continue
                    stats.operator_type_counts[key].update({norm: int(value)})
        origin_types = record.get("operator_types_origin") or {}
        if isinstance(origin_types, dict):
            for op_name, counts in origin_types.items():
                if not isinstance(counts, dict):
                    continue
                normalized_inner: Counter = Counter()
                for dtype, value in counts.items():
                    norm = normalize_type_label(dtype)
                    if not norm:
                        continue
                    normalized_inner[norm] += int(value)
                if normalized_inner:
                    stats.origin_operator_type_counts[op_name].update(normalized_inner)

        top_level_sort = record.get("order_by_top_types") or {}
        if isinstance(top_level_sort, dict):
            normalized_top: Counter = Counter()
            for dtype, value in top_level_sort.items():
                norm = normalize_type_label(dtype)
                if not norm:
                    continue
                normalized_top[norm] += int(value)
            if normalized_top:
                stats.top_level_order_types.update(normalized_top)

        order_unique = record.get("order_by_top_types_unique") or []
        if not order_unique:
            sort_map = record.get("operator_types_origin") or record.get("operator_types") or {}
            if isinstance(sort_map, dict):
                order_unique = list((sort_map.get("sort") or {}).keys())
        if isinstance(order_unique, (list, tuple)):
            normalized_unique: set[str] = set()
            for dtype in order_unique:
                norm = normalize_type_label(dtype)
                if not norm:
                    continue
                normalized_unique.add(norm)
            for norm in normalized_unique:
                stats.top_level_order_types_unique[norm] += 1

        filter_ops = record.get("filter_expression_ops") or {}
        if isinstance(filter_ops, dict):
            for name, value in filter_ops.items():
                if not name:
                    continue
                stats.filter_expression_ops.update({name: int(value)})

        functions = record.get("functions") or {}
        if isinstance(functions, dict):
            _accumulate_function_counts(functions.get("projection"), stats.projection_functions)
            _accumulate_function_counts(functions.get("aggregate"), stats.aggregate_functions)
            _accumulate_function_counts(functions.get("window"), stats.window_functions)
            _accumulate_function_counts(functions.get("expression"), stats.expression_functions)

        features = record.get("features") or {}
        if isinstance(features, dict):
            if not is_schema:
                for name, enabled in features.items():
                    if enabled:
                        stats.feature_counts[name] += 1

        length = record.get("sql_length_bytes")
        if isinstance(length, int) and length >= 0:
            stats.sql_lengths.append(length)

        op_total = record.get("operator_total")
        if isinstance(op_total, int) and op_total >= 0:
            stats.operator_totals.append(op_total)

        if record.get("is_query"):
            order_appended = 0
            order_values = record.get("limit_with_order_values")
            if isinstance(order_values, list):
                for entry in order_values:
                    if isinstance(entry, int) and entry >= 0:
                        stats.limit_with_order.append(entry)
                        order_appended += 1
            without_values = record.get("limit_without_order_values")
            if isinstance(without_values, list):
                for entry in without_values:
                    if isinstance(entry, int) and entry >= 0:
                        stats.limit_without_order.append(entry)

            limit_info = record.get("limit") or {}
            if isinstance(limit_info, dict) and not order_values and not without_values:
                has_order = bool(limit_info.get("has_order_by"))
                value = limit_info.get("value")
                has_limit = bool(limit_info.get("has_limit"))
                if has_limit and isinstance(value, int) and value >= 0:
                    if has_order:
                        stats.limit_with_order.append(value)
                        order_appended += 1
                    else:
                        stats.limit_without_order.append(value)
                elif has_order and not has_limit:
                    stats.limit_with_order.append(0)
                    order_appended += 1

            operators = record.get("operators") or {}
            if isinstance(operators, dict):
                try:
                    sort_total = int(operators.get("sort", 0))
                except (TypeError, ValueError):
                    sort_total = 0
                if sort_total > order_appended:
                    stats.limit_with_order.extend([0] * (sort_total - order_appended))

        statement_type = record.get("statement_type")
        if isinstance(statement_type, str) and statement_type:
            stats.statement_types.update([statement_type])

    return stats


def _accumulate_function_counts(func_list: Optional[Iterable[str]], counter: Counter) -> None:
    if not func_list:
        return
    for name in func_list:
        if name:
            counter[name.upper()] += 1


def _iter_json_records(path: Path) -> Iterator[dict]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return iter(())
    except OSError:
        return iter(())

    def _generator() -> Iterator[dict]:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                yield data

    return _generator()


def aggregate_jsonl_paths(paths: Iterable[Path], data_paths: Iterable[Path] | None = None) -> AggregatedReport:
    all_records: List[dict] = []
    for path in paths:
        if path.is_dir():
            files = sorted(p for p in path.glob("*.jsonl"))
            for file_path in files:
                all_records.extend(_iter_json_records(file_path))
        elif path.exists():
            all_records.extend(_iter_json_records(path))
    def _is_query_record(rec: dict) -> bool:
        marker = rec.get("is_query")
        if marker is not None:
            return bool(marker)
        mode = rec.get("mode")
        return mode in {"plan_typed", "ast_only"}

    stats_all = aggregate_records(all_records)
    stats_queries = aggregate_records(all_records, predicate=_is_query_record)
    is_tpcds = any("tpcds" in str(record.get("file") or "").lower() for record in all_records)
    sources = set()
    for record in all_records:
        source = str(record.get("operator_source") or "").lower()
        if not source:
            source = "substrait" if record.get("mode") == "plan_typed" else "ast"
        sources.add(source)
    if "substrait" in sources:
        operator_source = "Substrait"
    elif sources:
        operator_source = "AST"
    else:
        operator_source = "Unknown"
    metadata = {"is_tpcds": is_tpcds, "operator_source": operator_source}
    profile = None
    if data_paths:
        collected_paths = list(data_paths)
        if collected_paths:
            profile = load_data_profile(collected_paths)
    schema_profile = collect_schema_profile(all_records)
    return AggregatedReport(stats_all, stats_queries, metadata, profile, schema_profile)


def aggregate_labeled_jsonl(
    labeled_paths: Dict[str, Iterable[Path]],
    labeled_data_paths: Dict[str, Iterable[Path]] | None = None,
    metadata: Dict[str, Dict[str, Any]] | None = None,
) -> AggregatedReportMap:
    workloads: AggregatedReportMap = {}
    for label, entries in labeled_paths.items():
        if not label:
            continue
        path_list: List[Path] = []
        for entry in entries:
            candidate = Path(entry)
            if candidate.is_dir():
                path_list.extend(sorted(candidate.glob("*.jsonl")))
            else:
                path_list.append(candidate)
        if not path_list:
            continue
        data_list: Iterable[Path] | None = None
        if labeled_data_paths:
            data_entries = labeled_data_paths.get(label)
            if data_entries:
                data_list = []
                for entry in data_entries:
                    candidate = Path(entry)
                    if candidate.is_dir():
                        data_list.extend(sorted(candidate.glob("*.jsonl")))
                    else:
                        data_list.append(candidate)
        report = aggregate_jsonl_paths(path_list, data_list)
        if metadata and metadata.get(label):
            report.metadata.update(metadata[label])
        workloads[label] = report
    return workloads


def collect_schema_profile(records: Iterable[dict]) -> SchemaProfile | None:
    profile = SchemaProfile()
    for record in records:
        if (record.get("mode") or "").lower() != "schema":
            continue
        sql_text = record.get("normalized_sql") or record.get("sql")
        if not sql_text:
            continue
        tables = parse_schema_tables(sql_text)
        if not tables:
            continue
        for metadata in tables.values():
            profile.table_count += 1
            if metadata.columns:
                profile.total_columns += len(metadata.columns)
                normalized_columns = [
                    normalize_type_label(value) or value for value in metadata.columns.values() if value is not None
                ]
                profile.column_type_counts.update(normalized_columns)
            if metadata.primary_key_columns:
                profile.tables_with_primary_keys += 1
                profile.total_primary_key_columns += len(metadata.primary_key_columns)
                normalized_pk = [
                    normalize_type_label(value) or value
                    for value in metadata.primary_key_columns.values()
                    if value is not None
                ]
                profile.primary_key_type_counts.update(normalized_pk)
            if metadata.foreign_key_columns:
                profile.tables_with_foreign_keys += 1
                profile.total_foreign_key_columns += len(metadata.foreign_key_columns)
                normalized_fk = [
                    normalize_type_label(value) or value
                    for value in metadata.foreign_key_columns.values()
                    if value is not None
                ]
                profile.foreign_key_type_counts.update(normalized_fk)
    if not profile.has_data():
        return None
    return profile


def _counter_to_dict(counter: Counter) -> Dict[str, int]:
    return {str(key): int(value) for key, value in counter.items()}


def _stats_to_dict(stats: AggregatedStats) -> Dict[str, Any]:
    return {
        "total_queries": stats.total_queries,
        "plan_typed": stats.plan_typed,
        "ast_only": stats.ast_only,
        "error_queries": stats.error_queries,
        "schema_statements": stats.schema_statements,
        "query_statements": stats.query_statements,
        "operator_counts": _counter_to_dict(stats.operator_counts),
        "join_counts": _counter_to_dict(stats.join_counts),
        "operator_type_counts": {k: _counter_to_dict(v) for k, v in stats.operator_type_counts.items()},
        "origin_operator_type_counts": {
            k: _counter_to_dict(v) for k, v in stats.origin_operator_type_counts.items()
        },
        "projection_functions": _counter_to_dict(stats.projection_functions),
        "aggregate_functions": _counter_to_dict(stats.aggregate_functions),
        "window_functions": _counter_to_dict(stats.window_functions),
        "expression_functions": _counter_to_dict(stats.expression_functions),
        "feature_counts": _counter_to_dict(stats.feature_counts),
        "filter_expression_ops": _counter_to_dict(stats.filter_expression_ops),
        "sql_length_histogram": _counter_to_dict(stats.sql_length_histogram),
        "scalar_expression_depth_histogram": _counter_to_dict(stats.scalar_expression_depth_histogram),
        "filter_predicate_depth_histogram": _counter_to_dict(stats.filter_predicate_depth_histogram),
        "group_by_key_histogram": _counter_to_dict(stats.group_by_key_histogram),
        "joins_per_query_histogram": _counter_to_dict(stats.joins_per_query_histogram),
        "union_all_histogram": _counter_to_dict(stats.union_all_histogram),
        "union_all_fan_in_histogram": _counter_to_dict(stats.union_all_fan_in_histogram),
        "order_by_types_histogram": _counter_to_dict(stats.order_by_types_histogram),
        "projection_expression_histogram": _counter_to_dict(stats.projection_expression_histogram),
        "filter_expression_histogram": _counter_to_dict(stats.filter_expression_histogram),
        "limit_with_order_histogram": _counter_to_dict(stats.limit_with_order_histogram),
        "limit_without_order_histogram": _counter_to_dict(stats.limit_without_order_histogram),
        "sql_lengths": list(stats.sql_lengths),
        "operator_totals": list(stats.operator_totals),
        "statement_types": _counter_to_dict(stats.statement_types),
        "joins_per_query": list(stats.joins_per_query),
        "union_all_totals": list(stats.union_all_totals),
        "union_all_fan_in_values": list(stats.union_all_fan_in_values),
        "limit_with_order": list(stats.limit_with_order),
        "limit_without_order": list(stats.limit_without_order),
        "projection_expression_totals": list(stats.projection_expression_totals),
        "filter_expression_totals": list(stats.filter_expression_totals),
        "scalar_expression_depths": list(stats.scalar_expression_depths),
        "filter_predicate_depths": list(stats.filter_predicate_depths),
        "group_by_key_counts": list(stats.group_by_key_counts),
        "top_level_order_types": _counter_to_dict(stats.top_level_order_types),
        "top_level_order_types_unique": _counter_to_dict(stats.top_level_order_types_unique),
    }


def _data_profile_to_dict(profile: DataProfile | None) -> Optional[Dict[str, Any]]:
    if not profile:
        return None
    return {
        "tables": [metric.to_record() for metric in profile.tables],
        "columns": [metric.to_record() for metric in profile.columns],
        "histograms": [metric.to_record() for metric in profile.histograms],
        "ndv_summary": profile.ndv_summary,
    }


def _schema_profile_to_dict(profile: SchemaProfile | None) -> Optional[Dict[str, Any]]:
    if not profile:
        return None
    return {
        "table_count": profile.table_count,
        "total_columns": profile.total_columns,
        "column_type_counts": _counter_to_dict(profile.column_type_counts),
        "tables_with_primary_keys": profile.tables_with_primary_keys,
        "total_primary_key_columns": profile.total_primary_key_columns,
        "primary_key_type_counts": _counter_to_dict(profile.primary_key_type_counts),
        "tables_with_foreign_keys": profile.tables_with_foreign_keys,
        "total_foreign_key_columns": profile.total_foreign_key_columns,
        "foreign_key_type_counts": _counter_to_dict(profile.foreign_key_type_counts),
    }


def aggregated_report_to_dict(report: AggregatedReport) -> Dict[str, Any]:
    return {
        "metadata": dict(report.metadata),
        "all_statements": _stats_to_dict(report.all_statements),
        "query_statements": _stats_to_dict(report.query_statements),
        "data_profile": _data_profile_to_dict(report.data_profile),
        "schema_profile": _schema_profile_to_dict(report.schema_profile),
    }


def _counter_from_dict(payload: Any) -> Counter:
    counter: Counter = Counter()
    if isinstance(payload, dict):
        for key, value in payload.items():
            try:
                counter[str(key)] += int(value)
            except (TypeError, ValueError):
                continue
    return counter


def _int_list(payload: Any) -> List[int]:
    values: List[int] = []
    if isinstance(payload, (list, tuple)):
        for value in payload:
            try:
                values.append(int(value))
            except (TypeError, ValueError):
                continue
    return values


def _stats_from_dict(payload: Dict[str, Any]) -> AggregatedStats:
    stats = AggregatedStats()
    stats.total_queries = int(payload.get("total_queries") or 0)
    stats.plan_typed = int(payload.get("plan_typed") or 0)
    stats.ast_only = int(payload.get("ast_only") or 0)
    stats.error_queries = int(payload.get("error_queries") or 0)
    stats.schema_statements = int(payload.get("schema_statements") or 0)
    stats.query_statements = int(payload.get("query_statements") or 0)
    stats.operator_counts = _counter_from_dict(payload.get("operator_counts"))
    stats.join_counts = _counter_from_dict(payload.get("join_counts"))

    op_type_counts = payload.get("operator_type_counts") or {}
    if isinstance(op_type_counts, dict):
        for name, values in op_type_counts.items():
            stats.operator_type_counts[str(name)] = _counter_from_dict(values)
    origin_op_types = payload.get("origin_operator_type_counts") or {}
    if isinstance(origin_op_types, dict):
        for name, values in origin_op_types.items():
            stats.origin_operator_type_counts[str(name)] = _counter_from_dict(values)

    stats.projection_functions = _counter_from_dict(payload.get("projection_functions"))
    stats.aggregate_functions = _counter_from_dict(payload.get("aggregate_functions"))
    stats.window_functions = _counter_from_dict(payload.get("window_functions"))
    stats.expression_functions = _counter_from_dict(payload.get("expression_functions"))
    stats.feature_counts = _counter_from_dict(payload.get("feature_counts"))
    stats.filter_expression_ops = _counter_from_dict(payload.get("filter_expression_ops"))
    stats.sql_length_histogram = _counter_from_dict(payload.get("sql_length_histogram"))
    stats.scalar_expression_depth_histogram = _counter_from_dict(payload.get("scalar_expression_depth_histogram"))
    stats.filter_predicate_depth_histogram = _counter_from_dict(payload.get("filter_predicate_depth_histogram"))
    stats.group_by_key_histogram = _counter_from_dict(payload.get("group_by_key_histogram"))
    stats.joins_per_query_histogram = _counter_from_dict(payload.get("joins_per_query_histogram"))
    stats.union_all_histogram = _counter_from_dict(payload.get("union_all_histogram"))
    stats.union_all_fan_in_histogram = _counter_from_dict(payload.get("union_all_fan_in_histogram"))
    stats.order_by_types_histogram = _counter_from_dict(payload.get("order_by_types_histogram"))
    stats.projection_expression_histogram = _counter_from_dict(payload.get("projection_expression_histogram"))
    stats.filter_expression_histogram = _counter_from_dict(payload.get("filter_expression_histogram"))
    stats.limit_with_order_histogram = _counter_from_dict(payload.get("limit_with_order_histogram"))
    stats.limit_without_order_histogram = _counter_from_dict(payload.get("limit_without_order_histogram"))

    stats.sql_lengths = _int_list(payload.get("sql_lengths"))
    stats.operator_totals = _int_list(payload.get("operator_totals"))
    stats.statement_types = _counter_from_dict(payload.get("statement_types"))
    stats.joins_per_query = _int_list(payload.get("joins_per_query"))
    stats.union_all_totals = _int_list(payload.get("union_all_totals"))
    stats.union_all_fan_in_values = _int_list(payload.get("union_all_fan_in_values"))
    stats.limit_with_order = _int_list(payload.get("limit_with_order"))
    stats.limit_without_order = _int_list(payload.get("limit_without_order"))
    stats.projection_expression_totals = _int_list(payload.get("projection_expression_totals"))
    stats.filter_expression_totals = _int_list(payload.get("filter_expression_totals"))
    stats.scalar_expression_depths = _int_list(payload.get("scalar_expression_depths"))
    stats.filter_predicate_depths = _int_list(payload.get("filter_predicate_depths"))
    stats.group_by_key_counts = _int_list(payload.get("group_by_key_counts"))
    stats.top_level_order_types = _counter_from_dict(payload.get("top_level_order_types"))
    stats.top_level_order_types_unique = _counter_from_dict(payload.get("top_level_order_types_unique"))
    return stats


def _data_profile_from_dict(payload: Dict[str, Any] | None) -> DataProfile | None:
    if not payload:
        return None
    tables: List[DataTableMetric] = []
    columns: List[ColumnMCVMetric] = []
    histograms: List[ColumnHistogramMetric] = []
    ndv_summary: Dict[str, Any] = dict(payload.get("ndv_summary") or {})
    for record in payload.get("tables") or []:
        if isinstance(record, dict):
            try:
                tables.append(DataTableMetric.from_record(record))
            except Exception:
                continue
    for record in payload.get("columns") or []:
        if isinstance(record, dict):
            try:
                columns.append(ColumnMCVMetric.from_record(record))
            except Exception:
                continue
    for record in payload.get("histograms") or []:
        if isinstance(record, dict):
            try:
                histograms.append(ColumnHistogramMetric.from_record(record))
            except Exception:
                continue
    return DataProfile(tables, columns, histograms, ndv_summary)


def _schema_profile_from_dict(payload: Dict[str, Any] | None) -> SchemaProfile | None:
    if not payload:
        return None
    profile = SchemaProfile()
    try:
        profile.table_count = int(payload.get("table_count") or 0)
        profile.total_columns = int(payload.get("total_columns") or 0)
        profile.tables_with_primary_keys = int(payload.get("tables_with_primary_keys") or 0)
        profile.total_primary_key_columns = int(payload.get("total_primary_key_columns") or 0)
        profile.tables_with_foreign_keys = int(payload.get("tables_with_foreign_keys") or 0)
        profile.total_foreign_key_columns = int(payload.get("total_foreign_key_columns") or 0)
    except (TypeError, ValueError):
        pass
    profile.column_type_counts = _counter_from_dict(payload.get("column_type_counts"))
    profile.primary_key_type_counts = _counter_from_dict(payload.get("primary_key_type_counts"))
    profile.foreign_key_type_counts = _counter_from_dict(payload.get("foreign_key_type_counts"))
    return profile if profile.has_data() else None


def aggregated_report_from_dict(payload: Dict[str, Any]) -> AggregatedReport:
    metadata = dict(payload.get("metadata") or {})
    all_stats = _stats_from_dict(payload.get("all_statements") or {})
    query_stats = _stats_from_dict(payload.get("query_statements") or {})
    data_profile = _data_profile_from_dict(payload.get("data_profile"))
    schema_profile = _schema_profile_from_dict(payload.get("schema_profile"))
    return AggregatedReport(all_stats, query_stats, metadata, data_profile, schema_profile)
