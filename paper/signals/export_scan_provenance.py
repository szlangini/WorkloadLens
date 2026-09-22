#!/usr/bin/env python3
"""Export what is actually known about each WorkloadLens data scan.

The paper has said the profiles are unsampled and that both TPC-DS and Prod-DS are SF100.
Neither is right for the delivered scans, and the scans themselves record enough to say so
precisely. This writes that out.

What the records contain: `data_table_stats` gives each table's true row count; every
`data_column_stats` record gives the column's `sample_fraction` and a `row_count` that is the
SAMPLED row count, not the table's. Comparing the two recovers the sampling.

What they do NOT contain: the scan command, its settings, the sampling seed, and the wall time.
No record of any kind carries them, so they cannot be exported and are reported as unavailable.

Usage:  python paper/signals/export_scan_provenance.py --analyses-dir paper/work/sf100/analyses
"""
from __future__ import annotations

import argparse, collections, json
from pathlib import Path
from typing import Any, Dict

# Tables whose row count pins the TPC-DS-family scale. `item` and `store` are small enough that
# they are scanned unsampled, so their column row_count IS the true count -- which matters because
# the Prod-DS scan emitted no data_table_stats records at all.
SCALE_MARKERS = {"store_sales": {2_880_404: "SF1", 28_800_991: "SF10", 288_009_910: "SF100"},
                 "customer": {100_000: "SF1", 500_000: "SF10", 2_000_000: "SF100"},
                 "item": {18_000: "SF1", 102_000: "SF10", 204_000: "SF100"},
                 "store": {12: "SF1", 102: "SF10", 402: "SF100"}}
TOLERANCE = 0.01


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--analyses-dir", type=Path,
                    default=Path("paper/work/sf100/analyses"))
    ap.add_argument("--out", type=Path, default=Path("paper/work/sf100/scan_provenance.json"))
    args = ap.parse_args()

    out: Dict[str, Any] = {
        "what": "provenance of each WorkloadLens data scan, derived from the scan records",
        "not_recorded": ("the scan command, its settings, the sampling seed and the scan wall "
                         "time are not present in any record type; they cannot be exported"),
        "how_sampling_is_visible": ("data_column_stats.sample_fraction, and the gap between a "
                                    "column's row_count (sampled) and its table's row_count in "
                                    "data_table_stats (true)"),
        "benchmarks": {},
    }
    for d in sorted(args.analyses_dir.iterdir()):
        f = d / "data_metrics.jsonl"
        if not f.exists():
            continue
        tables: Dict[str, Dict[str, Any]] = {}
        samp = collections.Counter()
        ncols = 0
        total_bytes = 0
        for line in f.open():
            r = json.loads(line)
            t = r.get("record_type")
            if t == "data_table_stats":
                tables[r["table"]] = {"true_rows": r.get("row_count"),
                                      "bytes": r.get("size_bytes"),
                                      "format": r.get("format"),
                                      "file": r.get("file")}
                total_bytes += r.get("size_bytes") or 0
            elif t == "data_column_stats":
                ncols += 1
                sf = r.get("sample_fraction")
                samp[sf if sf is not None else "unsampled"] += 1
                tb = tables.setdefault(r["table"], {})
                tb.setdefault("sampled_rows", r.get("row_count"))
                tb.setdefault("sample_fraction", r.get("sample_fraction"))
        scale, how = None, None
        for marker, m in SCALE_MARKERS.items():
            tb = tables.get(marker) or {}
            tr = tb.get("true_rows")
            if tr in m:
                scale, how = m[tr], f"{marker}.true_rows == {tr:,}"
                break
            # no table record: recover the true count from the sampled one and the fraction
            sr, sf = tb.get("sampled_rows"), tb.get("sample_fraction")
            if sr is None:
                continue
            est = sr / sf if sf else sr
            for want, label in m.items():
                if abs(est - want) / want <= TOLERANCE:
                    scale = label
                    how = (f"{marker}: {sr:,} sampled"
                           + (f" / {sf} = {est:,.0f} estimated" if sf else " (unsampled)")
                           + f", within {TOLERANCE:.0%} of {want:,}")
                    break
            if scale:
                break
        frac = {str(k): v for k, v in sorted(samp.items(), key=lambda kv: str(kv[0]))}
        # A missing sample_fraction is NOT evidence of a full scan. Where a table record exists,
        # compare its row count against the column records': JOB's movie_info and name disagree
        # (14,835,720 vs 5,128,907 and 4,167,491 vs 2,908,507) with no fraction recorded at all.
        partial = {}
        for tname, tv in tables.items():
            tr, sr = tv.get("true_rows"), tv.get("sampled_rows")
            if isinstance(tr, int) and isinstance(sr, int) and tr != sr:
                partial[tname] = {"table_record_rows": tr, "column_record_rows": sr,
                                  "ratio": round(sr / tr, 4) if tr else None,
                                  "sample_fraction_recorded": tv.get("sample_fraction")}
        out["benchmarks"][d.name] = {
            "tables": len(tables), "columns": ncols,
            "inferred_scale": scale or "no TPC-DS-family scale marker in this schema",
            "scale_inferred_from": how or "n/a",
            "sample_fraction_histogram": frac,
            "sampling": ("mixed" if len(frac) > 1 else
                         "none recorded" if list(frac) == ["unsampled"] else f"uniform {list(frac)[0]}"),
            "tables_whose_column_records_cover_fewer_rows_than_the_table": partial,
            "scan_scope": (
                "sampled — a sample_fraction is recorded for every column"
                if "unsampled" not in frac else
                "UNVERIFIED — no data_table_stats records exist, so column coverage cannot be "
                "checked against the tables"
                if not any(isinstance(v.get("true_rows"), int) for v in tables.values()) else
                "UNVERIFIED — some tables' column records cover fewer rows than the table record, "
                "with no sample_fraction to explain it"
                if partial else
                "verified full — no sampling recorded and every table's column records cover all "
                "of its rows"),
            "source_dir": str(Path((list(tables.values()) or [{}])[0].get("file", "")).parent),
            "total_source_bytes": total_bytes,
            "total_source_gib": round(total_bytes / 2**30, 2),
            "tables_detail": {k: v for k, v in sorted(tables.items())},
        }
    args.out.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"wrote {args.out}")
    for b, v in out["benchmarks"].items():
        print(f"  {b:<14}{str(v['inferred_scale']):<8}{v['tables']:>4} tables {v['columns']:>5} cols"
              f"   sampling: {v['sampling']:<16}{v['total_source_gib']:>8.2f} GiB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
