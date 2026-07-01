from __future__ import annotations

from typing import List

import sqlglot.expressions as exp

_SELECT_CLASS = getattr(exp, "Select", None)
_SUBQUERY_CLASSES = tuple(
    cls
    for cls in (
        getattr(exp, name, None)
        for name in (
            "Union",
            "UnionAll",
            "Except",
            "ExceptAll",
            "Intersect",
            "IntersectAll",
            "Values",
            "Subquery",
            "Query",
        )
    )
    if cls is not None
)
_DML_CLASSES = tuple(
    cls for cls in (getattr(exp, name, None) for name in ("Insert", "Update", "Delete", "Merge")) if cls is not None
)
_DDL_CLASSES = tuple(
    cls for cls in (getattr(exp, name, None) for name in ("Create", "Alter", "Drop", "Truncate")) if cls is not None
)
_TRANSACTION_CLASSES = tuple(
    cls for cls in (getattr(exp, name, None) for name in ("Begin", "Commit", "Rollback")) if cls is not None
)
_SHOW_CLASS = getattr(exp, "Show", None)
_SESSION_CLASSES = tuple(
    cls for cls in (getattr(exp, name, None) for name in ("Set", "Use")) if cls is not None
)


def classify_statement_type(ast: exp.Expression) -> str:
    """Categorize the outermost statement into a coarse bucket."""

    if (_SELECT_CLASS is not None and isinstance(ast, _SELECT_CLASS)) or (
        _SUBQUERY_CLASSES and isinstance(ast, _SUBQUERY_CLASSES)
    ):
        return "Select"
    if _DML_CLASSES and isinstance(ast, _DML_CLASSES):
        return "DML"
    if _DDL_CLASSES and isinstance(ast, _DDL_CLASSES):
        return "DDL"
    if _TRANSACTION_CLASSES and isinstance(ast, _TRANSACTION_CLASSES):
        return "Transaction"
    if _SHOW_CLASS is not None and isinstance(ast, _SHOW_CLASS):
        return "Show"
    if _SESSION_CLASSES and isinstance(ast, _SESSION_CLASSES):
        return "Session"
    return ast.__class__.__name__


def normalize_sql_dialect(sql: str) -> str:
    """
    Apply lightweight textual rewrites so SQLGlot can parse certain vendor-specific
    constructs (e.g. T-SQL's ``SELECT TOP 100 DISTINCT ...``) without errors.
    """
    cleaned_lines: List[str] = []
    for raw_line in sql.splitlines():
        line = raw_line.replace("TOP 100 DISTINCT(", "DISTINCT TOP 100(")
        line = line.replace("top 100 distinct(", "distinct top 100(")
        cleaned_lines.append(line)
    return "\n".join(cleaned_lines)
