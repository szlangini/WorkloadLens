#!/usr/bin/env bash
# =============================================================================
#  reproduce.sh — master entry point for the WorkloadLens benchmark-comparison
#  reproduction (paper "Scaling Analytical Benchmarks to Production Complexity",
#  Sections 3-5).
#
#  Reproduces every WorkloadLens-fed artifact in the paper's coverage analysis:
#     Figure 2  join-key & aggregate-input logical types
#     Figure 3  NULL-fraction & MCV-share CDFs
#     Figure 6  schema column types & GROUP BY key count
#     Table 3   LIMIT magnitude (no ORDER BY)
#  (Section 6's engine experiments are a separate artifact: prod-ds-kit's
#   reproduce_EAB.sh.)
#
#  Pipeline:  download -> analyze (WorkloadLens) -> aggregate -> figures
#
#  Usage:
#     ./reproduce.sh all                 # full pipeline at SF=10 (the default)
#     ./reproduce.sh all --sf 100        # paper-exact scale (heavy: hours, ~200GB)
#     ./reproduce.sh --quick all         # SF=1 smoke, generate-only subset (~min)
#     ./reproduce.sh download            # only provision benchmarks (fetch.sh)
#     ./reproduce.sh analyze             # only run WorkloadLens over them
#     ./reproduce.sh aggregate           # only (re)build per-signal CSVs
#     ./reproduce.sh figures             # only (re)render figures + Table 3
#     ./reproduce.sh clean | purge       # remove outputs | full work/ reset
#
#  SCALE: --sf affects only the scalable suites' DATA scans (NULL/MCV/schema)
#  and the Prod-DS LIMIT magnitude. AST signals are scale-independent. The paper
#  uses SF=100 (esp. Table 3, where Prod-DS LIMITs reach the 1M-10M extract mode);
#  at SF=10 the figures are qualitatively identical and Prod-DS's LIMIT mass sits
#  ~two decades lower (in 1K-10K). See README.md.
# =============================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PAPER_DIR="$HERE"
REPO_ROOT="$(cd "$PAPER_DIR/.." && pwd)"
WORK="${WL_PAPER_WORK:-$PAPER_DIR/work}"
SF="${SF:-10}"
QUICK=0

# Work tree is SF-scoped so SF=10 / SF=100 runs coexist cleanly. set_paths is
# re-invoked after --sf parsing.
set_paths() {
  BENCH="$WORK/sf$SF/benchmarks"
  ANALYSES="$WORK/sf$SF/analyses"
  SIGNALS="$WORK/sf$SF/signals"
  FIGURES="$WORK/sf$SF/figures"
}
set_paths
THREADS="${THREADS:-$(nproc 2>/dev/null || echo 4)}"
# Sampling policy (matches the paper pipeline): scan in full below the
# threshold; only the ~14 GB ClickBench parquet is column-sampled.
SAMPLE_THRESHOLD="${SAMPLE_THRESHOLD:-11811160064}"   # 11 GiB
SAMPLE_FRACTION="${SAMPLE_FRACTION:-0.71}"

# Benchmarks to run WorkloadLens over (jcch_skewed = the -k scan for the MCV
# figure; its coverage equals jcch's).
ANALYZE_BENCHES="tpcds tpch dsb jcch jcch_skewed job clickbench redbench prodds"
# Quick smoke: generate-only suites, no multi-GB downloads.
QUICK_BENCHES="tpcds tpch jcch prodds"

WL_BIN="${WL_BIN:-$REPO_ROOT/.venv/bin/workloadlens}"; command -v "$WL_BIN" >/dev/null 2>&1 || WL_BIN="workloadlens"
PY="${PY:-$REPO_ROOT/.venv/bin/python}"; [ -x "$PY" ] || PY="python3"

