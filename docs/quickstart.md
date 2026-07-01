# WorkloadLens Quickstart

This guide walks through a complete WorkloadLens run using the bundled example data.

---

## 1. Install

Using the install script (creates a venv and runs smoke tests):

```bash
bash install.sh
source .venv/bin/activate
```

Or manually:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[test]"
workloadlens version
```

---

## 2. Run the Example

```bash
cd examples/quickstart
bash run.sh
```

This runs the full pipeline on a small retail schema with 5 queries and sample data.

---

## 3. What Happened

The `run.sh` script executes two commands:

1. **`workloadlens init`** — Creates a `workloadlens.json` config pointing to the local schema, queries, and data files.

2. **`workloadlens pipeline`** — Runs the three pipeline stages in sequence:
   - **run** — Parses each SQL file against the schema, extracting operator counts, join topology, function usage, expression depth, and other query-centric signals. Outputs `coverage/coverage.jsonl`.
   - **data** — Scans the `.tbl` data files, computing table-level metrics (row count, file size, bytes/row) and column-level statistics (NULL rates, MCV, string lengths, numeric outliers, histogram skew). Outputs `data/data_metrics.jsonl`.
   - **report** — Aggregates the JSONL files into a PDF report with tables, histograms, and percentile plots. Outputs `reports/workloadlens_report.pdf`.

---

## 4. Interpret Results

After the run, `examples/quickstart/outputs/` contains:

```
outputs/
├── coverage/
│   └── coverage.jsonl      # One JSON record per query
├── data/
│   └── data_metrics.jsonl   # One JSON record per table + per column
├── reports/
│   └── workloadlens_report.pdf  # Visual summary
└── report.json              # Machine-readable aggregate
```

**coverage.jsonl** — Each line is a JSON object with fields like `query_name`, `join_count`, `operator_counts`, `functions`, `predicates`, `expression_depth`, and type distributions.

**data_metrics.jsonl** — Each line describes either a table (row count, file size) or a column (NULL rate, NDV, MCV top-K, string length stats, outlier rate, histogram Q-error).

**PDF report** — Contains summary tables, operator share charts, join-count histograms, null-rate percentile curves, and data distribution plots.

---

## 5. Compare Workloads

To compare two workloads, create configs for each and use `workloadlens compare`:

```bash
workloadlens compare \
  -c quickstart=workloadlens_a.json \
  -c other=workloadlens_b.json \
  --out comparison.pdf \
  --json-out comparison.json \
  --no-open
```

This produces a side-by-side PDF highlighting differences in operator mix, join depth, data skew, and schema structure.

---

## 6. Next Steps

- **Signal definitions** — See [signals.md](signals.md) for the full catalog of query-centric and data-centric metrics.
- **Methodology** — See [methodology.md](methodology.md) for sampling strategy, histogram construction, outlier detection, and verification workflow.
- **CLI reference** — Run `workloadlens --help` or any subcommand with `--help` for all available options.
