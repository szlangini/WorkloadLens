from __future__ import annotations

from collections import Counter, defaultdict
import re
from typing import Dict, Iterable, List, Optional, Set, Tuple

from sqlglot import exp

from ..utils.expr_depth import expression_height
from ..utils.types import normalize_type_label

_SET_OPERATION_CLASSES = tuple(
    cls
    for cls in (
        getattr(exp, "Union", None),
        getattr(exp, "UnionAll", None),
        getattr(exp, "Except", None),
        getattr(exp, "ExceptAll", None),
        getattr(exp, "Intersect", None),
        getattr(exp, "IntersectAll", None),
    )
    if cls is not None
)

# Aggregate expression classes we care about when inferring DISTINCT semantics.
_AGGREGATE_CLASSES = tuple(
    cls
    for cls in (
        getattr(exp, "Avg", None),
        getattr(exp, "Count", None),
        getattr(exp, "Min", None),
        getattr(exp, "Max", None),
        getattr(exp, "Sum", None),
    )
    if cls is not None
)

_PRED_TRANSPARENT_NODES = tuple(
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


def summarize_ast_operator_types(
    expr: exp.Expression,
    schema_types: Dict[str, Dict[str, str]],
) -> Dict[str, Counter]:
    """
    Produce a best-effort type histogram per logical operator using only the AST
    and the provided schema metadata (table -> column -> type).
    """
    if not schema_types:
        return {}

    schema_lookup: Dict[str, Dict[str, str]] = {table: dict(cols) for table, cols in schema_types.items()}
    _augment_schema_with_cte_types(expr, schema_lookup)

    hist: Dict[str, Counter] = defaultdict(Counter)

    for select_node in _iter_select_like(expr):
        alias_map = _collect_aliases(select_node)
        _register_alias_schemas(alias_map, schema_lookup)
        derived_columns = _collect_derived_column_types(select_node, schema_lookup)
        table_names = set(alias_map.values())
        for table_name in table_names:
            for dtype in schema_lookup.get(table_name, {}).values():
                normalized = normalize_type_label(dtype)
                if normalized:
                    hist["scan"][normalized] += 1

        # WHERE clauses
        for where in select_node.find_all(exp.Where):
            join_column_ids = _join_condition_column_ids(
                where.this,
                alias_map,
                schema_lookup,
                fallback_table=select_node,
                derived_columns=derived_columns,
            )
            hist["filter"].update(
                _collect_column_types(
                    where.this,
                    alias_map,
                    schema_lookup,
                    fallback_table=select_node,
                    derived_columns=derived_columns,
                    excluded_columns=join_column_ids,
                )
            )
            hist["join"].update(
                _collect_join_types_from_where(
                    where.this,
                    alias_map,
                    schema_lookup,
                    fallback_table=select_node,
                    derived_columns=derived_columns,
                )
            )

        # HAVING clauses
        for having in select_node.find_all(exp.Having):
            hist["filter"].update(
                _collect_column_types(
                    having.this,
                    alias_map,
                    schema_lookup,
                    fallback_table=select_node,
                    derived_columns=derived_columns,
                )
            )

        # JOIN predicates
        for join in select_node.find_all(exp.Join):
            condition = join.args.get("on")
            if condition is not None:
                hist["join"].update(
                    _collect_column_types(
                        condition,
                        alias_map,
                        schema_lookup,
                        fallback_table=select_node,
                        derived_columns=derived_columns,
                    )
                )

        # GROUP BY
        group = select_node.args.get("group")
        if isinstance(group, exp.Group):
            for expr_item in group.expressions:
                hist["group_by"].update(
                    _collect_column_types(
                        expr_item,
                        alias_map,
                        schema_lookup,
                        fallback_table=select_node,
                        derived_columns=derived_columns,
                    )
                )

        # Aggregation functions
        for agg in select_node.find_all(exp.AggFunc):
            if isinstance(agg, exp.Grouping):
                arg_types = []
                for arg in agg.expressions:
                    arg_types.extend(
                        _collect_column_types(
                            arg,
                            alias_map,
                            schema_lookup,
                            fallback_table=select_node,
                            derived_columns=derived_columns,
                        )
                    )
                if arg_types:
                    hist["group_by"].update(arg_types)
                hist["aggregate_result"]["INT"] += 1
                continue
            arg_types: List[str] = []
            args = list(agg.expressions)
            if getattr(agg, "this", None) is not None:
                args.append(agg.this)
            for arg in args:
                arg_types.extend(
                    _collect_column_types(
                        arg,
                        alias_map,
                        schema_lookup,
                        fallback_table=select_node,
                        derived_columns=derived_columns,
                    )
                )
            if arg_types:
                hist["aggregate"].update(arg_types)
            result_type = _infer_aggregate_result_type(agg, arg_types)
            if result_type:
                hist["aggregate_result"][result_type] += 1

        # Projection expressions
        for proj in select_node.expressions:
            hist["project"].update(
                _collect_column_types(
                    proj,
                    alias_map,
                    schema_lookup,
                    fallback_table=select_node,
                    derived_columns=derived_columns,
                )
            )

        # ORDER BY expressions
        order = select_node.args.get("order")
        if isinstance(order, exp.Order):
            for order_expr in order.expressions:
                hist["sort"].update(
                    _collect_column_types(
                        order_expr,
                        alias_map,
                        schema_lookup,
                        fallback_table=select_node,
                        derived_columns=derived_columns,
                    )
                )

    # Remove empty counters
    return {key: counter for key, counter in hist.items() if counter}


def collect_top_level_order_types(
    expr: exp.Expression,
    schema_types: Dict[str, Dict[str, str]],
) -> Counter:
    """
    Return a histogram of logical types referenced in the outermost ORDER BY clause.
    """
    if not schema_types:
        return Counter()
    select_node = expr if isinstance(expr, exp.Select) else next(expr.find_all(exp.Select), None)
    if not isinstance(select_node, exp.Select):
        return Counter()
    order = select_node.args.get("order")
    if not isinstance(order, exp.Order):
        return Counter()

    alias_map = _collect_aliases(select_node)
    _register_alias_schemas(alias_map, schema_types)
    derived_columns = _collect_derived_column_types(select_node, schema_types)
    counter: Counter = Counter()
    for order_expr in order.expressions:
        types = _collect_column_types(
            order_expr,
            alias_map,
            schema_types,
            fallback_table=select_node,
            derived_columns=derived_columns,
        )
        for dtype in types:
            counter[dtype] += 1
    return counter


def _limit_literal_value(limit_expr: exp.Expression | None) -> Optional[int]:
    if limit_expr is None:
        return None
    if isinstance(limit_expr, exp.Limit):
        literal = getattr(limit_expr, "expression", None)
        if isinstance(literal, exp.Literal) and not literal.is_string:
            try:
                return int(literal.this)
            except (TypeError, ValueError):
                return None
        return None
    fetch_cls = getattr(exp, "Fetch", None)
    if fetch_cls is not None and isinstance(limit_expr, fetch_cls):
        count = limit_expr.args.get("count")
        if isinstance(count, exp.Literal) and not count.is_string:
            try:
                return int(count.this)
            except (TypeError, ValueError):
                return None
    return None


_TOP_LIMIT_RE = re.compile(
    r"(?is)\bSELECT\s+TOP\s*(?:\(\s*)?(\d+)\s*(?:\)\s*)?(?:PERCENT\b|WITH\s+TIES\b)?"
)


def _top_limit_value(raw_sql: Optional[str]) -> Optional[int]:
    if not raw_sql:
        return None
    match = _TOP_LIMIT_RE.search(raw_sql)
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def _is_rownum_expr(expr: exp.Expression | None) -> bool:
    if expr is None:
        return False
    name: Optional[str] = None
    if isinstance(expr, exp.Column):
        name = expr.name
    elif isinstance(expr, exp.Identifier):
        name = str(expr.this)
    return bool(name and name.upper() == "ROWNUM")


def _rownum_limit_value(select_node: exp.Select) -> Tuple[Optional[int], bool]:
    where = select_node.args.get("where")
    if not isinstance(where, exp.Where):
        return None, False

    lt_cls = getattr(exp, "LT", None)
    lte_cls = getattr(exp, "LTE", None)
    eq_cls = getattr(exp, "EQ", None)
    gt_cls = getattr(exp, "GT", None)
    gte_cls = getattr(exp, "GTE", None)
    comparison_types: Tuple[type, ...] = tuple(
        cls for cls in (lt_cls, lte_cls, eq_cls, gt_cls, gte_cls) if cls is not None
    )
    if not comparison_types:
        return None, False

    for node in where.find_all(comparison_types):
        left = node.args.get("this")
        right = node.args.get("expression")
        node_type = type(node)
        if _is_rownum_expr(left):
            if node_type in (lt_cls, lte_cls, eq_cls) and isinstance(right, exp.Literal) and not right.is_string:
                try:
                    return int(right.this), True
                except (TypeError, ValueError):
                    return None, True
            if node_type in (lt_cls, lte_cls, eq_cls):
                return None, True
        if _is_rownum_expr(right):
            if node_type in (gt_cls, gte_cls, eq_cls) and isinstance(left, exp.Literal) and not left.is_string:
                try:
                    return int(left.this), True
                except (TypeError, ValueError):
                    return None, True
            if node_type in (gt_cls, gte_cls, eq_cls):
                return None, True
    return None, False


def detect_limit_clause(select_node: exp.Select, raw_sql: Optional[str] = None) -> Tuple[bool, Optional[int], Optional[str]]:
    """
    Detect LIMIT-like clauses and return (has_limit, value, syntax).
    Syntax is one of: limit, fetch, top, rownum.
    """
    top_value = _top_limit_value(raw_sql)
    if top_value is not None:
        return True, top_value, "top"

    limit_node = select_node.args.get("limit")
    fetch_cls = getattr(exp, "Fetch", None)
    if isinstance(limit_node, exp.Limit):
        return True, _limit_literal_value(limit_node), "limit"
    if fetch_cls is not None and isinstance(limit_node, fetch_cls):
        return True, _limit_literal_value(limit_node), "fetch"

    rownum_value, rownum_present = _rownum_limit_value(select_node)
    if rownum_present:
        return True, rownum_value, "rownum"

    return False, None, None


def collect_limit_metrics(expr: exp.Expression) -> Tuple[List[int], List[int]]:
    """
    Return two lists capturing LIMIT distribution for all SELECT statements in the query.

    The first list contains LIMIT values (integers >=0) for SELECT statements that also
    have an ORDER BY clause. SELECT statements with ORDER BY but without a LIMIT are
    represented as 0.

    The second list contains LIMIT values for SELECT statements that have a LIMIT but
    do not include ORDER BY.
    """
    order_limits: List[int] = []
    non_order_limits: List[int] = []

    for select_node in _iter_select_like(expr):
        order = select_node.args.get("order")
        has_order = isinstance(order, exp.Order)
        has_limit, value, _syntax = detect_limit_clause(select_node)

        if has_order:
            if has_limit and value is not None and value >= 0:
                order_limits.append(value)
            else:
                order_limits.append(0)
        elif has_limit and value is not None and value >= 0:
            non_order_limits.append(value)

    return order_limits, non_order_limits


def collect_union_all_fan_in(expr: exp.Expression) -> List[int]:
    """
    Return a list of UNION ALL fan-in sizes (number of input branches) for each
    UNION ALL chain in the query.
    """
    union_cls = getattr(exp, "Union", None)
    if union_cls is None:
        return []

    def _is_union_all(node: Optional[exp.Expression]) -> bool:
        return isinstance(node, union_cls) and not bool(node.args.get("distinct"))

    def _count_inputs(node: Optional[exp.Expression]) -> int:
        if node is None:
            return 0
        if _is_union_all(node):
            left = node.args.get("this")
            right = node.args.get("expression")
            return _count_inputs(left) + _count_inputs(right)
        return 1

    fan_in_values: List[int] = []
    for union_node in expr.find_all(union_cls):
        if not _is_union_all(union_node):
            continue
        parent = getattr(union_node, "parent", None)
        if _is_union_all(parent):
            continue
        fan_in = _count_inputs(union_node)
        if fan_in >= 2:
            fan_in_values.append(fan_in)
    return fan_in_values


def _iter_select_like(expr: exp.Expression) -> Iterable[exp.Select]:
    stack = [expr]
    seen = set()
    while stack:
        node = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, exp.Select):
            yield node
        for child in node.iter_expressions():
            stack.append(child)


