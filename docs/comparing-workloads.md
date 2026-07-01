# Comparing workloads

`workloadlens compare` renders one PDF that places multiple workloads
side-by-side on every WorkloadLens panel — operator-type distributions,
LIMIT magnitudes, GROUP BY key counts, NULL/MCV column CDFs, etc.

This page explains:

1. How to feed the command,
2. Which `--render-style` to pick for which workload count,
3. How `--sort-by` affects the narrative,
4. Where the published production baselines fit in.

## Feeding the command

Each workload supplies a `workloadlens.json` project config (or the JSON
paths it would resolve to). Repeat `-c label=path` for every workload you
want in the comparison:

```bash
workloadlens compare \
  -c "TPC-DS=tpcds/workloadlens.json" \
  -c "TPC-H=tpch/workloadlens.json" \
  -c "PROD-DS=prodds/workloadlens.json" \
  --out compare.pdf
```

The label is what appears in every chart legend. Use the same casing the
paper uses (`TPC-DS`, not `tpc_ds`) so reviewers don't have to translate.

You can also pass pre-aggregated JSON via `--aggregated-in` if you've already
run `compare --json-out` once and just want to re-render.

## Choosing `--render-style`

The renderer ships four panel styles. They all read the same
`AggregatedReport`; they only differ in how each per-signal matrix is drawn.

| Style              | Default? | Sweet spot                                                           |
|--------------------|----------|----------------------------------------------------------------------|
| `bars`             | yes      | 2 – 5 workloads. Auto-pivots to horizontal bars at N ≥ 6.            |
| `heatmap`          |          | 6+ workloads. Outliers leap out instantly.                           |
| `dot-plot`         |          | Many workloads, where you want a Cleveland-dot density read.         |
| `small-multiples`  |          | Many workloads with distinct shapes; one panel per workload.         |

Picking a style is a *one-flag* decision: the underlying signals and
aggregation are unchanged, so you can re-render the same data with a
different style without re-running `pipeline`.

### Workload-count guidance

| Workloads | Recommended style                                          |
|-----------|------------------------------------------------------------|
| 2 – 3     | `bars` (vertical, inside legend — same as legacy reports). |
| 4 – 5     | `bars` (still vertical; CDFs now cycle line styles).       |
| 6 – 9     | `bars` (auto-horizontal), or `heatmap` for the cover view. |
| 10 – 15   | `heatmap` or `small-multiples`.                            |
| 16+       | `heatmap`, possibly with `--sort-by distance`.             |

### Production baselines

If a workload label contains `snowflake`, `redshift`, `production`, or
`realworld`, the renderer treats it as a reference baseline:

- In bar panels: neutral grey fill with a `//` hatch.
- In CDF panels: dashed dark line.
- In heatmaps: a boxed left edge on the row.
- In dot plots: filled squares (the other workloads use circles).

This lets reviewers tell at a glance which series is the published
production curve and which series is a benchmark candidate trying to match
it.

## Choosing `--sort-by`

`--sort-by` re-orders workloads across every panel. The reference is the
first workload after sorting.

| Value      | What it does                                                                     |
|------------|----------------------------------------------------------------------------------|
| `input`    | Preserve the `-c` order. Default. Use when you control the order yourself.       |
| `label`    | Alphabetic. Use when the consumer skims by name.                                 |
| `distance` | L1 distance from `--reference <label>` on a normalised operator-type vector.     |

The signal vector behind `distance` covers four operator roles
(`filter`, `join_key`, `aggregate`, `group_by`) and four canonical type
buckets (`INT`, `TEXT`, `DECIMAL`, `DATE`), normalised per role. It is a
coarse proxy for "how alike are these workloads?" — good enough for visual
ordering, not a substitute for the per-signal panels themselves.

If you don't pass `--reference`, the first workload after the initial
ordering is used.

## What changes at large N

For 2 – 3 workloads, layout and palette are unchanged from older WorkloadLens
releases. From N = 4 onwards the renderer adapts:

| N range  | Adaptation                                                                       |
|----------|----------------------------------------------------------------------------------|
| ≥ 4      | CDF panels cycle through `["-", "--", "-.", ":"]` so overlapped curves disambig. |
| ≥ 6      | Grouped bars pivot to horizontal; legend goes below the axes (≤ 5 columns).      |
| ≥ 6      | Font sizes scale down (`min(1, 4 / N)`).                                         |
| any      | Bar width is floored at 0.05 of the axis. Below 0.10 in-bar value labels drop.   |
| any      | The curated 12-colour palette extends via `matplotlib.colormaps['tab20']`        |
|          | when more than 12 workloads are compared.                                        |

## Common recipes

A typical paper-revision compare command (9 workloads, distance-sorted,
heatmap cover plus the default bars later):

```bash
# Bars view, distance-sorted from your baseline:
workloadlens compare \
  -c "TPC-DS=tpcds/workloadlens.json" \
  -c "PROD-DS=prodds/workloadlens.json" \
  ... \
  --sort-by distance --reference PROD-DS \
  --out compare_sorted.pdf

# Heatmap "cover page" for outlier detection:
workloadlens compare \
  -c "TPC-DS=tpcds/workloadlens.json" \
  -c "PROD-DS=prodds/workloadlens.json" \
  ... \
  --render-style heatmap \
  --sort-by distance --reference PROD-DS \
  --out compare_heatmap.pdf
```

See `examples/compare/` for ready-made commands and the rationale behind
each style.
