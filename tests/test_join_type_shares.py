from collections import Counter

from workloadlens.report.pdf import _join_type_shares


def test_join_type_share_mapping_basic():
    counts = Counter({
        "JOIN_TYPE_INNER": 8,
        "JOIN_TYPE_LEFT": 2,
        "JOIN_TYPE_SEMI": 1,
        "JOIN_TYPE_ANTI": 1,
    })
    shares = _join_type_shares(counts)
    assert round(shares.get("Inner", 0.0), 1) == 66.7
    assert round(shares.get("Outer", 0.0), 1) == 16.7
    assert round(shares.get("Semi", 0.0), 1) == 8.3
    assert round(shares.get("Anti", 0.0), 1) == 8.3


def test_join_type_share_handles_unknown_keys():
    counts = Counter({"JOIN_TYPE_UNKNOWN": 5})
    shares = _join_type_shares(counts)
    assert shares == {}


def test_join_type_share_cross_counts_as_inner():
    counts = Counter({"JOIN_TYPE_CROSS": 3, "JOIN_TYPE_RIGHT": 1})
    shares = _join_type_shares(counts)
    assert round(shares.get("Inner", 0.0), 1) == 75.0
    assert round(shares.get("Outer", 0.0), 1) == 25.0


def test_join_type_share_left_semi_detected():
    counts = Counter({"JOIN_TYPE_LEFT_SEMI": 4, "JOIN_TYPE_LEFT": 1})
    shares = _join_type_shares(counts)
    assert round(shares.get("Semi", 0.0), 1) == 80.0
    assert round(shares.get("Outer", 0.0), 1) == 20.0