def _column_infos(
    expression: Optional[exp.Expression],
    alias_map: Dict[str, str],
    schema_types: Dict[str, Dict[str, str]],
    *,
    fallback_table: Optional[exp.Select] = None,
    derived_columns: Optional[Dict[str, str]] = None,
) -> List[Tuple[exp.Column, str, Optional[str]]]:
    if expression is None:
        return []
    columns: List[exp.Column] = []
    if isinstance(expression, exp.Column):
        columns.append(expression)
    elif isinstance(expression, exp.Expression):
        columns.extend(list(expression.find_all(exp.Column)))
    infos: List[Tuple[exp.Column, str, Optional[str]]] = []
    seen: set[Tuple[str, Optional[str]]] = set()
    for column in columns:
        info = _resolve_column_info(
            column,
            alias_map,
            schema_types,
            fallback_table=fallback_table,
            derived_columns=derived_columns,
        )
        if not info:
            continue
        dtype, source = info
        key = (dtype, source)
        if key in seen:
            continue
        seen.add(key)
        infos.append((column, dtype, source))
    return infos


def _collect_aliases(select_node: exp.Select) -> Dict[str, str]:
    alias_map: Dict[str, str] = {}
    def _register_subquery_alias(node: Optional[exp.Expression]) -> None:
        if not isinstance(node, exp.Subquery):
            return
        alias = node.args.get("alias")
        alias_name: Optional[str] = None
        if isinstance(alias, exp.TableAlias):
            ident = alias.this
            if isinstance(ident, exp.Identifier):
                alias_name = ident.name.lower()
        elif isinstance(alias, str):
            alias_name = alias.lower()
        if alias_name:
            alias_map.setdefault(alias_name, alias_name)

    for table in select_node.find_all(exp.Table):
        base_name = _table_base_name(table)
        if not base_name:
            continue
        alias_map[base_name.lower()] = base_name.lower()
        table_name = table.alias_or_name
        if table_name:
            alias_map[table_name.lower()] = base_name.lower()
        alias = table.args.get("alias")
        if isinstance(alias, exp.TableAlias):
            alias_ident = alias.this
            if isinstance(alias_ident, exp.Identifier):
                alias_map[alias_ident.name.lower()] = base_name.lower()

    from_expr = select_node.args.get("from_") or select_node.args.get("from")
    if isinstance(from_expr, exp.From):
        primary = getattr(from_expr, "this", None)
        if isinstance(primary, exp.Expression):
            _register_subquery_alias(primary)
        for expr_item in getattr(from_expr, "expressions", []) or []:
            _register_subquery_alias(expr_item)

    for join in select_node.args.get("joins") or []:
        _register_subquery_alias(getattr(join, "this", None))

    return alias_map


