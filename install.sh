#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_DIR"

echo "=== WorkloadLens Install ==="
echo ""

# Step 0: Check system dependencies (read from system-requirements.txt)
SYS_REQ="$REPO_DIR/system-requirements.txt"
if [ ! -f "$SYS_REQ" ]; then
  echo "ERROR: system-requirements.txt not found in $REPO_DIR"
  exit 1
fi

# Detect package manager
PKG_MGR=""
if command -v apt-get >/dev/null 2>&1; then
  PKG_MGR="apt"
elif command -v yum >/dev/null 2>&1; then
  PKG_MGR="yum"
elif command -v brew >/dev/null 2>&1; then
  PKG_MGR="brew"
fi

MISSING_BINS=()
MISSING_PKGS=()

while IFS='|' read -r binary apt_pkg yum_pkg brew_pkg; do
  # Skip comments and empty lines
  [[ "$binary" =~ ^[[:space:]]*# ]] && continue
  [[ -z "$binary" ]] && continue

  binary="$(echo "$binary" | xargs)"
  apt_pkg="$(echo "$apt_pkg" | xargs)"
  yum_pkg="$(echo "$yum_pkg" | xargs)"
  brew_pkg="$(echo "$brew_pkg" | xargs)"

  if ! command -v "$binary" >/dev/null 2>&1; then
    MISSING_BINS+=("$binary")
    case "$PKG_MGR" in
      apt)  MISSING_PKGS+=("$apt_pkg") ;;
      yum)  MISSING_PKGS+=("$yum_pkg") ;;
      brew) MISSING_PKGS+=("$brew_pkg") ;;
    esac
  fi
done < "$SYS_REQ"

if [ ${#MISSING_BINS[@]} -gt 0 ]; then
  echo "ERROR: Missing system dependencies: ${MISSING_BINS[*]}"
  echo ""
  case "$PKG_MGR" in
    apt)
      echo "Install with:  sudo apt-get install -y ${MISSING_PKGS[*]}" ;;
    yum)
      echo "Install with:  sudo yum install -y ${MISSING_PKGS[*]}" ;;
    brew)
      echo "Install with:  brew install ${MISSING_PKGS[*]}" ;;
    *)
      echo "Please install the missing tools using your system package manager."
      echo "  Debian/Ubuntu (apt):  sudo apt-get install -y ${MISSING_BINS[*]}"
      echo "  RHEL/CentOS (yum):    sudo yum install -y ${MISSING_BINS[*]}"
      echo "  macOS (brew):         brew install ${MISSING_BINS[*]}" ;;
  esac
  exit 1
fi
echo "All system dependencies found."

# Step 1: Create venv (idempotent)
if [ ! -d ".venv" ]; then
  echo "Creating Python virtual environment (.venv)..."
  python3 -m venv .venv
else
  echo "Virtual environment (.venv) already exists."
fi

# Activate
# shellcheck disable=SC1091
source .venv/bin/activate

# Step 2: Install WorkloadLens with test dependencies
echo ""
echo "Installing WorkloadLens + test dependencies..."
pip install -e ".[test]" --quiet

# Step 3: Verify CLI
echo ""
echo "Verifying CLI..."
workloadlens version

# Step 4: Fetch TPC-DS schema from official tpcds-kit (idempotent)
echo ""
TPCDS_KIT_DIR="$REPO_DIR/tpcds-kit"
TPCDS_SCHEMA_SRC="$TPCDS_KIT_DIR/tools/tpcds.sql"
TPCDS_SCHEMA_DST="$REPO_DIR/tests/data/tpcds_schema.sql"

if [ -f "$TPCDS_SCHEMA_DST" ]; then
  echo "TPC-DS schema already present at tests/data/tpcds_schema.sql."
elif [ -f "$TPCDS_SCHEMA_SRC" ]; then
  echo "Copying TPC-DS schema from existing tpcds-kit checkout..."
  cp "$TPCDS_SCHEMA_SRC" "$TPCDS_SCHEMA_DST"
  echo "  -> tests/data/tpcds_schema.sql"
else
  echo "Cloning gregrahn/tpcds-kit (TPC-DS schema + tools)..."
  git clone --depth 1 https://github.com/gregrahn/tpcds-kit.git "$TPCDS_KIT_DIR"
  cp "$TPCDS_SCHEMA_SRC" "$TPCDS_SCHEMA_DST"
  echo "  -> tests/data/tpcds_schema.sql"
fi

# Step 5: Build dsqgen and generate TPC-DS queries (idempotent)
QUERY_DIR="$REPO_DIR/tests/queries"
if [ -d "$QUERY_DIR" ] && [ "$(ls -1 "$QUERY_DIR"/query*.sql 2>/dev/null | wc -l)" -ge 99 ]; then
  echo "TPC-DS queries already present in tests/queries/."
else
  echo "Building dsqgen query generator..."
  (
    cd "$TPCDS_KIT_DIR/tools"
    # Old TPC-DS C compiles under modern GCC (>=14 treats implicit-int /
    # implicit-function-declaration as errors, and defaults to -fno-common).
    make -f Makefile.suite dsqgen \
      LINUX_CFLAGS="-g -Wall -fcommon -Wno-implicit-int -Wno-implicit-function-declaration -Wno-return-type" \
      LDFLAGS="-Wl,--allow-multiple-definition" >/dev/null
  )
  echo "Generating TPC-DS queries..."
  mkdir -p "$QUERY_DIR"
  "$TPCDS_KIT_DIR/tools/dsqgen" \
    -DIRECTORY "$TPCDS_KIT_DIR/query_templates" \
    -INPUT "$TPCDS_KIT_DIR/query_templates/templates.lst" \
    -OUTPUT_DIR "$QUERY_DIR" \
    -DISTRIBUTIONS "$TPCDS_KIT_DIR/tools/tpcds.idx" \
    -DIALECT ansi \
    -SCALE 10 \
    -QUALIFY Y 2>/dev/null

  # Split single query_0.sql into individual query{1..99}.sql files
  python3 -c "
import re
from pathlib import Path
content = Path('$QUERY_DIR/query_0.sql').read_text()
parts = re.split(r'^-- start query (\d+) in stream \d+ using template .*\$', content, flags=re.MULTILINE)
count = 0
for i in range(1, len(parts), 2):
    num = parts[i]
    sql = parts[i+1].strip()
    Path(f'$QUERY_DIR/query{num}.sql').write_text(sql + '\n')
    count += 1
Path('$QUERY_DIR/query_0.sql').unlink()
print(f'  -> {count} queries generated in tests/queries/')
"
fi

# Step 6: Run smoke tests
echo ""
echo "Running smoke tests..."
pytest tests/test_smoke.py tests/test_pipeline_end_to_end.py -v

echo ""
echo "=== Install complete ==="
echo ""
echo "To activate the environment:"
echo "  source .venv/bin/activate"
echo ""
echo "To run the quickstart example:"
echo "  cd examples/quickstart && bash run.sh"
