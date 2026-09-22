#!/usr/bin/env bash
# Assemble the SF-scoped analyses tree that aggregate_signals.py + render_paper_figures.py
# expect, from two sources that each hold only half of what the paper figures need:
#
#   * the comparison benchmarks (TPC-DS, TPC-H, DSB, JCC-H, JOB, ClickBench, RedBench, ...)
#     live in paper/data/analyses/ and are scale-independent for the AST signals;
#   * Prod-DS must come from the current revision campaign, NOT from paper/data/analyses/,
#     whose Prod-DS scan predates the key-skew and MCV-recalibration work. Rendering with
#     the stale scan silently produces the submission's Figure 3 (MCV share 59.2/40.3/26.6/
#     22.4/14.7/9.8) instead of the revision's (74.1/61.5/46.4/41.5/30.3/18.4).
#
# Everything downstream of this script is the repository's own pipeline: the per-signal CSVs
# are produced by aggregate_signals.py, never edited by hand. A PROVENANCE.md next to the
# assembled tree records where each benchmark came from, with size, mtime and checksum.
#
# Usage:
#   paper/signals/assemble_analyses.sh [--sf 100] [--prodds <dir>] [--others <dir>] [--link]
#   SF=100 paper/reproduce.sh aggregate figures      # then render as usual
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PAPER_DIR="$(cd "$HERE/.." && pwd)"
REPO_ROOT="$(cd "$PAPER_DIR/.." && pwd)"

SF="${SF:-100}"
PRODDS_SRC="${PRODDS_SRC:-/home/jvs34/keyskew_work/campaign/wl_sf${SF}/analyses/prodds}"
OTHERS_SRC="${OTHERS_SRC:-$PAPER_DIR/data/analyses}"
WORK="${WL_PAPER_WORK:-$PAPER_DIR/work}"
LINK=0

while [ $# -gt 0 ]; do
  case "$1" in
    --sf)     SF="$2"; PRODDS_SRC="/home/jvs34/keyskew_work/campaign/wl_sf${SF}/analyses/prodds"; shift 2;;
    --prodds) PRODDS_SRC="$2"; shift 2;;
    --others) OTHERS_SRC="$2"; shift 2;;
    --link)   LINK=1; shift;;
    -h|--help) sed -n '2,20p' "$0"; exit 0;;
    *) echo "unknown argument: $1" >&2; exit 2;;
  esac
done

DEST="$WORK/sf${SF}/analyses"
# jcch_skewed is the -k data scan used for the MCV figure; its coverage equals jcch's.
OTHER_BENCHES="tpcds tpch dsb jcch jcch_skewed job clickbench redbench"

log() { printf '\033[1;36m[assemble]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[assemble FATAL]\033[0m %s\n' "$*" >&2; exit 1; }

[ -d "$PRODDS_SRC" ] || die "Prod-DS analyses not found: $PRODDS_SRC"
[ -d "$OTHERS_SRC" ] || die "comparison analyses not found: $OTHERS_SRC"
[ -s "$PRODDS_SRC/data_metrics.jsonl" ] || die "no data scan in $PRODDS_SRC"

mkdir -p "$DEST"
PROV="$WORK/sf${SF}/PROVENANCE.md"

place() {            # place <bench> <source-dir>
  local bench="$1" src="$2" dst="$DEST/$1"
  [ -d "$src" ] || { echo "  skip $bench (missing $src)"; return 0; }
  rm -rf "$dst"
  if [ "$LINK" = 1 ]; then ln -s "$src" "$dst"; else cp -r "$src" "$dst"; fi
  {
    printf '| `%s` | `%s` | ' "$bench" "$src"
    local parts=()
    for f in coverage.jsonl data_metrics.jsonl; do
      if [ -s "$src/$f" ]; then
        parts+=("$f $(wc -l < "$src/$f" | tr -d ' ') lines, $(date -r "$src/$f" '+%Y-%m-%d'), md5 $(md5sum "$src/$f" | cut -c1-8)")
      fi
    done
    local IFS='; '; printf '%s |\n' "${parts[*]}"
  } >> "$PROV"
}

log "assembling $DEST (SF=$SF, $([ "$LINK" = 1 ] && echo symlinks || echo copies))"
cat > "$PROV" <<EOF
# Provenance of \`work/sf${SF}/analyses\`

Assembled by \`paper/signals/assemble_analyses.sh\` on $(date '+%Y-%m-%d %H:%M:%S %Z').

Prod-DS comes from the revision measurement campaign; the comparison benchmarks come from the
repository's own scans, which are scale-independent for the AST signals. The per-signal CSVs in
\`work/sf${SF}/signals\` are generated from this tree by \`aggregate_signals.py\` and are never
edited by hand.

| benchmark | source | files |
|---|---|---|
EOF

place prodds "$PRODDS_SRC"
for b in $OTHER_BENCHES; do place "$b" "$OTHERS_SRC/$b"; done

cat >> "$PROV" <<EOF

RedBench's data scan is deliberately identical to JOB's: its \`data_dir\` is a symlink to JOB's
IMDb database, so RedBench is a query workload over JOB's data. It is therefore excluded from
the data-side figures (NULL fraction CDF, MCV share CDF) and left without a value for those two
signals in the distance summaries, while staying in every query-shape figure.
EOF

log "wrote $PROV"
log "next: SF=$SF $PAPER_DIR/reproduce.sh aggregate figures"
ls -1 "$DEST"
