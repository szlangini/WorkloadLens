from __future__ import annotations

from dataclasses import dataclass, field
from collections import defaultdict
from typing import Dict, Iterable, List, Optional

import sqlglot
from sqlglot import exp

from .types import normalize_type_label


@dataclass
class SchemaTableMetadata:
    name: str
    columns: Dict[str, str] = field(default_factory=dict)
    primary_key_columns: Dict[str, str] = field(default_factory=dict)
    foreign_key_columns: Dict[str, str] = field(default_factory=dict)


def parse_schema_types(sql: str) -> Dict[str, Dict[str, str]]:
    """
    Parse CREATE TABLE statements from schema SQL and return a mapping:
      {table_name_lower: {column_name_lower: TYPE_STRING}}
    """
    tables = parse_schema_tables(sql)
    return {table: dict(meta.columns) for table, meta in tables.items() if meta.columns}


def parse_schema_tables(sql: str) -> Dict[str, SchemaTableMetadata]:
    """
    Parse CREATE TABLE statements and return per-table metadata including column and primary key mappings.
    Keys are normalised to lower-case to align with downstream operators.
    """
    if not sql or not sql.strip():
        return {}

    mapping: Dict[str, Dict[str, str]] = defaultdict(dict)
    primary_keys: Dict[str, Dict[str, str]] = defaultdict(dict)
    foreign_keys: Dict[str, Dict[str, str]] = defaultdict(dict)

    try:
        statements = sqlglot.parse(sql, error_level="ignore")
    except Exception:
        return {}

    for statement in statements:
        if not isinstance(statement, exp.Create):
            continue
        if _create_kind(statement).upper() != "TABLE":
            continue

        table_name = _extract_table_name(statement)
        if not table_name:
            continue

        column_defs, primary_definitions, foreign_definitions = _extract_columns_and_primary_keys(statement)
        if column_defs:
            mapping[table_name.lower()].update(column_defs)
        if primary_definitions:
            primary_keys[table_name.lower()].update(primary_definitions)
        if foreign_definitions:
            foreign_keys[table_name.lower()].update(foreign_definitions)

    tables: Dict[str, SchemaTableMetadata] = {}
    for table_name, columns in mapping.items():
        tables[table_name] = SchemaTableMetadata(
            name=table_name,
            columns=dict(columns),
            primary_key_columns=dict(primary_keys.get(table_name, {})),
            foreign_key_columns=dict(foreign_keys.get(table_name, {})),
        )
    # Tables without explicit column definitions (e.g., CTAS) will not appear in mapping.
    return tables


def _extract_columns_and_primary_keys(
    statement: exp.Create,
) -> tuple[Dict[str, str], Dict[str, str], Dict[str, str]]:
    column_map: Dict[str, str] = {}
    primary_columns: Dict[str, str] = {}
    foreign_columns: Dict[str, str] = {}

    table_expr = statement.this
    entries: Iterable[exp.Expression] = []
    if isinstance(table_expr, exp.Schema):
        entries = table_expr.expressions or []

    inline_primary_candidates: List[str] = []
    inline_foreign_candidates: List[str] = []

    for entry in entries:
        if isinstance(entry, exp.ColumnDef):
            column_name = _identifier_name(entry.this)
            dtype = _data_type_label(entry.args.get("kind"))
            if column_name and dtype:
                column_map[column_name] = dtype
            if column_name and _column_has_primary_key(entry):
                inline_primary_candidates.append(column_name)
            if column_name and _column_has_foreign_key(entry):
                inline_foreign_candidates.append(column_name)
        elif isinstance(entry, exp.Constraint):
            inline_primary_candidates.extend(_collect_primary_key_columns(entry))
            inline_foreign_candidates.extend(_collect_foreign_key_columns(entry))
        elif isinstance(entry, exp.PrimaryKey):
            inline_primary_candidates.extend(_collect_primary_key_columns(entry))
        elif isinstance(entry, exp.ForeignKey):
            inline_foreign_candidates.extend(_collect_foreign_key_columns(entry))

    seen: set[str] = set()
    for column_name in inline_primary_candidates:
        if column_name in seen:
            continue
        seen.add(column_name)
        dtype = column_map.get(column_name)
        if dtype:
            primary_columns[column_name] = dtype

    seen_fk: set[str] = set()
    for column_name in inline_foreign_candidates:
        if column_name in seen_fk:
            continue
        seen_fk.add(column_name)
        dtype = column_map.get(column_name)
        if dtype:
            foreign_columns[column_name] = dtype

    return column_map, primary_columns, foreign_columns


