# WorkloadLens/src/workloadlens/core/plan.py
from __future__ import annotations
import os
from pathlib import Path
from typing import Iterable, Optional, Tuple
import duckdb
import sqlglot
from sqlglot import exp

def connect(memory: bool = True) -> Tuple[duckdb.DuckDBPyConnection, bool]:
    """
    Create a DuckDB connection and attempt to enable the Substrait extension.

    Load order:
      1) If env var SQLCOV_SUBSTRAIT_PATH points to a local .duckdb_extension, load that (no network).
      2) Otherwise try: INSTALL substrait FROM community; then LOAD substrait.

    Returns:
      (connection, has_substrait)
    """
    # Allow loading locally built (unsigned) extensions
    os.environ.setdefault("DUCKDB_ALLOW_UNSIGNED_EXTENSIONS", "1")

    con = duckdb.connect(":memory:" if memory else None,
                         config={"allow_unsigned_extensions": "true"})
    has = False

    # 1) Prefer explicit local extension path
    local_ext = os.environ.get("SQLCOV_SUBSTRAIT_PATH")
    if local_ext and Path(local_ext).exists():
        try:
            con.execute(f"LOAD '{local_ext}'")
            return con, True
        except Exception:
            # fall through to community attempt
            pass

    # 2) Fallback to community repository (may not exist for every version/arch)
    try:
        con.execute("INSTALL substrait FROM community")
        con.execute("LOAD substrait")
        has = True
    except Exception:
        has = False

    return con, has

def apply_ddl(con: duckdb.DuckDBPyConnection, ddl_sql: Optional[str]) -> None:
    """Apply schema DDL (tables) to the connection."""
    _execute_with_fallback(con, ddl_sql, include_kinds={"TABLE"})


def apply_views(con: duckdb.DuckDBPyConnection, views_sql: Optional[str]) -> None:
    """Apply views DDL to the connection."""
    _execute_with_fallback(con, views_sql, include_kinds={"VIEW"})

def get_substrait_json(con: duckdb.DuckDBPyConnection, sql: str) -> str:
    """
    Obtain a Substrait plan (JSON) for the given SQL.

    IMPORTANT: get_substrait_json is a TABLE FUNCTION, not a scalar.
    It must be called in a FROM clause.
    """
    row = con.execute("SELECT * FROM get_substrait_json(?)", [sql]).fetchone()
    return row[0] if row else ""


def _execute_with_fallback(
    con: duckdb.DuckDBPyConnection,
    sql_text: Optional[str],
    *,
    include_kinds: Optional[Iterable[str]] = None,
) -> None:
    if not sql_text or not sql_text.strip():
        return
    try:
        con.execute(sql_text)
        return
    except Exception as original_error:
        primary_error = original_error

    statements = _extract_create_statements(sql_text, include_kinds=include_kinds)
    if not statements:
        raise primary_error

    last_error: Optional[Exception] = None
    for statement in statements:
        try:
            con.execute(statement)
        except Exception as exc:
            if _is_object_exists_error(exc):
                continue
            last_error = exc
    if last_error:
        raise last_error


def _extract_create_statements(
    sql_text: str,
    *,
    include_kinds: Optional[Iterable[str]] = None,
) -> list[str]:
    try:
        parsed = sqlglot.parse(sql_text, error_level="ignore")
    except Exception:
        return []

    allowed = {kind.upper() for kind in include_kinds} if include_kinds else None
    statements: list[str] = []

    for statement in parsed:
        if not isinstance(statement, exp.Create):
            continue
        kind = _create_kind(statement)
        if allowed and kind not in allowed:
            continue
        compiled = _statement_sql(statement)
        if compiled:
            statements.append(compiled)
    return statements


def _create_kind(statement: exp.Create) -> str:
    kind = statement.args.get("kind")
    if isinstance(kind, exp.Var):
        value = kind.this or ""
    elif isinstance(kind, exp.Identifier):
        value = kind.this or kind.name or ""
    elif isinstance(kind, str):
        value = kind
    else:
        value = ""
    return value.upper()


def _statement_sql(statement: exp.Create) -> str:
    try:
        compiled = statement.sql(dialect="duckdb")
    except Exception:
        try:
            compiled = statement.sql()
        except Exception:
            return ""
    return compiled.strip()


def _is_object_exists_error(exc: Exception) -> bool:
    if isinstance(exc, duckdb.CatalogException) and "already exists" in str(exc).lower():
        return True
    message = str(exc).lower()
    return "already exists" in message
