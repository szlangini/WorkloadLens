# WorkloadLens

WorkloadLens is a static coverage-analysis tool for SQL workloads. It analyzes queries, schemas, and data distributions to produce engine-agnostic metrics that characterize workload complexity at the logical level, without requiring execution traces or engine-internal telemetry. WorkloadLens was developed as the measurement instrument for the accompanying paper.

**Paper:** *Scaling Analytical Benchmarks to Production Complexity: A Data- and Query-Centric Extension to TPC-DS*
**Authors:** Jan Vincent Szlang, Sebastian Bress, Evangelia Kalyvianaki

## Quick Start

```bash
bash install.sh
source .venv/bin/activate
cd examples/quickstart && bash run.sh
```

This installs WorkloadLens in a virtual environment, then runs the full pipeline on a small retail schema with 5 queries and sample data. Results land in `examples/quickstart/outputs/` including per-query coverage JSONL, per-column data metrics, and a PDF report.

## What WorkloadLens Measures

### Query Signals

Derived from parsed SQL (SQLGlot ASTs), requiring only SQL text and schema DDL:

- **Operator type distributions** -- which logical types (number, text, date) are processed in filters, joins, aggregations, and ORDER BY
- **Operator usage patterns** -- LIMIT magnitude buckets, GROUP BY key counts, UNION ALL fan-in
- **Join-count distributions** -- joins per query, bucketed to match production workload studies
- **Function and predicate catalogs** -- aggregate functions, window functions, scalar expressions, predicate types
- **Expression depth** -- nesting depth in SELECT, WHERE, and HAVING clauses
- **Statement classification** -- DDL vs DML vs SELECT, byte length, token counts

### Data Signals

Derived from schema DDL and scanned data files:

- **Schema type exposure** -- column type distribution (TEXT, INT, DECIMAL, DATE) across the schema
- **NULL sparsity** -- per-column NULL fraction, reported as column-percentile CDF
- **MCV dominance** -- most-common-value concentration, top-K frequency, NDV
- **String length statistics** -- mean, max, P95 character lengths per string column
- **Numeric outlier rates** -- Tukey/IQR-based outlier share
- **Histogram skew** -- Q-error vs uniform expectation

See [docs/signals.md](docs/signals.md) for full definitions and bucket boundaries.

## Installation

Requires Python 3.9 or later. Key dependencies: DuckDB, SQLGlot, Typer, Matplotlib, Pandas.

```bash
# Automated (creates venv, installs, runs smoke tests)
bash install.sh

# Or manually
pip install -e .

# With test dependencies
pip install -e ".[test]"
```

## Usage

### Commands

| Command | Purpose |
|---|---|
| `workloadlens init` | Create a project configuration file |
| `workloadlens run` | Analyze SQL queries and emit coverage JSONL |
| `workloadlens data` | Profile data files and emit data metrics JSONL |
| `workloadlens pipeline` | End-to-end: run + data + report |
| `workloadlens report` | Generate PDF/JSON from existing JSONL outputs |
| `workloadlens compare` | Multi-workload comparison report |
| `workloadlens config-check` | Validate configuration files |

### Pipeline Example

```bash
# Initialize a project config
workloadlens init \
  --schema schema.sql \
  --queries "queries/*.sql" \
  --data /path/to/data \
  --output-dir outputs/my_workload \
  --config workloadlens.json \
  --scope queries \
  --layout tidy

# Run the full pipeline
workloadlens pipeline --config workloadlens.json --no-open
```

### Comparison Example

```bash
workloadlens compare \
  -c workload_a=config_a.json \
  -c workload_b=config_b.json \
  --out comparison.pdf \
  --json-out comparison.json \
  --no-open
```

## Output Format

| File | Content |
|---|---|
| `coverage/coverage.jsonl` | One JSON record per query: operator counts, join topology, functions, predicates, expression depth |
| `data/data_metrics.jsonl` | One JSON record per table/column: NULL rates, MCV, string lengths, outlier rates, histogram skew |
| `reports/workloadlens_report.pdf` | Visual summary with tables, histograms, and percentile plots |
| `report.json` (via `--json-out`) | Machine-readable aggregate of all metrics |

## Documentation

- [docs/signals.md](docs/signals.md) -- Signal categories and metric definitions
- [docs/methodology.md](docs/methodology.md) -- Sampling, histograms, verification workflow
- [docs/quickstart.md](docs/quickstart.md) -- End-to-end walkthrough
- [docs/comparing-workloads.md](docs/comparing-workloads.md) -- `workloadlens compare`: render styles, sort modes, baselines
- [examples/compare/README.md](examples/compare/README.md) -- Ready-made `workloadlens compare` recipes
- [AGENTS.md](AGENTS.md) -- Structured project overview for developers and AI agents

## Running Tests

```bash
pip install -e ".[test]"
pytest tests/ -v
```

Some tests require the DuckDB Substrait extension; these are skipped automatically if the extension is unavailable. TPC-DS-specific tests skip when query files are not present.

## For AI Agents

Read [AGENTS.md](AGENTS.md) for a structured project overview.

**Agent prompt (copy-paste):** Give this prompt to an AI coding agent (Claude Code, Cursor, Copilot, etc.) to set up and run WorkloadLens:

> Clone https://github.com/szlangini/WorkloadLens.git and read AGENTS.md for the full project guide. Run `bash install.sh` to create a virtualenv, install dependencies, build the TPC-DS query generator, and run smoke tests. Then activate the environment with `source .venv/bin/activate` and run the quickstart example: `cd examples/quickstart && bash run.sh`. Verify the test suite passes with `pytest tests/ -v`. Use `workloadlens --help` to explore available commands. Key entry points: `workloadlens pipeline` for end-to-end analysis, `workloadlens compare` for multi-workload comparison, and `workloadlens report` for PDF/JSON generation from existing outputs.

## License

Academic License -- non-commercial research and educational use only. See [LICENSE](LICENSE) for details.
