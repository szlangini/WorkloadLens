from __future__ import annotations

from typing import Iterable, Tuple

from sqlglot import exp


_TRANSPARENT_NODES = tuple(
    cls
    for cls in (
        getattr(exp, "Alias", None),
        getattr(exp, "Tuple", None),
        getattr(exp, "Array", None),
        getattr(exp, "Paren", None),
        getattr(exp, "Ordered", None),
        getattr(exp, "Column", None),
    )
    if cls is not None
)


def _child_expressions(node: exp.Expression) -> Iterable[exp.Expression]:
    if isinstance(node, exp.Alias):
        child = node.this
        if isinstance(child, exp.Expression):
            return [child]
        return []
    if isinstance(node, (getattr(exp, "Tuple", tuple()), getattr(exp, "Array", tuple()))):
        expressions = getattr(node, "expressions", None) or []
        return [child for child in expressions if isinstance(child, exp.Expression)]
    if isinstance(node, getattr(exp, "Ordered", tuple())):
        child = node.this
        return [child] if isinstance(child, exp.Expression) else []
    # Default traversal
    return [child for child in node.iter_expressions() if isinstance(child, exp.Expression)]


def expression_height(node: exp.Expression | None) -> int:
    """
    Return the height of a scalar expression tree.

    Leaves (columns, literals) have height 1. Certain wrapper nodes such as alias,
    tuple, and parentheses are considered transparent and do not add depth.
    """
    if node is None or not isinstance(node, exp.Expression):
        return 0

    children = list(_child_expressions(node))
    if not children:
        return 1

    child_heights = [expression_height(child) for child in children]
    if isinstance(node, _TRANSPARENT_NODES):
        return max(child_heights, default=1)
    return 1 + max(child_heights)


def compute_expression_depths(ast: exp.Expression) -> Tuple[int, int]:
    """
    Return the maximum scalar expression depth and maximum WHERE/HAVING predicate depth
    encountered within the query tree rooted at ``ast``.
    """
    max_scalar = 0
    max_filter = 0

    def _register(expr: exp.Expression | None, is_filter: bool = False) -> None:
        nonlocal max_scalar, max_filter
        if not isinstance(expr, exp.Expression):
            return
        depth = expression_height(expr)
        if depth > max_scalar:
            max_scalar = depth
        if is_filter and depth > max_filter:
            max_filter = depth

    for select in ast.find_all(exp.Select):
        # Projection list
        for projection in select.expressions or []:
            _register(projection)

        # GROUP BY expressions
        group = select.args.get("group")
        if isinstance(group, exp.Group):
            for grouping in group.expressions or []:
                _register(grouping)

        # ORDER BY expressions
        order = select.args.get("order")
        if isinstance(order, exp.Order):
            for ordered in order.expressions or []:
                if isinstance(ordered, exp.Ordered):
                    _register(ordered.this)
                else:
                    _register(ordered)

        # QUALIFY expressions
        qualify = select.args.get("qualify")
        if isinstance(qualify, exp.Qualify):
            _register(qualify.this, is_filter=True)

        # LIMIT/OFFSET expressions
        limit = select.args.get("limit")
        if isinstance(limit, exp.Limit):
            _register(limit.expression)
            _register(limit.args.get("offset"))
        else:
            fetch_cls = getattr(exp, "Fetch", None)
            if fetch_cls is not None and isinstance(limit, fetch_cls):
                _register(limit.args.get("count"))
        offset = select.args.get("offset")
        if offset is not None:
            _register(offset)

        # Window partition/order expressions (handled via projection/window nodes already)

        # WHERE clause
        where = select.args.get("where")
        if isinstance(where, exp.Where):
            _register(where.this, is_filter=True)

        # HAVING clause
        having = select.args.get("having")
        if isinstance(having, exp.Having):
            _register(having.this, is_filter=True)

    return max_scalar, max_filter


__all__ = [
    "expression_height",
    "compute_expression_depths",
]
