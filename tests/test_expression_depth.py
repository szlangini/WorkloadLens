from sqlglot import parse_one

from workloadlens.utils.expr_depth import compute_expression_depths, expression_height


def test_expression_height_simple_and_nested():
    assert expression_height(parse_one("a + b")) == 2
    assert expression_height(parse_one("a + (b * (c + 1))")) == 4
    assert expression_height(parse_one("CASE WHEN a > 1 THEN b + 2 ELSE c END")) == 4
    assert expression_height(parse_one("a IN (1, 2, 3)")) == 2


def test_compute_expression_depths_projection_and_filters():
    query = parse_one(
        """
        SELECT a + (b * (c + 1)) AS total, d
        FROM t
        WHERE a > 1 AND (b = 2 OR c BETWEEN 3 AND 5)
        GROUP BY a + (b * (c + 1)), d
        HAVING SUM(a) > 10
        """
    )
    scalar_depth, filter_depth = compute_expression_depths(query)
    assert scalar_depth == 4
    assert filter_depth == 4


def test_compute_expression_depths_in_clause():
    query = parse_one("SELECT a FROM t WHERE a IN (1, 2, 3, 4)")
    scalar_depth, filter_depth = compute_expression_depths(query)
    assert scalar_depth == 2
    assert filter_depth == 2
