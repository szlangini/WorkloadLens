#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== WorkloadLens Quickstart ==="

# Step 1: Initialize config
workloadlens init \
  --schema schema.sql \
  --queries "queries/*.sql" \
  --data data \
  --output-dir outputs \
  --config workloadlens.json \
  --scope queries \
  --layout tidy

# Step 2: Run full pipeline
workloadlens pipeline \
  --config workloadlens.json \
  --no-open \
  --json-out outputs/report.json

echo ""
echo "=== Done! ==="
echo "Results in: $SCRIPT_DIR/outputs/"
echo "  - coverage/coverage.jsonl  (per-query metrics)"
echo "  - data/data_metrics.jsonl  (per-table/column stats)"
echo "  - reports/                 (PDF report)"
echo "  - report.json              (machine-readable aggregate)"
