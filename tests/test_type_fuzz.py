from __future__ import annotations

import random
import string
from collections import Counter, defaultdict
from typing import Dict, Iterable, List, Tuple

import sqlglot
from workloadlens.coverage.types_ast import collect_top_level_order_types, summarize_ast_operator_types
from workloadlens.utils.types import normalize_type_label


SCHEMA: Dict[str, Dict[str, str]] = {
    "main": {
        "int_a": "INTEGER",
        "int_b": "INT",
        "int_c": "BIGINT",
        "dec_a": "DECIMAL(12, 2)",
        "dec_b": "NUMERIC(9, 3)",
        "text_a": "VARCHAR(64)",
        "text_b": "TEXT",
        "text_c": "CHAR(10)",
        "date_a": "DATE",
        "date_b": "DATE",
    },
    "aux": {
        "int_x": "INTEGER",
        "int_y": "INT",
        "dec_x": "DECIMAL(6, 2)",
        "dec_y": "NUMERIC(18, 4)",
        "text_x": "TEXT",
        "text_y": "VARCHAR(128)",
        "date_x": "DATE",
        "date_y": "DATE",
    },
}

TABLE_ALIASES = {"main": "m", "aux": "a"}


def _column_map() -> Dict[str, Dict[str, List[str]]]:
    mapping: Dict[str, Dict[str, List[str]]] = {}
    for table, columns in SCHEMA.items():
        table_map: Dict[str, List[str]] = defaultdict(list)
        for name, dtype in columns.items():
            table_map[normalize_type_label(dtype)].append(name)
        mapping[table] = table_map
    return mapping


COLUMN_BY_TYPE = _column_map()
TYPE_ORDER = sorted({label for table in COLUMN_BY_TYPE.values() for label in table})


def _random_identifier(rng: random.Random, prefix: str) -> str:
    suffix = "".join(rng.choice(string.ascii_lowercase) for _ in range(4))
    return f"{prefix}_{suffix}"


def _literal_for_type(rng: random.Random, type_label: str) -> str:
    if type_label == "INT":
        return str(rng.randint(-10_000, 10_000))
    if type_label == "DECIMAL":
        return f"{rng.randint(-1000, 1000)}.{rng.randint(0, 999):03d}"
    if type_label == "DATE":
        year = rng.randint(1990, 2030)
        month = rng.randint(1, 12)
        day = rng.randint(1, 28)
        return f"DATE '{year:04d}-{month:02d}-{day:02d}'"
    # treat everything else as text-like
    random_text = "".join(rng.choice(string.ascii_lowercase) for _ in range(5))
    return f"'{random_text}'"


def _pick_column(
    rng: random.Random,
    table: str,
    type_label: str | None = None,
) -> Tuple[str, str]:
    table_columns = COLUMN_BY_TYPE[table]
    type_choice = type_label or rng.choice(list(table_columns.keys()))
    column = rng.choice(table_columns[type_choice])
    alias = TABLE_ALIASES[table]
    return f"{alias}.{column}", type_choice


def _join_condition(rng: random.Random) -> Tuple[str, Counter]:
    type_label = rng.choice([label for label in TYPE_ORDER if label in COLUMN_BY_TYPE["main"] and label in COLUMN_BY_TYPE["aux"]])
    left, normalized = _pick_column(rng, "main", type_label)
    right, _ = _pick_column(rng, "aux", type_label)
    op = "=" if normalized in {"TEXT", "DATE"} else rng.choice(["=", "<>"])
    if normalized in {"INT", "DECIMAL"} and rng.random() < 0.2:
        left_expr = f"COALESCE({left}, {_literal_for_type(rng, normalized)})"
        right_expr = f"COALESCE({right}, {_literal_for_type(rng, normalized)})"
    else:
        left_expr = left
        right_expr = right
    return f"{left_expr} {op} {right_expr}", Counter({normalized: 2})


