# Reproducibility: WorkloadLens benchmark comparison (paper Sections 3–5)

This directory reproduces, **from scratch**, every WorkloadLens-derived figure
and table in the coverage-analysis part of:

> **Scaling Analytical Benchmarks to Production Complexity: A Data- and
> Query-Centric Extension to TPC-DS** *(Experiment, Analysis & Benchmark)*,
> J. V. Szlang, S. Breß, E. Kalyvianaki.

It downloads/generates a suite of analytical benchmarks, runs **WorkloadLens**
over each, aggregates the per-signal metrics, and renders the publication
figures + tables — i.e. the artifact required by the PVLDB Reproducibility
guidelines ("scripts to transform the raw data into the graphs included in the
paper"). The engine-performance experiments of **Section 6** are a *separate*
artifact (`prod-ds-kit/reproduce_EAB.sh`); this package covers **only** the
WorkloadLens comparison of Sections 3–5.

Artifact availability (per the paper):
WorkloadLens — <https://github.com/szlangini/WorkloadLens> ·
Prod-DS kit — <https://github.com/szlangini/prod-ds-kit>

---

## 1. What it reproduces — artifact → paper map

| Output (`work/figures/`)                  | Paper artifact | Signal | Baseline |
|---|---|---|---|
| `fig_join_key_logical_types.{pdf,png}`    | **Figure 2a** | join-key logical types | Snowflake [27] |
| `fig_aggregate_logical_types.{pdf,png}`   | **Figure 2b** | aggregate-input logical types | Snowflake [27] |
| `fig_null_fraction_cdf.{pdf,png}`         | **Figure 3a** | NULL-fraction CDF | Redshift [31] |
| `fig_mcv_share_cdf.{pdf,png}`             | **Figure 3b** | MCV-share CDF | Redshift [31] |
| `fig_schema_column_types.{pdf,png}`       | **Figure 6a** | schema column types | Redshift [31] + Tableau [33] |
| `fig_group_by_key_count.{pdf,png}`        | **Figure 6b** | GROUP BY key count | Snowflake [27] |
| `table3_limit_magnitude.{tex,md,csv}`     | **Table 3**   | LIMIT magnitude (no `ORDER BY`) | Snowflake [27] |

Supporting (analysis; not in the 13-page body): `fig_filter_logical_types`,
`fig_limit_magnitude` (bar form of Table 3), `fig_production_distance_summary_{A,B,C}`.

Production baselines are **transcribed from the primary studies** (Snowflake
[27], Amazon Redshift/Redset [31], Tableau "Get Real" [33]); no proprietary
data is used. Every value carries its source/figure/method tag in
[`signals/production_baselines.yaml`](signals/production_baselines.yaml).

## 2. Benchmarks profiled

| Bench | Provisioning | Upstream | Scale |
|---|---|---|---|
| TPC-DS | generate | gregrahn/tpcds-kit (`dsdgen`+`dsqgen`) | `--sf` |
| TPC-H | generate | gregrahn/tpch-kit (`dbgen`+`qgen`) | `--sf` |
| DSB | generate | microsoft/DSB (TPC-DS schema + templates) | `--sf` |
| JCC-H | generate | hyrise/jcch-dbgen (`+ -k` → `jcch_skewed` for MCV) | `--sf` |
| JOB | download | gregrahn/join-order-benchmark + CWI `imdb.tgz` | fixed |
| ClickBench | download | ClickHouse/ClickBench + `hits.parquet` (≈14 GB) | fixed |
| RedBench | download | DataManagementLab/Redbench (reuses IMDb) | fixed |
| Prod-DS | generate | prod-ds-kit (sibling artifact, STR=5 default) | `--sf` |

SSB and CAB are intentionally excluded (paper §5.1: out of the data-/query-level
scope of the comparison).

## 3. Quick start

```bash
# from the WorkloadLens repo root: install the tool + figure deps
python3 -m venv .venv && . .venv/bin/activate
pip install -e .                      # WorkloadLens (pulls matplotlib, pandas, duckdb…)
pip install -r paper/requirements.txt # PyYAML for the baseline table

# functional smoke check (SF=1, generate-only subset, no multi-GB downloads): ~minutes
paper/reproduce.sh --quick all

# full reproduction at SF=10 (default): downloads ~40 GB, runs a few hours
paper/reproduce.sh all

# paper-exact scale (Table 3 reaches the 1M–10M Prod-DS extract mode): heavy
paper/reproduce.sh all --sf 100
```

Per-phase targets (re-run any stage independently):

```bash
paper/reproduce.sh download   # clone generators + download/generate data+queries
paper/reproduce.sh analyze    # run WorkloadLens (coverage + data scan) per benchmark
paper/reproduce.sh aggregate  # collapse metrics into per-signal CSVs
paper/reproduce.sh figures    # render Figures 2/3/6 + Table 3
paper/reproduce.sh clean      # drop signals/ + figures/  (purge = wipe work/)
```

Outputs land in `work/sf<N>/` (git-ignored): `benchmarks/`, `analyses/`
(WorkloadLens JSONL), `signals/` (per-signal CSVs), `figures/`.

## 4. Scale factor — SF=10 vs SF=100

`--sf` affects only the **data scans** (NULL/MCV/schema, on the scalable suites)
and the **Prod-DS LIMIT magnitude** in Table 3. All AST/query-shape signals are
scale-independent.

- **SF=10 (default)** — fast to provision and verify; Figures 2/3/6 are
  qualitatively identical to the paper. The only visible difference is the
  Prod-DS row of Table 3: its `LIMIT` extracts scale with SF, so at SF=10 their
  mass sits in the `1K–10K` bucket.
- **SF=100 (paper)** — the manuscript's scale. Prod-DS `LIMIT`s reach the
  `1M–10M` bucket and reproduce the Snowflake production extract mode (≈67 % vs
  71 %). Use `--sf 100`. JOB/ClickBench/RedBench are fixed published datasets and
  do not scale.

The Table 3 caption is stamped with the SF actually run.

## 5. Hardware & software

- **Hardware (tested):** 2× AMD EPYC 7453 (56 physical cores), ~1 TiB RAM.
  *Minimum:* 8 cores, 32 GB RAM. **Disk:** SF=10 ≈ 40 GB (ClickBench 14 GB +
  IMDb 2 GB + generated suites); SF=100 ≈ 10× the scalable suites.
- **OS:** Ubuntu 24.04 LTS (Linux 6.x).
- **WorkloadLens:** this repo (`pip install -e .`), Python ≥3.9 (tested 3.12),
  DuckDB 1.4.1, sqlglot ≥18, matplotlib ≥3.8, pandas ≥2.1, PyYAML ≥6.
- **Benchmark build tools:** `git`, `make`, `gcc`, `bison`, `flex`,
  `curl`/`wget`, `tar`.

WorkloadLens is engine-agnostic and **static**: it parses SQL via sqlglot and
scans data files for column statistics; it does not execute the workloads, so no
database engine is needed for this artifact.

## 6. Measurement notes / determinism

- WorkloadLens is deterministic given fixed inputs; data generators are seeded
  (`-RNGSEED 0`, fixed `dbgen` seed) so reruns are byte-stable at a given SF.
- ClickBench's 14 GB parquet is **column-sampled** above an 11 GiB threshold
  (fraction 0.71), matching the paper pipeline; all smaller datasets are scanned
  in full. Override via `SAMPLE_THRESHOLD` / `SAMPLE_FRACTION` env vars.
- The reproducibility criterion is **behavioural agreement** with the paper
  (same orderings, gaps, and curve shapes), not identical pixels.

## 7. Layout

```
paper/
  reproduce.sh                 # master entry point (download|analyze|aggregate|figures|all)
  fetch.sh                     # from-scratch benchmark provisioning (one fn per benchmark)
  requirements.txt             # PyYAML (figure aggregator)
  signals/
    aggregate_signals.py       # JSONL -> per-signal CSVs (+ baseline rows)
    render_paper_figures.py    # CSVs/JSONL -> Figures 2/3/6 + Table 3
    production_baselines.yaml   # transcribed Snowflake/Redshift/Tableau values + provenance
  work/                        # (git-ignored) all generated/downloaded artifacts
    tools/                     # cloned upstream generators (SF-independent, shared)
    cache/                     # downloaded archives (imdb.tgz, …)
    sf<N>/                     # SF-scoped so SF=10 and SF=100 coexist cleanly
      benchmarks/<bench>/{schema.sql,queries/*.sql,data/}
      analyses/<bench>/{coverage.jsonl,data_metrics.jsonl}
      signals/*.csv
      figures/*.{pdf,png}, table3_limit_magnitude.{tex,md,csv}
```

Because the data/analyses/figures tree is **SF-scoped** (`work/sf10/`,
`work/sf100/`), you can run both scales without a stale mix and without
`purge` in between.

The **aggregate statistics** that back these figures (the per-signal CSVs that
are the direct plot inputs) are committed under [`data/`](data/), together with
the rendered figures — so reviewers can inspect the exact numbers without
provisioning any benchmarks. The Prod-DS **Table 3** row there is reported at
SF=100 (the one scale-sensitive signal; 44.4 % in the 1M–10M extract bucket); the
data distributions are scale-invariant. The raw per-benchmark scans are kept
local (git-ignored). See [`data/README.md`](data/README.md) for the per-artifact
map and provenance.

## 8. Caveats

- **Prod-DS** is produced by the sibling **prod-ds-kit** artifact (its data
  generator runs at the default STR=5 / NULL=medium / MCV=medium operating
  point). `fetch.sh` locates it via `$PRODDS_KIT`, then `~/prod-ds-cleanup`,
  else clones it. The MCV mid-tail is deliberately kept *below* the Redshift
  fleet (query-safety; paper §5.2.6).
- **DSB** query materialisation depends on the DSB repo's template layout, which
  varies across DSB releases; `fetch.sh` tries the known layouts and warns if it
  must be done manually. DSB shares the TPC-DS schema.
- **RedBench** read queries are derived from the Redbench generator; `fetch.sh`
  copies the repo's pre-generated IMDb workload if present, otherwise points to
  the Redbench README to materialise it.
- The renderer degrades gracefully: any benchmark not yet provisioned simply
  contributes an empty row, so partial runs still produce figures.
