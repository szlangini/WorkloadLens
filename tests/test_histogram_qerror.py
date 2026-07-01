from __future__ import annotations

import random

import pytest

from workloadlens.datafiles import compute_histogram_skew_qerror


def test_histogram_qerror_uniform() -> None:
    values = list(range(200))
    random.shuffle(values)
    result = compute_histogram_skew_qerror(values, num_buckets=20, topk=20)
    assert result is not None
    mean_q, max_q, bucket_count, ndv = result
    assert bucket_count > 0
    assert ndv > bucket_count
    assert mean_q == pytest.approx(1.0, rel=0.2)
    assert max_q < 2.0


def test_histogram_qerror_skewed() -> None:
    values: list[int] = []
    values.extend([0] * 1000)
    for v in range(1, 101):
        freq = 50 if v <= 30 else 1
        values.extend([v] * freq)
    random.shuffle(values)
    result = compute_histogram_skew_qerror(values, num_buckets=20, topk=5)
    assert result is not None
    mean_q, _, _, _ = result
    assert mean_q > 2.0


def test_histogram_qerror_after_mcv_removal() -> None:
    values: list[int | None] = [None] * 200
    values.extend([0] * 900)
    values.extend([1] * 100)
    values.extend(list(range(2, 102)))
    random.shuffle(values)
    result = compute_histogram_skew_qerror(values, num_buckets=10, topk=2)
    assert result is not None
    mean_q, _, _, _ = result
    assert mean_q < 2.0