def _table_base_name(table: exp.Table) -> Optional[str]:
    if isinstance(table.this, exp.Identifier):
        return table.this.name
    if isinstance(table.this, exp.Table):
        return table.this.alias_or_name
    return table.alias_or_name


def _augment_schema_with_cte_types(
    expr: exp.Expression,
    schema_lookup: Dict[str, Dict[str, str]],
    seen: Optional[Set[int]] = None,
) -> None:
    if seen is None:
        seen = set()
    if id(expr) in seen:
        return
    seen.add(id(expr))

    with_expr = expr.args.get("with_") or expr.args.get("with")
    if isinstance(with_expr, exp.With):
        for cte in with_expr.expressions:
            body = getattr(cte, "this", None)
            if isinstance(body, exp.Expression):
                _augment_schema_with_cte_types(body, schema_lookup, seen)
                alias = getattr(cte, "alias", None)
                column_types = _infer_query_output_types(body, schema_lookup)
                if alias and column_types:
                    key = alias.lower()
                    schema_lookup.setdefault(key, {})
                    schema_lookup[key].update(column_types)

    for child in expr.iter_expressions():
        if isinstance(child, exp.Expression):
            _augment_schema_with_cte_types(child, schema_lookup, seen)


def _register_alias_schemas(alias_map: Dict[str, str], schema_lookup: Dict[str, Dict[str, str]]) -> None:
    for alias, base in alias_map.items():
        if not alias:
            continue
        base_schema = schema_lookup.get(base)
        if not base_schema:
            continue
        schema_lookup.setdefault(alias, base_schema)


