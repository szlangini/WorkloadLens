from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import textwrap

import pytest
import sqlglot

from workloadlens.coverage.types_ast import summarize_ast_operator_types, collect_filter_expression_ops, compute_filter_predicate_depth


BASE_SCHEMA = {
    "customers": {
        "id": "INT",
        "email": "TEXT",
        "signup_date": "DATE",
        "region_id": "INT",
        "nickname": "TEXT",
        "referred_by": "INT",
    },
    "orders": {
        "id": "INT",
        "customer_id": "INT",
        "order_date": "DATE",
        "status": "TEXT",
        "source": "TEXT",
        "region_id": "INT",
    },
    "calendar": {
        "date_key": "INT",
        "d_date": "DATE",
    },
    "regions": {
        "region_id": "INT",
        "code": "TEXT",
        "name": "TEXT",
    },
    "newsletters": {
        "email": "TEXT",
        "locale": "TEXT",
    },
    "stores": {
        "store_id": "INT",
        "code": "TEXT",
    },
    "inventory": {
        "store_id": "INT",
        "sku": "TEXT",
        "updated_at": "DATE",
    },
    "events": {
        "event_id": "INT",
        "name": "TEXT",
        "event_date": "DATE",
    },
    "employees": {
        "emp_id": "INT",
        "nickname": "TEXT",
        "manager_id": "INT",
    },
    "loyalty": {
        "customer_id": "INT",
        "tier": "TEXT",
    },
    "customer_address": {
        "ca_zip": "TEXT",
    },
}


@dataclass(frozen=True)
class JoinCase:
    name: str
    sql: str
    expected: Counter
    schema: dict[str, dict[str, str]] | None = None


CASES = [
    JoinCase(
        name="simple_inner_int",
        sql="SELECT * FROM customers c JOIN orders o ON c.id = o.customer_id",
        expected=Counter({"INT": 2}),
    ),
    JoinCase(
        name="function_expression_join",
        sql=textwrap.dedent(
            """
            SELECT *
            FROM stores s
            JOIN customer_address a
              ON SUBSTR(s.code, 1, 2) = SUBSTR(a.ca_zip, 1, 2)
            """
        ),
        expected=Counter({"TEXT": 2}),
    ),
    JoinCase(
        name="cast_expression_join",
        sql=textwrap.dedent(
            """
            SELECT *
            FROM orders o
            JOIN calendar cal
              ON CAST(o.order_date AS DATE) = cal.d_date
            """
        ),
        expected=Counter({"DATE": 2}),
    ),
    JoinCase(
        name="coalesce_join",
        sql=textwrap.dedent(
            """
            SELECT *
            FROM orders o
            JOIN customers c
              ON COALESCE(o.status, o.source) = c.nickname
            """
        ),
        expected=Counter({"TEXT": 3}),
    ),
    JoinCase(
        name="simple_inner_text",
        sql="SELECT * FROM customers c JOIN newsletters n ON c.email = n.email",
        expected=Counter({"TEXT": 2}),
    ),
    JoinCase(
        name="simple_inner_date",
        sql="SELECT * FROM orders o JOIN calendar cal ON o.order_date = cal.d_date",
        expected=Counter({"DATE": 2}),
    ),
    JoinCase(
        name="multi_condition_mixed",
        sql="SELECT * FROM customers c JOIN orders o ON c.id = o.customer_id AND c.signup_date = o.order_date",
        expected=Counter({"INT": 2, "DATE": 2}),
    ),
    JoinCase(
        name="multi_condition_text_int",
        sql="SELECT * FROM customers c JOIN regions r ON c.region_id = r.region_id AND c.nickname = r.name",
        expected=Counter({"INT": 2, "TEXT": 2}),
    ),
    JoinCase(
        name="comma_join_int",
        sql="SELECT * FROM customers c, orders o WHERE c.id = o.customer_id",
        expected=Counter({"INT": 2}),
    ),
    JoinCase(
        name="comma_join_mixed",
        sql="SELECT * FROM customers c, orders o WHERE c.id = o.customer_id AND c.email = o.status",
        expected=Counter({"INT": 2, "TEXT": 2}),
    ),
    JoinCase(
        name="left_join",
        sql="SELECT * FROM orders o LEFT JOIN customers c ON o.customer_id = c.id",
        expected=Counter({"INT": 2}),
    ),
    JoinCase(
        name="right_join",
        sql="SELECT * FROM stores s RIGHT JOIN inventory i ON s.store_id = i.store_id",
        expected=Counter({"INT": 2}),
    ),
    JoinCase(
        name="full_outer_join",
        sql="SELECT * FROM customers c FULL OUTER JOIN loyalty l ON c.id = l.customer_id",
        expected=Counter({"INT": 2}),
    ),
    JoinCase(
        name="semi_join_in",
        sql="SELECT * FROM customers c WHERE c.id IN (SELECT o.customer_id FROM orders o)",
        expected=Counter({"INT": 1}),
    ),
    JoinCase(
        name="semi_join_not_in",
        sql="SELECT * FROM customers c WHERE c.email NOT IN (SELECT n.email FROM newsletters n)",
        expected=Counter({"TEXT": 1}),
    ),
    JoinCase(
        name="semi_join_function",
        sql=textwrap.dedent(
            """
            SELECT *
            FROM customers c
            WHERE SUBSTR(c.email, 1, 3) IN (
                SELECT SUBSTR(n.email, 1, 3) FROM newsletters n
            )
            """
        ),
        expected=Counter({"TEXT": 1}),
    ),
    JoinCase(
        name="exists_correlated",
        sql=textwrap.dedent(
            """
            SELECT *
            FROM customers c
            WHERE EXISTS (
                SELECT 1 FROM orders o
                WHERE o.customer_id = c.id
                  AND o.status = c.nickname
            )
            """
        ),
        expected=Counter({"INT": 6, "TEXT": 6}),
    ),
    JoinCase(
        name="cte_basic",
        sql=textwrap.dedent(
            """
            WITH fav AS (
                SELECT id, email FROM customers
            )
            SELECT * FROM fav f JOIN orders o ON f.id = o.customer_id
            """
        ),
        expected=Counter({"INT": 2}),
    ),
    JoinCase(
        name="cte_region_lookup",
        sql=textwrap.dedent(
            """
            WITH pairs AS (
                SELECT customer_id, region_id FROM orders
            )
            SELECT * FROM pairs p JOIN regions r ON p.region_id = r.region_id
            """
        ),
        expected=Counter({"INT": 2}),
    ),
    JoinCase(
        name="cte_double_join",
        sql=textwrap.dedent(
            """
            WITH pairs AS (
                SELECT customer_id, region_id FROM orders
            )
            SELECT * FROM pairs p JOIN customers c
              ON p.customer_id = c.id AND p.region_id = c.region_id
            """
        ),
        expected=Counter({"INT": 4}),
    ),
    JoinCase(
        name="derived_simple",
        sql=textwrap.dedent(
            """
            SELECT *
            FROM (
                SELECT customer_id, status FROM orders
            ) o
            JOIN customers c
              ON o.customer_id = c.id AND o.status = c.nickname
            """
        ),
        expected=Counter({"INT": 2, "TEXT": 2}),
    ),
    JoinCase(
        name="derived_union",
        sql=textwrap.dedent(
            """
            SELECT *
            FROM (
                SELECT customer_id, order_date AS key_date FROM orders
                UNION ALL
                SELECT event_id AS customer_id, event_date AS key_date FROM events
            ) combined
            JOIN customers c ON combined.customer_id = c.id
            JOIN calendar cal ON combined.key_date = cal.d_date
            """
        ),
        expected=Counter({"INT": 2, "DATE": 2}),
    ),
    JoinCase(
        name="derived_distinct",
        sql=textwrap.dedent(
            """
            SELECT *
            FROM customers c
            JOIN (
                SELECT DISTINCT region_id FROM regions
            ) r ON c.region_id = r.region_id
            """
        ),
        expected=Counter({"INT": 2}),
    ),
    JoinCase(
        name="cte_aggregate",
        sql=textwrap.dedent(
            """
            WITH latest AS (
                SELECT store_id, MAX(updated_at) AS last_seen
                FROM inventory
                GROUP BY store_id
            )
            SELECT * FROM latest l JOIN stores s ON l.store_id = s.store_id
            """
        ),
        expected=Counter({"INT": 2}),
    ),
]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_join_logical_type_detection(case: JoinCase) -> None:
    ast = sqlglot.parse_one(case.sql)
    hist = summarize_ast_operator_types(ast, case.schema or BASE_SCHEMA)
    assert hist.get("join", Counter()) == case.expected


