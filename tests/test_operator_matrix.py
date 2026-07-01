from __future__ import annotations

from dataclasses import dataclass
from collections import Counter
from itertools import product

import pytest
from sqlglot import parse_one, exp

from workloadlens.coverage.operators import summarize_ast_operators
from workloadlens.core.normalize import extract_functions


@dataclass(frozen=True)
class QuerySpec:
    cte_count: int
    join_types: tuple[str, ...]
    group_by: bool
    order_by: bool
    limit_rows: bool
    window: bool
    arithmetic_ops: tuple[str, ...]
    include_abs: bool
    use_union: bool


JOIN_LABELS = {
    "inner": "JOIN_TYPE_INNER",
    "left": "JOIN_TYPE_LEFT",
    "right": "JOIN_TYPE_RIGHT",
    "full": "JOIN_TYPE_FULL",
    "cross": "JOIN_TYPE_CROSS",
}


def _render_join(join_key: str, join_index: int) -> str:
    if join_key == "inner":
        alias = f"dd{join_index}"
        return f"JOIN date_dim {alias} ON f.ss_sold_date_sk = {alias}.d_date_sk"
    if join_key == "left":
        alias = f"s{join_index}"
        return f"LEFT JOIN store {alias} ON f.ss_store_sk = {alias}.s_store_sk"
    if join_key == "right":
        alias = f"ca{join_index}"
        return f"RIGHT JOIN customer_address {alias} ON {alias}.ca_address_sk = f.ss_addr_sk"
    if join_key == "full":
        alias = f"p{join_index}"
        return f"FULL OUTER JOIN promotion {alias} ON {alias}.p_promo_sk = f.ss_promo_sk"
    if join_key == "cross":
        alias = f"c{join_index}"
        return f"CROSS JOIN customer {alias}"
    raise AssertionError(f"Unknown join key: {join_key}")


ARITH_EXPR = {
    "ADD": "SUM(f.ss_sales_price) + 5 AS sum_plus",
    "SUB": "SUM(f.ss_sales_price) - 10 AS sum_minus",
    "DIVIDE": "SUM(f.ss_sales_price) / 2 AS sum_div",
    "MULTIPLY": "SUM(f.ss_sales_price) * 2 AS sum_mul",
}


def _build_select(spec: QuerySpec, case_index: int, branch_index: int) -> str:
    select_parts: list[str] = []

    if spec.group_by:
        select_parts.append("f.ss_item_sk")

    select_parts.extend(
        [
            "SUM(f.ss_sales_price) AS sum_sales",
            "AVG(f.ss_sales_price) AS avg_sales",
        ]
    )

    for op in spec.arithmetic_ops:
        select_parts.append(ARITH_EXPR[op])

    if spec.include_abs:
        select_parts.append("ABS(AVG(f.ss_sales_price)) AS abs_avg_sales")

    if spec.window:
        select_parts.append(
            "RANK() OVER (PARTITION BY f.ss_store_sk ORDER BY f.ss_sales_price) AS sales_rank"
        )

    joins = [
        _render_join(join_key, case_index * 10 + idx)
        for idx, join_key in enumerate(spec.join_types)
    ]

    where_clause = f"WHERE f.ss_sales_price > {case_index + branch_index + 1}"

    body_parts = [
        f"SELECT {', '.join(select_parts)}",
        "FROM store_sales AS f",
    ]
    body_parts.extend(joins)
    body_parts.append(where_clause)

    if spec.group_by:
        body_parts.append("GROUP BY f.ss_item_sk")
        body_parts.append(f"HAVING SUM(f.ss_sales_price) > {case_index + branch_index + 5}")

    if spec.order_by and not spec.use_union:
        body_parts.append("ORDER BY sum_sales DESC")

    if spec.limit_rows and not spec.use_union:
        body_parts.append(f"LIMIT {10 + (case_index % 7)}")

    return "\n".join(body_parts)


def _build_sql(spec: QuerySpec, case_index: int) -> str:
    ctes = []
    for i in range(spec.cte_count):
        alias = f"cte_{case_index}_{i}"
        ctes.append(
            f"{alias} AS (SELECT ss_item_sk FROM store_sales WHERE ss_item_sk > {i})"
        )

    with_clause = ""
    if ctes:
        with_clause = "WITH " + ",\n     ".join(ctes)

    first_select = _build_select(spec, case_index, branch_index=0)
    if spec.use_union:
        second_select = _build_select(spec, case_index, branch_index=1)
        body = f"{first_select}\nUNION ALL\n{second_select}"
    else:
        body = first_select

    query_parts = [with_clause, body]
    return "\n".join(part for part in query_parts if part) + "\n"


