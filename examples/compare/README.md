# `workloadlens compare` — usage examples

This folder is a guided tour through `workloadlens compare` for projects that
compare more than a couple of workloads at once. For setting up an individual
workload, see `../quickstart/`.

## Prerequisites

Each workload you want to compare needs a `workloadlens.json` (or equivalent
project config) pointing at a previously generated `coverage.jsonl` and
`data_metrics.jsonl`. The fastest way to produce one is `workloadlens
pipeline` against any source data — the quickstart project two folders up is a
ready-made example.

For a realistic multi-workload example with 9 benchmarks, see the
[Prod-DS revision repo](https://github.com/szlangini/prod-ds-kit) (`Benchmarks/<bench>/`).

## The four render styles

`--render-style` chooses how each grouped comparison panel is drawn.

| Style              | When to use it                                                                   |
|--------------------|----------------------------------------------------------------------------------|
| `bars` (default)   | 2 – 5 workloads. Above 5 the renderer auto-pivots to horizontal bars.            |
| `heatmap`          | 6+ workloads. Outliers and per-row patterns are immediately readable.            |
| `dot-plot`         | 6+ workloads where you also want a quick "are values clustered or spread" read.  |
| `small-multiples`  | Many workloads with distinct shapes; one panel per workload, easy to scan.       |

All four use the same input pipeline (`AggregatedReport`) — they only differ in
how the per-panel matrix is rendered. Production-baseline series (labels
matching `snowflake`, `redshift`, `production`, `realworld`) are visually
distinct in every mode.

## Sort order

`--sort-by` controls workload ordering across charts:

| Value        | Behaviour                                                              |
|--------------|------------------------------------------------------------------------|
| `input`      | Preserve the `-c` argument order. Default.                             |
| `label`      | Alphabetic by label.                                                   |
| `distance`   | L1 distance from `--reference <label>` on a normalised type vector.    |

`distance` is the right choice when you want the comparison story to read
"closest to my baseline → least similar" — the reference workload is always
placed first.

## Example commands

Bars at default settings (2 workloads):

```bash
workloadlens compare \
  -c "TPC-DS=tpcds/workloadlens.json" \
  -c "PROD-DS=prodds/workloadlens.json" \
  --out compare.pdf
```

Heatmap with 9 workloads, distance-sorted from PROD-DS:

```bash
workloadlens compare \
  -c "TPC-DS=tpcds/workloadlens.json" \
  -c "TPC-H=tpch/workloadlens.json" \
  -c "SSB=ssb/workloadlens.json" \
  -c "DSB=dsb/workloadlens.json" \
  -c "JCC-H=jcch/workloadlens.json" \
  -c "JOB=job/workloadlens.json" \
  -c "ClickBench=clickbench/workloadlens.json" \
  -c "CAB=cab/workloadlens.json" \
  -c "PROD-DS=prodds/workloadlens.json" \
  --render-style heatmap \
  --sort-by distance \
  --reference PROD-DS \
  --out compare_heatmap.pdf
```

Small multiples, distance-sorted:

```bash
workloadlens compare \
  $(printf -- '-c "%s=%s/workloadlens.json" ' \
      TPC-DS tpcds TPC-H tpch SSB ssb DSB dsb \
      JCC-H jcch JOB job ClickBench clickbench CAB cab PROD-DS prodds) \
  --render-style small-multiples \
  --sort-by distance \
  --reference PROD-DS \
  --out compare_multiples.pdf
```

## What changes at N workloads

| N range  | What the renderer does                                                          |
|----------|---------------------------------------------------------------------------------|
| N ≤ 3    | Identical to the legacy renderer: vertical bars, inside legend, full fontsize.  |
| N = 4–5  | CDF panels start cycling line styles to disambiguate overlap.                   |
| N ≥ 6    | Bars auto-pivot to horizontal. Legend moves below the axes with 5 columns max.  |
| N ≥ 6    | Font sizes scale down (`min(1, 4 / N)`); bar width is floored at 0.05.          |
| N > 12   | The curated 12-colour palette is extended via `matplotlib.colormaps['tab20']`. |

Backward-compat with the 2 – 3-workload reports the report pipeline used
before is preserved.
