# WorkloadLens Methodology

This document captures the end-to-end methodology used by WorkloadLens to characterize SQL workloads and their backing data. It is meant as a reusable reference when describing the tool in papers that compare synthetic benchmarks (e.g., TPC-DS/TPCH) with production traces (e.g., Snowflake and Amazon Redshift workloads) and when explaining how we verify that benchmark evolutions continue to mimic real-world pressures.

> **Scope**: The material below intentionally goes deeper than the README. It discusses how metrics are produced, what statistical shortcuts are used (sampling, outlier fallbacks, histograms), how PDFs/JSON exports are structured, and how to run verification loops where we compare benchmark runs against proprietary workload snapshots.

---

## 1. Goals & Verification Use Cases

1. **Rich characterization** – capture both query-structure and data-shape features that explain why a workload is hard or easy on modern analytical systems.
2. **Explain changes** – when we evolve benchmarks (e.g., tweak TPC-DS data generators or SQL), highlight which traits moved and why.
3. **Verification** – compare benchmark runs against ground-truth telemetry from Snowflake/Redshift customers. When operator mixes, join depths, cardinality patterns, and column distributions converge, we gain confidence that the benchmark mirrors real workloads.
4. **Regression guardrails** – provide repeatable JSON/PDF artifacts so CI/CD can detect drifts in our workload suites.

---

## 2. Pipeline Overview

| Stage | Command | Inputs | Outputs | Notes |
| --- | --- | --- | --- | --- |
| Query analysis | `workloadlens run` | Schema DDL, SQL files/globs | `coverage.jsonl` | SQLGlot-based parsing + (optional) DuckDB Substrait plans. |
| Data scan | `workloadlens data` | Data directory (`.tbl`, `.csv`) + optional schema | `data_metrics.jsonl` | Table stats are cheap; column stats (MCV, histogram, string/outlier metrics) use DuckDB + adaptive sampling. |
| Reporting | `workloadlens report` / `compare` | JSONL outputs (single or multiple workloads) | PDF report, optional aggregated JSON (`--json-out`) | Rich tables, histograms, comparison dashboards. |
| Pipeline | `workloadlens pipeline` | Project config (`workloadlens_*.json`) | All of the above | Orchestrates run → data → report, with telemetry (`--verbose`). |

A typical verification workflow uses two configs (e.g., TPC-DS vs. production-trace) and feeds their outputs into `workloadlens compare` to highlight deltas.

---

## 3. Query Characterization

### 3.1 Parsing and Normalization

1. **Dialect cleanup** – SQL is normalized via SQLGlot; vendor-specific syntax is rewritten so metrics are comparable.
2. **AST operator census** – A traversal counts logical operators, joins, subqueries, CTEs, unions, windows, etc.
3. **Plan extraction (optional)** – If DuckDB is present, we emit Substrait JSON plans and summarize physical operators. AST-only operators (`WINDOW`, `UNION ALL`, etc.) are retained to avoid blind spots.
4. **Predicate/function catalog** – We extract predicates, aggregate/analytic functions, scalar expressions, and literal usage for downstream heatmaps.

### 3.2 Metrics Table

| Metric family | Description | Why it matters |
| --- | --- | --- |
| Operator counts & shares | Per-query counts + workload-level share of scans, joins, aggregations, sorts, windows, set ops. | Indicates plan shape similarity between benchmarks and production traces. |
| Join topology | Join count per query, join type mix, self-joins, semi/anti joins. | Higher join depth often correlates with optimizer stress; we monitor to match Snowflake/Redshift telemetry. |
| Filter/Projection complexity | Filter predicates per query, predicate depth (nesting), columns referenced. | Highlights workloads that push predicate pushdown, UDF usage, etc. |
| Aggregation/Window usage | Distribution of aggregates, DISTINCT aggregates, window functions, partition/order specs. | Analytical workloads often lean on analytic windows; we need similar footprints. |
| Query length & tokens | Byte length, statement type, literal/identifier counts. | Proxy for ad-hoc query complexity. |
| Expression depth | Scalar expression depth in SELECT/WHERE/HAVING. | Captures nested expressions seen in BI workloads. |

Outputs land in `coverage.jsonl` with one JSON record per query; the `report` step aggregates them into summary tables, histograms, and percentile plots.

---

## 4. Data Characterization

### 4.1 Table-Level Metrics

