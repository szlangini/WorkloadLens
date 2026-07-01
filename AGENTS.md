# WorkloadLens Agent Guide

## Project snapshot
- WorkloadLens statically analyzes SQL workloads + backing data to produce per-query coverage JSONL, data metrics JSONL, and PDF/JSON reports.
- Primary entrypoint is the CLI (`workloadlens`) defined in `src/workloadlens/cli.py`.
- Metric definitions and methodology live in `docs/methodology.md` and `docs/signals.md`.

## Repo layout
- `src/workloadlens/` — core package
  - `cli.py` — Typer CLI: `init`, `run`, `data`, `pipeline`, `report`, `compare`, `config-check`
  - `core/` — SQL normalization + DuckDB/Substrait plan extraction
  - `coverage/` — operator/function/predicate extraction + AST analysis
  - `report/` — aggregation + PDF/JSON export
  - `datafiles.py` — data directory scanning + column stats (DuckDB)
- `tests/` — pytest suite (see testing notes below)
  - `tests/data/tpcds_schema.sql` — TPC-DS schema (not shipped; fetched by `install.sh` from gregrahn/tpcds-kit)
- `docs/` — methodology, signals, quickstart
- `examples/quickstart/` — self-contained example with schema, queries, and data
- `workloadlens_example.json` — example configuration template

## Local setup
```bash
pip install -e ".[test]"
```

Or use the install script:
```bash
bash install.sh
```

## Common commands
```bash
# Create config (paths are resolved relative to the config file)
workloadlens init \
  --schema schema.sql \
  --queries "queries/*.sql" \
  --data data \
  --output-dir outputs \
  --config workloadlens.json \
  --scope queries \
  --layout tidy

# End-to-end pipeline (run -> data -> report)
workloadlens pipeline --config workloadlens.json --no-open

# Compare workloads
workloadlens compare \
  -c a=workloadlens_a.json \
  -c b=workloadlens_b.json \
  --out comparison.pdf \
  --json-out comparison.json

# Validate configs
workloadlens config-check workloadlens.json
```

## Running the quickstart example
```bash
cd examples/quickstart && bash run.sh
```

## Output layout (tidy)
- `output_dir/coverage/coverage.jsonl`
- `output_dir/data/data_metrics.jsonl`
- `output_dir/reports/workloadlens_report.pdf`
- PNG export (via `--export-pngs`) goes to `output_dir/reports/<report_stem>_png/` unless `--png-dir` is set.

## Testing
```bash
pytest tests/ -v
```

- DuckDB is required for most tests (`duckdb==1.4.1` is pinned).
- Substrait tests: Some tests attempt to `INSTALL substrait FROM community` via DuckDB. If the extension is unavailable, those tests are **skipped** automatically. You can provide a local extension path via `SQLCOV_SUBSTRAIT_PATH`.
- End-to-end tests generate temporary data/files; no repo files are modified.
- Some TPC-DS-specific tests skip if query files are not present (they are not shipped in this repo).

## Gotchas
- `pipeline` and `report` open PDFs by default; use `--no-open` in headless environments.
- Column stats sampling kicks in when total data size exceeds 2 GiB (defaults: 25% sample). Override with `--column-sample-*` flags.
- Config paths are resolved relative to the config file, not the current working directory.
