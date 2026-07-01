from __future__ import annotations
from typing import Dict, List, Tuple, Set
import sqlglot
from sqlglot import exp

# Reasonable default aggregate set (expand as needed)
AGG_NAMES: Set[str] = {
    "SUM","COUNT","AVG","MIN","MAX","ANY_VALUE","ANYVALUE","ARRAY_AGG","STRING_AGG",
    "BIT_AND","BIT_OR","BOOL_AND","BOOL_OR","STDDEV","STDDEV_SAMP","STDDEV_POP",
    "VARIANCE","VAR_SAMP","VAR_POP","GROUPING","GROUPING_ID","MEDIAN","PERCENTILE_CONT",
}

_ARITHMETIC_OPERATOR_NODES = {
    "Add": "ADD",
    "Sub": "SUB",
    "Mul": "MULTIPLY",
    "Div": "DIVIDE",
    "Mod": "MOD",
    "Pow": "POWER",
}
_ARITHMETIC_OPERATOR_CLASSES = {
    getattr(exp, name): label for name, label in _ARITHMETIC_OPERATOR_NODES.items() if hasattr(exp, name)
}

_LOGICAL_COMPARISON_NODES = {
    "And": "AND",
    "Or": "OR",
    "Xor": "XOR",
    "Not": "NOT",
    "BitwiseAnd": "BIT_AND",
    "BitwiseOr": "BIT_OR",
    "BitwiseXor": "BIT_XOR",
    "EQ": "EQUAL",
    "NEQ": "NOT_EQUAL",
    "GT": "GREATER_THAN",
    "GTE": "GREATER_EQUAL",
    "LT": "LESS_THAN",
    "LTE": "LESS_EQUAL",
    "Between": "BETWEEN",
    "Like": "LIKE",
    "ILike": "ILIKE",
    "In": "IN",
    "Extract": "EXTRACT",
}
_LOGICAL_COMPARISON_CLASSES = {
    getattr(exp, name): label for name, label in _LOGICAL_COMPARISON_NODES.items() if hasattr(exp, name)
}

# Canonicalization for common window function names
_WINDOW_NORMALIZE = {
    "ROWNUMBER": "ROW_NUMBER",
    "ROW_NUMBER": "ROW_NUMBER",
    "DENSERANK": "DENSE_RANK",
    "DENSE_RANK": "DENSE_RANK",
    "NTILE": "NTILE",
    "FIRSTVALUE": "FIRST_VALUE",
    "FIRST_VALUE": "FIRST_VALUE",
    "LASTVALUE": "LAST_VALUE",
    "LAST_VALUE": "LAST_VALUE",
    "NTHVALUE": "NTH_VALUE",
    "NTH_VALUE": "NTH_VALUE",
    "LEAD": "LEAD",
    "LAG": "LAG",
    "RANK": "RANK",
}

def _normalize_name(name: str, is_window: bool = False) -> str:
    name_up = (name or "").upper()
    if is_window:
        return _WINDOW_NORMALIZE.get(name_up, name_up)
    return name_up

def parse_and_normalize(sql: str, read_dialect: str | None = None, to_dialect: str = "duckdb") -> Tuple[exp.Expression, str]:
    """
    Parse SQL using SQLGlot; return AST + normalized SQL in a canonical dialect.
    """
    ast = sqlglot.parse_one(sql, read=read_dialect)
    norm_sql = ast.sql(dialect=to_dialect)
    return ast, norm_sql

def _func_name(func: exp.Expression) -> str:
    # Try to derive a function name from SQLGlot node
    if isinstance(func, exp.Anonymous):
        return (func.name or "").upper()
    # Most functions are subclasses of exp.Func
    cls = func.__class__.__name__
    # Try identifier (for some funcs) otherwise use class name
    ident = getattr(func, "this", None)
    if isinstance(ident, exp.Identifier) and ident.this:
        return str(ident.this).upper()
    return cls.upper()


def _is_window_context(func: exp.Expression) -> bool:
    node = func
    while node is not None:
        parent = getattr(node, "parent", None)
        if parent is None:
            return False
        if isinstance(parent, exp.Window):
            return True
        node = parent
    return False


def _is_aggregate_function(func: exp.Expression, normalized_name: str) -> bool:
    if isinstance(func, exp.AggFunc):
        return True
    return normalized_name in AGG_NAMES


def extract_functions(ast: exp.Expression) -> Dict[str, List[str]]:
    """
    Extract functions from the AST.
      - projection_funcs: functions used in SELECT list
      - agg_funcs: aggregate functions anywhere
      - window_funcs: functions inside OVER() windows
      - expression_ops: scalar operators (arithmetic, comparison, logical, casts, ...)
      - all_funcs: all function names anywhere
    Names are normalized to stable uppercase with underscores where appropriate.
    """
    all_funcs: List[str] = []
    agg_funcs: List[str] = []
    window_funcs: List[str] = []
    proj_funcs: List[str] = []
    expr_ops: List[str] = []
    scalar_funcs: List[str] = []

    # all functions
    for f in ast.find_all(exp.Func):
        name = _func_name(f)
        name_n = _normalize_name(name, is_window=False)
        all_funcs.append(name_n)
        if _is_aggregate_function(f, name_n):
            agg_funcs.append(name_n)
        elif not _is_window_context(f):
            scalar_funcs.append(name_n)

    # windows
    for w in ast.find_all(exp.Window):
        fn = getattr(w, "this", None)
        if isinstance(fn, exp.Expression):
            wname = _func_name(fn)
            window_funcs.append(_normalize_name(wname, is_window=True))

    # projection-only
    for sel in ast.find_all(exp.Select):
        for proj in sel.expressions:
            for f in proj.find_all(exp.Func):
                pname = _func_name(f)
                normalized = _normalize_name(pname, is_window=False)
                if _is_aggregate_function(f, normalized):
                    continue
                if _is_window_context(f):
                    continue
                proj_funcs.append(normalized)

    # arithmetic operators (for legacy expression_ops)
    for cls, label in _ARITHMETIC_OPERATOR_CLASSES.items():
        for _ in ast.find_all(cls):
            expr_ops.append(label)
            scalar_funcs.append(label)

    # logical / comparison operators (only contribute to scalar summary)
    for cls, label in _LOGICAL_COMPARISON_CLASSES.items():
        for _ in ast.find_all(cls):
            scalar_funcs.append(label)

    return {
        "projection_funcs": proj_funcs,
        "agg_funcs": agg_funcs,
        "window_funcs": window_funcs,
        "expression_ops": expr_ops,
        "scalar_funcs": scalar_funcs,
        "all_funcs": all_funcs,
    }