def _filter_condition(rng: random.Random) -> Tuple[str, Counter]:
    kind = rng.choice(["literal", "column", "between", "in", "null", "case"])
    expected = Counter()
    if kind == "literal":
        column, type_label = _pick_column(rng, "main")
        op = rng.choice(["=", "<>", ">", "<", ">=", "<="]) if type_label in {"INT", "DECIMAL", "DATE"} else "="
        expr = f"{column} {op} {_literal_for_type(rng, type_label)}"
        expected[type_label] += 1
        return expr, expected
    if kind == "column":
        type_label = rng.choice(TYPE_ORDER)
        column_left, _ = _pick_column(rng, "main", type_label)
        column_right, _ = _pick_column(rng, "main", type_label)
        expr = f"{column_left} = {column_right}"
        expected[type_label] += 2
        return expr, expected
    if kind == "between":
        column, type_label = _pick_column(rng, "main", rng.choice(["INT", "DECIMAL", "DATE"]))
        expr = f"{column} BETWEEN {_literal_for_type(rng, type_label)} AND {_literal_for_type(rng, type_label)}"
        expected[type_label] += 1
        return expr, expected
    if kind == "in":
        column, type_label = _pick_column(rng, "main")
        literals = ", ".join(_literal_for_type(rng, type_label) for _ in range(3))
        expr = f"{column} IN ({literals})"
        expected[type_label] += 1
        return expr, expected
    if kind == "null":
        column, type_label = _pick_column(rng, "main")
        expr = f"{column} IS {'NOT ' if rng.random() < 0.5 else ''}NULL"
        expected[type_label] += 1
        return expr, expected
    # case expression mixes numeric predicate with text payload
    int_col, _ = _pick_column(rng, "main", "INT")
    text_col1, _ = _pick_column(rng, "main", "TEXT")
    text_col2, _ = _pick_column(rng, "main", "TEXT")
    expr = f"CASE WHEN {int_col} > {_literal_for_type(rng, 'INT')} THEN {text_col1} ELSE {text_col2} END <> {_literal_for_type(rng, 'TEXT')}"
    expected["INT"] += 1
    expected["TEXT"] += 2
    return expr, expected


def _projection_expression(rng: random.Random) -> Tuple[str, Counter]:
    kind = rng.choice(["column", "arithmetic", "concat", "case", "coalesce"])
    expected = Counter()
    if kind == "column":
        column, type_label = _pick_column(rng, "main")
        return column, Counter({type_label: 1})
    if kind == "arithmetic":
        left, type_label = _pick_column(rng, "main", rng.choice(["INT", "DECIMAL"]))
        right, _ = _pick_column(rng, "main", type_label)
        op = rng.choice(["+", "-", "*"])
        expr = f"({left} {op} {right})"
        expected[type_label] += 2
        return expr, expected
    if kind == "concat":
        left, _ = _pick_column(rng, "main", "TEXT")
        right_literal = _literal_for_type(rng, "TEXT")
        expr = f"{left} || {right_literal}"
        return expr, Counter({"TEXT": 1})
    if kind == "case":
        num_col, _ = _pick_column(rng, "main", "INT")
        text_col1, _ = _pick_column(rng, "main", "TEXT")
        text_col2, _ = _pick_column(rng, "main", "TEXT")
        expr = f"CASE WHEN {num_col} > {_literal_for_type(rng, 'INT')} THEN {text_col1} ELSE {text_col2} END AS {_random_identifier(rng, 'alias')}"
        expected["INT"] += 1
        expected["TEXT"] += 2
        return expr, expected
    column, type_label = _pick_column(rng, "main")
    expr = f"COALESCE({column}, {_literal_for_type(rng, type_label)})"
    expected[type_label] += 1
    return expr, expected


def _order_expression(rng: random.Random) -> Tuple[str, Counter]:
    kind = rng.choice(["column", "function", "direction"])
    column, type_label = _pick_column(rng, "main")
    if kind == "column":
        return column, Counter({type_label: 1})
    if kind == "function" and type_label in {"TEXT", "DATE"}:
        func = "UPPER" if type_label == "TEXT" else "DATE_TRUNC('month',"
        if type_label == "TEXT":
            expr = f"{func}({column})"
            return expr, Counter({type_label: 1})
        expr = f"DATE_TRUNC('day', {column})"
        return expr, Counter({type_label: 1})
    direction = rng.choice(["ASC", "DESC"])
    expr = f"{column} {direction}"
    return expr, Counter({type_label: 1})


