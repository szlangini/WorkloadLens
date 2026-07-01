# Reported experimental data — aggregate statistics (paper Sections 3–5)

This directory holds the **aggregate statistics** from which every
WorkloadLens-derived figure and table of the paper's coverage analysis
(**Figures 2, 3, 6** and **Table 3**) was rendered — the per-signal CSVs that are
the direct plot inputs — plus the rendered figures themselves.

The CSVs in `signals/` are byte-identical to the statistics behind the paper
figures. Reporting them is the PVLDB reproducibility deliverable ("the data that
transforms into the graphs"). The raw per-benchmark WorkloadLens scans they were
aggregated from are **not** committed (kept local, git-ignored) — the aggregate
statistics are what is needed to inspect and reproduce the figures.

## Aggregate statistic → paper artifact

| Statistic (`signals/`)            | Backs        | Rendered (`figures/`)                  |
|---|---|---|
| `join_key_logical_types.csv`      | **Fig 2a**   | `fig_join_key_logical_types.pdf`       |
| `aggregate_logical_types.csv`     | **Fig 2b**   | `fig_aggregate_logical_types.pdf`      |
| `null_fraction_distribution.csv`  | **Fig 3a**   | `fig_null_fraction_cdf.pdf`            |
| `mcv_share_distribution.csv`      | **Fig 3b**   | `fig_mcv_share_cdf.pdf`                |
| `schema_column_types.csv`         | **Fig 6a**   | `fig_schema_column_types.pdf`          |
| `group_by_key_count.csv`          | **Fig 6b**   | `fig_group_by_key_count.pdf`           |
| `limit_magnitude.csv` → Table 3   | **Table 3**  | `table3_limit_magnitude.{csv,md,tex}`  |

Supporting statistics (`filter_logical_types`, `feature_prevalence`,
`join_count_per_query`, `union_all_fan_in`) are included as well.

## Provenance

Query-shape statistics (Fig 2, 6, feature signals) come from WorkloadLens's
static SQL analysis; data statistics (Fig 3, schema) from its column scans.
Production baselines (`snowflake`/`redshift`/`tableau` rows) are transcribed from
the primary studies — see [`../signals/production_baselines.yaml`](../signals/production_baselines.yaml);
no proprietary data is used. SSB/CAB rows appear in some CSVs for completeness
but are excluded from the paper comparison (Sec. 5.1).