def _classify_join_from_ast(join: exp.Join) -> str:
    kind = (join.args.get("kind") or "").upper()
    side = (join.args.get("side") or "").upper()
    method = (join.args.get("method") or "").upper()

    if method == "SEMI":
        return "JOIN_TYPE_SEMI"
    if method == "ANTI":
        return "JOIN_TYPE_ANTI"
    if side in {"LEFT", "RIGHT"}:
        return f"JOIN_TYPE_{side}"
    if side == "FULL":
        return "JOIN_TYPE_FULL"
    if kind == "CROSS":
        return "JOIN_TYPE_CROSS"
    if kind == "LEFT":
        return "JOIN_TYPE_LEFT"
    if kind == "RIGHT":
        return "JOIN_TYPE_RIGHT"
    if kind == "FULL":
        return "JOIN_TYPE_FULL"
    return "JOIN_TYPE_INNER"


def _expected_ops_from_ast(ast: exp.Expression) -> dict[str, int]:
    ops: dict[str, int] = {}

    table_nodes = list(ast.find_all(exp.Table))
    if table_nodes:
        ops["scan"] = len(table_nodes)

    join_nodes = list(ast.find_all(exp.Join))
    if join_nodes:
        ops["join"] = len(join_nodes)

    where_nodes = list(ast.find_all(exp.Where))
    having_nodes = list(ast.find_all(exp.Having))
    if where_nodes or having_nodes:
        ops["filter"] = len(where_nodes) + len(having_nodes)

    group_nodes = list(ast.find_all(exp.Group))
    if group_nodes:
        ops["aggregate"] = len(group_nodes)

    selects_with_agg = 0
    project_total = 0
    window_total = 0
    top_k_total = 0
    for select_node in ast.find_all(exp.Select):
        exprs = list(select_node.expressions or [])
        project_total += max(len(exprs), 1)
        has_group = bool(next(select_node.find_all(exp.Group), None))
        has_agg = bool(next(select_node.find_all(exp.AggFunc), None))
        if has_group or has_agg:
            selects_with_agg += 1
        window_total += sum(1 for _ in select_node.find_all(exp.Window))
        if select_node.args.get("order") and select_node.args.get("limit"):
            top_k_total += 1

    if project_total:
        ops["project"] = project_total
    if selects_with_agg:
        ops["aggregate"] = max(ops.get("aggregate", 0), selects_with_agg)
    if window_total:
        ops["window"] = window_total
    if top_k_total:
        ops["top_k"] = top_k_total

    union_all_count = 0
    for union_node in ast.find_all(exp.Union):
        distinct = union_node.args.get("distinct")
        if not distinct:
            union_all_count += 1
    if union_all_count:
        ops["union_all"] = union_all_count

    order_nodes = list(ast.find_all(exp.Order))
    if order_nodes:
        ops["sort"] = len(order_nodes)

    limit_nodes = list(ast.find_all(exp.Limit))
    fetch_cls = getattr(exp, "Fetch", None)
    if fetch_cls is not None:
        limit_nodes.extend(list(ast.find_all(fetch_cls)))
    if limit_nodes:
        ops["limit"] = len(limit_nodes)

    set_nodes_ids = set()
    for name in ("Union", "UnionAll", "Intersect", "IntersectAll", "Except", "ExceptAll"):
        cls = getattr(exp, name, None)
        if cls is None:
            continue
        for node in ast.find_all(cls):
            node_id = id(node)
            if node_id not in set_nodes_ids:
                set_nodes_ids.add(node_id)
    if set_nodes_ids:
        ops["set"] = len(set_nodes_ids)

    cte_cls = getattr(exp, "CTE", None)
    if cte_cls is not None:
        cte_total = len(list(ast.find_all(cte_cls)))
        if cte_total:
            ops["cte"] = cte_total

    return ops


def _expected_join_counts_from_ast(ast: exp.Expression) -> Counter:
    counts: Counter[str] = Counter()
    for join in ast.find_all(exp.Join):
        counts[_classify_join_from_ast(join)] += 1
    return counts