def _collect_derived_column_types(
    select_node: exp.Select,
    schema_lookup: Dict[str, Dict[str, str]],
) -> Dict[str, str]:
    derived: Dict[str, Optional[str]] = {}
    alias_map = _collect_aliases(select_node)

    def _record_columns(node: Optional[exp.Expression]) -> None:
        if not isinstance(node, exp.Subquery):
            return
        alias = node.args.get("alias")
        alias_name: Optional[str] = None
        if isinstance(alias, exp.TableAlias):
            ident = alias.this
            if isinstance(ident, exp.Identifier):
                alias_name = ident.name.lower()
        elif isinstance(alias, str):
            alias_name = alias.lower()

        column_types = _infer_query_output_types(node, schema_lookup)
        if alias_name and column_types:
            schema_lookup.setdefault(alias_name, {})
            schema_lookup[alias_name].update(column_types)

        for name, dtype in column_types.items():
            key = name.lower()
            if key in derived and derived[key] != dtype:
                derived[key] = None
            else:
                derived[key] = dtype

    from_expr = select_node.args.get("from_") or select_node.args.get("from")
    if isinstance(from_expr, exp.From):
        primary = getattr(from_expr, "this", None)
        if isinstance(primary, exp.Expression):
            _record_columns(primary)
        for expr_item in getattr(from_expr, "expressions", []) or []:
            _record_columns(expr_item)

    for join in select_node.args.get("joins") or []:
        node = getattr(join, "this", None)
        if isinstance(node, exp.Expression):
            _record_columns(node)

    column_types: Dict[str, str] = {}
    derived_columns = {name: dtype for name, dtype in derived.items() if dtype}

    for idx, proj in enumerate(select_node.expressions):
        output_name = proj.alias_or_name or f"_col{idx + 1}"
        types = _collect_column_types(
            proj,
            alias_map,
            schema_lookup,
            fallback_table=select_node,
            derived_columns=derived_columns,
        )
        dtype = _infer_expression_type(
            proj,
            alias_map,
            schema_lookup,
            fallback_table=select_node,
            derived_columns=derived_columns,
        )
        if not dtype and types:
            dtype = types[0]
        if dtype:
            column_types[output_name.lower()] = dtype

    column_types.update(derived_columns)
    return column_types


def _predicate_height(
    node: Optional[exp.Expression],
    alias_map: Dict[str, str],
    schema_lookup: Dict[str, Dict[str, str]],
    *,
    fallback_table: Optional[exp.Select] = None,
    derived_columns: Optional[Dict[str, str]] = None,
    join_column_ids: Optional[Set[int]] = None,
) -> int:
    if not isinstance(node, exp.Expression):
        return 0

    join_column_ids = join_column_ids or set()
    category, allow_traverse = _filter_expression_category(
        node,
        alias_map,
        schema_lookup,
        fallback_table=fallback_table,
        derived_columns=derived_columns,
        join_column_ids=join_column_ids,
    )

    if isinstance(node, (exp.And, exp.Or)):
        children = list(node.flatten())
    else:
        children = list(node.iter_expressions()) if allow_traverse else []

    child_heights = [
        _predicate_height(
            child,
            alias_map,
            schema_lookup,
            fallback_table=fallback_table,
            derived_columns=derived_columns,
            join_column_ids=join_column_ids,
        )
        for child in children
        if isinstance(child, exp.Expression)
    ]

    if category is None or isinstance(node, _PRED_TRANSPARENT_NODES):
        return max(child_heights, default=0)

    return 1 + max(child_heights, default=0)


def compute_filter_predicate_depth(
    expr: exp.Expression,
    schema_types: Dict[str, Dict[str, str]],
) -> int:
    if not schema_types:
        return 0

    schema_lookup: Dict[str, Dict[str, str]] = {table: dict(cols) for table, cols in schema_types.items()}
    _augment_schema_with_cte_types(expr, schema_lookup)

    max_depth = 0

    for select_node in _iter_select_like(expr):
        alias_map = _collect_aliases(select_node)
        _register_alias_schemas(alias_map, schema_lookup)
        derived_columns = _collect_derived_column_types(select_node, schema_lookup)

        where = select_node.args.get("where")
        if isinstance(where, exp.Where):
            join_column_ids = _join_condition_column_ids(
                where.this,
                alias_map,
                schema_lookup,
                fallback_table=select_node,
                derived_columns=derived_columns,
            )
            depth = _predicate_height(
                where.this,
                alias_map,
                schema_lookup,
                fallback_table=select_node,
                derived_columns=derived_columns,
                join_column_ids=join_column_ids,
            )
            if depth > max_depth:
                max_depth = depth

        for having in select_node.find_all(exp.Having):
            depth = _predicate_height(
                having.this,
                alias_map,
                schema_lookup,
                fallback_table=select_node,
                derived_columns=derived_columns,
                join_column_ids=None,
            )
            if depth > max_depth:
                max_depth = depth

    return max_depth


