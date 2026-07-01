from __future__ import annotations

from collections import Counter

import sqlglot
from sqlglot import exp

from workloadlens.coverage.operators import (
    count_filter_predicates,
    count_projection_expressions,
    summarize_ast_operators,
)
from workloadlens.coverage.types_ast import (
    collect_filter_expression_ops,
    summarize_ast_operator_types,
)
from .tpcds_utils import resolve_query_path


def _load_query(identifier: int) -> exp.Expression:
    sql_path = resolve_query_path(identifier)
    return sqlglot.parse_one(sql_path.read_text(), read="tsql")


def test_query12_manual_type_counts() -> None:
    """
    Query 12 (top 100 category revenue slice) contains:
    - three base tables (web_sales, item, date_dim) joined by two equality predicates
      plus the SELECT list, so we expect exactly two logical joins.
    - a WHERE clause with one IN list and one BETWEEN predicate (the join predicates are
      excluded), giving four total filter predicate leaves and a breakdown of
      AND=3, IN=1, BETWEEN=1.
    - nine projected columns (five character labels, four numerics) and four aggregate
      expressions (three SUM occurrences feeding both the measure and window ratio).
    The test exercises the AST-only logical type summariser to ensure these counts line up.
    """
    ast = _load_query(12)
    schema = {
        "web_sales": {
            "ws_item_sk": "INT",
            "ws_sold_date_sk": "INT",
            "ws_ext_sales_price": "DECIMAL",
        },
        "item": {
            "i_item_sk": "INT",
            "i_item_id": "TEXT",
            "i_item_desc": "TEXT",
            "i_category": "TEXT",
            "i_class": "TEXT",
            "i_current_price": "DECIMAL",
        },
        "date_dim": {
            "d_date_sk": "INT",
            "d_date": "DATE",
        },
    }

    operators, join_types = summarize_ast_operators(ast)
    assert operators["join"] == 2
    assert join_types == {"JOIN_TYPE_INNER": 2}

    assert count_projection_expressions(ast) == 7
    assert count_filter_predicates(ast) == 4
    assert collect_filter_expression_ops(ast, schema) == Counter({"and": 3, "in": 1, "between": 1})

    type_histograms = summarize_ast_operator_types(ast, schema)
    assert type_histograms["project"] == Counter({"TEXT": 5, "DECIMAL": 4})
    assert type_histograms["filter"] == Counter({"DATE": 1, "TEXT": 1})
    assert type_histograms["join"] == Counter({"INT": 4})
    assert type_histograms["aggregate"] == Counter({"DECIMAL": 4})
    assert type_histograms["aggregate_result"] == Counter({"DECIMAL": 4})

    aggregate_funcs = sum(1 for _ in ast.find_all(exp.AggFunc))
    assert aggregate_funcs == 4


def test_query22_manual_type_counts() -> None:
    """
    Query 22 (inventory balance rollup) pulls three tables with two inner joins,
    one BETWEEN predicate on the calendar, and an AVG over the inventory quantity.
    We hand count:
      - projection columns: five
      - filter predicate leaves: three (AND + BETWEEN)
      - aggregate expressions: one AVG()
    The logical type histogram should therefore show the single INT filter input,
    INT aggregate input, DOUBLE aggregate output, and text-heavy projections.
    """
    ast = _load_query(22)
    schema = {
        "inventory": {
            "inv_quantity_on_hand": "INT",
            "inv_item_sk": "INT",
            "inv_date_sk": "INT",
        },
        "date_dim": {
            "d_date_sk": "INT",
            "d_month_seq": "INT",
        },
        "item": {
            "i_product_name": "TEXT",
            "i_brand": "TEXT",
            "i_class": "TEXT",
            "i_category": "TEXT",
            "i_item_sk": "INT",
        },
    }

    operators, join_types = summarize_ast_operators(ast)
    assert operators["join"] == 2
    assert join_types == {"JOIN_TYPE_INNER": 2}

    assert count_projection_expressions(ast) == 5
    assert count_filter_predicates(ast) == 3
    assert collect_filter_expression_ops(ast, schema) == Counter({"and": 2, "between": 1})

    type_histograms = summarize_ast_operator_types(ast, schema)
    assert type_histograms["project"] == Counter({"TEXT": 4, "INT": 1})
    assert type_histograms["filter"] == Counter({"INT": 1})
    assert type_histograms["join"] == Counter({"INT": 4})
    assert type_histograms["aggregate"] == Counter({"INT": 1})
    assert type_histograms["aggregate_result"] == Counter({"DOUBLE": 1})

    aggregate_funcs = sum(1 for _ in ast.find_all(exp.AggFunc))
    assert aggregate_funcs == 1
