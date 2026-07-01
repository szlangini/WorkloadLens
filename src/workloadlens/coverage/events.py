from __future__ import annotations
import json
from collections import Counter, defaultdict
from typing import Any, Dict, List, Tuple, Iterable, Optional

from ..utils.types import normalize_type_label
TypeHistogram = Dict[str, Counter]


def summarize_substrait_json(plan_json: str) -> Tuple[Dict[str, int], Dict[str, int], TypeHistogram]:
    """
    Walk a Substrait JSON plan and produce:
      - op_counts: counts of logical operators (scan, project, filter, join, aggregate, sort, limit, set, ...)
      - join_types: counts of join types (INNER, LEFT, RIGHT, FULL, SEMI, ANTI, CROSS, ...)
      - type_hist: mapping of operator name -> Counter of logical data types referenced
    Notes:
      * Some engines push predicates into ReadRel; we count those as 'filter' too (deep search).
      * HAVING may appear as a filter above aggregate or embedded; we count it.
    """
    plan = json.loads(plan_json)
    op = Counter()
    join_types = Counter()
    type_hist: TypeHistogram = defaultdict(Counter)

    def _format_scalar_type(type_dict: Optional[Dict[str, Any]]) -> Optional[str]:
        if not isinstance(type_dict, dict):
            return None
        # Some Substrait types include multiple keys (e.g. nullability). Take the first type-like key.
        for key, value in type_dict.items():
            if key == "nullability":
                continue
            if key == "decimal":
                if isinstance(value, dict):
                    precision = value.get("precision")
                    scale = value.get("scale")
                    if precision is not None and scale is not None:
                        return f"DECIMAL({precision},{scale})"
                    if precision is not None:
                        return f"DECIMAL({precision})"
                return normalize_type_label("DECIMAL")
            if key in {"i8", "i16", "i32", "i64"}:
                return normalize_type_label(key)
            if key in {"fp32", "fp64"}:
                return normalize_type_label("DOUBLE" if key == "fp64" else "FLOAT")
            if key in {"bool", "boolean"}:
                return normalize_type_label("BOOLEAN")
            if key in {"string", "varchar"}:
                return normalize_type_label("TEXT")
            if key == "fixedChar":
                length = value.get("length") if isinstance(value, dict) else None
                return normalize_type_label("TEXT")
            if key in {"date", "time", "timestamp"}:
                return normalize_type_label(key)
            if key == "struct":
                return normalize_type_label("STRUCT")
            if key == "list":
                return normalize_type_label("LIST")
            if key == "map":
                return normalize_type_label("MAP")
            if key == "binary":
                return normalize_type_label("BINARY")
            return normalize_type_label(key)
        return None

    def _literal_type(literal: Dict[str, Any]) -> Optional[str]:
        if not isinstance(literal, dict):
            return None
        for key, value in literal.items():
            if key in {"i8", "i16", "i32", "i64"}:
                return normalize_type_label(key)
            if key in {"fp32", "fp64"}:
                return normalize_type_label("DOUBLE" if key == "fp64" else "FLOAT")
            if key == "boolean":
                return normalize_type_label("BOOLEAN")
            if key == "string":
                return normalize_type_label("TEXT")
            if key == "binary":
                return normalize_type_label("BINARY")
            if key == "decimal":
                if isinstance(value, dict):
                    precision = value.get("precision")
                    scale = value.get("scale")
                    if precision is not None and scale is not None:
                        return normalize_type_label("DECIMAL")
                return normalize_type_label("DECIMAL")
        return None

    def _schema_from_struct(struct: Dict[str, Any]) -> List[Optional[str]]:
        if not isinstance(struct, dict):
            return []
        types = struct.get("types")
        if not isinstance(types, list):
            return []
        return [_format_scalar_type(t) for t in types]

    def _field_index(struct_field: Dict[str, Any]) -> Optional[int]:
        if not isinstance(struct_field, dict):
            return None
        if "field" in struct_field:
            try:
                return int(struct_field["field"])
            except Exception:
                return None
        if "child" in struct_field:
            return _field_index(struct_field["child"])
        # Default to 0 when unspecified (Substrait treats missing 'field' as 0)
        return 0

    def _selection_type(selection: Dict[str, Any], schema: List[Optional[str]]) -> Optional[str]:
        if not isinstance(selection, dict):
            return None
        direct = selection.get("directReference")
        if not isinstance(direct, dict):
            return None
        struct_field = direct.get("structField")
        if not isinstance(struct_field, dict):
            return None
        idx = _field_index(struct_field)
        if idx is None or idx < 0 or idx >= len(schema):
            return None
        return schema[idx]

    def _collect_selection_types(
        expr: Any,
        schema: List[Optional[str]],
        *,
        include_literals: bool = True,
    ) -> Iterable[Optional[str]]:
        """Return all column types referenced via selection nodes inside expr."""
        if expr is None:
            return []
        stack = [expr]
        seen: set[int] = set()
        results: list[Optional[str]] = []
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                sel = node.get("selection")
                if isinstance(sel, dict):
                    t = _selection_type(sel, schema)
                    if t is not None:
                        results.append(t)
                if include_literals:
                    lit = node.get("literal")
                    if isinstance(lit, dict):
                        t = _literal_type(lit)
                        if t is not None:
                            results.append(t)
                for value in node.values():
                    stack.append(value)
            elif isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, (str, int, float, bool)):
                continue
            else:
                node_id = id(node)
                if node_id in seen:
                    continue
                seen.add(node_id)
        return results

    def _expression_output_type(expr: Any, schema: List[Optional[str]]) -> Optional[str]:
        if not isinstance(expr, dict):
            return None
        if "selection" in expr:
            selection = expr.get("selection")
            return _selection_type(selection, schema) if isinstance(selection, dict) else None
        if "literal" in expr:
            return _literal_type(expr["literal"])
        if "scalarFunction" in expr:
            func = expr["scalarFunction"]
            if isinstance(func, dict):
                out = func.get("outputType")
                fmt = _format_scalar_type(out)
                if fmt:
                    return fmt
                # fallback to argument type
                args = func.get("arguments") or []
                for arg in args:
                    if isinstance(arg, dict):
                        val = arg.get("value")
                        if isinstance(val, dict):
                            t = _expression_output_type(val, schema)
                            if t:
                                return t
        if "cast" in expr:
            cast_expr = expr["cast"]
            if isinstance(cast_expr, dict):
                typ = _format_scalar_type(cast_expr.get("type"))
                if typ:
                    return typ
                return _expression_output_type(cast_expr.get("input"), schema)
        if "ifThen" in expr:
            ifthen = expr["ifThen"]
            if isinstance(ifthen, dict):
                thens = ifthen.get("thens") or []
                for clause in thens:
                    if isinstance(clause, dict):
                        out = clause.get("then")
                        t = _expression_output_type(out, schema)
                        if t:
                            return t
                else_clause = ifthen.get("else")
                if else_clause:
                    return _expression_output_type(else_clause, schema)
        # Fallback: look deeper for selections
        for value in expr.values():
            if isinstance(value, (dict, list)):
                t = _expression_output_type(value, schema)
                if t:
                    return t
        return None

    def _apply_projection_select(projection: Dict[str, Any], schema: List[Optional[str]]) -> List[Optional[str]]:
        if not isinstance(projection, dict):
            return schema
        select = projection.get("select")
        if not isinstance(select, dict):
            return schema
        items = select.get("structItems")
        if not isinstance(items, list) or not items:
            return schema
        new_schema: List[Optional[str]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            field = item.get("field")
            if field is not None:
                try:
                    idx = int(field)
                except Exception:
                    idx = None
            else:
                child = item.get("child")
                idx = _field_index(child) if isinstance(child, dict) else 0
            if idx is None or idx < 0 or idx >= len(schema):
                new_schema.append(None)
            else:
                new_schema.append(schema[idx])
        return new_schema if new_schema else schema

    def _apply_emit(common: Dict[str, Any], combined_schema: List[Optional[str]]) -> List[Optional[str]]:
        if not isinstance(common, dict):
            return combined_schema
        emit = common.get("emit")
        if not isinstance(emit, dict):
            return combined_schema
        mapping = emit.get("outputMapping")
        if not isinstance(mapping, list) or not mapping:
            return combined_schema
        output_schema: List[Optional[str]] = []
        for idx in mapping:
            try:
                pos = int(idx)
            except Exception:
                pos = None
            if pos is None or pos < 0 or pos >= len(combined_schema):
                output_schema.append(None)
            else:
                output_schema.append(combined_schema[pos])
        return output_schema

    def _update_type_hist(op_name: str, types: Iterable[Optional[str]]) -> None:
        filtered = []
        for t in types:
            normalized = normalize_type_label(t)
            if normalized:
                filtered.append(normalized)
        if filtered:
            type_hist[op_name].update(filtered)

    def _summarize_rel(rel: Any) -> List[Optional[str]]:
        if rel is None:
            return []
        if isinstance(rel, list):
            schema: List[Optional[str]] = []
            for item in rel:
                schema = _summarize_rel(item)
            return schema
        if not isinstance(rel, dict):
            return []

        if "window" in rel:
            window_rel = rel["window"]
            input_schema = _summarize_rel(window_rel.get("input"))
            functions = window_rel.get("windowFunctions") or []
            result_types: List[Optional[str]] = []
            total_windows = 0
            for func in functions:
                if not isinstance(func, dict):
                    continue
                total_windows += 1
                func_spec = func.get("function")
                args = []
                if isinstance(func_spec, dict):
                    args = func_spec.get("arguments") or []
                    result_type = _format_scalar_type(func_spec.get("outputType"))
                else:
                    result_type = None
                arg_types: List[Optional[str]] = []
                for arg in args:
                    if isinstance(arg, dict):
                        value = arg.get("value")
                        if isinstance(value, dict):
                            arg_types.extend(
                                _collect_selection_types(value, input_schema, include_literals=False)
                            )
                partition_items = func.get("partitionBy") or []
                for part in partition_items:
                    if isinstance(part, dict):
                        arg_types.extend(
                            _collect_selection_types(part, input_schema, include_literals=False)
                        )
                order_items = func.get("orderBy") or func.get("sorts") or []
                for item in order_items:
                    if isinstance(item, dict):
                        expr = item.get("expr") or item.get("expression")
                        if expr:
                            arg_types.extend(
                                _collect_selection_types(expr, input_schema, include_literals=False)
                            )
                if arg_types:
                    _update_type_hist("window", arg_types)
                if result_type:
                    _update_type_hist("window", [result_type])
                result_types.append(result_type)
            if total_windows:
                op["window"] += total_windows
            combined = list(input_schema) + result_types
            output_schema = _apply_emit(window_rel.get("common", {}), combined)
            return output_schema if output_schema else combined or input_schema

        if "project" in rel:
            proj = rel["project"]
            input_schema = _summarize_rel(proj.get("input"))
            expressions = proj.get("expressions") or []
            expr_types: List[Optional[str]] = []
            for expr in expressions:
                if isinstance(expr, dict):
                    t = _expression_output_type(expr, input_schema)
                    expr_types.append(t)
                    _update_type_hist("project", [t])
            combined = list(input_schema) + expr_types
            output_schema = _apply_emit(proj.get("common", {}), combined)
            if not output_schema:
                output_schema = combined if combined else input_schema
            op["project"] += 1
            return output_schema

        if "aggregate" in rel:
            agg = rel["aggregate"]
            input_schema = _summarize_rel(agg.get("input"))
            grouping_types: List[Optional[str]] = []
            groups = agg.get("groupings") or []
            for grp in groups:
                if not isinstance(grp, dict):
                    continue
                exprs = grp.get("groupingExpressions") or []
                for expr in exprs:
                    if isinstance(expr, dict):
                        t = _expression_output_type(expr, input_schema)
                        grouping_types.append(t)
            measure_types: List[Optional[str]] = []
            measures = agg.get("measures") or []
            for measure in measures:
                if not isinstance(measure, dict):
                    continue
                inner = measure.get("measure")
                if not isinstance(inner, dict):
                    continue
                args = inner.get("arguments") or []
                arg_types = []
                for arg in args:
                    if isinstance(arg, dict):
                        val = arg.get("value")
                        if isinstance(val, dict):
                            arg_types.extend(
                                _collect_selection_types(val, input_schema, include_literals=False)
                            )
                output_type = _format_scalar_type(inner.get("outputType"))
                measure_types.append(output_type)
                if arg_types:
                    _update_type_hist("aggregate", arg_types)
                if output_type:
                    _update_type_hist("aggregate_result", [output_type])
            if grouping_types:
                _update_type_hist("group_by", grouping_types)
            output_schema = grouping_types + measure_types
            op["aggregate"] += 1
            return output_schema

        if "filter" in rel:
            filt = rel["filter"]
            input_schema = _summarize_rel(filt.get("input"))
            condition = filt.get("condition")
            if condition is not None:
                _update_type_hist(
                    "filter",
                    _collect_selection_types(condition, input_schema, include_literals=False),
                )
            op["filter"] += 1
            return input_schema

        if "join" in rel:
            join = rel["join"]
            left_schema = _summarize_rel(join.get("left"))
            right_schema = _summarize_rel(join.get("right"))
            combined_schema = left_schema + right_schema
            join_type = join.get("type") or join.get("join_type") or "JOIN_TYPE_INNER"
            if isinstance(join_type, str):
                join_types[join_type] += 1
            op["join"] += 1

            condition = join.get("expression") or join.get("condition")
            if condition is not None:
                _update_type_hist(
                    "join", _collect_selection_types(condition, combined_schema, include_literals=False)
                )
            post_filter = join.get("postJoinFilter")
            if post_filter is not None:
                _update_type_hist(
                    "filter",
                    _collect_selection_types(post_filter, combined_schema, include_literals=False),
                )
                op["filter"] += 1
            return combined_schema

        if "read" in rel:
            read = rel["read"]
            base_schema = []
            base = read.get("baseSchema")
            if isinstance(base, dict):
                struct = base.get("struct")
                if isinstance(struct, dict):
                    base_schema = _schema_from_struct(struct)
            elif "namedTable" in read:
                # Without base schema we cannot infer; keep unknown placeholder for each column? unknown count not known.
                base_schema = []
            schema = base_schema
            projection = read.get("projection")
            if projection:
                schema = _apply_projection_select(projection, schema)
            op["scan"] += 1
            _update_type_hist("scan", schema)
            read_filter = read.get("filter")
            if read_filter is not None:
                _update_type_hist(
                    "filter",
                    _collect_selection_types(read_filter, schema, include_literals=False),
                )
                op["filter"] += 1
            return schema

        if "sort" in rel:
            sort = rel["sort"]
            input_schema = _summarize_rel(sort.get("input"))
            sorts = sort.get("sorts") or []
            for item in sorts:
                if not isinstance(item, dict):
                    continue
                expr = item.get("expr")
                if expr:
                    sort_types = list(
                        _collect_selection_types(expr, input_schema, include_literals=False)
                    )
                    if sort_types:
                        _update_type_hist("sort", sort_types)
                    else:
                        t = _expression_output_type(expr, input_schema)
                        if t:
                            _update_type_hist("sort", [t])
            op["sort"] += 1
            return input_schema

        if "fetch" in rel:
            fetch = rel["fetch"]
            input_schema = _summarize_rel(fetch.get("input"))
            op["limit"] += 1
            return input_schema

        if "topn" in rel:
            topn = rel["topn"]
            input_schema = _summarize_rel(topn.get("input"))
            sorts = topn.get("sorts") or []
            for item in sorts:
                if not isinstance(item, dict):
                    continue
                expr = item.get("expr") or item.get("expression")
                if expr:
                    _update_type_hist("sort", _collect_selection_types(expr, input_schema))
            op["top_k"] += 1
            return input_schema

        if "set" in rel:
            set_rel = rel["set"]
            inputs = set_rel.get("inputs")
            left_schema: List[Optional[str]] = []
            right_schema: List[Optional[str]] = []
            if isinstance(inputs, list) and len(inputs) >= 2:
                left_schema = _summarize_rel(inputs[0])
                right_schema = _summarize_rel(inputs[1])
            else:
                left_schema = _summarize_rel(set_rel.get("left"))
                right_schema = _summarize_rel(set_rel.get("right"))
            op["set"] += 1
            op_kind = set_rel.get("op") or set_rel.get("setType") or ""
            if isinstance(op_kind, str) and "UNION_ALL" in op_kind.upper():
                op["union_all"] += 1
            # Assume schemas are identical; return left schema for downstream nodes.
            return left_schema or right_schema

        # Fallback: walk nested dicts to avoid missing shapes
        for value in rel.values():
            if isinstance(value, (dict, list)):
                schema = _summarize_rel(value)
                if schema:
                    return schema
        return []

    relations = plan.get("relations")
    if isinstance(relations, list):
        for relation in relations:
            if isinstance(relation, dict):
                if "root" in relation and isinstance(relation["root"], dict):
                    _summarize_rel(relation["root"].get("input"))
                elif "rel" in relation and isinstance(relation["rel"], dict):
                    _summarize_rel(relation["rel"])
                else:
                    _summarize_rel(relation)
    else:
        _summarize_rel(plan)

    return dict(op), dict(join_types), {name: Counter(counter) for name, counter in type_hist.items()}