def _join_condition_column_ids(
    expression: Optional[exp.Expression],
    alias_map: Dict[str, str],
    schema_lookup: Dict[str, Dict[str, str]],
    *,
    fallback_table: Optional[exp.Select] = None,
    derived_columns: Optional[Dict[str, str]] = None,
) -> Set[int]:
    if expression is None:
        return set()
    column_ids: Set[int] = set()
    for eq in expression.find_all(exp.EQ):
        left_infos = _column_infos(
            eq.this if isinstance(eq.this, exp.Expression) else None,
            alias_map,
            schema_lookup,
            fallback_table=fallback_table,
            derived_columns=derived_columns,
        )
        right_infos = _column_infos(
            eq.expression if isinstance(eq.expression, exp.Expression) else None,
            alias_map,
            schema_lookup,
            fallback_table=fallback_table,
            derived_columns=derived_columns,
        )
        if not left_infos or not right_infos:
            continue
        cross_tables = any(
            left_source and right_source and left_source != right_source
            for _, _, left_source in left_infos
            for _, _, right_source in right_infos
        )
        if not cross_tables:
            continue
        column_ids.update(id(col) for col, _, _ in left_infos)
        column_ids.update(id(col) for col, _, _ in right_infos)
    return column_ids


def _infer_expression_type(
    expression: Optional[exp.Expression],
    alias_map: Dict[str, str],
    schema_lookup: Dict[str, Dict[str, str]],
    *,
    fallback_table: Optional[exp.Select] = None,
    derived_columns: Optional[Dict[str, str]] = None,
    _seen: Optional[Set[int]] = None,
) -> Optional[str]:
    if expression is None:
        return None
    if _seen is None:
        _seen = set()
    node_id = id(expression)
    if node_id in _seen:
        return None
    _seen.add(node_id)

    if isinstance(expression, exp.Window):
        inner = expression.this
        if isinstance(inner, exp.Expression):
            dtype = _infer_expression_type(
                inner,
                alias_map,
                schema_lookup,
                fallback_table=fallback_table,
                derived_columns=derived_columns,
                _seen=_seen,
            )
            if dtype:
                return dtype

    infos = _column_infos(
        expression if isinstance(expression, exp.Expression) else None,
        alias_map,
        schema_lookup,
        fallback_table=fallback_table,
        derived_columns=derived_columns,
    )
    if infos:
        priority = {
            "DOUBLE": 1,
            "DECIMAL": 1,
            "INT": 1,
            "BOOLEAN": 2,
            "DATE": 3,
            "TIMESTAMP": 3,
            "TIME": 3,
            "TEXT": 4,
        }
        best_dtype: Optional[str] = None
        best_rank = float("inf")
        for _, dtype, _ in infos:
            if not dtype:
                continue
            norm = normalize_type_label(dtype) or dtype
            rank = priority.get(norm, 5)
            if rank < best_rank:
                best_rank = rank
                best_dtype = norm
        if best_dtype:
            return best_dtype

    if isinstance(expression, exp.Literal):
        if expression.is_string:
            return "TEXT"
        if expression.is_int:
            return "INT"
        if expression.is_number:
            return "DECIMAL"
        if expression.is_boolean:
            return "BOOLEAN"

    if isinstance(expression, exp.Cast):
        target = expression.args.get("to")
        if isinstance(target, exp.DataType):
            name = getattr(target, "this", None)
            if isinstance(name, str):
                return normalize_type_label(name) or name.upper()

    if isinstance(expression, exp.Case):
        for clause in expression.args.get("ifs") or []:
            if isinstance(clause, exp.Expression):
                result = clause.args.get("true")
                dtype = _infer_expression_type(
                    result,
                    alias_map,
                    schema_lookup,
                    fallback_table=fallback_table,
                    derived_columns=derived_columns,
                    _seen=_seen,
                )
                if dtype:
                    return dtype
        default_expr = expression.args.get("default")
        if isinstance(default_expr, exp.Expression):
            dtype = _infer_expression_type(
                default_expr,
                alias_map,
                schema_lookup,
                fallback_table=fallback_table,
                derived_columns=derived_columns,
                _seen=_seen,
            )
            if dtype:
                return dtype

    for value in expression.iter_expressions():
        dtype = _infer_expression_type(
            value,
            alias_map,
            schema_lookup,
            fallback_table=fallback_table,
            derived_columns=derived_columns,
            _seen=_seen,
        )
        if dtype:
            return dtype
    return None


def _collect_column_types(
    expression: Optional[exp.Expression],
    alias_map: Dict[str, str],
    schema_types: Dict[str, Dict[str, str]],
    *,
    fallback_table: Optional[exp.Select] = None,
    derived_columns: Optional[Dict[str, str]] = None,
    excluded_columns: Optional[Set[int]] = None,
) -> List[str]:
    if expression is None:
        return []
    types: List[str] = []
    for column in expression.find_all(exp.Column):
        if excluded_columns and id(column) in excluded_columns:
            continue
        info = _resolve_column_info(
            column,
            alias_map,
            schema_types,
            fallback_table=fallback_table,
            derived_columns=derived_columns,
        )
        if not info:
            continue
        dtype, _ = info
        normalized = normalize_type_label(dtype)
        if normalized:
            types.append(normalized)
    return types