def _aggregate_expression(rng: random.Random) -> Tuple[str, Counter]:
    pattern = rng.choice(["sum", "avg", "count", "min", "max", "count_distinct"])
    if pattern in {"sum", "avg"}:
        column, type_label = _pick_column(rng, "main", rng.choice(["INT", "DECIMAL"]))
        func = pattern.upper()
        expr = f"{func}({column})"
        return expr, Counter({type_label: 1})
    if pattern == "count":
        column, type_label = _pick_column(rng, "main")
        return f"COUNT({column})", Counter({type_label: 1})
    if pattern == "count_distinct":
        column, type_label = _pick_column(rng, "main")
        return f"COUNT(DISTINCT {column})", Counter({type_label: 1})
    # min/max can operate on any type
    column, type_label = _pick_column(rng, "main")
    func = "MIN" if pattern == "min" else "MAX"
    return f"{func}({column})", Counter({type_label: 1})


def _build_group_columns(rng: random.Random) -> List[str]:
    count = rng.randint(0, 2)
    seen: set[str] = set()
    result: List[str] = []
    for _ in range(count):
        column, _ = _pick_column(rng, "main")
        if column in seen:
            continue
        result.append(column)
        seen.add(column)
    return result


def test_fuzz_join_type_inference() -> None:
    rng = random.Random(1337)
    iterations = 200
    for _ in range(iterations):
        conditions: List[str] = []
        expected = Counter()
        for _ in range(rng.randint(1, 3)):
            clause, counts = _join_condition(rng)
            conditions.append(clause)
            expected.update(counts)
        join_condition = " AND ".join(conditions)
        sql = f"SELECT 1 FROM main m JOIN aux a ON {join_condition}"
        ast = sqlglot.parse_one(sql, read="duckdb")
        counts = summarize_ast_operator_types(ast, SCHEMA)
        join_counter = counts.get("join", Counter())
        assert join_counter == expected


def test_fuzz_filter_type_inference() -> None:
    rng = random.Random(2024)
    iterations = 200
    for _ in range(iterations):
        clauses: List[str] = []
        expected = Counter()
        for _ in range(rng.randint(1, 4)):
            clause, counts = _filter_condition(rng)
            clauses.append(clause)
            expected.update(counts)
        connector = " AND " if rng.random() < 0.5 else " OR "
        where_expr = connector.join(f"({clause})" for clause in clauses)
        sql = f"SELECT 1 FROM main m WHERE {where_expr}"
        ast = sqlglot.parse_one(sql, read="duckdb")
        counts = summarize_ast_operator_types(ast, SCHEMA)
        filter_counter = counts.get("filter", Counter())
        assert filter_counter == expected


def test_fuzz_projection_type_inference() -> None:
    rng = random.Random(9001)
    iterations = 200
    for _ in range(iterations):
        expressions: List[str] = []
        expected = Counter()
        for _ in range(rng.randint(1, 5)):
            expr, counts = _projection_expression(rng)
            expressions.append(expr)
            expected.update(counts)
        select_list = ", ".join(expressions)
        sql = f"SELECT {select_list} FROM main m"
        ast = sqlglot.parse_one(sql, read="duckdb")
        counts = summarize_ast_operator_types(ast, SCHEMA)
        project_counter = counts.get("project", Counter())
        assert project_counter == expected


def test_fuzz_order_by_type_inference() -> None:
    rng = random.Random(5150)
    iterations = 200
    for _ in range(iterations):
        order_parts: List[str] = []
        expected = Counter()
        for _ in range(rng.randint(1, 4)):
            expr, counts = _order_expression(rng)
            order_parts.append(expr)
            expected.update(counts)
        sql = f"SELECT 1 FROM main m ORDER BY {', '.join(order_parts)}"
        ast = sqlglot.parse_one(sql, read="duckdb")
        counter = collect_top_level_order_types(ast, SCHEMA)
        assert counter == expected


def test_fuzz_aggregate_type_inference() -> None:
    rng = random.Random(4242)
    iterations = 200
    for _ in range(iterations):
        aggregate_parts: List[str] = []
        expected = Counter()
        for _ in range(rng.randint(1, 4)):
            expr, counts = _aggregate_expression(rng)
            aggregate_parts.append(expr)
            expected.update(counts)
        group_columns = _build_group_columns(rng)
        select_items = group_columns + aggregate_parts
        select_clause = ", ".join(select_items)
        group_clause = f" GROUP BY {', '.join(group_columns)}" if group_columns else ""
        sql = f"SELECT {select_clause} FROM main m{group_clause}"
        ast = sqlglot.parse_one(sql, read="duckdb")
        counts = summarize_ast_operator_types(ast, SCHEMA)
        aggregate_counter = counts.get("aggregate", Counter())
        assert aggregate_counter == expected
