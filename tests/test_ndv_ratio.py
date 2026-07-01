from __future__ import annotations

import pytest

from workloadlens.datafiles import ColumnMCVMetric, DataProfile
from workloadlens.report.pdf import _percentile_curve


def _sample_profile() -> DataProfile:
    return DataProfile(
        columns=[
            ColumnMCVMetric(
                table="t",
                column="col_a",
                file="t.tbl",
                column_type="INT",
                row_count=100,
                null_count=0,
                topk_sum=0,
                max_count=0,
                distinct_count=100,
                reported_k=1,
                requested_k=1,
                is_primary_key=True,
            ),
            ColumnMCVMetric(
                table="t",
                column="col_b",
                file="t.tbl",
                column_type="TEXT",
                row_count=100,
                null_count=0,
                topk_sum=0,
                max_count=0,
                distinct_count=1,
                reported_k=1,
                requested_k=1,
                is_foreign_key=True,
            ),
            ColumnMCVMetric(
                table="t",
                column="col_c",
                file="t.tbl",
                column_type="TEXT",
                row_count=100,
                null_count=50,
                topk_sum=0,
                max_count=0,
                distinct_count=2,
                reported_k=1,
                requested_k=1,
            ),
        ]
    )


def test_ndv_ratio_variants() -> None:
    profile = _sample_profile()

    ratios_rows = profile.ndv_ratios(denominator="rows", universe="all", clamp=False)
    ratios_non_null = profile.ndv_ratios(denominator="non_null", universe="all", clamp=False)
    ratios_rows_eligible = profile.ndv_ratios(denominator="rows", universe="eligible", clamp=False)
    ratios_non_null_eligible = profile.ndv_ratios(denominator="non_null", universe="eligible", clamp=False)

    assert ratios_rows == pytest.approx([1.0, 0.01, 0.02])
    assert ratios_non_null == pytest.approx([1.0, 0.01, 0.04])
    assert ratios_rows_eligible == pytest.approx([0.02])
    assert ratios_non_null_eligible == pytest.approx([0.04])


def test_percentile_curve_is_quantile_not_cdf() -> None:
    xs, ys = _percentile_curve([0.3, 0.1, 0.2], clamp=False)
    assert xs == pytest.approx([100 / 3, 200 / 3, 100])
    assert ys == pytest.approx([10.0, 20.0, 30.0])


def test_ndv_definition_nulls_and_markers() -> None:
    # Manual ColumnMCVMetric: NDV / non-null rows
    metric = ColumnMCVMetric(
        table="t",
        column="c",
        file="t.tbl",
        row_count=4,
        null_count=2,
        topk_sum=0,
        max_count=0,
        distinct_count=2,
        reported_k=1,
        requested_k=1,
    )
    profile = DataProfile(columns=[metric])
    ratios = profile.ndv_ratios(denominator="non_null", universe="all", clamp=False)
    assert ratios == [1.0]
