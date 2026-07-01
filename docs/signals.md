# WorkloadLens Signal Definitions

WorkloadLens extracts two categories of signals: **query-centric** (derived from parsed SQL) and **data-centric** (derived from schema definitions and scanned data files). This document defines each signal, its granularity, and how it is reported.

For implementation details (sampling, histograms, outlier methodology), see [methodology.md](methodology.md).

---

## 1. Query-Centric Signals

These signals are extracted by parsing SQL statements through SQLGlot ASTs and (optionally) DuckDB Substrait plans. They require only the SQL text and schema DDL — no data access is needed.

### 1.1 Operator Type Distributions

For each core operator category (filters, joins, aggregations, ORDER BY), WorkloadLens classifies the logical types processed:

| Type bucket | Examples |
|---|---|
| Number | INTEGER, DECIMAL, FLOAT, BIGINT |
| Text | VARCHAR, CHAR, STRING |
| Date | DATE, TIMESTAMP, INTERVAL |
| Other | BOOLEAN, ARRAY, STRUCT, unknown |

Results are reported as per-operator share percentages across the workload.

### 1.2 Operator Usage Patterns

**LIMIT magnitude buckets:**

| Bucket | Range |
|---|---|
| 0 | No LIMIT |
| 1-10 | LIMIT 1 through 10 |
| 11-100 | LIMIT 11 through 100 |
| 101-1K | LIMIT 101 through 1,000 |
| 1K-10K | LIMIT 1,001 through 10,000 |
| 10K-100K | LIMIT 10,001 through 100,000 |
| 1M-10M | LIMIT 1,000,001 through 10,000,000 |
| >10M | LIMIT above 10,000,000 |

**GROUP BY key count distribution:**

| Bucket | Range |
|---|---|
| 0 | No GROUP BY |
| 1-2 | 1 or 2 grouping keys |
| 3-5 | 3 to 5 grouping keys |
| 6-10 | 6 to 10 grouping keys |
| 10+ | More than 10 grouping keys |

**UNION ALL fan-in distribution:** Number of UNION ALL branches per query.

### 1.3 Join-Count Distributions

The number of joins per query, reported as histogram buckets aligned with production workload studies:

| Bucket | Range |
|---|---|
| 0 | No joins |
| 1-2 | 1 or 2 joins |
| 3-5 | 3 to 5 joins |
| 6-10 | 6 to 10 joins |
| 11-20 | 11 to 20 joins |
| 21-30 | 21 to 30 joins |
| 31-50 | 31 to 50 joins |
| 51-100 | 51 to 100 joins |
| 101-500 | 101 to 500 joins |
| 500+ | More than 500 joins |

Join types (INNER, LEFT, RIGHT, FULL, CROSS, SEMI, ANTI) are tracked separately.

### 1.4 Statement Classification

| Metric | Description |
|---|---|
| Statement type | DDL, DML, or SELECT |
| Byte length | Raw SQL byte count |
| Token count | Number of SQL tokens after normalization |

### 1.5 Function and Predicate Catalog

| Category | Examples |
|---|---|
| Aggregate functions | COUNT, SUM, AVG, MIN, MAX, COUNT DISTINCT |
| Window functions | ROW_NUMBER, RANK, DENSE_RANK, NTILE, LAG, LEAD |
| Scalar expressions | CASE, CAST, COALESCE, EXTRACT, SUBSTRING |
| Predicate types | Equality, range, IN-list, LIKE, IS NULL, BETWEEN, EXISTS |

Each is counted per query and aggregated across the workload as frequency distributions.

### 1.6 Expression Depth

Nesting depth of scalar expressions in SELECT, WHERE, and HAVING clauses. Reported as maximum depth per query and workload-level percentile distributions.

---

## 2. Data-Centric Signals

These signals are derived from schema DDL and from scanning the actual data files (`.tbl`, `.csv`). They require a data directory and optionally a schema for type-aware analysis.

### 2.1 Schema-Level Type Exposure

Column type distribution across all tables in the schema:

| Type bucket | Mapped SQL types |
|---|---|
| TEXT | VARCHAR, CHAR, STRING, TEXT |
| INT | INTEGER, SMALLINT, BIGINT, TINYINT |
| DECIMAL | DECIMAL, NUMERIC, FLOAT, DOUBLE, REAL |
| DATE | DATE, TIMESTAMP, TIME, INTERVAL |
| Other | BOOLEAN, ARRAY, STRUCT, BLOB |

Reported as share of total columns per type bucket, enabling comparison of schema designs across workloads.

### 2.2 NULL Sparsity

Per-column NULL fraction (null_count / row_count). Reported as:
- Individual column NULL rates
- Column-percentile CDF: for each percentile P, the NULL fraction of the column at that percentile rank

This captures the overall sparsity profile of a workload's data. Production workloads typically show higher NULL rates than synthetic benchmarks.

### 2.3 MCV Dominance

Most-Common-Value analysis per column:

| Metric | Description |
|---|---|
| NDV | Number of distinct non-NULL values |
| Top-K frequency | Frequency of each of the K most common values (default K=20) |
| Top-K sum | Combined frequency of all top-K values |
| Max frequency | Frequency of the single most common value |
| Run-length | Average span of consecutive identical values (sortedness indicator) |
| Sorted flag | Whether the column is fully sorted |

MCV dominance reveals skew: a column where the top-5 values account for 90% of rows behaves very differently from a uniform column under aggregation and join operators.

### 2.4 String Length Statistics

Per string column:

| Metric | Description |
|---|---|
| Mean length | Average character count |
| Max length | Maximum character count |
| P95 length | 95th-percentile character count |

Wide VARCHAR columns with high P95 lengths create storage and network pressure that benchmarks should replicate.

### 2.5 Numeric Outlier Rates

Per numeric column, using the Tukey/IQR method:
- Compute Q1, Q3, IQR = Q3 - Q1
- Outliers are values below Q1 - 1.5*IQR or above Q3 + 1.5*IQR
- Report the outlier share (fraction of non-NULL rows that are outliers)
- Columns with 4 or fewer distinct values are excluded to avoid noise

### 2.6 Histogram Skew

Per numeric column:
- Build an equal-width histogram (up to 254 buckets)
- Compare observed bucket counts to the uniform expectation (total_rows / num_buckets)
- Report mean Q-error and max Q-error

Q-error = max(observed/expected, expected/observed). A mean Q-error near 1.0 indicates uniform data; higher values indicate skew that stresses hash-based and sort-based operators differently.

---

## Outputs

All signals are emitted in two formats:

| File | Content |
|---|---|
| `coverage.jsonl` | One JSON record per query with all query-centric signals |
| `data_metrics.jsonl` | One JSON record per table/column with all data-centric signals |

The `report` and `compare` commands aggregate these into PDF visualizations and optional JSON summaries (`--json-out`).

---

## References

- [methodology.md](methodology.md) — Sampling, histograms, verification workflow
- Paper Section 4.2: Signal taxonomy and comparison methodology