def _create_kind(statement: exp.Create) -> str:
    kind = statement.args.get("kind")
    if isinstance(kind, exp.Var):
        return (kind.this or "").upper()
    if isinstance(kind, str):
        return kind.upper()
    return ""


def _extract_table_name(statement: exp.Create) -> Optional[str]:
    table_expr = statement.this
    table_name = None
    if isinstance(table_expr, exp.Schema):
        inner = table_expr.this
        if isinstance(inner, exp.Table):
            table_name = inner.alias_or_name
    elif isinstance(table_expr, exp.Table):
        table_name = table_expr.alias_or_name
    elif isinstance(table_expr, exp.Identifier):
        table_name = table_expr.name
    return table_name.lower() if table_name else None


def _identifier_name(identifier: exp.Expression | None) -> Optional[str]:
    if isinstance(identifier, exp.Identifier):
        return identifier.name.lower()
    if isinstance(identifier, exp.Column):
        return (identifier.alias_or_name or "").lower()
    return None


def _data_type_label(kind_expr: exp.Expression | str | None) -> Optional[str]:
    dtype = None
    if isinstance(kind_expr, exp.DataType):
        try:
            dtype = kind_expr.sql(dialect="duckdb")
        except Exception:
            dtype = kind_expr.sql()
    elif isinstance(kind_expr, exp.Expression):
        try:
            dtype = kind_expr.sql()
        except Exception:
            dtype = None
    elif isinstance(kind_expr, str):
        dtype = kind_expr
    if not dtype:
        return None
    normalized = normalize_type_label(dtype)
    return normalized or None


def _column_has_primary_key(column_def: exp.ColumnDef) -> bool:
    constraints = column_def.args.get("constraints") or []
    for constraint in constraints:
        kind = constraint.args.get("kind")
        if isinstance(kind, exp.PrimaryKeyColumnConstraint):
            return True
    return False


def _column_has_foreign_key(column_def: exp.ColumnDef) -> bool:
    constraints = column_def.args.get("constraints") or []
    for constraint in constraints:
        kind = constraint.args.get("kind")
        if isinstance(kind, (exp.Reference, exp.ForeignKey)):
            return True
    return False


def _collect_primary_key_columns(node: exp.Expression) -> List[str]:
    columns: List[str] = []
    if isinstance(node, exp.Constraint):
        for expr in node.expressions or []:
            columns.extend(_collect_primary_key_columns(expr))
    elif isinstance(node, exp.PrimaryKey):
        for entry in node.expressions or []:
            name = _pk_column_name(entry)
            if name:
                columns.append(name)
    elif isinstance(node, exp.PrimaryKeyColumnConstraint):
        ref = node.this
        name = _pk_column_name(ref) if isinstance(ref, exp.Expression) else None
        if name:
            columns.append(name)
    return columns


def _collect_foreign_key_columns(node: exp.Expression) -> List[str]:
    columns: List[str] = []
    if isinstance(node, exp.Constraint):
        for expr in node.expressions or []:
            columns.extend(_collect_foreign_key_columns(expr))
    elif isinstance(node, exp.ForeignKey):
        for entry in node.expressions or []:
            name = _pk_column_name(entry)
            if name:
                columns.append(name)
    return columns


def _pk_column_name(expr: exp.Expression | None) -> Optional[str]:
    if expr is None:
        return None
    candidate = expr
    if isinstance(candidate, exp.Ordered):
        candidate = candidate.this
    if isinstance(candidate, exp.Column):
        ident = candidate.this
        if isinstance(ident, exp.Identifier):
            return ident.name.lower()
        name = candidate.alias_or_name
        return name.lower() if name else None
    if isinstance(candidate, exp.Identifier):
        return candidate.name.lower()
    return None
