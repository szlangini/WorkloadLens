from __future__ import annotations

from collections import Counter
from typing import Dict, Iterable, Tuple

from sqlglot import exp


def _get_exp_classes(names: Iterable[str]) -> Tuple[type, ...]:
    classes = []
    for name in names:
        cls = getattr(exp, name, None)
        if cls is not None:
            classes.append(cls)
    return tuple(classes)


_SET_LIKE_CLASSES = _get_exp_classes(["Union", "UnionAll", "Intersect", "IntersectAll", "Except", "ExceptAll"])


def _classify_join(j: exp.Join) -> str:
    """
    Map a sqlglot Join expression into a JOIN_TYPE_* string that mirrors what we
    emit for Substrait plans.
    """
    kind = (j.args.get("kind") or "").upper()
    side = (j.args.get("side") or "").upper()
    method = (j.args.get("method") or "").upper()

    # Semi / anti get priority if the engine encoded them this way.
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

    # When the parser sees `JOIN` without a qualifier or a comma join, `kind`
    # and `side` are empty; treat both as inner joins.
    return "JOIN_TYPE_INNER"


def count_projection_expressions(expr: exp.Expression) -> int:
    """
    Count the number of projection expressions (SELECT list entries) present across
    the query, treating a bare SELECT * as a single expression.
    """
    total = 0
    for select_node in expr.find_all(exp.Select):
        expressions = list(select_node.expressions or [])
        total += max(len(expressions), 1)
    return total


def count_filter_predicates(expr: exp.Expression) -> int:
    """
    Count the number of predicate expressions that appear in WHERE or HAVING clauses.
    Each boolean predicate (e.g., column comparisons, BETWEEN, IN) contributes one
    to the total, regardless of how they are combined with AND/OR.
    """
    predicate_cls = getattr(exp, "Predicate", None)
    if predicate_cls is None:
        return 0

    total = 0
    select_like = tuple(
        cls
        for cls in (
            getattr(exp, "Select", None),
            getattr(exp, "Subquery", None),
            getattr(exp, "Query", None),
        )
        if cls is not None
    )

    def _count(node: exp.Expression) -> int:
        if node is None:
            return 0
        if predicate_cls is not None and isinstance(node, predicate_cls):
            base = 1
        else:
            base = 0

        if select_like and isinstance(node, select_like):
            return base

        if isinstance(node, exp.Expression):
            for child in node.iter_expressions():
                base += _count(child)
        elif isinstance(node, list):
            for item in node:
                if isinstance(item, exp.Expression):
                    base += _count(item)
        return base

    clause_classes = [getattr(exp, "Where", None), getattr(exp, "Having", None)]
    for clause_cls in clause_classes:
        if clause_cls is None:
            continue
        for clause in expr.find_all(clause_cls):
            condition = getattr(clause, "this", None)
            if condition is None:
                continue
            total += _count(condition)
    return total


def summarize_ast_operators(expr: exp.Expression) -> Tuple[Dict[str, int], Dict[str, int]]:
    """
    Cheap operator summary directly from the SQL AST, used when we cannot obtain
    a typed plan. We keep the shape close to summarize_substrait_json.
    """
    op = Counter()
    joins = Counter()

    table_nodes = list(expr.find_all(exp.Table))
    if table_nodes:
        op["scan"] = len(table_nodes)

    join_nodes = list(expr.find_all(exp.Join))
    if join_nodes:
        op["join"] = len(join_nodes)
        for join in join_nodes:
            joins[_classify_join(join)] += 1

    filter_nodes = list(expr.find_all(exp.Where)) + list(expr.find_all(exp.Having))
    if filter_nodes:
        op["filter"] = len(filter_nodes)

    group_nodes = list(expr.find_all(exp.Group))
    if group_nodes:
        op["aggregate"] = len(group_nodes)

    selects_with_agg = 0
    project_total = 0
    window_total = 0
    top_k_total = 0
    for select_node in expr.find_all(exp.Select):
        exprs = list(select_node.expressions or [])
        project_total += max(len(exprs), 1)
        has_group = bool(next(select_node.find_all(exp.Group), None))
        has_agg = bool(next(select_node.find_all(exp.AggFunc), None))
        if has_group or has_agg:
            selects_with_agg += 1
        window_total += sum(1 for _ in select_node.find_all(exp.Window))
        if select_node.args.get("order") and select_node.args.get("limit"):
            top_k_total += 1

    semi_joins = 0
    anti_joins = 0
    for exists_node in expr.find_all(exp.Exists):
        if isinstance(exists_node.parent, exp.Not):
            anti_joins += 1
        else:
            semi_joins += 1
    for in_node in expr.find_all(exp.In):
        query = in_node.args.get("query")
        if not isinstance(query, exp.Subquery):
            continue
        if isinstance(in_node.parent, exp.Not):
            anti_joins += 1
        else:
            semi_joins += 1
    if semi_joins:
        joins["JOIN_TYPE_SEMI"] += semi_joins
    if anti_joins:
        joins["JOIN_TYPE_ANTI"] += anti_joins

    if project_total:
        op["project"] = project_total
    if selects_with_agg:
        op["aggregate"] = max(op.get("aggregate", 0), selects_with_agg)
    if window_total:
        op["window"] = window_total
    if top_k_total:
        op["top_k"] = top_k_total

    union_all_count = 0
    for union_node in expr.find_all(exp.Union):
        distinct = union_node.args.get("distinct")
        if not distinct:
            union_all_count += 1
    if union_all_count:
        op["union_all"] = union_all_count

    order_nodes = list(expr.find_all(exp.Order))
    if order_nodes:
        op["sort"] = len(order_nodes)

    limit_nodes = list(expr.find_all(exp.Limit))
    fetch_cls = getattr(exp, "Fetch", None)
    if fetch_cls is not None:
        limit_nodes.extend(list(expr.find_all(fetch_cls)))
    if limit_nodes:
        op["limit"] = len(limit_nodes)

    if _SET_LIKE_CLASSES:
        seen_set_nodes = set()
        set_count = 0
        for cls in _SET_LIKE_CLASSES:
            for node in expr.find_all(cls):
                node_id = id(node)
                if node_id not in seen_set_nodes:
                    seen_set_nodes.add(node_id)
                    set_count += 1
        if set_count:
            op["set"] = set_count

    cte_total = 0
    cte_cls = getattr(exp, "CTE", None)
    if cte_cls is not None:
        cte_total = len(list(expr.find_all(cte_cls)))
    else:
        with_cls = getattr(exp, "With", None)
        if with_cls is not None:
            for with_expr in expr.find_all(with_cls):
                exprs = getattr(with_expr, "expressions", None) or []
                cte_total += len(exprs)
    if cte_total:
        op["cte"] = cte_total

    return dict(op), dict(joins)