log()  { printf '\033[1;36m[repro %s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
warn() { printf '\033[1;33m[repro WARN]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[repro FATAL]\033[0m %s\n' "$*" >&2; exit 1; }

# ---- phases ----------------------------------------------------------------

cmd_download() {
  local list="$ANALYZE_BENCHES"; [ "$QUICK" = 1 ] && list="$QUICK_BENCHES"
  # fetch.sh dispatches jcch_skewed under jcch; pass unique fetch targets.
  local targets; targets="$(echo "$list" | tr ' ' '\n' | sed 's/jcch_skewed/jcch/' | sort -u | tr '\n' ' ')"
  log "download/generate benchmarks (SF=$SF): $targets"
  SF="$SF" bash "$PAPER_DIR/fetch.sh" --sf "$SF" $targets
}

analyze_one() {
  local b="$1" bd="$BENCH/$b" od="$ANALYSES/$b"
  [ -d "$bd" ] || { warn "$b: no benchmark dir ($bd) — skip (run download first?)"; return 0; }
  mkdir -p "$od"
  local ext=""; [ "$b" = "clickbench" ] && ext="--ext .parquet"

  if [ -d "$bd/queries" ] && [ "$(ls "$bd/queries"/*.sql 2>/dev/null | wc -l)" -gt 0 ] && [ -f "$bd/schema.sql" ]; then
    log "$b: workloadlens run (coverage)"
    "$WL_BIN" run --schema "$bd/schema.sql" --queries "$bd/queries/*.sql" \
      --json "$od/coverage.jsonl" --scope queries >"$od/run.log" 2>&1 \
      || warn "$b: run failed (see $od/run.log)"
  else warn "$b: missing schema.sql or queries/ — skipping coverage"; fi

  if [ -d "$bd/data" ] && [ -n "$(ls -A "$bd/data" 2>/dev/null)" ] && [ -f "$bd/schema.sql" ]; then
    log "$b: workloadlens data (column stats, threads=$THREADS)"
    "$WL_BIN" data "$bd/data" --out "$od/data_metrics.jsonl" --schema "$bd/schema.sql" \
      --column-stats --column-sample --column-sample-threshold "$SAMPLE_THRESHOLD" \
      --column-sample-fraction "$SAMPLE_FRACTION" --threads "$THREADS" $ext \
      >"$od/data.log" 2>&1 || warn "$b: data scan failed (see $od/data.log)"
  else warn "$b: missing schema.sql or data/ — skipping data scan"; fi
}

cmd_analyze() {
  local list="$ANALYZE_BENCHES"; [ "$QUICK" = 1 ] && list="$QUICK_BENCHES"
  command -v "$WL_BIN" >/dev/null 2>&1 || [ -x "$WL_BIN" ] || die "workloadlens not found — pip install -e . in $REPO_ROOT"
  mkdir -p "$ANALYSES"
  log "WorkloadLens over: $list"
  for b in $list; do analyze_one "$b"; done
}

cmd_aggregate() {
  mkdir -p "$SIGNALS"
  log "aggregate signals -> $SIGNALS"
  "$PY" "$PAPER_DIR/signals/aggregate_signals.py" \
    --analyses-dir "$ANALYSES" --out-dir "$SIGNALS" \
    --baselines "$PAPER_DIR/signals/production_baselines.yaml" || die "aggregate failed"
}

cmd_figures() {
  mkdir -p "$FIGURES"
  log "render figures + Table 3 -> $FIGURES"
  "$PY" "$PAPER_DIR/signals/render_paper_figures.py" \
    --analyses-dir "$ANALYSES" --data-dir "$SIGNALS" --out-dir "$FIGURES" --sf "$SF" \
    || die "render failed"
  log "artifacts:"; ls -1 "$FIGURES"/*.pdf "$FIGURES"/table3_* 2>/dev/null | sed 's/^/    /'
}

cmd_all() { cmd_download; cmd_analyze; cmd_aggregate; cmd_figures
  log "DONE — figures + Table 3 in $FIGURES (paper map: see README.md)"; }

# ---- main ------------------------------------------------------------------
TARGET=""
while [ $# -gt 0 ]; do
  case "$1" in
    --sf) SF="$2"; shift 2;;
    --sf=*) SF="${1#*=}"; shift;;
    --quick) QUICK=1; SF=1; shift;;
    -h|--help) sed -n '2,45p' "$0"; exit 0;;
    all|download|fetch|analyze|aggregate|figures|clean|purge) TARGET="$1"; shift;;
    *) die "unknown argument: $1 (try --help)";;
  esac
done
TARGET="${TARGET:-all}"
set_paths   # SF may have changed via --sf / --quick
[ "$QUICK" = 1 ] && log "QUICK smoke: SF=1, generate-only subset ($QUICK_BENCHES)"
log "target=$TARGET  SF=$SF  threads=$THREADS  work=$WORK/sf$SF"

case "$TARGET" in
  all) cmd_all;;
  download|fetch) cmd_download;;
  analyze) cmd_analyze;;
  aggregate) cmd_aggregate;;
  figures) cmd_figures;;
  clean) log "removing signals + figures (data/analyses kept)"; rm -rf "$SIGNALS" "$FIGURES"; echo done;;
  purge) log "PURGE — removing entire work/ tree"; rm -rf "$WORK"; echo done;;
  *) die "unknown target: $TARGET";;
esac
