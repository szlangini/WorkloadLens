#!/usr/bin/env bash
# =============================================================================
#  fetch.sh — provision the analytical benchmarks of the WorkloadLens paper
#  comparison (Sections 3-5) FROM SCRATCH: clone the upstream generators,
#  download the fixed datasets, and generate/materialise data + queries.
#
#  Lays out, under paper/work/benchmarks/<bench>/:
#       schema.sql        table DDL
#       queries/*.sql     one SQL file per query
#       data/             generated/downloaded data files
#  ready for `reproduce.sh analyze` to run WorkloadLens over.
#
#  Usage:
#     ./fetch.sh [--sf N] [bench ...]        # default: all paper benchmarks at SF=10
#     ./fetch.sh --sf 100 tpcds prodds       # only those, at SF=100
#     ./fetch.sh --list                      # show benchmarks + provisioning kind
#
#  Benchmarks: tpcds tpch dsb jcch job clickbench redbench prodds
#    (jcch also produces jcch_skewed via dbgen -k for the MCV figure.)
#    SSB and CAB are out of the paper's Section-5 scope and not provisioned.
#
#  Scale: --sf only affects the SCALABLE generators (tpcds, tpch, dsb, jcch,
#  prodds). JOB/ClickBench/RedBench are fixed-size published datasets.
#
#  Build/runtime deps: git, make, gcc, bison, flex, curl (or wget), python3,
#  and the WorkloadLens venv. Disk: SF=10 ~ 40 GB incl. ClickBench (14 GB) +
#  IMDb (2 GB); SF=100 is ~10x for the scalable suites.
# =============================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PAPER_DIR="$HERE"
REPO_ROOT="$(cd "$PAPER_DIR/.." && pwd)"
WORK="${WL_PAPER_WORK:-$PAPER_DIR/work}"
SF="${SF:-10}"
# Data + queries are SF-scoped so SF=10 and SF=100 trees coexist without a stale
# mix; the cloned generators (tools/) are SF-independent and shared.
BENCH="$WORK/sf$SF/benchmarks"
TOOLS="$WORK/tools"
JOBS="${JOBS:-$(nproc 2>/dev/null || echo 4)}"

ALL_BENCHES="tpcds tpch dsb jcch job clickbench redbench prodds"

# ---- canonical upstream sources --------------------------------------------
URL_TPCDS_KIT="https://github.com/gregrahn/tpcds-kit.git"
URL_TPCH_KIT="https://github.com/gregrahn/tpch-kit.git"
URL_DSB="https://github.com/microsoft/DSB.git"
URL_JCCH="https://github.com/hyrise/jcch-dbgen.git"
URL_JOB="https://github.com/gregrahn/join-order-benchmark.git"
URL_JOB_IMDB="http://homepages.cwi.nl/~boncz/job/imdb.tgz"
URL_CLICKBENCH="https://github.com/ClickHouse/ClickBench.git"
URL_CLICKBENCH_DATA="https://datasets.clickhouse.com/hits_compatible/hits.parquet"
URL_REDBENCH="https://github.com/DataManagementLab/Redbench.git"
# Prod-DS is produced by the sibling artifact (prod-ds-kit). Located via
# $PRODDS_KIT, else ~/prod-ds-cleanup, else cloned from here:
URL_PRODDS_KIT="https://github.com/szlangini/prod-ds-kit.git"

