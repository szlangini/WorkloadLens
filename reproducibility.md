# Reproducibility

This repository is the **WorkloadLens** artifact for the paper *Scaling Analytical
Benchmarks to Production Complexity: A Data- and Query-Centric Extension to TPC-DS*.

The [`paper/`](paper/) directory reproduces, **from scratch**, every
WorkloadLens-derived figure and table in the paper's coverage analysis
(Sections 3–5): **Figures 2, 3, 6** and **Table 3**. It downloads/generates a
suite of analytical benchmarks, runs WorkloadLens over each, aggregates the
per-signal metrics, and renders the publication figures.

> The engine-performance experiments of Section 6 are a *separate* artifact and
> are not covered here.

**Reviewers short on time:** the **aggregate statistics** that back every figure
and table below — the per-signal CSVs that are the direct plot inputs — are
committed under [`paper/data/`](paper/data/), together with the rendered figures.
Inspect the numbers directly. See [§5](#5-reported-aggregate-statistics).

## 1. Prerequisites

- Linux, Python ≥ 3.9 (tested on 3.12)
- Benchmark-generator build tools: `git`, `make`, `gcc`, `bison`, `flex`,
  `curl`/`wget`, `tar`
- Disk: ≈ 40 GB at the default SF = 10; ≈ 10× that at SF = 100

WorkloadLens is **static** — it parses SQL and scans data files, so no database
engine is needed for this artifact.

## 2. Setup

### Option A — Docker (recommended, hermetic)

A pinned `Dockerfile` (`python:3.12-slim`) builds the whole environment —
WorkloadLens plus the benchmark-generator toolchain — in one command:

```bash
docker build -t workloadlens .

# Reproduce the paper figures. The image's default entrypoint is the
# WorkloadLens CLI, so override it to run the paper pipeline; mount paper/work
# so the rendered figures land on the host:
docker run --rm -v "$PWD/paper/work:/app/paper/work" \
    --entrypoint bash workloadlens paper/reproduce.sh --quick all   # SF1 smoke, ~minutes
docker run --rm -v "$PWD/paper/work:/app/paper/work" \
    --entrypoint bash workloadlens paper/reproduce.sh all           # SF10 (default)
```

`docker run --rm workloadlens --help` runs the CLI itself for standalone use.

### Option B — native

```bash
git clone https://github.com/szlangini/WorkloadLens.git
cd WorkloadLens

python3 -m venv .venv && source .venv/bin/activate
pip install -e .                       # WorkloadLens + deps (duckdb==1.4.1, sqlglot, matplotlib, pandas)
pip install -r paper/requirements.txt  # PyYAML, for the baseline table
```

## 3. Run

```bash
# Quick smoke test — SF=1, generate-only subset, no multi-GB downloads (~minutes)
paper/reproduce.sh --quick all

# Full reproduction — SF=10 (default); downloads ~40 GB, runs a few hours
paper/reproduce.sh all

# Paper-exact scale — SF=100 (heavy: hours, large disk footprint)
paper/reproduce.sh all --sf 100
```

Each stage can also be run on its own:

```bash
paper/reproduce.sh download    # provision/generate every benchmark
paper/reproduce.sh analyze     # run WorkloadLens (coverage + data scan) over each
paper/reproduce.sh aggregate   # collapse metrics into per-signal CSVs
paper/reproduce.sh figures     # render Figures 2/3/6 + Table 3
paper/reproduce.sh clean       # drop signals/ + figures/  (purge = wipe work/)
```

## 4. Output

Artifacts land in `paper/work/sf<N>/figures/` (git-ignored):

| File | Paper artifact |
|---|---|
| `fig_join_key_logical_types.{pdf,png}` | Figure 2a |
| `fig_aggregate_logical_types.{pdf,png}` | Figure 2b |
| `fig_null_fraction_cdf.{pdf,png}` | Figure 3a |
| `fig_mcv_share_cdf.{pdf,png}` | Figure 3b |
| `fig_schema_column_types.{pdf,png}` | Figure 6a |
| `fig_group_by_key_count.{pdf,png}` | Figure 6b |
| `table3_limit_magnitude.{tex,md,csv}` | Table 3 |

## 5. Reported aggregate statistics

The repository commits the **aggregate statistics** that back every figure and
table of Sections 3–5, under [`paper/data/`](paper/data/): the per-signal CSVs
that are the direct plot inputs, plus the rendered figures. These CSVs are
byte-identical to the statistics the paper figures were rendered from.

| Statistic (`paper/data/signals/`) | Backs |
|---|---|
| `join_key_logical_types.csv`, `aggregate_logical_types.csv` | Figure 2 (a, b) |
| `null_fraction_distribution.csv`, `mcv_share_distribution.csv` | Figure 3 (a, b) |
| `schema_column_types.csv`, `group_by_key_count.csv` | Figure 6 (a, b) |
| `limit_magnitude.csv` → `figures/table3_limit_magnitude.{csv,md,tex}` | Table 3 |

The raw per-benchmark WorkloadLens scans these were aggregated from are kept
local (git-ignored); the aggregate statistics are what is reported. See
[`paper/data/README.md`](paper/data/README.md) for the full per-artifact map and
provenance.

See [`paper/README.md`](paper/README.md) for benchmark provenance,
hardware/software versions, and measurement caveats.
