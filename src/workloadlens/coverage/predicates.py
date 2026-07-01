from __future__ import annotations
import json
from typing import Any, Dict, List, Optional, Tuple

# Utilities to extract predicates from a Substrait plan JSON string.
# We handle both:
#   - Explicit FilterRel:   {"filter": {"input": {...}, "condition": {...}}}
#   - Pushed-down filters:  predicates nested somewhere under a "read" subtree
#
# Output shape (list of dicts):
#   {
#     "where": "filter" | "read_pushdown",
#     "path": "project>filter>read",          # rough operator path for debugging
#     "function": "gt:i32_i32",               # if resolvable via functionReference
#     "columns": ["b"],                       # best-effort column extraction
#     "literals": [10],                       # best-effort literal extraction
#   }

Predicate = Dict[str, Any]

def _build_function_ref_map(plan_dict: Dict[str, Any]) -> Dict[int, str]:
    refmap: Dict[int, str] = {}
    for ext in plan_dict.get("extensions", []):
        fn = ext.get("extensionFunction")
        if isinstance(fn, dict):
            name = fn.get("name")
            anchor = fn.get("functionAnchor")
            if isinstance(anchor, int) and isinstance(name, str):
                refmap[anchor] = name
    return refmap

def _collect_predicate_shapes(node: Any) -> List[Dict[str, Any]]:
    """Return a list of nodes that look predicate-ish anywhere under 'node'."""
    hits: List[Dict[str, Any]] = []
    stack = [(node, "")]
    while stack:
        cur, _ = stack.pop()
        if isinstance(cur, dict):
            # canonical Substrait key in Python json is camelCase: scalarFunction
            if "scalarFunction" in cur and isinstance(cur["scalarFunction"], dict):
                hits.append(cur["scalarFunction"])
            # other predicate shapes often show up like these keys
            for k in ("in_predicate", "between", "like", "is_null", "is_not_null"):
                if k in cur:
                    hits.append({k: cur[k]})
            for v in cur.values():
                stack.append((v, ""))
        elif isinstance(cur, list):
            for v in cur:
                stack.append((v, ""))
    return hits

def _extract_cols_and_literals(sf: Dict[str, Any]) -> Tuple[List[str], List[Any]]:
    cols: List[str] = []
    lits: List[Any] = []

    def walk_value(val: Any):
        if not isinstance(val, dict):
            return
        # selection → directReference → structField[.field] : best-effort
        sel = val.get("selection")
        if isinstance(sel, dict):
            dr = sel.get("directReference", {})
            sf = dr.get("structField", {})
            # substrait structField.field is 0-based; we don't have names at this point
            # columns list will be populated via best-effort name discovery elsewhere,
            # but at least record a placeholder:
            if isinstance(sf, dict) and "field" in sf:
                cols.append(f"@col[{sf.get('field')}]")
            else:
                cols.append("@col")
        # literal value extraction
        lit = val.get("literal")
        if isinstance(lit, dict):
            # pick first primitive we see
            for k in ("i32", "i64", "fp32", "fp64", "string", "bool", "i16", "i8"):
                if k in lit:
                    lits.append(lit[k])
                    break
        # casts wrap literals/selection
        if "cast" in val and isinstance(val["cast"], dict):
            walk_value(val["cast"].get("input"))

    # scalarFunction.arguments[*].value.{selection|literal|cast}
    for arg in sf.get("arguments", []):
        val = arg.get("value")
        if isinstance(val, dict):
            walk_value(val)

    return cols, lits

def _node_is_filter(node: Dict[str, Any]) -> bool:
    return "filter" in node and isinstance(node["filter"], dict)

def _node_is_read(node: Dict[str, Any]) -> bool:
    return "read" in node and isinstance(node["read"], dict)

def extract_predicates(plan_json: str) -> List[Predicate]:
    plan = json.loads(plan_json)
    refmap = _build_function_ref_map(plan)

    preds: List[Predicate] = []

    def walk(node: Any, path: str):
        if isinstance(node, list):
            for i, it in enumerate(node):
                walk(it, path)
            return
        if not isinstance(node, dict):
            return

        # Root / relation indirection
        if "rel" in node and isinstance(node["rel"], dict):
            walk(node["rel"], path or "rel")
            return
        if "root" in node and isinstance(node["root"], dict):
            walk(node["root"].get("input"), (path + ">root").strip(">"))
            return

        # Project
        if "project" in node:
            walk(node["project"].get("input"), (path + ">project").strip(">"))
            # no return; keep scanning nested content for predicates in expressions too
        # Aggregate
        if "aggregate" in node:
            walk(node["aggregate"].get("input"), (path + ">aggregate").strip(">"))
        # Join
        if "join" in node:
            j = node["join"]
            walk(j.get("left"), (path + ">join.left").strip(">"))
            walk(j.get("right"), (path + ">join.right").strip(">"))

        # Explicit FilterRel
        if _node_is_filter(node):
            f = node["filter"]
            condition = f.get("condition", {})
            # collect all scalarFunction shapes inside the condition (usually just one)
            sfs = _collect_predicate_shapes(condition)
            if sfs:
                for sf in sfs:
                    func_name = sf.get("functionReference")
                    resolved = refmap.get(func_name, None) if isinstance(func_name, int) else None
                    cols, lits = _extract_cols_and_literals(sf)
                    preds.append({
                        "where": "filter",
                        "path": (path + ">filter").strip(">"),
                        "function": resolved or "scalarFunction",
                        "columns": cols,
                        "literals": lits,
                    })

            # also keep walking the filter's input
            walk(f.get("input"), (path + ">filter.input").strip(">"))
            return

        # ReadRel (pushdown predicates may appear anywhere under this subtree)
        if _node_is_read(node):
            r = node["read"]
            # deep search for predicate-like nodes
            sfs = _collect_predicate_shapes(r)
            for sf in sfs:
                # skip if this read also sits under a normal FilterRel we already captured;
                # not easy to detect here, but duplicates won't hurt; we can deduplicate later.
                func_name = sf.get("functionReference")
                resolved = refmap.get(func_name, None) if isinstance(func_name, int) else None
                cols, lits = _extract_cols_and_literals(sf)
                preds.append({
                    "where": "read_pushdown",
                    "path": (path + ">read").strip(">"),
                    "function": resolved or "scalarFunction",
                    "columns": cols,
                    "literals": lits,
                })
            return

        # Sort / Fetch / Set / etc: keep walking
        for v in node.values():
            walk(v, path)

    # entry
    walk(plan.get("relations", plan), "")

    # Best-effort dedup (same (where,path,function,cols,lits) tuple)
    seen = set()
    uniq: List[Predicate] = []
    for p in preds:
        key = (
            p.get("where"),
            p.get("path"),
            p.get("function"),
            tuple(p.get("columns") or []),
            tuple(p.get("literals") or []),
        )
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq
