from sqlglot import parse_one

from workloadlens.coverage.types_ast import summarize_ast_operator_types
from workloadlens.utils.schema import parse_schema_types


def _schema(types_sql: str):
    return parse_schema_types(types_sql.strip())


def test_sum_uses_numeric_input_type():
    schema = _schema(
        """
        CREATE TABLE sales (
            amount INT
        );
        """
    )
    ast = parse_one("SELECT SUM(amount) FROM sales")
    hist = summarize_ast_operator_types(ast, schema)
    assert hist.get("aggregate", {}).get("INT", 0) == 1
    assert hist.get("aggregate_result", {}).get("INT", 0) == 1


def test_sum_inside_subquery_falls_back_to_table():
    schema = _schema(
        """
        CREATE TABLE sales (
            amount INT
        );
        """
    )
    ast = parse_one("SELECT SUM(amount) FROM (SELECT amount FROM sales) AS sub")
    hist = summarize_ast_operator_types(ast, schema)
    assert hist.get("aggregate", {}).get("INT", 0) == 1


def test_grouping_function_counts_text_inputs():
    schema = _schema(
        """
        CREATE TABLE items (
            category TEXT
        );
        """
    )
    ast = parse_one("SELECT GROUPING(category) FROM items GROUP BY ROLLUP(category)")
    hist = summarize_ast_operator_types(ast, schema)
    assert hist.get("aggregate", {}).get("TEXT", 0) == 0
    # grouping() is a separate function; verify group-by column is tracked
    assert hist.get("group_by", {}).get("TEXT", 0) == 1