def _collect_join_types_from_where(
    expression: Optional[exp.Expression],
    alias_map: Dict[str, str],
    schema_types: Dict[str, Dict[str, str]],
    *,
    fallback_table: Optional[exp.Select] = None,
    derived_columns: Optional[Dict[str, str]] = None,
) -> List[str]:
    if expression is None:
        return []

    join_types: List[str] = []
    for eq in expression.find_all(exp.EQ):
        left_infos = _column_infos(
            eq.this if isinstance(eq.this, exp.Expression) else None,
            alias_map,
            schema_types,
            fallback_table=fallback_table,
            derived_columns=derived_columns,
        )
        right_infos = _column_infos(
            eq.expression if isinstance(eq.expression, exp.Expression) else None,
            alias_map,
            schema_types,
            fallback_table=fallback_table,
            derived_columns=derived_columns,
        )
        if not left_infos or not right_infos:
            continue
        for _, left_type, left_source in left_infos:
            if not left_source:
                continue
            for _, right_type, right_source in right_infos:
                if not right_source or left_source == right_source:
                    continue
                if left_type:
                    join_types.append(normalize_type_label(left_type) or left_type)
                if right_type:
                    join_types.append(normalize_type_label(right_type) or right_type)

    for in_expr in expression.find_all(exp.In):
        inner_query = in_expr.args.get("query")
        inner_expression = in_expr.args.get("expression")
        inner_expressions = in_expr.args.get("expressions") or []

        include_outer = False
        if isinstance(inner_query, exp.Expression):
            include_outer = True
        elif isinstance(inner_expression, exp.Expression):
            include_outer = next(inner_expression.find_all(exp.Column), None) is not None
        elif inner_expressions:
            include_outer = any(
                isinstance(element, exp.Expression) and next(element.find_all(exp.Column), None)
                for element in inner_expressions
            )

        outer_infos = _column_infos(
            in_expr.this if isinstance(in_expr.this, exp.Expression) else None,
            alias_map,
            schema_types,
            fallback_table=fallback_table,
            derived_columns=derived_columns,
        )
        if include_outer:
            for _, outer_type, _ in outer_infos:
                if outer_type:
                    join_types.append(normalize_type_label(outer_type) or outer_type)

    return join_types


_LIKE_EXPRESSIONS = tuple(
    cls
    for cls in (
        getattr(exp, "Like", None),
        getattr(exp, "ILike", None),
        getattr(exp, "RegexpLike", None),
    )
    if cls is not None
)


def _collect_filter_expression_ops(
    expression: Optional[exp.Expression],
    alias_map: Dict[str, str],
    schema_lookup: Dict[str, Dict[str, str]],
    *,
    fallback_table: Optional[exp.Select] = None,
    derived_columns: Optional[Dict[str, str]] = None,
    join_column_ids: Optional[Set[int]] = None,
) -> Counter:
    if expression is None:
        return Counter()
    join_column_ids = join_column_ids or set()
    counter: Counter = Counter()

    def visit(node: Optional[exp.Expression]) -> None:
        if not isinstance(node, exp.Expression):
            return
        category, traverse_children = _filter_expression_category(
            node,
            alias_map,
            schema_lookup,
            fallback_table=fallback_table,
            derived_columns=derived_columns,
            join_column_ids=join_column_ids,
        )
        if category:
            counter[category] += 1
        if traverse_children:
            for child in node.iter_expressions():
                visit(child)

    visit(expression)
    return counter


def _filter_expression_category(
    node: exp.Expression,
    alias_map: Dict[str, str],
    schema_lookup: Dict[str, Dict[str, str]],
    *,
    fallback_table: Optional[exp.Select] = None,
    derived_columns: Optional[Dict[str, str]] = None,
    join_column_ids: Optional[Set[int]] = None,
) -> Tuple[Optional[str], bool]:
    join_column_ids = join_column_ids or set()

    if isinstance(node, (exp.EQ, getattr(exp, "NullSafeEQ", tuple()))):
        left_infos = _column_infos(
            node.this if isinstance(node.this, exp.Expression) else None,
            alias_map,
            schema_lookup,
            fallback_table=fallback_table,
            derived_columns=derived_columns,
        )
        right_infos = _column_infos(
            node.expression if isinstance(node.expression, exp.Expression) else None,
            alias_map,
            schema_lookup,
            fallback_table=fallback_table,
            derived_columns=derived_columns,
        )
        column_ids = {id(col) for col, _, _ in left_infos + right_infos}
        if column_ids and column_ids.issubset(join_column_ids):
            return None, False
        return "equals", True

    if isinstance(node, exp.NEQ):
        return "notequal", True

    if isinstance(node, exp.In):
        return "in", False

    if isinstance(node, getattr(exp, "Exists", tuple())):
        return "exists", False

    if isinstance(node, exp.And):
        return "and", True

    if isinstance(node, exp.Or):
        return "or", True

    if isinstance(node, exp.Not):
        child = node.this
        if isinstance(child, exp.Is):
            category, _ = _filter_expression_category(
                child,
                alias_map,
                schema_lookup,
                fallback_table=fallback_table,
                derived_columns=derived_columns,
                join_column_ids=join_column_ids,
            )
            if category == "isnull":
                return "isnotnull", False
        return "not", True

    if isinstance(node, exp.Is):
        if isinstance(node.expression, exp.Null):
            return "isnull", False
        return None, True

    if isinstance(node, exp.Case) or isinstance(node, getattr(exp, "If", tuple())):
        return "ifthenelse", True

    if isinstance(node, exp.GTE):
        return "greatereq", True

    if isinstance(node, exp.GT):
        return "greater", True

    if isinstance(node, exp.LT):
        return "lessthan", True

    if isinstance(node, exp.LTE):
        return "lessequal", True

    if _LIKE_EXPRESSIONS and isinstance(node, _LIKE_EXPRESSIONS):
        return "contains", True

    if isinstance(node, exp.Between):
        return "between", True

    return None, True