| Metric | Method | Usage |
| --- | --- | --- |
| File size | `Path.stat().st_size` | Total storage footprint; used for sampling decisions. |
| Row count | Streaming newline count (with basic delimiter awareness). | Baseline for bytes/row, join fanouts, and sampling thresholds. |
| Bytes per row | `size_bytes / max(row_count, 1)` | Highlights wide tables vs. narrow lookup tables. |
| Format | File suffix (`tbl`, `csv`, …). | Allows mixing other formats if needed. |

The scan phase is single-pass and cheap; it runs before column stats so we can decide whether to sample.

### 4.2 Column-Level Metrics

We use DuckDB to load each table and project columns with schema-aligned names. For very large datasets we apply adaptive sampling:

- **Threshold**: when cumulative table bytes exceed 2 GiB, we sample ~25 % of rows per table (configurable via `--column-sample-*`).
- **Sampling method**: DuckDB `random()` predicate to avoid dialect-specific `USING SAMPLE` syntax.
- **Fallback**: If sampling yields 0 rows (tiny tables), we automatically rerun on the full data.

| Metric | Description | Implementation notes |
| --- | --- | --- |
| MCV (Most Common Values) | Top-K distinct counts per column; includes total rows, null count, NDV, top-K sum, max frequency, run-length, sorted flag. | Sortedness uses DuckDB window checks; run-length is average span of identical values. |
| String length stats | Mean, max, P95 char lengths. | Helps detect wide VARCHAR columns that trigger storage or network pressure. |
| Numeric outlier rate | Tukey/IQR-based outlier share with fallback to percentile winsorization for degenerate distributions. | Flags columns with heavy tails. |
| Histogram skew | Numeric histograms compared to uniform expectations; we report mean/max Q-error. | Useful to see whether our benchmark data has realistic skew. |
| Sample fraction | Stored per column (`sample_fraction`) so we know whether reported stats are full or sampled. | Propagated into JSON and PDFs. |

### 4.3 Histogram & Outlier Methodology

1. **Bucket selection** – up to 254 buckets, respecting DuckDB limits, sized via equal-width bins.
2. **Skew metric** – For each bucket we compare observed counts vs. uniform expectation, producing mean/max Q-error.
3. **Outliers** – Using Q1/Q3, we label rows beyond `1.5 * IQR`; if the column is binary or has ≤ 4 distincts, we skip outlier reporting to avoid noise.
4. **Null handling** – Null fraction is reported separately; NDV ignores NULL to avoid inflating cardinalities.

---

## 5. Schema & Metadata Profiling

During reporting we parse the schema DDL to build a catalog:

| Artifact | Description |
| --- | --- |
| Column catalog | Column type distribution (INT, DECIMAL, DATE/TIMESTAMP, TEXT, etc.). |
| Primary key coverage | Count of tables with PKs plus PK type mix. |
| Foreign key coverage | Count of tables with FKs plus FK type mix; recorded so we can compare relational modeling depth between workloads. |
| Table metadata | Links configs to schema paths, query globs, data directories, and output files. |

These summaries feed comparison PDFs so we can confirm that our schema shapes resemble production (e.g., share of TIMESTAMP columns, number of degenerate dimension tables, etc.). When schema information is available we also render the column-type distribution and a combined “primary vs. foreign key type” chart directly inside the data chapter so that readers can connect relational modeling choices with observed data distributions.

---

## 6. Reporting & Visualization Guidance

When exporting to PDF, we aim for a consistent story arc:

1. **Summary page** – workload metadata, total queries, schema size, data footprint, sampling indicators.
2. **Query structure** – stacked bars for operator shares, histograms for join counts and statement lengths, tables for top functions/predicates.
3. **Data overview** – histograms for table bytes, rows, bytes/row and (when schema is present) the column-type distribution so readers see how DDL maps onto data sizes.
4. **Column deep dive** – percentile curves for MCV dominance, null rates, NDV vs. row count, numeric outlier rates, string length stats, plus a combined “Primary vs. Foreign key type distribution” chart when PK/FK metadata is available.
5. **Histograms** – Q-error plots + sample columns with widest skew.
6. **Schema profile** – pie/stacked charts of type distribution, PK mix, table counts.
7. **Comparison overlay (when using `compare`)** – mirrored histograms and delta tables showing relative differences.
8. **Methodology appendix** – optional page (usually at bottom) describing sampling knobs, metric definitions, and data provenance (good spot to cite this document).

### Suggested Figure/Table Inventory

