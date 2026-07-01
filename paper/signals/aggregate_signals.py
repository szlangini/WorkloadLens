#!/usr/bin/env python3
"""
Aggregation step of the WorkloadLens benchmark-comparison reproduction
(paper Sections 3-5).

Collapses the per-query and per-column metrics WorkloadLens emits for each
benchmark (``coverage.jsonl`` + ``data_metrics.jsonl``) into one CSV per signal,
ready for plotting (``render_paper_figures.py``) and for the qualitative
comparison against the Snowflake / Amazon Redshift / Tableau production
baselines transcribed in ``production_baselines.yaml``.

Inputs (one folder per benchmark):
    <analyses-dir>/<bench>/coverage.jsonl
    <analyses-dir>/<bench>/data_metrics.jsonl
Outputs (one CSV per signal):
    <out-dir>/<signal>.csv

Paths are configurable; the defaults point at the reproduction working tree
(``paper/work/``) so the script is self-contained inside the WorkloadLens repo
and carries no author-local paths.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

# paper/signals/aggregate_signals.py  ->  paper/
PAPER_DIR = Path(__file__).resolve().parent.parent

# The eight benchmarks profiled in the paper's Section 5 comparison. SSB and CAB
# are intentionally excluded: the paper (Sec 5.1) omits SSB as a smaller TPC-H
# derivative and CAB as a multi-tenant cloud workload outside the data-/query-
# level scope. JCC-H's *skewed* data scan (analyses/jcch_skewed) is consumed
# directly by the renderer for the MCV curve, so it needs no CSV row here.
DEFAULT_BENCH_ORDER = [
    "tpcds", "tpch", "dsb", "jcch", "job", "clickbench", "redbench", "prodds",
]

# Set from CLI args in main(); the writer functions read these as globals so the
# validated signal-computation bodies stay byte-for-byte identical to the
# original revision pipeline.
ANALYSES: Path = PAPER_DIR / "work" / "analyses"
OUT: Path = PAPER_DIR / "work" / "signals"
BENCH_ORDER: List[str] = list(DEFAULT_BENCH_ORDER)

# Map our internal signal keys (used by the per-CSV writers below) to the
# `signals.*` keys in production_baselines.yaml. The yaml is the source of
# truth; to add a new baseline source (e.g. a Tableau bar), edit the yaml.
_YAML_KEY_MAP = {
    "operator_logical_types_filter":   "filter_logical_types",
    "operator_logical_types_join":     "join_key_logical_types",
    "operator_logical_types_aggregate": "aggregate_logical_types",
    "column_logical_types_schema":     "schema_column_types",
    "group_by_key_count":              "group_by_key_count",
    "limit_magnitude":                 "limit_magnitude",
    "null_fraction_distribution":      "null_fraction_distribution",
    "mcv_share_distribution":          "mcv_share_distribution",
    "statement_type_mix":              "statement_type_mix",
}


def _load_baselines(path: Path) -> Dict[str, Dict[str, Dict[str, float]]]:
    """Load baselines from production_baselines.yaml.

    Returns {internal_signal_key: {source_label: {bucket: value}}}.
    """
    try:
        import yaml  # type: ignore
    except ImportError:
        print("[warn] PyYAML not installed -> production baselines skipped "
              "(install paper/requirements.txt)")
        return {}
    if not path.exists():
        print(f"[warn] baselines file not found: {path}")
        return {}
    data = yaml.safe_load(open(path))
    if not isinstance(data, dict):
        return {}
    yaml_signals = data.get("signals", {})
    out: Dict[str, Dict[str, Dict[str, float]]] = {}
    for internal_key, yaml_key in _YAML_KEY_MAP.items():
        sig = yaml_signals.get(yaml_key, {})
        if not isinstance(sig, dict):
            continue
        sources = sig.get("sources", {})
        if not isinstance(sources, dict):
            continue
        out[internal_key] = {}
        for source_label, source_data in sources.items():
            values = (source_data or {}).get("values") or {}
            out[internal_key][source_label] = dict(values)
    return out


# Set from the baselines file in main().
PROD_BASELINE: Dict[str, Dict[str, dict]] = {}


def load_jsonl(path: Path) -> List[dict]:
    if not path.exists():
        return []
    out = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def normalise_type(t: str) -> str:
    t = (t or "").upper()
    if t in ("INT", "INTEGER", "BIGINT", "SMALLINT", "TINYINT"):
        return "INT"
    if t in ("TEXT", "STRING", "VARCHAR", "CHAR", "CLOB"):
        return "TEXT"
    if t in ("DECIMAL", "NUMERIC", "DOUBLE", "FLOAT", "REAL"):
        return "DECIMAL"
    if t in ("DATE", "TIMESTAMP", "TIME", "DATETIME"):
        return "DATE"
    return "OTHER"


# ------- Per-benchmark aggregators ----------

def aggregate_query_signals(coverage_records: List[dict]) -> dict:
    """Aggregate per-query coverage records into per-benchmark signals."""
    type_counts = defaultdict(Counter)          # role -> type -> count
    group_by_keys = Counter()                   # bucket -> n queries
    limit_buckets = Counter()                   # bucket -> n queries
    union_all_fanin = Counter()                 # bucket -> n queries
    join_count_per_query = Counter()            # bucket -> n queries
    feature_counts = Counter()                  # feature -> # queries that use it
    total_queries = 0

    for rec in coverage_records:
        # Skip schema/DDL statements so the signal counts are scope-independent:
        # a coverage file produced with --scope all carries CREATE TABLE/VIEW
        # records (mode == "schema") that must not be counted as queries.
        if rec.get("mode") == "schema":
            continue
        total_queries += 1
        op_types = rec.get("operator_types", {}) or {}
        for role, by_type in op_types.items():
            if not isinstance(by_type, dict):
                continue
            for t, c in by_type.items():
                type_counts[role][normalise_type(t)] += c

        # GROUP BY key count — PER GROUPING OPERATION (matches Snowflake [27]
        # Table 13, which counts grouping keys per group-by operator). NOTE:
        # operator_types["group_by"] SUMS keys across every GROUP BY clause in a
        # query (subqueries / UNION branches / CTEs), which over-counts
        # multi-block queries (e.g. a 4-clause query 6+6+6+4 -> "22 keys").
        # WorkloadLens' group_by_key_counts is the per-operator list; bucket each
        # element so the unit matches the production baseline.
        for gk in (rec.get("group_by_key_counts") or [0]):
            group_by_keys[bucket_int(gk, [(0, "0"), (2, "1-2"), (5, "3-5"),
                                          (10, "6-10")], "10+")] += 1

        # join count (from operators.join + join_types)
        ops = rec.get("operators", {}) or {}
        joins = ops.get("join", 0) + ops.get("hash_join", 0) + ops.get("merge_join", 0)
        join_count_per_query[bucket_int(joins, [(0, "0"), (2, "1-2"),
                                                  (5, "3-5"), (10, "6-10"),
                                                  (50, "11-50"), (100, "51-100"),
                                                  (500, "101-500")], "501+")] += 1

        # LIMIT magnitude via SQL
        sql = (rec.get("normalized_sql") or "").upper()
        lim_val = parse_limit(sql)
        if lim_val is None:
            limit_buckets["0"] += 1
        else:
            limit_buckets[bucket_int(lim_val, [(10, "1-10"), (100, "11-100"),
                                                (1000, "101-1K"),
                                                (10_000, "1K-10K"),
                                                (100_000, "10K-100K"),
                                                (1_000_000, "100K-1M"),
                                                (10_000_000, "1M-10M")],
                                       ">10M")] += 1

        # UNION ALL fan-in
        ua_count = sql.count(" UNION ALL ")
        if ua_count == 0:
            union_all_fanin["0"] += 1
        else:
            union_all_fanin[bucket_int(ua_count + 1,
                                         [(2, "2"), (4, "3-4"), (8, "5-8"),
                                          (16, "9-16"), (64, "17-64"),
                                          (256, "65-256")], ">256")] += 1

        # Feature prevalence
        if "GROUP BY" in sql:
            feature_counts["group_by"] += 1
        if " JOIN " in sql or sql.count(",") and "FROM" in sql:  # rough
            pass
        if " LIMIT " in sql:
            feature_counts["limit"] += 1
        if "UNION ALL" in sql:
            feature_counts["union_all"] += 1
        if " OVER (" in sql or " OVER(" in sql:
            feature_counts["window_functions"] += 1
        if sql.lstrip().startswith("WITH"):
            feature_counts["cte"] += 1

    return {
        "total_queries": total_queries,
        "type_counts": {role: dict(c) for role, c in type_counts.items()},
        "group_by_keys": dict(group_by_keys),
        "limit_buckets": dict(limit_buckets),
        "union_all_fanin": dict(union_all_fanin),
        "join_count_per_query": dict(join_count_per_query),
        "feature_counts": dict(feature_counts),
    }


def parse_limit(sql: str) -> int | None:
    """Extract LIMIT N from a normalized SQL string."""
    import re
    m = re.search(r"\bLIMIT\s+(\d+)", sql)
    if m:
        return int(m.group(1))
    return None


def bucket_int(value: int, ranges: List[Tuple[int, str]], overflow: str) -> str:
    """Return the bucket label for value given an ordered list of (upper, label)."""
    for upper, label in ranges:
        if value <= upper:
            return label
    return overflow


def aggregate_data_signals(data_records: List[dict]) -> dict:
    """Aggregate per-column data records into per-benchmark column-level signals."""
    columns_seen = 0
    null_fractions: List[float] = []
    mcv_shares: List[float] = []
    schema_types = Counter()
    string_lengths: List[float] = []
    pk_columns = 0
    fk_columns = 0

    for rec in data_records:
        if rec.get("record_type") != "data_column_stats":
            continue
        rows = rec.get("row_count") or 0
        if rows <= 0:
            continue
        columns_seen += 1
        nf = (rec.get("null_count") or 0) / rows
        mc = (rec.get("max_count") or 0) / rows
        null_fractions.append(nf)
        mcv_shares.append(mc)
        schema_types[normalise_type(rec.get("column_type") or "")] += 1
        if rec.get("string_avg_length") is not None:
            string_lengths.append(rec["string_avg_length"])
        if rec.get("is_primary_key"):
            pk_columns += 1
        if rec.get("is_foreign_key"):
            fk_columns += 1

    return {
        "columns": columns_seen,
        "schema_types": dict(schema_types),
        "null_fractions": null_fractions,
        "mcv_shares": mcv_shares,
        "string_lengths": string_lengths,
        "pk_columns": pk_columns,
        "fk_columns": fk_columns,
    }


# ------- Distribution helpers ----------

def cdf_buckets(values: List[float], thresholds: List[float]) -> Dict[str, float]:
    n = len(values) or 1
    return {f">={t:.2f}": sum(1 for v in values if v >= t) / n * 100 for t in thresholds}


def percentile_buckets_share(values: List[float], thresholds: List[float]) -> Dict[str, float]:
    """Share of values >= each threshold, expressed as percent of columns."""
    return cdf_buckets(values, thresholds)


# ------- Driver ----------

def run(analyses: Path, out: Path, bench_order: List[str]) -> None:
    summary: Dict[str, dict] = {}
    for bench in bench_order:
        cov = load_jsonl(analyses / bench / "coverage.jsonl")
        dat = load_jsonl(analyses / bench / "data_metrics.jsonl")
        if not cov:
            print(f"[warn] {bench}: no coverage records ({analyses / bench / 'coverage.jsonl'})")
        if not dat:
            print(f"[warn] {bench}: no data records ({analyses / bench / 'data_metrics.jsonl'})")
        s = aggregate_query_signals(cov)
        d = aggregate_data_signals(dat)
        summary[bench] = {"queries": s, "data": d}

    # Materialise: one CSV per signal across benchmarks.
    write_csv_filter_types(summary)
    write_csv_join_types(summary)
    write_csv_aggregate_types(summary)
    write_csv_schema_types(summary)
    write_csv_group_by_keys(summary)
    write_csv_limit_buckets(summary)
    write_csv_union_all(summary)
    write_csv_join_count(summary)
    write_csv_features(summary)
    write_csv_null_distribution(summary)
    write_csv_mcv_distribution(summary)
    write_summary_json(summary)
    print(f"[ok] wrote signal tables to {out}")


def _pct_table(role: str, summary: dict, types=("INT", "TEXT", "DECIMAL", "DATE", "OTHER")) -> List[str]:
    rows = ["bench," + ",".join(types) + ",total"]
    for bench in BENCH_ORDER:
        sigs = summary[bench]["queries"]["type_counts"].get(role, {})
        total = sum(sigs.values()) or 1
        cells = [f"{sigs.get(t, 0) / total * 100:.2f}" for t in types]
        rows.append(f"{bench}," + ",".join(cells) + f",{total}")
    # Production-baseline rows (Snowflake / Redshift / Tableau, from the yaml).
    key = f"operator_logical_types_{role}"
    if key in PROD_BASELINE:
        for src, vals in PROD_BASELINE[key].items():
            tot = sum(vals.values()) or 1
            cells = [f"{vals.get(t, 0) / tot * 100:.2f}" for t in types]
            rows.append(f"{src}," + ",".join(cells) + f",{tot}")
    return rows


def write_csv_filter_types(summary):
    OUT.joinpath("filter_logical_types.csv").write_text("\n".join(_pct_table("filter", summary)))


def write_csv_join_types(summary):
    OUT.joinpath("join_key_logical_types.csv").write_text("\n".join(_pct_table("join", summary)))


def write_csv_aggregate_types(summary):
    OUT.joinpath("aggregate_logical_types.csv").write_text("\n".join(_pct_table("aggregate", summary)))


def write_csv_schema_types(summary):
    types = ("INT", "TEXT", "DECIMAL", "DATE", "OTHER")
    rows = ["bench," + ",".join(types) + ",columns"]
    for bench in BENCH_ORDER:
        st = summary[bench]["data"]["schema_types"]
        total = sum(st.values()) or 1
        cells = [f"{st.get(t, 0) / total * 100:.2f}" for t in types]
        rows.append(f"{bench}," + ",".join(cells) + f",{total}")
    src = PROD_BASELINE.get("column_logical_types_schema", {})
    for s, v in src.items():
        tot = sum(v.values()) or 1
        cells = [f"{v.get(t, 0) / tot * 100:.2f}" for t in types]
        rows.append(f"{s}," + ",".join(cells) + f",{tot}")
    OUT.joinpath("schema_column_types.csv").write_text("\n".join(rows))


def _bucket_csv(summary, key: str, buckets: List[str], baseline_key: str, fname: str):
    rows = ["bench," + ",".join(buckets) + ",total"]
    for bench in BENCH_ORDER:
        b = summary[bench]["queries"][key]
        total = sum(b.values()) or 1
        cells = [f"{b.get(bk, 0) / total * 100:.2f}" for bk in buckets]
        rows.append(f"{bench}," + ",".join(cells) + f",{total}")
    src = PROD_BASELINE.get(baseline_key, {})
    for s, v in src.items():
        tot = sum(v.values()) or 1
        cells = [f"{v.get(bk, 0) / tot * 100:.2f}" for bk in buckets]
        rows.append(f"{s}," + ",".join(cells) + f",{tot}")
    OUT.joinpath(fname).write_text("\n".join(rows))


def write_csv_group_by_keys(summary):
    _bucket_csv(summary, "group_by_keys", ["0", "1-2", "3-5", "6-10", "10+"],
                "group_by_key_count", "group_by_key_count.csv")


def write_csv_limit_buckets(summary):
    _bucket_csv(summary, "limit_buckets",
                ["0", "1-10", "11-100", "101-1K", "1K-10K", "10K-100K",
                 "100K-1M", "1M-10M", ">10M"],
                "limit_magnitude", "limit_magnitude.csv")


def write_csv_union_all(summary):
    _bucket_csv(summary, "union_all_fanin",
                ["0", "2", "3-4", "5-8", "9-16", "17-64", "65-256", ">256"],
                "union_all_fan_in", "union_all_fan_in.csv")


def write_csv_join_count(summary):
    _bucket_csv(summary, "join_count_per_query",
                ["0", "1-2", "3-5", "6-10", "11-50", "51-100", "101-500", "501+"],
                "join_count_per_query", "join_count_per_query.csv")


def write_csv_features(summary):
    feats = ["group_by", "limit", "union_all", "window_functions", "cte"]
    rows = ["bench," + ",".join(feats) + ",total_queries"]
    for bench in BENCH_ORDER:
        f = summary[bench]["queries"]["feature_counts"]
        total = summary[bench]["queries"]["total_queries"] or 1
        cells = [f"{f.get(fe, 0) / total * 100:.2f}" for fe in feats]
        rows.append(f"{bench}," + ",".join(cells) + f",{total}")
    OUT.joinpath("feature_prevalence.csv").write_text("\n".join(rows))


def _distribution_csv(summary, attr: str, baseline_key: str, fname: str):
    """Shared logic for NULL / MCV / similar threshold distributions."""
    thresholds = [0.01, 0.10, 0.30, 0.50, 0.70, 0.90]
    bucket_labels = [f">={t:.2f}" for t in thresholds]
    header_labels = [f"share_{lbl}" for lbl in bucket_labels]
    rows = ["bench," + ",".join(header_labels) + ",columns"]
    for bench in BENCH_ORDER:
        vals = summary[bench]["data"].get(attr, [])
        n = len(vals) or 1
        cells = [f"{sum(1 for v in vals if v >= t) / n * 100:.2f}" for t in thresholds]
        rows.append(f"{bench}," + ",".join(cells) + f",{n}")
    src = PROD_BASELINE.get(baseline_key, {})
    for source_label, values in src.items():
        cells = [f"{values.get(lbl, 0.0):.2f}" for lbl in bucket_labels]
        rows.append(f"{source_label}," + ",".join(cells) + ",n/a")
    OUT.joinpath(fname).write_text("\n".join(rows))


def write_csv_null_distribution(summary):
    _distribution_csv(summary, "null_fractions", "null_fraction_distribution",
                      "null_fraction_distribution.csv")


def write_csv_mcv_distribution(summary):
    _distribution_csv(summary, "mcv_shares", "mcv_share_distribution",
                      "mcv_share_distribution.csv")


def write_summary_json(summary):
    out = OUT / "summary.json"

    def cleanup(s):
        d = dict(s)
        # Truncate raw arrays to keep summary small.
        d.pop("null_fractions", None)
        d.pop("mcv_shares", None)
        d.pop("string_lengths", None)
        return d

    payload = {b: {"queries": summary[b]["queries"], "data": cleanup(summary[b]["data"])}
                for b in BENCH_ORDER}
    out.write_text(json.dumps(payload, indent=2))


def main(argv=None) -> int:
    global ANALYSES, OUT, BENCH_ORDER, PROD_BASELINE
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--analyses-dir", type=Path, default=PAPER_DIR / "work" / "analyses",
                    help="dir with one <bench>/{coverage,data_metrics}.jsonl per benchmark")
    ap.add_argument("--out-dir", type=Path, default=PAPER_DIR / "work" / "signals",
                    help="dir to write per-signal CSVs + summary.json")
    ap.add_argument("--baselines", type=Path,
                    default=PAPER_DIR / "signals" / "production_baselines.yaml",
                    help="production baseline values (Snowflake/Redshift/Tableau)")
    ap.add_argument("--benchmarks", nargs="*", default=DEFAULT_BENCH_ORDER,
                    help="benchmark keys to aggregate (default: the paper's 8)")
    args = ap.parse_args(argv)

    ANALYSES = args.analyses_dir
    OUT = args.out_dir
    BENCH_ORDER = list(args.benchmarks)
    PROD_BASELINE = _load_baselines(args.baselines)

    OUT.mkdir(parents=True, exist_ok=True)
    print(f"[aggregate] analyses={ANALYSES}")
    print(f"[aggregate] out={OUT}  benchmarks={BENCH_ORDER}")
    run(ANALYSES, OUT, BENCH_ORDER)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