def collect_filter_expression_ops(
    expr: exp.Expression,
    schema_types: Dict[str, Dict[str, str]],
) -> Counter:
    if not schema_types:
        return Counter()

    schema_lookup: Dict[str, Dict[str, str]] = {table: dict(cols) for table, cols in schema_types.items()}
    _augment_schema_with_cte_types(expr, schema_lookup)

    counter: Counter = Counter()
    for select_node in _iter_select_like(expr):
        alias_map = _collect_aliases(select_node)
        _register_alias_schemas(alias_map, schema_lookup)
        derived_columns = _collect_derived_column_types(select_node, schema_lookup)

        where = select_node.args.get("where")
        if where:
            join_column_ids = _join_condition_column_ids(
                where.this,
                alias_map,
                schema_lookup,
                fallback_table=select_node,
                derived_columns=derived_columns,
            )
            counter.update(
                _collect_filter_expression_ops(
                    where.this,
                    alias_map,
                    schema_lookup,
                    fallback_table=select_node,
                    derived_columns=derived_columns,
                    join_column_ids=join_column_ids,
                )
            )

        for having in select_node.find_all(exp.Having):
            counter.update(
                _collect_filter_expression_ops(
                    having.this,
                    alias_map,
                    schema_lookup,
                    fallback_table=select_node,
                    derived_columns=derived_columns,
                )
            )

    return counter


def _select_group_by_key_count(select_node: exp.Select) -> int:
    """
    Return the maximum number of grouping keys referenced by a single SELECT statement.
    Accounts for GROUP BY clauses, DISTINCT projections, and DISTINCT aggregate arguments.
    """
    max_keys = 0
    group = select_node.args.get("group")
    if isinstance(group, exp.Group):
        base_count = _count_group_items(group.expressions or [])
        max_keys = max(max_keys, base_count)

        def _update_with(nodes: Optional[Iterable[exp.Expression]]) -> None:
            nonlocal max_keys
            if not nodes:
                return
            for node in nodes:
                if isinstance(node, exp.GroupingSets):
                    for grouping in node.expressions or []:
                        total = base_count + _count_group_expression(grouping)
                        max_keys = max(max_keys, total)
                else:
                    total = base_count + _count_group_expression(node)
                    max_keys = max(max_keys, total)

        _update_with(group.args.get("rollup"))
        _update_with(group.args.get("cube"))
        _update_with(group.args.get("grouping_sets"))
    else:
        if select_node.args.get("distinct"):
            distinct_count = _count_distinct_expressions(select_node.expressions or [])
            max_keys = max(max_keys, distinct_count)

    distinct_agg = _count_distinct_aggregate_items(select_node)
    if distinct_agg:
        max_keys = max(max_keys, distinct_agg)

    return max_keys


def collect_group_by_key_counts(expr: exp.Expression) -> List[int]:
    """
    Return the number of grouping keys referenced by each SELECT statement
    within the query. Only SELECT statements with at least one grouping key
    are included in the result list.
    """
    counts: List[int] = []
    for select_node in _iter_select_like(expr):
        value = _select_group_by_key_count(select_node)
        if value > 0:
            counts.append(value)
    return counts


def compute_group_by_key_count(expr: exp.Expression) -> int:
    """
    Return the maximum number of grouping keys present in any SELECT statement
    within the query. Queries without grouping return 0.
    """
    counts = collect_group_by_key_counts(expr)
    if counts:
        return max(counts)
    return 0


def _count_group_items(items: Iterable[exp.Expression]) -> int:
    return sum(_count_group_expression(item) for item in items if not isinstance(item, exp.Star))


def _count_group_expression(node: Optional[exp.Expression]) -> int:
    if node is None:
        return 0
    if isinstance(node, exp.Star):
        return 0
    if isinstance(node, (exp.Tuple, exp.Distinct)):
        return sum(_count_group_expression(child) for child in node.expressions or [])
    if isinstance(node, (exp.Rollup, exp.Cube)):
        return sum(_count_group_expression(child) for child in node.expressions or [])
    if isinstance(node, exp.GroupingSets):
        return max((_count_group_expression(child) for child in node.expressions or []), default=0)
    if isinstance(node, exp.Expression):
        return 1
    return 0


def _count_distinct_expressions(items: Iterable[exp.Expression]) -> int:
    count = 0
    for item in items:
        if isinstance(item, exp.Star):
            continue
        if isinstance(item, exp.Paren):
            count += _count_group_expression(item.this)
        else:
            count += _count_group_expression(item)
    return count


