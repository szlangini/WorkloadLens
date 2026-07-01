# workloadlens/tests/test_functions.py
from workloadlens.core.normalize import extract_functions
from sqlglot import parse_one

def test_extract_functions_basic():
    ast = parse_one(
        "SELECT sum(x) - 1 AS s, coalesce(y, 0) AS y0, row_number() over (order by y) FROM t GROUP BY y"
    )
    fx = extract_functions(ast)
    assert "SUM" in {f.upper() for f in fx["agg_funcs"]}
    assert "ROW_NUMBER" in {f.upper() for f in fx["window_funcs"]}
    projection_funcs = {f.upper() for f in fx["projection_funcs"]}
    assert "COALESCE" in projection_funcs
    assert "SUM" not in projection_funcs
    assert "ROW_NUMBER" not in projection_funcs
    scalar = {f.upper() for f in fx["scalar_funcs"]}
    assert "SUB" in scalar
    assert "SUM" not in scalar


def test_extract_functions_scalar_ops():
    ast = parse_one(
        """
        SELECT a + b AS total
        FROM t
        WHERE a = b
          AND c > 10
        OR d BETWEEN 1 AND 5
        """
    )
    fx = extract_functions(ast)
    scalar = {name.upper() for name in fx["scalar_funcs"]}
    assert {"ADD", "EQUAL", "GREATER_THAN", "BETWEEN", "OR"}.issubset(scalar)
