from sqlglot import parse_one

from workloadlens.coverage.operators import summarize_ast_operators


def _join_count(sql: str, join_key: str) -> int:
    ast = parse_one(sql)
    _, joins = summarize_ast_operators(ast)
    return joins.get(join_key, 0)


def test_exists_detected_as_semi_join():
    sql = "SELECT * FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.id = t.id)"
    assert _join_count(sql, "JOIN_TYPE_SEMI") == 1


def test_not_exists_detected_as_anti_join():
    sql = "SELECT * FROM t WHERE NOT EXISTS (SELECT 1 FROM u WHERE u.id = t.id)"
    assert _join_count(sql, "JOIN_TYPE_ANTI") == 1


def test_in_subquery_counts_as_semi_join():
    sql = "SELECT * FROM t WHERE t.id IN (SELECT u.id FROM u)"
    assert _join_count(sql, "JOIN_TYPE_SEMI") == 1


def test_not_in_subquery_counts_as_anti_join():
    sql = "SELECT * FROM t WHERE t.id NOT IN (SELECT u.id FROM u)"
    assert _join_count(sql, "JOIN_TYPE_ANTI") == 1