# ---- logging ---------------------------------------------------------------
log()  { printf '\033[1;34m[fetch %s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
warn() { printf '\033[1;33m[fetch WARN]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[fetch FATAL]\033[0m %s\n' "$*" >&2; exit 1; }

need() { command -v "$1" >/dev/null 2>&1 || die "missing required tool: $1"; }

download() {  # url dest
  local url="$1" dest="$2"
  [ -s "$dest" ] && { log "exists, skip download: $(basename "$dest")"; return 0; }
  mkdir -p "$(dirname "$dest")"
  if command -v curl >/dev/null 2>&1; then curl -fL --retry 3 -o "$dest.part" "$url"
  else wget -O "$dest.part" "$url"; fi || { rm -f "$dest.part"; die "download failed: $url"; }
  mv "$dest.part" "$dest"
}

clone() {  # url dir
  local url="$1" dir="$2"
  if [ -d "$dir/.git" ]; then log "repo present: $dir"; return 0; fi
  log "cloning $url -> $dir"
  git clone --depth 1 "$url" "$dir" || die "git clone failed: $url"
}

# Split a multi-statement .sql file into one file per statement (split on ';').
split_sql() {  # infile outdir prefix
  local infile="$1" outdir="$2" prefix="$3"
  mkdir -p "$outdir"
  awk -v dir="$outdir" -v pre="$prefix" '
    BEGIN { n=0; buf="" }
    { buf = buf $0 "\n" }
    /;[[:space:]]*$/ {
      gsub(/^[[:space:]]+/, "", buf)
      if (length(buf) > 5) { n++; printf "%s", buf > sprintf("%s/%s_%03d.sql", dir, pre, n); close(sprintf("%s/%s_%03d.sql", dir, pre, n)) }
      buf=""
    }
    END { if (length(buf) > 5) { n++; printf "%s", buf > sprintf("%s/%s_%03d.sql", dir, pre, n) } }
  ' "$infile"
}

bench_dir() { echo "$BENCH/$1"; }
have_data() { local d; d="$(bench_dir "$1")/data"; [ -d "$d" ] && [ -n "$(ls -A "$d" 2>/dev/null)" ]; }
have_queries() { local q; q="$(bench_dir "$1")/queries"; [ -d "$q" ] && [ "$(ls "$q"/*.sql 2>/dev/null | wc -l)" -gt 0 ]; }

# =============================================================================
#  TPC-DS  — gregrahn/tpcds-kit (dsdgen + dsqgen). Reuses the repo's prebuilt
#  tpcds-kit if present (saves a clone+build).
# =============================================================================
fetch_tpcds() {
  local bd; bd="$(bench_dir tpcds)"; mkdir -p "$bd/data" "$bd/queries"
  local kit; if [ -x "$REPO_ROOT/tpcds-kit/tools/dsdgen" ]; then kit="$REPO_ROOT/tpcds-kit"
             else kit="$TOOLS/tpcds-kit"; clone "$URL_TPCDS_KIT" "$kit"
                  log "building dsdgen/dsqgen"; make -C "$kit/tools" -s OS=LINUX >/dev/null 2>&1 || die "tpcds-kit build failed"; fi
  cp -f "$kit/tools/tpcds.sql" "$bd/schema.sql"

  if have_data tpcds; then log "tpcds data present, skip"; else
    log "dsdgen SF=$SF (this takes a while at high SF)"
    ( cd "$kit/tools" && ./dsdgen -SCALE "$SF" -DIR "$bd/data" -FORCE -TERMINATE N \
        -DISTRIBUTIONS "$kit/tools/tpcds.idx" >/dev/null 2>&1 ) || die "dsdgen failed"
  fi
  if have_queries tpcds; then log "tpcds queries present, skip"; else
    log "dsqgen 99 query templates"
    ( cd "$kit/tools" && ./dsqgen -DIRECTORY "$kit/query_templates" \
        -INPUT "$kit/query_templates/templates.lst" -SCALE "$SF" -DIALECT netezza \
        -OUTPUT_DIR "$bd" -RNGSEED 0 >/dev/null 2>&1 ) || die "dsqgen failed"
    split_sql "$bd/query_0.sql" "$bd/queries" "query" && rm -f "$bd/query_0.sql"
  fi
  log "tpcds: $(ls "$bd/data" | wc -l) data files, $(ls "$bd/queries"/*.sql | wc -l) queries"
}

# =============================================================================
#  TPC-H  — gregrahn/tpch-kit (dbgen + qgen)
# =============================================================================
fetch_tpch() {
  local bd; bd="$(bench_dir tpch)"; mkdir -p "$bd/data" "$bd/queries"
  local kit="$TOOLS/tpch-kit"; clone "$URL_TPCH_KIT" "$kit"
  [ -x "$kit/dbgen/dbgen" ] || { log "building tpch dbgen/qgen"; make -C "$kit/dbgen" -s >/dev/null 2>&1 || die "tpch-kit build failed"; }
  cp -f "$kit/dbgen/dss.ddl" "$bd/schema.sql"

  if have_data tpch; then log "tpch data present, skip"; else
    log "dbgen SF=$SF"
    ( cd "$kit/dbgen" && ./dbgen -s "$SF" -f >/dev/null 2>&1 && mv ./*.tbl "$bd/data/" ) || die "tpch dbgen failed"
  fi
  if have_queries tpch; then log "tpch queries present, skip"; else
    log "qgen 22 queries"
    for q in $(seq 1 22); do
      ( cd "$kit/dbgen" && DSS_QUERY="$kit/dbgen/queries" ./qgen -d "$q" 2>/dev/null ) \
        > "$bd/queries/q$(printf '%02d' "$q").sql" || warn "qgen q$q failed"
    done
  fi
  log "tpch: $(ls "$bd/data" | wc -l) data files, $(ls "$bd/queries"/*.sql 2>/dev/null | wc -l) queries"
}

# =============================================================================
#  DSB  — microsoft/DSB (TPC-DS-derived dsdgen + own query templates)
# =============================================================================
fetch_dsb() {
  local bd; bd="$(bench_dir dsb)"; mkdir -p "$bd/data" "$bd/queries"
  local kit="$TOOLS/DSB"; clone "$URL_DSB" "$kit"
  local dt="$kit/code/tools"
  [ -x "$dt/dsdgen" ] || { log "building DSB dsdgen/dsqgen"; make -C "$dt" -s >/dev/null 2>&1 || warn "DSB build had warnings"; }
  # DSB extends the TPC-DS schema verbatim.
  if [ -f "$dt/tpcds.sql" ]; then cp -f "$dt/tpcds.sql" "$bd/schema.sql"
  elif [ -f "$(bench_dir tpcds)/schema.sql" ]; then cp -f "$(bench_dir tpcds)/schema.sql" "$bd/schema.sql"; fi

  if have_data dsb; then log "dsb data present, skip"; else
    log "DSB dsdgen SF=$SF"
    ( cd "$dt" && ./dsdgen -SCALE "$SF" -DIR "$bd/data" -FORCE -TERMINATE N \
        -DISTRIBUTIONS "$dt/tpcds.idx" >/dev/null 2>&1 ) || warn "DSB dsdgen failed (check $dt)"
  fi
  if have_queries dsb; then log "dsb queries present, skip"; else
    log "materialising DSB query templates via dsqgen (-STREAMS 1)"
    local made=0
    for tdir in "$kit"/code/templates/*query*/ "$kit"/query_templates*/ "$kit"/templates/; do
      [ -d "$tdir" ] || continue
      local lst="$tdir/templates.lst"; [ -f "$lst" ] || continue
      local name; name="$(basename "$tdir" | tr -cd 'a-zA-Z0-9_')"
      ( cd "$dt" && ./dsqgen -DIRECTORY "$tdir" -INPUT "$lst" -SCALE "$SF" \
          -DIALECT netezza -OUTPUT_DIR "$bd" -RNGSEED 0 >/dev/null 2>&1 ) \
        && { split_sql "$bd/query_0.sql" "$bd/queries" "$name"; rm -f "$bd/query_0.sql"; made=1; }
    done
    [ "$made" = 1 ] || warn "DSB query materialisation produced nothing — see $kit (template layout varies by DSB version)"
  fi
  log "dsb: $(ls "$bd/data" 2>/dev/null | wc -l) data files, $(ls "$bd/queries"/*.sql 2>/dev/null | wc -l) queries"
}

# =============================================================================
#  JCC-H  — hyrise/jcch-dbgen (TPC-H schema + skew via -k). Produces both the
#  normal scan (jcch) and the skewed scan (jcch_skewed, for the MCV figure).
# =============================================================================
fetch_jcch() {
  local kit="$TOOLS/jcch-dbgen"; clone "$URL_JCCH" "$kit"
  [ -x "$kit/dbgen" ] || { log "building jcch-dbgen"; make -C "$kit" -s >/dev/null 2>&1 || die "jcch-dbgen build failed"; }
  local ddl; ddl="$(ls "$kit"/dss.ddl "$kit"/*.ddl 2>/dev/null | head -1)"

  # normal + skewed share schema + queries; differ only in the data scan.
  local bd; bd="$(bench_dir jcch)"; mkdir -p "$bd/data" "$bd/queries"
  local bs; bs="$(bench_dir jcch_skewed)"; mkdir -p "$bs/data"
  [ -n "$ddl" ] && { cp -f "$ddl" "$bd/schema.sql"; cp -f "$ddl" "$bs/schema.sql"; }

  if have_data jcch; then log "jcch data present, skip"; else
    log "jcch dbgen SF=$SF (normal)"
    ( cd "$kit" && ./dbgen -s "$SF" -f >/dev/null 2>&1 && mv ./*.tbl "$bd/data/" ) || warn "jcch dbgen failed"
  fi
  if have_data jcch_skewed; then log "jcch_skewed data present, skip"; else
    log "jcch dbgen SF=$SF -k (skewed, for MCV curve)"
    ( cd "$kit" && ./dbgen -s "$SF" -k -f >/dev/null 2>&1 && mv ./*.tbl "$bs/data/" ) || warn "jcch -k dbgen failed"
  fi
  if have_queries jcch; then log "jcch queries present, skip"; else
    log "jcch qgen 22 queries"
    for q in $(seq 1 22); do
      ( cd "$kit" && DSS_QUERY="$kit/queries" ./qgen -d "$q" 2>/dev/null ) \
        > "$bd/queries/q$(printf '%02d' "$q").sql" || warn "jcch qgen q$q failed"
    done
  fi
  ln -sfn "$bd/queries" "$bs/queries"
  log "jcch: $(ls "$bd/data" 2>/dev/null|wc -l) files · jcch_skewed: $(ls "$bs/data" 2>/dev/null|wc -l) files · $(ls "$bd/queries"/*.sql 2>/dev/null|wc -l) queries"
}

# =============================================================================
#  JOB  — gregrahn/join-order-benchmark (queries + DDL) + CWI IMDb CSV dump
# =============================================================================
fetch_job() {
  local bd; bd="$(bench_dir job)"; mkdir -p "$bd/data" "$bd/queries"
  local kit="$TOOLS/join-order-benchmark"; clone "$URL_JOB" "$kit"
  # schema.sql = table DDL (not the fk-index file)
  if [ -f "$kit/schema.sql" ]; then cp -f "$kit/schema.sql" "$bd/schema.sql"
  elif [ -f "$kit/schematext.sql" ]; then cp -f "$kit/schematext.sql" "$bd/schema.sql"; fi
  if ! have_queries job; then
    # JOB queries are NNx.sql (e.g. 1a.sql); skip the schema/fkindex files.
    for f in "$kit"/*.sql; do
      case "$(basename "$f")" in schema.sql|schematext.sql|fkindexes.sql) continue;; esac
      cp -f "$f" "$bd/queries/"
    done
  fi
  if have_data job; then log "job data present, skip"; else
    log "downloading IMDb dump (~1.2 GB) -> extracting CSVs"
    download "$URL_JOB_IMDB" "$WORK/cache/imdb.tgz"
    tar -xzf "$WORK/cache/imdb.tgz" -C "$bd/data" || die "imdb.tgz extract failed"
  fi
  log "job: $(ls "$bd/data"/*.csv 2>/dev/null|wc -l) csv files, $(ls "$bd/queries"/*.sql 2>/dev/null|wc -l) queries"
}

# =============================================================================
#  ClickBench  — ClickHouse/ClickBench (queries) + hits.parquet (14 GB)
# =============================================================================
fetch_clickbench() {
  local bd; bd="$(bench_dir clickbench)"; mkdir -p "$bd/data" "$bd/queries"
  local kit="$TOOLS/ClickBench"; clone "$URL_CLICKBENCH" "$kit"
  # schema: the DuckDB create.sql (single hits table)
  local crt; crt="$(ls "$kit"/duckdb/create.sql "$kit"/*/create.sql 2>/dev/null | head -1)"
  [ -n "$crt" ] && cp -f "$crt" "$bd/schema.sql"
  if ! have_queries clickbench; then
    local qf; qf="$(ls "$kit"/queries.sql "$kit"/*/queries.sql 2>/dev/null | head -1)"
    if [ -n "$qf" ]; then
      local i=0; while IFS= read -r line; do
        [ -z "${line// }" ] && continue
        i=$((i+1)); printf '%s\n' "$line" > "$bd/queries/q$(printf '%02d' "$i").sql"
      done < "$qf"
    else warn "ClickBench queries.sql not found in $kit"; fi
  fi
  if have_data clickbench; then log "clickbench data present, skip"; else
    log "downloading hits.parquet (~14 GB) — large!"
    download "$URL_CLICKBENCH_DATA" "$bd/data/hits.parquet"
  fi
  log "clickbench: $(ls "$bd/data" 2>/dev/null|wc -l) data files, $(ls "$bd/queries"/*.sql 2>/dev/null|wc -l) queries"
}

# =============================================================================
#  RedBench  — DataManagementLab/Redbench (IMDb-based read trace). Reuses JOB
#  data; copies the repo's pre-generated IMDb workload.
# =============================================================================
fetch_redbench() {
  local bd; bd="$(bench_dir redbench)"; mkdir -p "$bd/queries"
  local kit="$TOOLS/Redbench"; clone "$URL_REDBENCH" "$kit"
  have_data job || { warn "redbench reuses JOB/IMDb data — fetch job first"; }
  ln -sfn "$(bench_dir job)/data" "$bd/data"
  [ -f "$(bench_dir job)/schema.sql" ] && cp -f "$(bench_dir job)/schema.sql" "$bd/schema.sql"
  if ! have_queries redbench; then
    local src; src="$(ls -d "$kit"/output/generated_workloads/imdb "$kit"/**/generated_workloads/imdb 2>/dev/null | head -1)"
    if [ -n "$src" ] && [ -d "$src" ]; then
      log "copying pre-generated RedBench IMDb workload from $src"
      find "$src" -name '*.sql' -exec cp -f {} "$bd/queries/" \;
    else
      warn "RedBench pre-generated workload not found; run the Redbench generator in $kit (see its README), then re-run: ./fetch.sh redbench"
    fi
  fi
  log "redbench: data->job, $(ls "$bd/queries"/*.sql 2>/dev/null|wc -l) queries"
}

# =============================================================================
#  Prod-DS  — produced by the sibling artifact prod-ds-kit (the paper's other
#  reproducibility package). Generated at the default operating point
#  (STR=5, STRLEN=0, NULL=medium, MCV=medium).
# =============================================================================
fetch_prodds() {
  local bd; bd="$(bench_dir prodds)"; mkdir -p "$bd/data" "$bd/queries"
  local kit="${PRODDS_KIT:-$HOME/prod-ds-cleanup}"
  [ -d "$kit" ] || { kit="$TOOLS/prod-ds-kit"; clone "$URL_PRODDS_KIT" "$kit"; }
  local py; py="$kit/.venv/bin/python"; [ -x "$py" ] || py="python3"
  log "Prod-DS via prod-ds-kit at $kit (SF=$SF, default STR=5/MCV=medium/NULL=medium)"

  if have_data prodds && have_queries prodds; then log "prodds present, skip"; return 0; fi
  # Generate at the documented default; scale override when SF != 10.
  local scale_arg=""; [ "$SF" != "10" ] && scale_arg="--scale $SF"
  ( cd "$kit" && "$py" wrap_dsdgen.py --default $scale_arg --output-dir "$bd/data" ) \
    || warn "wrap_dsdgen.py failed — generate Prod-DS data manually (see prod-ds-kit REPRODUCIBILITY.md) and place in $bd/data"
  ( cd "$kit" && "$py" wrap_dsqgen.py --default $scale_arg --output-dir "$bd/queries" ) \
    || warn "wrap_dsqgen.py failed — materialise Prod-DS queries manually into $bd/queries"
  # Schema: the STR=5 DDL the kit emits.
  local sch; sch="$(ls "$bd"/schema.sql "$kit"/**/tpcds_*str5*.sql 2>/dev/null | head -1)"
  [ -n "$sch" ] && cp -f "$sch" "$bd/schema.sql" 2>/dev/null || true
  log "prodds: $(ls "$bd/data" 2>/dev/null|wc -l) data files, $(ls "$bd/queries"/*.sql 2>/dev/null|wc -l) queries"
}

# =============================================================================
#  driver
# =============================================================================
list_benches() {
  cat <<EOF
Paper benchmarks (Sections 3-5 comparison):
  tpcds       generate   gregrahn/tpcds-kit  (dsdgen + dsqgen)      [SF]
  tpch        generate   gregrahn/tpch-kit   (dbgen + qgen)         [SF]
  dsb         generate   microsoft/DSB       (dsdgen + templates)   [SF]
  jcch        generate   hyrise/jcch-dbgen   (+ -k -> jcch_skewed)  [SF]
  job         download   join-order-benchmark + CWI imdb.tgz        [fixed]
  clickbench  download   ClickHouse/ClickBench + hits.parquet 14GB  [fixed]
  redbench    download   DataManagementLab/Redbench (reuses IMDb)   [fixed]
  prodds      generate   prod-ds-kit (sibling artifact)             [SF]
EOF
}

main() {
  local benches=()
  while [ $# -gt 0 ]; do
    case "$1" in
      --sf) SF="$2"; shift 2;;
      --sf=*) SF="${1#*=}"; shift;;
      --list) list_benches; exit 0;;
      -h|--help) sed -n '2,40p' "$0"; exit 0;;
      *) benches+=("$1"); shift;;
    esac
  done
  [ "${#benches[@]}" -eq 0 ] && read -r -a benches <<< "$ALL_BENCHES"

  BENCH="$WORK/sf$SF/benchmarks"   # recompute: SF may have changed via --sf
  need git; need make; need awk
  mkdir -p "$BENCH" "$TOOLS" "$WORK/cache"
  log "scale factor SF=$SF · work tree: $WORK"
  log "provisioning: ${benches[*]}"

  for b in "${benches[@]}"; do
    case "$b" in
      tpcds) fetch_tpcds;;
      tpch) fetch_tpch;;
      dsb) fetch_dsb;;
      jcch|jcch_skewed) fetch_jcch;;
      job) fetch_job;;
      clickbench) fetch_clickbench;;
      redbench) fetch_redbench;;
      prodds) fetch_prodds;;
      *) warn "unknown benchmark: $b (see --list)";;
    esac
  done
  log "done. Next: ./reproduce.sh analyze   (run WorkloadLens over the benchmarks)"
}

main "$@"
