from __future__ import annotations

from dataclasses import dataclass
from itertools import product
import math

import pytest
from sqlglot import parse_one

from workloadlens.coverage.types_ast import summarize_ast_operator_types
from workloadlens.utils.math import counter_shares


SCHEMA_TYPES = {
    "sales": {
        "id": "INT",
        "amount": "DECIMAL(12,2)",
        "region": "TEXT",
        "flag": "BOOLEAN",
        "qty": "INT",
    },
    "dim": {
        "id": "INT",
        "code": "TEXT",
    },
}


@dataclass(frozen=True)
class TypeSpec:
    decimal_filters: int  # number of s.amount predicates
    boolean_filter: bool  # include s.flag in WHERE
    join_dim: bool        # join dim table on id
    having_clause: bool   # add HAVING SUM(amount) > 200
    include_count: bool   # include COUNT(*) measure
    order_by_amount: bool # order by SUM(amount)


def _build_query(spec: TypeSpec) -> str:
    select_parts = ["s.region"]
    group_parts = ["s.region"]

    if spec.join_dim:
        select_parts.append("d.code")
        group_parts.append("d.code")

    select_parts.append("SUM(s.amount) AS total_amount")
    if spec.include_count:
        select_parts.append("COUNT(*) AS order_count")

    query = [f"SELECT {', '.join(select_parts)}"]
    query.append("FROM sales s")
    if spec.join_dim:
        query.append("JOIN dim d ON s.id = d.id")

    where_clauses = []
    for i in range(spec.decimal_filters):
        where_clauses.append(f"s.amount > {50 + i * 10}")
    if spec.boolean_filter:
        where_clauses.append("s.flag")
    if where_clauses:
        query.append("WHERE " + " AND ".join(where_clauses))

    query.append("GROUP BY " + ", ".join(group_parts))

    if spec.having_clause:
        query.append("HAVING SUM(s.amount) > 200")

    if spec.order_by_amount:
        query.append("ORDER BY SUM(s.amount)")

    query.append("LIMIT 5;")
    return "\n".join(query)


def _generate_specs() -> list[TypeSpec]:
    specs = []
    for decimals, bool_flag, join_dim, having, include_count, order in product(
        [0, 1, 2],
        [False, True],
        [False, True],
        [False, True],
        [False, True],
        [False, True],
    ):
        specs.append(
            TypeSpec(
                decimal_filters=decimals,
                boolean_filter=bool_flag,
                join_dim=join_dim,
                having_clause=having,
                include_count=include_count,
                order_by_amount=order,
            )
        )
    return specs


@pytest.mark.parametrize("spec", _generate_specs(), ids=str)
def test_ast_type_histogram_shares(spec: TypeSpec) -> None:
    sql = _build_query(spec)
    ast = parse_one(sql)
    hist = summarize_ast_operator_types(ast, SCHEMA_TYPES)

    for op_name, counter in hist.items():
        total = sum(counter.values())
        if total == 0:
            continue
        shares = counter_shares(counter)
        assert math.isclose(sum(shares.values()), 1.0, rel_tol=1e-9)
        for dtype, count in counter.items():
            expected_share = count / total
            assert math.isclose(shares.get(dtype, 0.0), expected_share, rel_tol=1e-9)

    # Sanity: ensure filters and joins produce expected type coverage
    if spec.decimal_filters or spec.boolean_filter or spec.having_clause:
        filt_counter = hist.get("filter")
        if filt_counter:
            assert sum(filt_counter.values()) >= (spec.decimal_filters + spec.boolean_filter + (1 if spec.having_clause else 0))

    if spec.join_dim:
        join_counter = hist.get("join")
        assert join_counter is not None and join_counter.get("INT", 0) >= 2