def _generate_specs() -> list[tuple[str, QuerySpec, str]]:
    join_patterns = [
        (),
        ("inner",),
        ("left",),
        ("right",),
        ("full",),
        ("cross",),
        ("inner", "left"),
        ("left", "full"),
        ("inner", "cross"),
    ]
    cte_options = [0, 1, 2]
    group_opts = [False, True]
    window_opts = [False, True]
    order_opts = [False, True]
    limit_opts = [False, True]
    arith_opts = [
        (),
        ("SUB",),
        ("ADD", "SUB"),
        ("DIVIDE",),
        ("MULTIPLY", "ADD"),
    ]
    abs_opts = [False, True]
    union_opts = [False, True]

    cases: list[tuple[str, QuerySpec, str]] = []

    manual_specs = [
        QuerySpec(0, ("inner",), True, True, True, True, ("SUB",), True, False),
        QuerySpec(1, ("left",), True, False, False, False, ("ADD",), False, False),
        QuerySpec(2, ("right", "full"), True, False, False, True, ("SUB", "DIVIDE"), True, False),
        QuerySpec(0, ("cross",), False, False, False, True, (), False, False),
        QuerySpec(1, (), False, False, False, False, (), True, True),
    ]
    for idx, spec in enumerate(manual_specs):
        sql = _build_sql(spec, idx)
        cases.append((f"manual_{idx}", spec, sql))

    target_total = 180
    combo_iter = product(
        cte_options,
        join_patterns,
        group_opts,
        window_opts,
        order_opts,
        limit_opts,
        arith_opts,
        abs_opts,
        union_opts,
    )

    for combo in combo_iter:
        spec = QuerySpec(
            cte_count=combo[0],
            join_types=tuple(combo[1]),
            group_by=combo[2],
            order_by=combo[3],
            limit_rows=combo[4],
            window=combo[5],
            arithmetic_ops=tuple(combo[6]),
            include_abs=combo[7],
            use_union=combo[8],
        )
        if spec.use_union and (spec.order_by or spec.limit_rows):
            continue

        score = (
            spec.cte_count * 31
            + len(spec.join_types) * 17
            + int(spec.group_by) * 13
            + int(spec.window) * 11
            + int(spec.order_by) * 7
            + int(spec.limit_rows) * 5
            + len(spec.arithmetic_ops) * 3
            + int(spec.include_abs) * 2
            + int(spec.use_union)
        )
        if score % 5 not in (0, 1, 2):
            continue

        case_index = len(cases)
        sql = _build_sql(spec, case_index)
        cases.append((f"auto_{case_index}", spec, sql))

        if len(cases) >= target_total:
            break

    return cases


# Pre-generate cases so ids remain stable
_CASE_MATRIX = _generate_specs()


@pytest.mark.parametrize(
    "case_name,spec,sql",
    _CASE_MATRIX,
    ids=[case[0] for case in _CASE_MATRIX],
)
def test_operator_matrix(case_name: str, spec: QuerySpec, sql: str):
    ast = parse_one(sql)
    op_counts, join_counts = summarize_ast_operators(ast)

    expected_ops = _expected_ops_from_ast(ast)
    expected_join_counts = _expected_join_counts_from_ast(ast)

    assert len(op_counts) == len(expected_ops), f"{case_name}: unexpected operator keys"
    assert op_counts == expected_ops, f"{case_name}: operator counts mismatch"
    assert join_counts == expected_join_counts, f"{case_name}: join type counts mismatch"

    branch_multiplier = 2 if spec.use_union else 1

    expected_filters = spec.cte_count + branch_multiplier
    if spec.group_by:
        expected_filters += branch_multiplier
    assert op_counts.get("filter", 0) == expected_filters, f"{case_name}: filter count"

    expected_aggregates = branch_multiplier
    assert op_counts.get("aggregate", 0) == expected_aggregates, f"{case_name}: aggregate count"

    expected_sort = branch_multiplier * int(spec.window) + int(spec.order_by and not spec.use_union)
    assert op_counts.get("sort", 0) == expected_sort, f"{case_name}: sort count"

    expected_limit = int(spec.limit_rows and not spec.use_union)
    assert op_counts.get("limit", 0) == expected_limit, f"{case_name}: limit count"

    expected_cte = spec.cte_count
    assert op_counts.get("cte", 0) == expected_cte, f"{case_name}: cte count"

    expected_join_from_spec = Counter()
    for join_key in spec.join_types:
        expected_join_from_spec[JOIN_LABELS[join_key]] += branch_multiplier
    assert join_counts == expected_join_from_spec, f"{case_name}: join types vs spec"

    expected_scan = spec.cte_count + branch_multiplier * (1 + len(spec.join_types))
    assert op_counts.get("scan", 0) == expected_scan, f"{case_name}: scan count"

    expected_set = int(spec.use_union)
    assert op_counts.get("set", 0) == expected_set, f"{case_name}: set count"

    funcs = extract_functions(ast)
    agg_names = {name.upper() for name in funcs["agg_funcs"]}
    assert "SUM" in agg_names, f"{case_name}: sum missing in aggregates"

    window_names = {name.upper() for name in funcs["window_funcs"]}
    if spec.window:
        assert "RANK" in window_names, f"{case_name}: rank missing in window funcs"
    else:
        assert "RANK" not in window_names, f"{case_name}: unexpected rank window func"

    expr_ops = {name.upper() for name in funcs["expression_ops"]}
    for op in spec.arithmetic_ops:
        assert op in expr_ops, f"{case_name}: {op} missing in expression ops"
    if not spec.arithmetic_ops and not spec.include_abs:
        assert expr_ops == set(), f"{case_name}: unexpected expression ops"

    all_funcs = {name.upper() for name in funcs["all_funcs"]}
    if spec.include_abs:
        assert "ABS" in all_funcs, f"{case_name}: ABS missing in all funcs"


def test_case_matrix_size() -> None:
    assert 100 <= len(_CASE_MATRIX) <= 300, "Expected diversified matrix size between 100 and 300"