| Figure/Table | Purpose |
| --- | --- |
| Figure: Operator share per workload | Visual comparison of operator mix vs. Snowflake/Redshift traces. |
| Figure: Join depth distribution | Highlights whether benchmark joins resemble production. |
| Figure: Table bytes histogram | Shows whether dataset sizes align with target systems. |
| Figure: Column null-rate percentile plot | Useful proxy for sparse column prevalence. |
| Figure: Numeric outlier box plot | Identifies columns with extreme skew. |
| Figure: Primary vs. Foreign key type distribution | Validates that schema modeling depth (natural keys vs. surrogate FKs) matches production. |
| Table: Top 10 functions/predicates | Evidence of analytical complexity. |
| Table: Schema metadata summary | Quick view of column counts/types. |
| Table: Sampling decisions | Transparently documents when stats were sampled. |

---

## 7. Verification Workflow (Benchmark ↔ Production)

1. **Baseline capture** – Run `workloadlens pipeline` on your benchmark config (e.g., `workloadlens_tpcds.json`) and on anonymized production traces (Snowflake/Redshift). Capture outputs in versioned directories (e.g., `outputs/tpcds`, `outputs/redshift_2024q1`).
2. **Aggregate & compare** – Use `workloadlens compare -c benchmark=... -c prod=... --json-out ...`. Inspect PDFs for large deltas in operator mix, join depth, data skew, schema mix.
3. **Drift tracking** – Store the JSON aggregates in a metrics database; compute divergence scores (e.g., L1 distance between operator share vectors, Kolmogorov-Smirnov stats for join depth). Flag regressions in CI.
4. **Acceptance criteria** – Define tolerances per metric family. Example thresholds:
   - Operator share delta ≤ 5 percentage points for scans/joins/aggregates.
   - Median join count delta ≤ 1.
   - Bytes/row distribution overlap ≥ 80 %.
   - Null-rate percentile curves within ±10 percentage points in the 10th–90th percentiles.
5. **Feedback loop** – When benchmarks diverge, adjust data generators (e.g., column distributions, null injection) or SQL templates, then re-run the pipeline and recompare until metrics fall within tolerance.

This loop turns WorkloadLens into a verification harness: once the deltas shrink, we gain confidence that the benchmark captures the “unique challenges” of Snowflake/Redshift workloads.

---

## 8. Example Narrative (TPC-DS)

When documenting TPC-DS modernization:

1. **Setup** – Run `workloadlens pipeline --config workloadlens_tpcds.json --verbose`. Capture telemetry (`analyze/data/report timings`, sampling status).
2. **Highlight key findings** – e.g., “TPC-DS now shows 28 % window functions vs. 26 % in Snowflake sample,” or “Dimension tables maintain ≤ 20 % null rate, matching production tolerance.”
3. **Contextualize data skew** – Use histogram skew metrics to explain whether TPC-DS fact tables now emulate the heavy tails seen in cloud workloads.
4. **Call out remaining gaps** – “Redshift traces still show deeper join chains (median=6) vs. TPC-DS (median=4); we plan to add multi-hop joins in Rev 2.”

This narrative can be reused verbatim in papers or progress reports; simply cite the tables/figures generated in the PDF or JSON.

---

## 9. Configuration & Knob Summary

| Area | Knob | Default | Notes |
| --- | --- | --- | --- |
| Column stats | `--column-stats` | Enabled in `pipeline` | Requires schema. |
| MCV size | `--mcv-k` | 20 | Top-K per column. |
| Sampling toggle | `--column-sample/--column-no-sample` | On | Applied once total data bytes exceed threshold. |
| Sampling threshold | `--column-sample-threshold` | 2 GiB | Based on sum of table sizes. |
| Sampling fraction | `--column-sample-fraction` | 0.25 | Per-table row sampling fraction. |
| Pipeline telemetry | `pipeline --verbose` | Off | Prints stage timings + sampling summary. |
| Comparison metadata | `compare --json-out` | Optional | Dumps aggregated stats for downstream dashboards. |

Knobs can be overridden per run or encoded in the `workloadlens_*.json` config (preferred for reproducibility). When describing experiments in a paper, cite the config file and mention whether sampling was active.

---

## 10. How to Reference This Methodology

- **In papers** – cite “WorkloadLens methodology (see docs/methodology.md)” and summarize the relevant sections (e.g., query characterization, sampling). Include the figure/table references created by the report.
- **In repos** – keep this document alongside configs to remind contributors how metrics are derived.
- **In verification reports** – append a short methodology appendix referencing the specific knobs and sampling decisions.

With this documentation in place you can comfortably extract 2–6 pages’ worth of text for Overleaf, adapting the sections that matter for your narrative (e.g., verification vs. Snowflake/Redshift, benchmark tuning history, or specific metric definitions).