FILTER_CASES = [
    JoinCase(
        name="filter_join_only",
        sql="SELECT * FROM customers c, orders o WHERE c.id = o.customer_id",
        expected=Counter(),
        schema={"customers": {"id": "INT"}, "orders": {"customer_id": "INT"}},
    ),
    JoinCase(
        name="filter_text_literal",
        sql="SELECT * FROM customers c WHERE c.email = 'foo@example.com'",
        expected=Counter({"equals": 1}),
        schema={"customers": {"email": "TEXT"}},
    ),
    JoinCase(
        name="filter_mixed_with_join",
        sql=textwrap.dedent(
            """
            SELECT *
            FROM customers c, orders o
            WHERE c.id = o.customer_id
              AND c.email LIKE 'A%'
            """
        ),
        expected=Counter({"and": 1, "contains": 1}),
        schema={
            "customers": {"id": "INT", "email": "TEXT"},
            "orders": {"customer_id": "INT"},
        },
    ),
]


@pytest.mark.parametrize("case", FILTER_CASES, ids=lambda case: case.name)
def test_filter_logical_type_detection(case: JoinCase) -> None:
    ast = sqlglot.parse_one(case.sql)
    ops = collect_filter_expression_ops(ast, case.schema or BASE_SCHEMA)
    assert ops == case.expected


@pytest.mark.parametrize(
    "sql,schema,expected",
    [
        ("SELECT * FROM t WHERE a = 1", {"t": {"a": "INT"}}, 1),
        ("SELECT * FROM t WHERE a = 1 AND b = 2", {"t": {"a": "INT", "b": "INT"}}, 2),
        (
            "SELECT * FROM t WHERE a = 1 AND (b = 2 OR b = 3)",
            {"t": {"a": "INT", "b": "INT"}},
            3,
        ),
    ],
    ids=["simple", "and_chain", "nested_and_or"],
)
def test_filter_predicate_depth_metric(sql: str, schema: Dict[str, Dict[str, str]], expected: int) -> None:
    ast = sqlglot.parse_one(sql)
    depth = compute_filter_predicate_depth(ast, schema)
    assert depth == expected
