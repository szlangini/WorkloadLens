from workloadlens.report.pdf import _join_count_shares


def test_join_count_shares_basic_ranges():
    values = [1, 2, 4, 7, 15, 25, 40, 60, 150, 700, 1500]
    shares = _join_count_shares(values)
    expected_bins = [
        ("1-2", 2 / len(values)),
        ("3-5", 1 / len(values)),
        ("6-10", 1 / len(values)),
        ("11-20", 1 / len(values)),
        ("21-30", 1 / len(values)),
        ("31-50", 1 / len(values)),
        ("51-100", 1 / len(values)),
        ("101-500", 1 / len(values)),
        ("501-1000", 1 / len(values)),
        ("1000+", 1 / len(values)),
    ]
    for label, fraction in expected_bins:
        assert abs(shares.get(label, 0.0) - (fraction * 100)) < 1e-9


def test_join_count_shares_ignores_zero():
    values = [0, 0, 5]
    shares = _join_count_shares(values)
    assert shares.get("3-5", 0.0) == 100.0