def _count_distinct_aggregate_items(select_node: exp.Select) -> int:
    """
    Return the maximum number of DISTINCT expressions referenced in aggregate
    functions for the given SELECT node.
    """
    max_items = 0
    for agg_class in _AGGREGATE_CLASSES:
        for agg in select_node.find_all(agg_class):
            candidates: List[exp.Expression] = []
            this_arg = agg.args.get("this")
            if isinstance(this_arg, exp.Distinct):
                candidates.append(this_arg)
            for expr_arg in agg.args.get("expressions") or []:
                if isinstance(expr_arg, exp.Distinct):
                    candidates.append(expr_arg)
            for node in candidates:
                max_items = max(max_items, _count_group_expression(node))
    return max_items


def _infer_query_output_types(
    node: Optional[exp.Expression],
    schema_lookup: Dict[str, Dict[str, str]],
) -> Dict[str, str]:
    if node is None:
        return {}
    if isinstance(node, exp.Subquery):
        return _infer_query_output_types(node.this, schema_lookup)
    if isinstance(node, exp.Select):
        alias_map = _collect_aliases(node)
        _register_alias_schemas(alias_map, schema_lookup)
        derived_columns = _collect_derived_column_types(node, schema_lookup)
        return derived_columns
    if _SET_OPERATION_CLASSES and isinstance(node, _SET_OPERATION_CLASSES):
        left = _infer_query_output_types(node.this, schema_lookup)
        right = _infer_query_output_types(node.expression, schema_lookup)
        if not left:
            return right
        result = dict(left)
        for key, value in right.items():
            result.setdefault(key, value)
        return result
    if isinstance(node, exp.Expression):
        for child in node.iter_expressions():
            if isinstance(child, exp.Select):
                inferred = _infer_query_output_types(child, schema_lookup)
                if inferred:
                    return inferred
    return {}


def _resolve_column_type(
    column: exp.Column,
    alias_map: Dict[str, str],
    schema_types: Dict[str, Dict[str, str]],
    *,
    fallback_table: Optional[exp.Select] = None,
    derived_columns: Optional[Dict[str, str]] = None,
) -> Optional[str]:
    info = _resolve_column_info(
        column,
        alias_map,
        schema_types,
        fallback_table=fallback_table,
        derived_columns=derived_columns,
    )
    if not info:
        return None
    dtype, _ = info
    return normalize_type_label(dtype)


def _resolve_column_info(
    column: exp.Column,
    alias_map: Dict[str, str],
    schema_types: Dict[str, Dict[str, str]],
    *,
    fallback_table: Optional[exp.Select] = None,
    derived_columns: Optional[Dict[str, str]] = None,
) -> Optional[Tuple[str, Optional[str]]]:
    column_name = column.alias_or_name
    if not column_name:
        return None
    column_name = column_name.lower()
    table = column.table
    if table:
        alias_key = table.lower()
        base_name = alias_map.get(alias_key, alias_key)
        dtype = schema_types.get(base_name, {}).get(column_name)
        if dtype:
            return normalize_type_label(dtype) or dtype, alias_key or base_name
        return None

    # Unqualified column: try to infer uniquely
    candidates: List[Tuple[str, str, str]] = []
    for alias_key, base_name in alias_map.items():
        dtype = schema_types.get(base_name, {}).get(column_name)
        if dtype:
            candidates.append((alias_key, base_name, dtype))
    if len(candidates) == 1:
        alias_key, base_name, dtype = candidates[0]
        return normalize_type_label(dtype) or dtype, alias_key or base_name

    if fallback_table:
        for table_expr in fallback_table.find_all(exp.Table):
            base_name = _table_base_name(table_expr)
            if not base_name:
                continue
            base_key = base_name.lower()
            dtype = schema_types.get(base_key, {}).get(column_name)
            if not dtype:
                continue
            alias_expr = table_expr.args.get("alias")
            alias_name = None
            if isinstance(alias_expr, exp.TableAlias):
                ident = alias_expr.this
                if isinstance(ident, exp.Identifier):
                    alias_name = ident.name.lower()
            if not alias_name and table_expr.alias_or_name:
                alias_name = table_expr.alias_or_name.lower()
            origin = alias_name or base_key
            return normalize_type_label(dtype) or dtype, origin
    if derived_columns and column_name in derived_columns:
        dtype = derived_columns[column_name]
        if dtype:
            origin = f"derived::{column_name}"
            return normalize_type_label(dtype) or dtype, origin
    return None


def _infer_aggregate_result_type(agg: exp.AggFunc, arg_types: List[str]) -> Optional[str]:
    name = agg.__class__.__name__.upper()

    if name in {"COUNT", "COUNTDISTINCT"} or isinstance(agg, exp.Count):
        return "INT"
    if name in {"GROUPING", "GROUPING_ID", "GROUPINGID"}:
        return "INT"
    if name in {"AVG", "STDDEV", "STDDEV_SAMP", "STDDEV_POP", "STDDEVSAMP", "STDDEVPOP", "VARIANCE", "VAR_SAMP", "VAR_POP"}:
        return "DOUBLE"
    if name in {"SUM"} and arg_types:
        first = arg_types[0]
        if first in {"INT", "BIGINT"}:
            return "INT"
        if first in {"DECIMAL", "NUMERIC"}:
            return "DECIMAL"
        if first in {"DOUBLE", "FLOAT", "REAL"}:
            return "DOUBLE"
    if name in {"MIN", "MAX"} and arg_types:
        return arg_types[0]
    if arg_types:
        return arg_types[0]
    return None
